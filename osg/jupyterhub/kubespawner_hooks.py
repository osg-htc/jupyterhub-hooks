"""
KubeSpawner hooks for customizing a user's server options based on the
groups that they belong to.

The hooks are configured via a YAML file whose structure is defined by
the `Configuration` class.
"""

import copy
import dataclasses
import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

import baydemir.parsing
import kubernetes.client as k8s  # type: ignore[import]

from osg.jupyterhub_util import htcondor  # not to be confused with the Python bindings
from osg.jupyterhub_util import comanage

__all__ = [
    "auth_state_hook",
    "modify_pod_hook",
    "options_form",
]

KUBESPAWNER_CONFIG = Path(
    os.environ.get("_osg_KUBESPAWNER_HOOKS_CONFIG", "/etc/osg/kubespawner_hooks_config.yaml")
)

CONDOR_CONDOR_HOST = os.environ.get("_condor_CONDOR_HOST", "")
CONDOR_SEC_TOKEN_ISSUER_KEY = os.environ.get("_condor_SEC_TOKEN_ISSUER_KEY", "")
CONDOR_UID_DOMAIN = os.environ.get("_condor_UID_DOMAIN", "")

NOTEBOOK_CONTAINER_NAME = "notebook"


@dataclasses.dataclass
class KubespawnerOverride:
    """
    `kubespawner_override` key-value pairs to apply to a server option.

    The override will be applied only if the user is a member of one of
    the listed groups or if no groups are listed.
    """

    groups: List[str]
    override: Dict[str, Any]


@dataclasses.dataclass
class ProfileList:
    """
    A list of server options (`profile_list` in KubeSpawner parlance).

    The options will be shown to the user only if the user is a member
    of one of the listed groups or if no groups are listed.
    """

    groups: List[str]
    servers: List[Dict[str, Any]]


@dataclasses.dataclass
class Configuration:
    """
    Defines the structure of the configuration file used by the hooks below.
    """

    server_defaults: Dict[str, Any]

    server_overrides: Dict[str, KubespawnerOverride]

    server_lists: List[ProfileList]


# --------------------------------------------------------------------------


def auth_state_hook(spawner, auth_state) -> None:
    """
    Saves the user's OIDC userinfo object to the spawner.
    """

    spawner.userdata = (auth_state or {}).get("cilogon_user", {})


def options_form(spawner) -> str:
    """
    Sets the spawner's `profile_list` for the current user.
    """
    ## Reference: https://discourse.jupyter.org/t/tailoring-spawn-options-and-server-configuration-to-certain-users/8449

    if person := comanage.get_person(spawner.userdata):
        spawner.log.info(
            f"Building options form for: sub = {person.sub}, groups = {person.groups}"
        )

        config = get_config()
        spawner.profile_list = []

        for server in get_servers(config, person):
            server_override = server.get("kubespawner_override", {})
            server_includes = server_override.pop("include", [])
            composite_override = copy.deepcopy(config.server_defaults)

            for key in server_includes:
                override = config.server_overrides[key]
                if not override.groups or set(override.groups).intersection(person.groups):
                    merge_override(composite_override, override.override, person.ospool)
            merge_override(composite_override, server_override, person.ospool)

            server["kubespawner_override"] = composite_override
            spawner.profile_list.append(server)

    return spawner._options_form_default()  # type: ignore[no-any-return]


def modify_pod_hook(spawner, pod: k8s.V1Pod) -> k8s.V1Pod:
    """
    Adds an HTCondor IDTOKEN to the notebook container's environment.

    Applies only to OSPool users.
    """

    person = comanage.get_person(spawner.userdata)

    if person and person.ospool:
        add_htcondor_idtoken(pod, person.ospool)

    return pod


# --------------------------------------------------------------------------


def get_config() -> Configuration:
    """
    Returns the configuration for the hooks.
    """

    try:
        config = baydemir.parsing.load_yaml(KUBESPAWNER_CONFIG, Configuration)
    except FileNotFoundError:
        config = Configuration(server_defaults={}, server_overrides={}, server_lists=[])

    return config


def get_notebook_container(pod: k8s.V1Pod) -> k8s.V1Container:
    """
    Returns the pod's notebook container.
    """

    for c in pod.spec.containers:
        if c.name == NOTEBOOK_CONTAINER_NAME:
            return c

    raise RuntimeError("Failed to locate the pod's notebook container")


def get_servers(
    config: Configuration, person: comanage.COmanagePerson
) -> Iterator[Dict[str, Any]]:
    """
    Yields the server options to show to the given user.
    """

    for spec in config.server_lists:
        if not spec.groups or set(spec.groups).intersection(person.groups):
            for server in spec.servers:
                yield server


def add_htcondor_idtoken(pod: k8s.V1Pod, user: comanage.OSPoolPerson) -> None:
    """
    Adds an HTCondor IDTOKEN to the notebook container's environment.
    """

    iss = CONDOR_CONDOR_HOST
    sub = f"{user.username}@{CONDOR_UID_DOMAIN}"
    kid = CONDOR_SEC_TOKEN_ISSUER_KEY

    token = htcondor.create_token(iss=iss, sub=sub, kid=kid)

    notebook = get_notebook_container(pod)

    notebook.env.append(
        k8s.V1EnvVar(
            name="_osg_HTCONDOR_IDTOKEN",
            value=token,
        )
    )


def build_value(raw_value: Any, user: Optional[comanage.OSPoolPerson]) -> Any:
    """
    Builds a Kubernetes Python API object or value.
    """

    if isinstance(raw_value, str):

        if user:

            ## The user's UID and GID should yield integers instead of a strings.

            if raw_value == "{user.uid}":
                return user.uid

            if raw_value == "{user.gid}":
                return user.gid

            for field in dataclasses.fields(user):
                k = f"{{user.{field.name}}}"
                v = getattr(user, field.name)

                raw_value = raw_value.replace(k, str(v))

        return raw_value

    if isinstance(raw_value, dict):

        ## The value could be an API object or a built-in dictionary.

        if "_" in raw_value:
            cls = k8s.__dict__[raw_value.pop("_")]
        else:
            cls = dict

        args = {}

        for k, v in raw_value.items():
            args[k] = build_value(v, user)

        return cls(**args)

    if isinstance(raw_value, list):
        return [build_value(x, user) for x in raw_value]

    return raw_value  # assume that this is a scalar to be used as-is


def merge_override(
    target: Dict[str, Any], source: Dict[str, Any], user: Optional[comanage.OSPoolPerson]
) -> None:
    """
    Merges one set of `kubespawner_override` keys into another.

    Unlike `KubeSpawner`, list values are concatenated, not replaced.
    """

    for k, raw_v in source.items():
        v = build_value(raw_v, user)  # substitute user.username, etc.

        if isinstance(v, dict):
            target.setdefault(k, {})
            target[k].update(v)
        elif isinstance(v, list):
            target.setdefault(k, [])
            target[k].extend(v)
        else:
            target[k] = v

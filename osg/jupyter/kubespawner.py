"""
KubeSpawner hooks.

The configuration file is a YAML file whose structure is determined by the
class `Configuration`.
"""
# FIXME: Assumptions: CILogon for auth, KubeSpawner uses "notebook"

import dataclasses
import os
import pathlib
from typing import Any, Dict, List, Literal, Optional

import kubernetes.client as k8s  # type: ignore[import]
from baydemir import parsing

from osg.jupyter import htcondor  # not to be confused with the Python bindings
from osg.jupyter import comanage

__all__ = [
    "auth_state_hook",
    "modify_pod_hook",
    "options_form",
    "pre_spawn_hook",
]

KUBESPAWNER_CONFIG = pathlib.Path(
    os.environ.get("_osg_KUBESPAWNER_HOOKS_CONFIG", "/etc/osg/kubespawner_hooks_config.yaml")
)

CONDOR_CONDOR_HOST = os.environ.get("_condor_CONDOR_HOST", "")
CONDOR_SEC_TOKEN_ISSUER_KEY = os.environ.get("_condor_SEC_TOKEN_ISSUER_KEY", "")
CONDOR_UID_DOMAIN = os.environ.get("_condor_UID_DOMAIN", "")

NOTEBOOK_CONTAINER_NAME = "notebook"


@dataclasses.dataclass
class PatchList:
    """
    A named list of patch operations.

    Patch operations allow for defining modifications to make to user pods
    beyond what `KubeSpawner` overrides support. They are inspired by JSON
    Patches (RFC 6902).

    A patch operation consists of:

      - `path`: Slash-delimited, rooted at either "pod" or "notebook"
      - `op`: Either "append", "extend", "prepend", "set", "set-default", or "merge-keys"
      - `value`: A scalar, a list of values, or a dictionary

    String values support substitutions of information about the user for
    whom the pod is being created.

    Dictionary values are used to build objects. If the name of a class in
    the Kubernetes Python API is specified via the `_` key, then that class
    will be used to construct the object instead of the built-in `dict`.

    Reference: https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md

    Examples:

    - path: pod/spec/volumes
      op: extend
      value:
      - name: shared-data
        nfs:
          server: nfs.example.com
          path: /data
          _: V1NFSVolumeSource
        _: V1Volume

    - path: notebook/security_context
      op: set
      value:
        run_as_user: "{user.uid}"
        run_as_group: "{user.gid}"
        _: V1SecurityContext
    """

    name: str
    spec: List[Dict[Literal["path", "op", "value"], Any]]


@dataclasses.dataclass
class ProfileList:
    """
    A named list of profiles.

    The structure of each profile is determined by `KubeSpawner.profile_list`.
    """

    name: str
    spec: List[Dict[str, Any]]


@dataclasses.dataclass
class UserOptions:
    """
    Defines the options to present to the user.

    Users are differentiated based on their group memberships.

    Note that the patches apply to *all* of the options that are eventually
    presented to the user.
    """

    groups: List[str]
    profile_lists: List[str]
    patch_lists: List[str]


@dataclasses.dataclass
class Configuration:
    """
    Defines the structure of the configuration file used by the hooks below.
    """

    patch_lists: List[PatchList]
    profile_lists: List[ProfileList]
    user_options: List[UserOptions]

    # Specify the default value of each `kubespawner_override` key.
    #
    # Each option in each profile list must specify the same set of
    # override keys because the spawner does not reset its configuration
    # between server launches.

    kubespawner_override_defaults: Dict[str, Any]


# --------------------------------------------------------------------------


def auth_state_hook(spawner, auth_state) -> None:
    """
    Saves the user's OIDC userinfo object to the spawner.
    """

    spawner.userdata = (auth_state or {}).get("cilogon_user", {})


def options_form(spawner) -> str:
    """
    Modifies the spawner's `profile_list` for the current user.
    """
    ## Reference: https://discourse.jupyter.org/t/tailoring-spawn-options-and-server-configuration-to-certain-users/8449

    config = get_config()
    defaults = config.kubespawner_override_defaults
    person = comanage.get_person(spawner.userdata)

    if person:
        spawner.profile_list = []

        for options in config.user_options:
            groups = set(options.groups)

            if groups.intersection(set(person.groups)) or not groups:
                for name in options.profile_lists:
                    if pl := get_profile_list(config, name):
                        for server in pl.spec:
                            server.setdefault("kubespawner_override", {})
                            for key, val in defaults.items():
                                server["kubespawner_override"].setdefault(key, val)
                        spawner.profile_list.extend(pl.spec)

    return spawner._options_form_default()  # type: ignore[no-any-return]


def pre_spawn_hook(spawner) -> None:
    """
    Modifies the spawner if the JupyterHub user is also an OSPool user.
    """

    person = comanage.get_person(spawner.userdata)

    if person and person.ospool:

        # Do not force a GID on files. Doing so might cause mounted secrets
        # to have permissions that they should not.

        spawner.fs_gid = None


def modify_pod_hook(spawner, pod: k8s.V1Pod) -> k8s.V1Pod:
    """
    Modifies the pod as specified in this hook's configuration.
    """

    config = get_config()
    person = comanage.get_person(spawner.userdata)

    if person:
        for options in config.user_options:
            groups = set(options.groups)

            if groups.intersection(set(person.groups)) or not groups:
                for name in options.patch_lists:
                    if pl := get_patch_list(config, name):
                        for patch in pl.spec:
                            spawner.log.info(
                                f"{spawner.user.name}: Applying patch: {pl.name}: {patch['path']}"
                            )
                            apply_patch(patch, pod, person.ospool)

        if person.ospool:
            add_htcondor_idtoken(pod, person.ospool)

    return pod


# --------------------------------------------------------------------------


def get_config() -> Configuration:
    """
    Returns the configuration for the hooks.
    """

    try:
        config = parsing.load_yaml(KUBESPAWNER_CONFIG, Configuration)
    except FileNotFoundError:
        config = Configuration(
            patch_lists=[], profile_lists=[], user_options=[], kubespawner_override_defaults={}
        )

    return config


def get_patch_list(config: Configuration, name: str) -> Optional[PatchList]:
    """
    Returns a patch list from the configuration with the given name.
    """

    for pl in config.patch_lists:
        if pl.name == name:
            return pl
    return None


def get_profile_list(config: Configuration, name: str) -> Optional[ProfileList]:
    """
    Returns a profile list from the configuration with the given name.
    """

    for pl in config.profile_lists:
        if pl.name == name:
            return pl
    return None


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


def apply_patch(patch, pod: k8s.V1Pod, user: Optional[comanage.OSPoolPerson] = None) -> None:
    """
    Applies a patch operation to the given pod for the given user.
    """

    notebook = get_notebook_container(pod)

    path = patch["path"]
    path_parts = path.split("/")
    op = patch["op"]
    value = build_value(patch["value"], user)

    if path_parts[0] == "pod":
        loc = pod
    elif path_parts[0] == "notebook":
        loc = notebook
    else:
        raise RuntimeError(f"Not a valid patch path: {path}")

    #
    # FIXME: Assumptions: The components of the patch's path exist
    #
    # We rely on the patch itself to tell us how to construct values, so if
    # some component does not exist, then there is generally not much we can
    # do about it.
    #

    for p in path_parts[1:-1]:
        loc = getattr(loc, p)

    if op in ["append", "extend", "prepend"]:
        if getattr(loc, path_parts[-1]) is None:
            # Ensure that we have a list on which to operate.
            setattr(loc, path_parts[-1], [])
        if op == "append":
            getattr(loc, path_parts[-1]).append(value)
        elif op == "extend":
            getattr(loc, path_parts[-1]).extend(value)
        elif op == "prepend":
            getattr(loc, path_parts[-1]).insert(0, value)
    elif op == "set":
        setattr(loc, path_parts[-1], value)
    elif op == "set-default":
        if getattr(loc, path_parts[-1]) is None:
            setattr(loc, path_parts[-1], value)
    elif op == "merge-keys":
        if getattr(loc, path_parts[-1]) is None:
            setattr(loc, path_parts[-1], value)
        else:
            current = getattr(loc, path_parts[-1])
            for k, v in patch["value"].items():
                if k != "_":
                    if isinstance(current, dict):
                        current[k] = build_value(v, user)
                    else:
                        setattr(current, k, build_value(v, user))
    else:
        raise RuntimeError(f"Not a valid patch op: {op}")


def build_value(raw_value, user: Optional[comanage.OSPoolPerson] = None) -> Any:
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


def get_notebook_container(pod: k8s.V1Pod) -> k8s.V1Container:
    """
    Returns the pod's notebook container.
    """

    for c in pod.spec.containers:
        if c.name == NOTEBOOK_CONTAINER_NAME:
            return c

    raise RuntimeError("Failed to locate the pod's notebook container")

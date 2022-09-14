"""
KubeSpawner hooks.

The configuration file is a YAML file containing a list of patch operations
inspired by JSON Patches (RFC 6902).

Field and class names must match the Kubernetes Python API.
See https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md,
in particular, the section "Documentation for Models".

A patch operation consists of:

  - `path`: Slash-delimited, rooted at either "pod" or "notebook"
  - `op`: Either "append", "extend", "prepend", or "set"
  - `value`: A scalar, a list of values, or a dictionary

String values support substitutions of information about the user for whom
the pod is being created.

Dictionary values are used to build objects. If the name of a class in the
Kubernetes Python API is specified via the `_` key, then that class will be
used to construct the object instead of the built-in `dict` class.
"""
# FIXME: Assumptions: CILogon for auth, usernames are ePPNs, KubeSpawner uses "notebook"

import dataclasses
import os
import pathlib
import re
from typing import Any

import kubernetes.client as k8s  # type: ignore[import]
import yaml

from osg.jupyter import htcondor  # not to be confused with the Python bindings
from osg.jupyter import comanage

__all__ = [
    "auth_state_hook",
    "modify_pod_hook",
    "pre_spawn_hook",
]

KUBESPAWNER_CONFIG = pathlib.Path(
    os.environ.get("_osg_JUPYTERHUB_KUBESPAWNER_CONFIG", "/etc/osg/jupyterhub_kubespawner.yaml")
)

CONDOR_CONDOR_HOST = os.environ["_condor_CONDOR_HOST"]
CONDOR_SEC_TOKEN_ISSUER_KEY = os.environ["_condor_SEC_TOKEN_ISSUER_KEY"]
CONDOR_UID_DOMAIN = os.environ["_condor_UID_DOMAIN"]

NOTEBOOK_CONTAINER_NAME = "notebook"


# --------------------------------------------------------------------------


def auth_state_hook(spawner, auth_state) -> None:
    """
    Saves the user's OIDC userinfo object to the spawner.
    """

    spawner.userdata = (auth_state or {}).get("cilogon_user", {})


def pre_spawn_hook(spawner) -> None:
    """
    Modifies the spawner if the JupyterHub user is also an OSPool user.
    """

    eppn = spawner.user.name

    if comanage.get_ospool_user(eppn, spawner.userdata):

        # Do not force a GID on files. Doing so might cause mounted secrets
        # to have permissions that they should not.

        spawner.fs_gid = None


def modify_pod_hook(spawner, pod: k8s.V1Pod) -> k8s.V1Pod:
    """
    Modifies the pod if the JupyterHub user is also an OSPool user.
    """

    eppn = spawner.user.name

    if user := comanage.get_ospool_user(eppn, spawner.userdata):
        if KUBESPAWNER_CONFIG.exists():
            with open(KUBESPAWNER_CONFIG, encoding="utf-8") as fp:
                config = yaml.safe_load(fp)
            if is_pod_modifiable(config, pod):
                for patch in config.get("patches", []):
                    apply_patch(patch, pod, user)
                add_htcondor_idtoken(pod, user)

    return pod


# --------------------------------------------------------------------------


def add_htcondor_idtoken(pod: k8s.V1Pod, user: comanage.OSPoolUser) -> None:
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


def apply_patch(patch, pod: k8s.V1Pod, user: comanage.OSPoolUser) -> None:
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

    for p in path_parts[1:-1]:
        loc = getattr(loc, p)

    if op == "append":
        getattr(loc, path_parts[-1]).append(value)
    elif op == "extend":
        getattr(loc, path_parts[-1]).extend(value)
    elif op == "prepend":
        getattr(loc, path_parts[-1]).insert(0, value)
    elif op == "set":
        setattr(loc, path_parts[-1], value)
    else:
        raise RuntimeError(f"Not a valid patch op: {op}")


def build_value(raw_value, user: comanage.OSPoolUser) -> Any:
    """
    Builds a Kubernetes Python API object or value.
    """

    if isinstance(raw_value, str):

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


def is_pod_modifiable(config, pod: k8s.V1Pod) -> bool:
    """
    Determines whether the pod should be patched according to the config.
    """

    notebook = get_notebook_container(pod)

    for rule in config.get("exceptions", []):
        if "image" in rule:
            if re.search(rule["image"], notebook.image):
                return False

    return True

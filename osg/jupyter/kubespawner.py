"""
KubeSpawner hooks.

The configuration file is a YAML file containing a list of patch operations
inspired by JSON Patches (RFC 6902).

Field and class names must match the Kubernetes Python API.
See https://github.com/kubernetes-client/python/blob/master/kubernetes/README.md.

A patch operation consists of:

  - `path`: Slash-delimited, rooted at either "pod" or "notebook"
  - `op`: Either "append", "extend", "prepend", or "set"
  - `value`: A scalar, a list of values, or a dictionary

Scalar values support substitutions of information about the user for whom
the pod is being created. Dictionary values are used to build objects. The
corresponding class in the Kubernetes Python API must be specified via the
`_` (underscore) field.
"""

import dataclasses
import os
import pathlib
from typing import Any

import kubernetes.client as k8s  # type: ignore[import]
import yaml

from osg.jupyter import comanage

__all__ = ["modify_pod_hook"]

KUBESPAWNER_CONFIG = pathlib.Path(
    os.environ.get("_osg_JUPYTERHUB_KUBESPAWNER_CONFIG", "/etc/osg/jupyterhub_kubespawner.yaml")
)


def modify_pod_hook(spawner, pod: k8s.V1Pod) -> k8s.V1Pod:
    """
    Modifies the pod if the JupyterHub user is also an OSPool user.
    """

    eppn = spawner.user.name  # FIXME: Assumption that JupyterHub usernames are ePPNs.

    if user := comanage.get_ospool_user(eppn):
        if KUBESPAWNER_CONFIG.exists():
            with open(KUBESPAWNER_CONFIG, encoding="utf-8") as fp:
                config = yaml.safe_load(fp)
            for patch in config:
                apply_patch(patch, pod, user)

    return pod


def apply_patch(patch, pod: k8s.V1Pod, user: comanage.OSPoolUser) -> None:
    """
    Applies a patch operation to the given pod for the given user.
    """

    for c in pod.spec.containers:
        if c.name == "notebook":
            notebook = c
            break
    else:
        raise RuntimeError("Failed to locate the pod's notebook container")

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
        cls = k8s.__dict__[raw_value["_"]]
        args = {}

        raw_value.pop("_")

        for k, v in raw_value.items():
            args[k] = build_value(v, user)

        return cls(**args)

    if isinstance(raw_value, list):
        return [build_value(x, user) for x in raw_value]

    return raw_value  # assume that this is a scalar to be used as-is

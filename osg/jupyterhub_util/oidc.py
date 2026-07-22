"""
Parse a user's identity and groups from their OIDC userinfo.
"""

import dataclasses
import functools
import os
import re
from typing import Any

__all__ = [
    "OIDCPerson",
    "OIDCSettings",
    "OSPoolPerson",
    #
    "get_person",
    "load_settings",
    "settings",
]

# The OSPool username becomes the HTCondor IDTOKEN subject and is
# interpolated into pod-spec values, so restrict it to a POSIX-portable
# username character set.  The leading char excludes `-`/`.` so the name
# cannot start like an option flag or a path segment.
_UNIX_USERNAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


@dataclasses.dataclass(frozen=True)
class OIDCSettings:
    """
    The environment-derived names of the OIDC claims to read.

    A single home for these knobs, mirroring the `Settings` dataclasses
    in the code service and the hooks.
    """

    sub_claim: str
    groups_claim: str


def load_settings() -> OIDCSettings:
    """
    Reads the OIDC claim names from the environment.

    Called lazily via `settings`, not at import, to keep environment
    reads out of import time as elsewhere.
    """

    return OIDCSettings(
        sub_claim=os.environ.get("_osg_KUBESPAWNER_SUB_CLAIM", "sub"),
        groups_claim=os.environ.get("_osg_KUBESPAWNER_GROUPS_CLAIM", "groups"),
    )


# Process-lifetime singleton; resettable in tests via
# `settings.cache_clear()`.
settings = functools.cache(load_settings)


@dataclasses.dataclass
class OSPoolPerson:
    """
    A user's OSPool identity: their Unix username, UID, and GID.
    """

    username: str
    uid: int
    gid: int


@dataclasses.dataclass
class OIDCPerson:
    """
    A user's identity derived from their OIDC userinfo.
    """

    sub: str
    groups: list[str]
    ospool: OSPoolPerson | None = None


def _string_items(value: Any) -> list[str]:
    """
    Returns the string elements of `value`, each stripped of surrounding
    whitespace, with empty results dropped.
    Non-string elements are ignored.

    `Any`: pyright won't flag iterating it; mypy sees no redundant cast.
    """

    return [x.strip() for x in value if isinstance(x, str) and x.strip()]


def get_person(oidc_userinfo: dict[str, Any]) -> OIDCPerson | None:
    """
    Builds an `OIDCPerson` from the given OIDC userinfo dict.

    Reads the "sub" and groups claims along with the "unix" claims that
    describe the user's OSPool identity.  Returns `None` when the userinfo
    contains no "sub" claim.
    """

    current = settings()

    sub = oidc_userinfo.get(current.sub_claim)

    # The groups claim may arrive as a delimited string, not a list (an
    # OIDC quirk): split a bare string on commas, else whitespace.
    # Never iterate a bare string as a list -- that treats its
    # characters as group names.
    raw_groups = oidc_userinfo.get(current.groups_claim)
    if isinstance(raw_groups, str):
        raw_groups = raw_groups.split(",") if "," in raw_groups else raw_groups.split()
    groups = _string_items(raw_groups) if isinstance(raw_groups, list) else []

    # NOTE: The OIDC client must be configured to return the "unix" claims
    # below so that we can avoid querying LDAP, which will block the current
    # thread when using the `ldap3` library.

    username = oidc_userinfo.get("unix_username")
    uid = oidc_userinfo.get("unix_uid")
    gid = oidc_userinfo.get("unix_gid")

    if sub:
        # Username must match the allowlist; UID/GID must be strictly
        # positive.  Convert first: the string "0" is truthy but maps to
        # root, and negative IDs would slip past a raw-value truthiness
        # check too.
        ospool_person = None
        try:
            uid_int = int(uid) if uid is not None else 0
            gid_int = int(gid) if gid is not None else 0
        except (ValueError, TypeError):
            uid_int = gid_int = 0
        if (
            isinstance(username, str)
            and _UNIX_USERNAME_RE.fullmatch(username)
            and uid_int > 0
            and gid_int > 0
        ):
            ospool_person = OSPoolPerson(username, uid_int, gid_int)
        return OIDCPerson(sub, groups, ospool_person)

    return None

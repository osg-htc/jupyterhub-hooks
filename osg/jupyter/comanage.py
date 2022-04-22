"""
Query OSG's COmanage instance.
"""
# FIXME: Assumptions: OSG's CILogon and COmanage setup

import contextlib
import dataclasses
import os
import sys
from typing import Any, Dict, Optional

import ldap3  # type: ignore[import]

__all__ = [
    "OSPoolUser",
    #
    "get_ospool_user",
]

LDAP_URL = os.environ["_comanage_LDAP_URL"]
LDAP_PEOPLE_BASE_DN = os.environ["_comanage_LDAP_PEOPLE_BASE_DN"]
LDAP_USERNAME = os.environ["_comanage_LDAP_USERNAME"]
LDAP_PASSWORD = os.environ["_comanage_LDAP_PASSWORD"]


@dataclasses.dataclass
class OSPoolUser:
    eppn: str
    username: str
    uid: int
    gid: int


@contextlib.contextmanager
def ldap_connection():
    with ldap3.Connection(
        ldap3.Server(LDAP_URL, get_info=ldap3.ALL), LDAP_USERNAME, LDAP_PASSWORD
    ) as conn:
        yield conn


def get_ospool_user(
    eppn: str,
    oidc_userinfo: Optional[Dict[str, Any]] = None,
) -> Optional[OSPoolUser]:
    """
    Returns the OSPool user for the given ePPN.

    As a fallback, use the OIDC "sub" claim.
    """

    oidc_sub_claim = (oidc_userinfo or {}).get("sub", None)
    ldap_attributes = ["isMemberOf", "voPersonApplicationUID", "uidNumber", "gidNumber"]
    ospool_user = None

    if not ospool_user:
        with ldap_connection() as conn:
            conn.search(
                LDAP_PEOPLE_BASE_DN,
                f"(eduPersonPrincipalName={eppn})",
                attributes=ldap_attributes,
            )

            ospool_user = make_ospool_user(eppn, conn.entries)

    if not ospool_user and oidc_sub_claim:
        with ldap_connection() as conn:
            conn.search(
                LDAP_PEOPLE_BASE_DN,
                f"(uid={oidc_sub_claim})",
                attributes=ldap_attributes,
            )

            ospool_user = make_ospool_user(eppn, conn.entries)

    return ospool_user


def make_ospool_user(eppn: str, entries) -> Optional[OSPoolUser]:
    """
    Returns an OSPool user object from the given list of LDAP entries.

    Assumes that the entries come from a search for the user with the
    given ePPN.
    """

    ospool_user = None

    if len(entries) == 1:
        attrs = entries[0].entry_attributes_as_dict

        groups = attrs["isMemberOf"]
        usernames = attrs["voPersonApplicationUID"]
        uids = attrs["uidNumber"]
        gids = attrs["gidNumber"]

        if (
            "ospool-login" in groups
            and len(usernames) == 1
            and len(uids) == 1
            and len(gids) == 1
        ):
            ospool_user = OSPoolUser(eppn, usernames[0], uids[0], gids[0])

    return ospool_user


def main() -> None:
    """
    Query LDAP using the first command-line argument as the filter.

    This is intended to aid in debugging issues.
    """

    with ldap_connection() as conn:
        conn.search(LDAP_PEOPLE_BASE_DN, sys.argv[1], attributes=["*"])
        print(conn.entries)


if __name__ == "__main__":
    main()

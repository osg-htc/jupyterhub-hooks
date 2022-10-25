"""
Query OSG's COmanage instance.
"""
# FIXME: Assumptions: OSG's CILogon and COmanage setup

import contextlib
import dataclasses
import os
import sys
from typing import Any, Dict, List, Optional

import ldap3  # type: ignore[import]

__all__ = [
    "COmanagePerson",
    "OSPoolPerson",
    #
    "get_person",
]

LDAP_URL = os.environ["_comanage_LDAP_URL"]
LDAP_PEOPLE_BASE_DN = os.environ["_comanage_LDAP_PEOPLE_BASE_DN"]
LDAP_USERNAME = os.environ["_comanage_LDAP_USERNAME"]
LDAP_PASSWORD = os.environ["_comanage_LDAP_PASSWORD"]


@dataclasses.dataclass
class OSPoolPerson:
    username: str
    uid: int
    gid: int


@dataclasses.dataclass
class COmanagePerson:
    sub: str
    groups: List[str]
    ospool: Optional[OSPoolPerson] = None


@contextlib.contextmanager
def ldap_connection():
    with ldap3.Connection(
        ldap3.Server(LDAP_URL, get_info=ldap3.ALL), LDAP_USERNAME, LDAP_PASSWORD
    ) as conn:
        yield conn


def get_person(oidc_userinfo: Dict[str, Any]) -> Optional[COmanagePerson]:
    """
    Returns the COmanage person for the given OIDC "sub" claim.
    """

    oidc_sub = oidc_userinfo.get("sub", None)
    ldap_attributes = ["isMemberOf", "voPersonApplicationUID", "uidNumber", "gidNumber"]
    person = None

    if not person and oidc_sub:
        with ldap_connection() as conn:
            conn.search(
                LDAP_PEOPLE_BASE_DN,
                f"(uid={oidc_sub})",
                attributes=ldap_attributes,
            )

            person = make_person(oidc_sub, conn.entries)

    return person


def make_person(oidc_sub: str, entries) -> Optional[COmanagePerson]:
    """
    Returns a COmanage person based on the given list of LDAP entries.
    """

    person = None

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
            ospool_person = OSPoolPerson(usernames[0], uids[0], gids[0])
        else:
            ospool_person = None

        person = COmanagePerson(oidc_sub, groups, ospool_person)

    return person


def main() -> None:
    """
    Query LDAP using the first command-line argument as the filter.

    This function is intended to aid in debugging issues.

    The filter must be formatted for use with the `ldap3` library.
    """

    with ldap_connection() as conn:
        conn.search(LDAP_PEOPLE_BASE_DN, sys.argv[1], attributes=["*"])
        print(conn.entries)


if __name__ == "__main__":
    main()

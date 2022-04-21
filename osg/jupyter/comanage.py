"""
Query COmanage.
"""

import contextlib
import dataclasses
import os
import sys
from typing import Optional

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


def get_ospool_user(eppn: str) -> Optional[OSPoolUser]:
    """
    Returns the OSPool user for the given ePPN.
    """

    with ldap_connection() as conn:
        conn.search(
            LDAP_PEOPLE_BASE_DN,
            f"(&(objectClass=eduPerson)(eduPersonPrincipalName={eppn}))",
            attributes=["isMemberOf", "voPersonApplicationUID", "uidNumber", "gidNumber"],
        )

        if len(conn.entries) == 1:
            attrs = conn.entries[0].entry_attributes_as_dict

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
                return OSPoolUser(eppn, usernames[0], uids[0], gids[0])

    return None


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

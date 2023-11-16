"""
Query OSG's COmanage infrastructure.
"""

import contextlib
import dataclasses
import os
import sys
from typing import Any, Dict, List, Optional

import ldap3  # type: ignore[import-untyped]
import ldap3.utils.conv  # type: ignore[import-untyped]

__all__ = [
    "COmanagePerson",
    "OSPoolPerson",
    #
    "get_person",
]


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
    url = os.environ["_comanage_LDAP_URL"]
    username = os.environ["_comanage_LDAP_USERNAME"]
    password = os.environ["_comanage_LDAP_PASSWORD"]
    server = ldap3.Server(url, get_info=ldap3.ALL)

    with ldap3.Connection(server, username, password) as conn:
        yield conn


def get_person(oidc_userinfo: Dict[str, Any]) -> Optional[COmanagePerson]:
    """
    Returns the COmanage person for the given OIDC "sub" claim.
    """

    person = None

    # NOTE: The OIDC Client in COmanage must be configured to return the
    # claims below so that we can avoid querying LDAP, which will block the
    # current thread when using the `ldap3` library.

    oidc_sub = oidc_userinfo.get("sub")
    groups = oidc_userinfo.get("groups")
    username = oidc_userinfo.get("unix_username")
    uid = oidc_userinfo.get("unix_uid")
    gid = oidc_userinfo.get("unix_gid")

    if oidc_sub and not person:
        if username and uid and gid:
            try:
                ospool_person = OSPoolPerson(username, int(uid), int(gid))
            except ValueError:
                ospool_person = None
        else:
            ospool_person = None
        person = COmanagePerson(oidc_sub, groups or [], ospool_person)

    # NOTE: The LDAP query below is dead code but left here in case the flow
    # ever needs to be resurrected.

    if oidc_sub and not person:
        base_dn = os.environ["_comanage_LDAP_BASE_DN"]
        safe_oidc_sub = ldap3.utils.conv.escape_filter_chars(oidc_sub, encoding="utf-8")
        search_filter = f"(uid={safe_oidc_sub})"
        attributes = ["isMemberOf", "voPersonApplicationUID", "uidNumber", "gidNumber"]

        with ldap_connection() as conn:
            conn.search(base_dn, search_filter, attributes=attributes)

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
        base_dn = os.environ["_comanage_LDAP_BASE_DN"]
        conn.search(base_dn, sys.argv[1], attributes=["*"])
        print(conn.entries)


if __name__ == "__main__":
    main()

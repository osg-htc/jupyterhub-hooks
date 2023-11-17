"""
Query OSG's COmanage infrastructure.
"""

import dataclasses
from typing import Any, Dict, List, Optional

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

    return person

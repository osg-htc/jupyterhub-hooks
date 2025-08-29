"""
Query OSG's COmanage infrastructure.
"""

import dataclasses
import os
from typing import Any, Dict, List, Optional

__all__ = [
    "COmanagePerson",
    "OSPoolPerson",
    #
    "get_person",
]

OIDC_SUB_CLAIM = os.environ.get("_osg_KUBESPAWNER_SUB_CLAIM", "sub")
OIDC_GROUPS_CLAIM = os.environ.get("_osg_KUBESPAWNER_GROUPS_CLAIM", "groups")


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

    # NOTE: The OIDC client must be configured to return the "unix" claims
    # below so that we can avoid querying LDAP, which will block the current
    # thread when using the `ldap3` library.

    sub = oidc_userinfo.get(OIDC_SUB_CLAIM)
    groups = oidc_userinfo.get(OIDC_GROUPS_CLAIM)

    username = oidc_userinfo.get("unix_username")
    uid = oidc_userinfo.get("unix_uid")
    gid = oidc_userinfo.get("unix_gid")

    if sub and not person:
        if username and uid and gid:
            try:
                ospool_person = OSPoolPerson(username, int(uid), int(gid))
            except ValueError:
                ospool_person = None
        else:
            ospool_person = None
        person = COmanagePerson(sub, groups or [], ospool_person)

    return person

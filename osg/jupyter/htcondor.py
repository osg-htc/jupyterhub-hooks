"""
Create HTCondor IDTOKENs.

Based on: https://github.com/CoffeaTeam/coffea-casa/blob/master/charts/coffea-casa/files/hub/auth.py
"""

import itertools
import os
import pathlib
import time
import uuid

import jwt  # type: ignore[import]
from cryptography.hazmat.primitives.hashes import SHA256  # type: ignore[import]
from cryptography.hazmat.primitives.kdf.hkdf import HKDF  # type: ignore[import]

__all__ = ["create_token"]

PASSWORD_FILE = pathlib.Path(
    os.environ.get("_condor_SEC_PASSWORD_FILE", "/etc/condor/passwords.d/POOL")
)


def unscramble(buf: bytes) -> bytes:
    """
    Undoes HTCondor's password scrambling.
    """
    deadbeef = [0xDE, 0xAD, 0xBE, 0xEF]

    return bytes(a ^ b for (a, b) in zip(buf, itertools.cycle(deadbeef)))


def get_password() -> bytes:
    with open(PASSWORD_FILE, mode="rb") as fp:
        raw_password = fp.read()
    return unscramble(raw_password)


def derive_key(password: bytes) -> bytes:
    ## The parameters to HKDF are fixed as part of the protocol.
    hkdf = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=b"htcondor",
        info=b"master jwt",
    )
    return hkdf.derive(password)  # type: ignore[no-any-return]


def create_token(
    iss: str,
    sub: str,
    lifetime: int = 60 * 60 * 24,
    kid: str = "POOL",
    scope: str = "condor:/READ condor:/WRITE",
) -> str:
    """
    Creates an HTCondor IDTOKEN with the specified characteristics.
    """

    now = int(time.time())

    payload = {
        "iss": iss,
        "sub": sub,
        "exp": now + lifetime,
        "iat": now,
        "jti": uuid.uuid4().hex,
        "scope": scope,
    }

    password = get_password()

    if kid == "POOL":
        password += password

    key = derive_key(password)

    token = jwt.encode(payload, key, headers={"kid": kid}, algorithm="HS256")

    if isinstance(token, bytes):  # for older versions of PyJWT
        return token.decode()
    return token  # type: ignore[no-any-return]

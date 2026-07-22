"""
Create HTCondor IDTOKENs.

Based on: https://github.com/CoffeaTeam/coffea-casa/blob/master/charts/coffea-casa/files/hub/auth.py
"""

import itertools
import os
import time
import uuid

import jwt
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = [
    "create_token",
    "read_password",
]


def _unscramble(buf: bytes) -> bytes:
    """
    Undoes HTCondor's password scrambling.
    """

    deadbeef = [0xDE, 0xAD, 0xBE, 0xEF]

    return bytes(a ^ b for (a, b) in zip(buf, itertools.cycle(deadbeef)))


def read_password(path: str | os.PathLike[str]) -> bytes:
    """
    Reads and unscrambles an HTCondor password file.
    """

    with open(path, mode="rb") as fp:
        raw_password = fp.read()
    return _unscramble(raw_password)


def _derive_key(password: bytes) -> bytes:
    """
    Derives the signing key for an HTCondor IDTOKEN from a password.
    """

    # The parameters to HKDF are fixed as part of the protocol.
    hkdf = HKDF(
        algorithm=SHA256(),
        length=32,
        salt=b"htcondor",
        info=b"master jwt",
    )
    return hkdf.derive(password)


def create_token(
    *,
    password: bytes,
    iss: str,
    sub: str,
    lifetime: int = 60 * 60 * 24,  # 1 day, in seconds
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

    # HTCondor derives the signing key for the "POOL" key ID from the
    # password repeated twice, so we must match that here or every minted
    # IDTOKEN would be rejected.

    if kid == "POOL":
        password += password

    # NOTE: The PyJWT API indicates that `key` should be a `str`, but the
    # API will accept `bytes` because that's what it actually needs for the
    # HMAC algorithm.

    key = _derive_key(password)

    token = jwt.encode(payload, key, headers={"kid": kid}, algorithm="HS256")

    return token

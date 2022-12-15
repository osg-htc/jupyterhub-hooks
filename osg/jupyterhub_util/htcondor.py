"""
Create HTCondor IDTOKENs.

Based on: https://github.com/CoffeaTeam/coffea-casa/blob/master/charts/coffea-casa/files/hub/auth.py
"""

import itertools
import os
import pathlib
import time
import uuid

import jwt
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

__all__ = ["create_token"]

PASSWORD_FILE = pathlib.Path(
    os.environ.get("_condor_SEC_PASSWORD_FILE", "/etc/condor/passwords.d/POOL")
)

# Despite starting with "_condor", this environment variable does
# not refer to an actual HTCondor configuration setting.
DEFAULT_IDTOKEN_SCOPE = os.environ.get(
    "_condor_DEFAULT_IDTOKEN_SCOPE", "condor:/READ condor:/WRITE"
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
    return hkdf.derive(password)


def create_token(
    *,
    iss: str,
    sub: str,
    lifetime: int = 60 * 60 * 24,
    kid: str = "POOL",
    scope: str = DEFAULT_IDTOKEN_SCOPE,
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

    # NOTE: The PyJWT API indicates that `key` should be a `str`, but the
    # API will accept `bytes` because that's what it actually needs for the
    # HMAC algorithm.

    key = derive_key(password)

    token = jwt.encode(payload, key, headers={"kid": kid}, algorithm="HS256")  # type: ignore[arg-type]

    return token

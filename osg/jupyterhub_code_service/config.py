"""
Configuration for the code-entry service.

The service is configured via a YAML file whose structure is defined by
the `CodeServiceConfig` class.
"""

import dataclasses
import datetime
import functools
import os
import pathlib
import re

# `Optional`, not `X | None`: see the note on `Code.expires`. The
# pyright suppression opts this one alias out of the project's PEP 604
# preference; `baydemir.parsing` requires the `typing.Union` form.
from typing import Optional  # pyright: ignore[reportDeprecated]

from osg.jupyterhub_util.config import (
    CachedConfigLoader,
    ConfigError,
    ConfigUnavailableError,
    bool_from_env,
    load_yaml_config,
    positive_int_from_env,
)

__all__ = [
    "Code",
    "CodeServiceConfig",
    "CodeServiceSettings",
    "ConfigError",
    "ConfigUnavailableError",
    #
    "config_loader",
    "load_config",
    "load_settings",
    "parse_expires",
    "settings",
]

# Interpolated into a Hub API URL path segment, so restrict to DNS-name
# characters.
# Leading char must be alphanumeric: `quote` won't encode `.`, so a bare
# `..` segment would survive and renormalize the URL to another endpoint.
_GROUP_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]*")


@dataclasses.dataclass
class Code:
    """
    A single shared code and the group memberships that it grants.

    `id` is non-secret and used only for logging; `secret` is the
    plaintext code matched against a submission.
    """

    id: str
    secret: str
    grants: list[str]
    # NB: `Optional[str]`, not `str | None`. The config loader
    # (`baydemir.parsing`) recognizes only `typing.Union`; a PEP 604
    # `X | None` union is rejected as an unparsable type, so any code
    # carrying an `expires` value would fail to load.
    expires: Optional[str] = None  # pyright: ignore[reportDeprecated]


@dataclasses.dataclass
class CodeServiceConfig:
    """
    Defines the structure of the code-entry service's configuration file.
    """

    codes: list[Code] = dataclasses.field(default_factory=list[Code])


@dataclasses.dataclass(frozen=True)
class CodeServiceSettings:  # pylint: disable=too-many-instance-attributes
    """
    The service's environment-derived settings.

    These are the process-lifetime tuning knobs read from the
    `CODE_SERVICE_*` environment variables (plus the `JUPYTERHUB_*`
    values that Z2JH injects), as opposed to the code list in the YAML
    file (see `CodeServiceConfig`). This is their single home: `app`,
    `grants`, `limiter`, and `handlers` read them from here rather than
    re-reading the environment themselves.
    """

    config_path: pathlib.Path
    group_prefix: str
    create_missing_groups: bool
    burst: int
    refill_seconds: int
    base_url: str
    service_prefix: str
    service_url: str
    api_url: str
    api_token: str


def load_settings() -> CodeServiceSettings:
    """
    Reads and validates the service's settings from the environment.

    Raises `ConfigError` for a bad tuning knob. Called lazily via
    `settings`, not at import, so a bad value is reported at startup
    (inside `app.main`'s guard) rather than as an import-time traceback.
    """

    return CodeServiceSettings(
        config_path=pathlib.Path(
            os.environ.get("CODE_SERVICE_CONFIG", "/etc/osg/code_service_config.yaml")
        ),
        # Ignore an empty override: the prefix is the safety net against a
        # code granting an arbitrary group, so it must not be silently
        # disabled.
        group_prefix=os.environ.get("CODE_SERVICE_GROUP_PREFIX", "").strip() or "code-",
        create_missing_groups=bool_from_env("CODE_SERVICE_CREATE_MISSING_GROUPS", False),
        burst=positive_int_from_env("CODE_SERVICE_BURST", 5),
        refill_seconds=positive_int_from_env("CODE_SERVICE_REFILL_SECONDS", 10),
        # Z2JH injects this; the trailing default matches the Hub's own.
        base_url=os.environ.get("JUPYTERHUB_BASE_URL", "/"),
        # The remaining `JUPYTERHUB_*` values are also Z2JH-injected but
        # have no sensible default: a missing one is a broken deployment,
        # so let the `KeyError` crash-loop the service at startup.
        service_prefix=os.environ["JUPYTERHUB_SERVICE_PREFIX"],
        service_url=os.environ["JUPYTERHUB_SERVICE_URL"],
        # Normalized here so `grants.HubGroups` can join path segments
        # without re-trimming.
        api_url=os.environ["JUPYTERHUB_API_URL"].rstrip("/"),
        api_token=os.environ["JUPYTERHUB_API_TOKEN"],
    )


# Process-lifetime singleton: the environment does not change under a
# running service. `functools.cache` does not cache exceptions, so an
# invalid setting keeps failing closed; `settings.cache_clear()` resets
# it for tests.
settings = functools.cache(load_settings)


def parse_expires(value: str) -> datetime.datetime:
    """
    Parses an ISO-8601 timestamp, assuming UTC when it is naive.
    """

    expires = datetime.datetime.fromisoformat(value)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=datetime.timezone.utc)
    return expires


def _validate_grants(code: Code) -> None:
    """
    Raises `ConfigError` if `code`'s grants are semantically invalid.
    """

    if not code.grants:
        raise ConfigError(f"Code {code.id!r} grants no groups")
    for group in code.grants:
        if not group.strip():
            raise ConfigError(f"Code {code.id!r} has a blank grant")
        # See `_GROUP_NAME_RE`.
        if not _GROUP_NAME_RE.fullmatch(group):
            raise ConfigError(
                f"Code {code.id!r} has a grant with invalid characters:" + f" {group!r}"
            )

    # A code whose grants all lack the prefix loads cleanly but drops
    # every grant at redemption (see `grants.match_code`), surfacing as a
    # user-facing 500.
    # Reject it here instead.
    prefix = settings().group_prefix

    if not any(g.startswith(prefix) for g in code.grants):
        raise ConfigError(
            f"Code {code.id!r} has no grant beginning with the required" + f" prefix {prefix!r}"
        )


def _validate(config: CodeServiceConfig) -> None:
    """
    Raises `ConfigError` if `config` is semantically invalid.

    Catches what `baydemir.parsing` cannot: right type, wrong contents.
    """

    seen_ids: set[str] = set()
    seen_secrets: set[str] = set()

    for code in config.codes:
        # `id` labels this code's log lines; keep it unique and non-blank.
        if not code.id or not code.id.strip():
            raise ConfigError("A code has a blank `id`")
        if code.id in seen_ids:
            raise ConfigError(f"Duplicate code `id` {code.id!r}")
        seen_ids.add(code.id)

        # A blank secret can never be redeemed.
        if not code.secret or not code.secret.strip():
            raise ConfigError(f"Code {code.id!r} has a blank `secret`")
        # `match_code` strips the submission before comparing, so a
        # secret with surrounding whitespace could never match.
        if code.secret != code.secret.strip():
            raise ConfigError(f"Code {code.id!r} has a `secret` with surrounding whitespace")
        # Surface the collision at startup, not on the unlucky redeemer.
        if code.secret in seen_secrets:
            raise ConfigError(f"Code {code.id!r} duplicates another code's `secret`")
        seen_secrets.add(code.secret)

        _validate_grants(code)

        if code.expires is not None:
            try:
                parse_expires(code.expires)
            except ValueError:
                raise ConfigError(
                    f"Code {code.id!r} has an `expires` that is not a valid"
                    + f" ISO-8601 timestamp: {code.expires!r}"
                ) from None


def load_config() -> CodeServiceConfig:
    """
    Returns the validated configuration for the code-entry service.

    A missing file is an error, not an empty config: failing open would
    let the service report healthy while granting nothing, locking users
    out via the rate limiter.
    An intentionally empty deployment supplies a file with empty `codes`.
    """

    config = load_yaml_config(settings().config_path, CodeServiceConfig)
    _validate(config)
    return config


# Re-read at most once per TTL, single-flight, serving the last-good
# config if a read is slow or hangs. See `CachedConfigLoader`.
config_loader = CachedConfigLoader(load_config)

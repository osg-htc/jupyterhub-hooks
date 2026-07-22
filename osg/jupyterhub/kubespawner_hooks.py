"""
KubeSpawner hooks for customizing a user's server options based on the
groups that they belong to.

The hooks are configured via a YAML file whose structure is defined by
the `Configuration` class.
"""

import copy
import dataclasses
import functools
import html
import os
import pathlib
import re
from collections.abc import Iterable, Iterator
from typing import Any, cast

import kubernetes_asyncio.client as k8s
from tornado import web

from osg.jupyterhub_util import htcondor  # not to be confused with HTCondor's Python bindings
from osg.jupyterhub_util import oidc
from osg.jupyterhub_util.config import (
    CachedConfigLoader,
    ConfigError,
    ConfigUnavailableError,
    load_yaml_config,
)

__all__ = [
    "CondorSettings",
    "ConfigError",
    "ConfigUnavailableError",
    "Configuration",
    "KubespawnerOverride",
    "ProfileList",
    "RequireCode",
    "Settings",
    "auth_state_hook",
    "load_settings",
    "modify_pod_hook",
    "options_form",
    "pre_spawn_hook",
    "settings",
]

NOTEBOOK_CONTAINER_NAME = "notebook"

_INVALID_CONFIG_MESSAGE = "The server configuration is invalid; contact an administrator."

_CONFIG_UNAVAILABLE_MESSAGE = (
    "The server configuration is temporarily unavailable;"
    + " please try launching again in a moment."
)


@dataclasses.dataclass(frozen=True)
class CondorSettings:
    """
    The fully specified HTCondor settings needed to mint an IDTOKEN.
    """

    condor_host: str
    sec_password_file: str
    sec_token_issuer_key: str
    uid_domain: str


@dataclasses.dataclass(frozen=True)
class Settings:
    """
    The hooks' environment-derived settings.

    This is their single home, mirroring `config.CodeServiceSettings` in
    the code service. `condor` is `None` when the HTCondor environment is
    incomplete; bundling the four vars lets the type checker see each
    field as a definite `str` once the bundle exists.
    """

    config_path: pathlib.Path
    condor: CondorSettings | None


def load_settings() -> Settings:
    """
    Reads the hooks' settings from the environment.

    Called lazily via `settings`, not at import, to keep environment
    reads out of import time as in the code service.
    """

    condor_host = os.environ.get("_condor_CONDOR_HOST")
    sec_password_file = os.environ.get("_condor_SEC_PASSWORD_FILE")
    sec_token_issuer_key = os.environ.get("_condor_SEC_TOKEN_ISSUER_KEY")
    uid_domain = os.environ.get("_condor_UID_DOMAIN")

    condor: CondorSettings | None = None
    if condor_host and sec_password_file and sec_token_issuer_key and uid_domain:
        condor = CondorSettings(
            condor_host,
            sec_password_file,
            sec_token_issuer_key,
            uid_domain,
        )

    return Settings(
        config_path=pathlib.Path(
            os.environ.get(
                "KUBESPAWNER_HOOKS_CONFIG",
                "/etc/osg/kubespawner_hooks_config.yaml",
            )
        ),
        condor=condor,
    )


# Process-lifetime singleton; resettable in tests via
# `settings.cache_clear()`. See `config.settings` in the code service.
settings = functools.cache(load_settings)


@dataclasses.dataclass
class KubespawnerOverride:
    """
    `kubespawner_override` key-value pairs to apply to a server option.

    The override will be applied only if the user is a member of one of
    the listed groups or if no groups are listed.
    """

    groups: list[str]
    override: dict[str, Any]


@dataclasses.dataclass
class ProfileList:
    """
    A list of server options (`profile_list` in KubeSpawner parlance).

    The options will be shown to the user only if the user is a member
    of one of the listed groups or if no groups are listed.
    """

    groups: list[str]
    servers: list[dict[str, Any]]


@dataclasses.dataclass
class RequireCode:
    """
    A conditional requirement enforced by `pre_spawn_hook`.

    A user whose username matches `when_username_matches` must belong to
    one of `satisfied_by_groups` (real or virtual) to spawn.

    `when_username_matches` must match the entire username (see
    `re.fullmatch`); to gate an exact name, `re.escape` its metacharacters.
    """

    when_username_matches: str

    satisfied_by_groups: list[str]

    message: str = "A valid access code is required before you can launch a server."


@dataclasses.dataclass
class Configuration:
    """
    Defines the structure of the configuration file used by the hooks below.

    This configuration defines:

      1. The server options that a user should see.
      2. How to configure each of those server options.
      3. Which users must hold a (real or virtual) group to spawn.
    """

    server_defaults: dict[str, Any]

    server_lists: list[ProfileList]

    server_overrides: dict[str, KubespawnerOverride]

    require_code: list[RequireCode] = dataclasses.field(default_factory=list[RequireCode])


# --------------------------------------------------------------------------


def auth_state_hook(spawner: Any, auth_state: Any) -> None:
    """
    Saves the user's OIDC userinfo object to the spawner.
    """

    userdata: dict[str, Any] = {}

    current_state: dict[str, Any] = auth_state or {}

    for key in [
        "bitbucket_user",
        "cilogon_user",
        "gitlab_user",
        "oauth_user",
        "openshift_user",
    ]:
        if key in current_state:
            value = current_state[key]
            # An IdP controls this shape; guard against a non-dict that
            # would later crash `oidc.get_person`'s `.get(...)`.
            # Skip empty dicts so that an empty earlier-listed key does
            # not mask a populated later one.
            if isinstance(value, dict) and value:
                userdata = cast("dict[str, Any]", value)
                break

    spawner.userdata = userdata
    spawner.log.info(f"Current userdata: {userdata!r}")


async def options_form(spawner: Any) -> str:
    """
    Sets the spawner's `profile_list` for the current user.
    """
    # Reference: https://discourse.jupyter.org/t/tailoring-spawn-options-and-server-configuration-to-certain-users/8449

    person = _get_person(spawner)
    effective = _effective_groups(spawner, person)

    person_as_dict = dataclasses.asdict(person)

    spawner.log.info(f"Building options form for {person_as_dict!r}")
    spawner.log.info(f"Effective groups: {sorted(effective)!r}")

    config = await _load_config_or_spawn_error(spawner)

    # An unsatisfied `require_code` rule would block the spawn regardless
    # of which option is chosen (see `pre_spawn_hook`), so hide every
    # option and show the rule's message instead of an unusable form.
    unmet = _first_unsatisfied(_applicable_require_code(config, spawner.user.name), effective)
    if unmet is not None:
        spawner.log.info(
            f"Hiding server options for {spawner.user.name!r}: "
            + f"unmet require_code rule {unmet.when_username_matches!r}"
        )
        spawner.profile_list = []
        return _require_code_form(unmet.message)

    profile_list: list[dict[str, Any]] = []

    # `_merge_override`/`_build_value` raise `ConfigError` per-user at
    # merge time, past `_validate`'s reach; convert to a spawn error.
    try:
        for server in _get_servers(config, effective):
            # Copy before mutating: the `pop` below would corrupt the
            # shared config dict that `_get_servers` yields.
            server = copy.deepcopy(server)
            server_override = server.get("kubespawner_override", {})
            server_includes = server_override.pop("include", [])
            # `kubespawner_override` is untyped, so `baydemir.parsing`
            # does not enforce that `include` is a list. A bare string
            # would otherwise be iterated character by character below,
            # silently dropping the intended override.
            if not isinstance(server_includes, list):
                raise ConfigError(f"server `include` must be a list, got {server_includes!r}")
            server_includes = cast("list[str]", server_includes)
            composite_override: dict[str, Any] = {}
            _merge_override(
                composite_override,
                copy.deepcopy(config.server_defaults),
                person.ospool,
            )

            for key in server_includes:
                override = config.server_overrides.get(key)
                if override is None:
                    spawner.log.warning(
                        f"Server include references unknown override key {key!r}; skipping"
                    )
                    continue
                if not override.groups or set(override.groups).intersection(effective):
                    _merge_override(
                        composite_override,
                        copy.deepcopy(override.override),
                        person.ospool,
                    )
            _merge_override(composite_override, server_override, person.ospool)

            server["kubespawner_override"] = composite_override
            profile_list.append(server)
    except ConfigError as exc:
        spawner.log.error(f"Cannot build server options: {exc}")
        raise _spawn_error(_INVALID_CONFIG_MESSAGE) from exc

    spawner.profile_list = profile_list

    # `_options_form_default` is KubeSpawner's own accessor for the
    # default options-form HTML; calling it is the documented pattern.
    return spawner._options_form_default()  # pylint: disable=protected-access


def modify_pod_hook(spawner: Any, pod: k8s.V1Pod) -> k8s.V1Pod:
    """
    Adds an HTCondor IDTOKEN to the notebook container's environment.

    Applies only to OSPool users.
    """

    # kubernetes_asyncio models are untyped; work through an `Any` alias.
    notebook: Any = _get_notebook_container(pod)
    person = _get_person(spawner)
    condor = settings().condor

    if person.ospool and condor:
        # A missing/unreadable password file or a crypto error would
        # otherwise abort the pod build with an opaque 500; convert it to
        # the same fail-closed-but-friendly spawn error the other hooks
        # use. A transient read failure should not hard-fail the spawn
        # with a confusing traceback.
        try:
            password = htcondor.read_password(condor.sec_password_file)
            iss = condor.condor_host
            sub = f"{person.ospool.username}@{condor.uid_domain}"
            kid = condor.sec_token_issuer_key
            token = htcondor.create_token(password=password, iss=iss, sub=sub, kid=kid)
        except Exception as exc:  # pylint: disable=broad-except
            spawner.log.error(
                f"Could not mint an HTCondor IDTOKEN for {spawner.user.name!r}",
                exc_info=True,
            )
            raise _spawn_error(
                "Could not prepare your OSPool credentials;"
                + " please try launching again in a moment."
            ) from exc

        env: list[Any] = notebook.env or []
        env.append(k8s.V1EnvVar(name="_osg_HTCONDOR_IDTOKEN", value=token))
        notebook.env = env

    return pod


async def pre_spawn_hook(spawner: Any) -> None:
    """
    Blocks a spawn when a `require_code` rule applies but is unsatisfied.

    Raising aborts the spawn; the exception's message is shown to the
    user.
    See `RequireCode` for when a rule applies and is satisfied.
    """

    config = await _load_config_or_spawn_error(spawner)

    person = _get_person(spawner)
    username: str = spawner.user.name

    applicable = _applicable_require_code(config, username)
    if not applicable:
        # No rule gates this user, so don't let a native-groups read
        # failure block them.
        return

    # A user whose OIDC groups already satisfy every applicable rule
    # is entitled regardless of the native list, so don't read it (and
    # don't let a failure to read it block them).
    if _first_unsatisfied(applicable, set(person.groups)) is None:
        return

    # A rule remains unsatisfied by the OIDC groups alone. Its virtual
    # (code-granted) groups live only in the native list, so fail loudly
    # if that read fails rather than block a user who has already entered
    # a valid code.
    try:
        native = _native_groups(spawner)
    except Exception as exc:  # pylint: disable=broad-except
        spawner.log.error(
            f"Could not read native groups for {username!r} while"
            + " enforcing a require_code rule",
            exc_info=True,
        )
        raise _spawn_error(
            "Could not verify your access code; please try launching again in a moment."
        ) from exc

    effective = _combine_groups(person, native)

    unmet = _first_unsatisfied(applicable, effective)
    if unmet is not None:
        spawner.log.info(
            f"Blocking spawn for {username!r}: "
            + f"unmet require_code rule {unmet.when_username_matches!r}"
        )
        raise _spawn_error(unmet.message)


# --------------------------------------------------------------------------


def _get_config() -> Configuration:
    """
    Returns the configuration for the hooks.

    A missing, malformed, or invalid file raises `ConfigError`, which
    `_load_config_or_spawn_error` turns into a spawn error. A missing
    file is treated as an error, not as an empty configuration, which
    would silently disable the `require_code` gate for everyone.
    """

    config = load_yaml_config(settings().config_path, Configuration)
    _validate(config)
    return config


# Re-read at most once per TTL, single-flight, serving the last-good
# config if a read is slow or hangs. See `CachedConfigLoader`.
_config_loader = CachedConfigLoader(_get_config)


async def _load_config_or_spawn_error(spawner: Any) -> Configuration:
    """
    Returns the hooks configuration, or aborts the spawn if it is invalid.

    Both `options_form` and `pre_spawn_hook` must load the configuration
    before doing their work and fail the spawn identically when it cannot
    be loaded, so that logging and the user-facing message stay in sync.

    The read is served from `CachedConfigLoader`, which caches the parsed
    config with a short TTL, offloads a refresh to a thread, and serves
    the last-good config if a read is slow -- so a slow or hung mount
    (e.g. a stalled ConfigMap/Secret) neither stalls the event loop nor
    exhausts its thread pool during a spawn storm. A read that yields an
    invalid config still fails the spawn.
    """

    try:
        return await _config_loader.get(log=spawner.log)
    except ConfigUnavailableError as exc:
        # A transient cold-start timeout, not a misconfiguration: guide the
        # user to retry rather than to contact an administrator.
        spawner.log.warning(f"Hooks configuration temporarily unavailable: {exc}")
        raise _spawn_error(_CONFIG_UNAVAILABLE_MESSAGE) from exc
    except ConfigError as exc:
        spawner.log.error(f"Cannot load hooks configuration: {exc}")
        raise _spawn_error(_INVALID_CONFIG_MESSAGE) from exc


def _validate(config: Configuration) -> None:
    """
    Raises `ConfigError` if a `require_code` rule is semantically invalid.

    `baydemir.parsing` checks types but not contents.
    Validating at load time turns a latent, per-spawn failure into a
    loud, fail-closed error that names the offending rule -- e.g. a
    malformed regex here would otherwise abort every user's spawn with
    an opaque 500.
    """

    for rule in config.require_code:
        try:
            re.compile(rule.when_username_matches)
        except re.error as exc:
            raise ConfigError(
                "require_code rule has an invalid `when_username_matches`"
                + f" regex {rule.when_username_matches!r}: {exc}"
            ) from exc

        # A rule with no groups can never be satisfied, so it would
        # silently block every user its pattern matches.
        if not rule.satisfied_by_groups:
            raise ConfigError(
                f"require_code rule {rule.when_username_matches!r} lists no"
                + " `satisfied_by_groups`, so it can never be satisfied"
            )
        for group in rule.satisfied_by_groups:
            if not group.strip():
                raise ConfigError(
                    f"require_code rule {rule.when_username_matches!r} has a"
                    + " blank group in `satisfied_by_groups`"
                )


def _get_notebook_container(pod: k8s.V1Pod) -> k8s.V1Container:
    """
    Returns the pod's notebook container.
    """

    # kubernetes_asyncio models are untyped; work through an `Any` alias.
    pod_any: Any = pod

    if pod_any.spec is None or pod_any.spec.containers is None:
        raise RuntimeError("The pod has no spec or no containers")

    for container in pod_any.spec.containers:
        if container.name == NOTEBOOK_CONTAINER_NAME:
            return container

    raise RuntimeError(f"The pod has no container named {NOTEBOOK_CONTAINER_NAME!r}")


def _get_servers(config: Configuration, effective: set[str]) -> Iterator[dict[str, Any]]:
    """
    Yields the server options to show to a user with these groups.
    """

    for spec in config.server_lists:
        if not spec.groups or set(spec.groups).intersection(effective):
            yield from spec.servers


def _get_person(spawner: Any) -> oidc.OIDCPerson:
    """
    Returns the OIDC person for the current user, or an empty person.
    """

    person = oidc.get_person(getattr(spawner, "userdata", None) or {})
    if person is None:
        person = oidc.OIDCPerson(sub="", groups=[])
    return person


def _native_groups(spawner: Any) -> set[str]:
    """
    Returns the user's native JupyterHub group memberships.

    Raises if the group list cannot be read, so that a caller can tell an
    empty membership apart from an unreadable one. `_effective_groups`
    tolerates the failure for the options form, but `pre_spawn_hook` must
    treat it as a transient error rather than silently dropping the
    virtual (code-granted) groups it enforces against.
    """

    return {g.name for g in spawner.user.groups}


def _effective_groups(spawner: Any, person: oidc.OIDCPerson) -> set[str]:
    """
    Returns the user's effective group memberships.

    This is the union of the user's OIDC groups (from the OIDC
    userinfo) and their native JupyterHub group memberships (e.g. the
    virtual groups granted by the code-entry service).
    """

    native: set[str] = set()

    try:
        native = _native_groups(spawner)
    except Exception:  # pylint: disable=broad-except
        # Never fail the options form over an unreadable group list; the
        # user just sees fewer options.
        # `pre_spawn_hook` handles this failure explicitly.
        spawner.log.warning(
            f"Could not read native groups for {spawner.user.name!r}",
            exc_info=True,
        )

    return _combine_groups(person, native)


def _combine_groups(person: oidc.OIDCPerson, native: set[str]) -> set[str]:
    """
    Union of a user's OIDC and native JupyterHub groups.

    The single source of truth for both callers; each keeps its own
    native-read failure policy.
    """

    return set(person.groups) | native


def _applicable_require_code(config: Configuration, username: str) -> list[RequireCode]:
    """
    Returns the `require_code` rules that apply to `username`.

    A rule applies when its `when_username_matches` fully matches the
    username. Kept separate from the satisfaction check so `pre_spawn_hook`
    can tell a user is ungated -- and skip its native-groups read --
    before consulting group membership.
    """

    return [
        rule
        for rule in config.require_code
        if re.fullmatch(rule.when_username_matches, username)
    ]


def _first_unsatisfied(rules: Iterable[RequireCode], effective: set[str]) -> RequireCode | None:
    """
    Returns the first rule in `rules` not satisfied by `effective`.

    A rule is satisfied when the user holds any of its
    `satisfied_by_groups`. Shared by `options_form` (to hide options) and
    `pre_spawn_hook` (to block a spawn) so both enforce identically.
    """

    for rule in rules:
        if not set(rule.satisfied_by_groups).intersection(effective):
            return rule
    return None


def _require_code_form(message: str) -> str:
    """
    Returns the options-form HTML shown when a `require_code` rule hides
    every server option.

    `pre_spawn_hook` remains the backstop if a user submits the form
    anyway. `message` is admin-configured but escaped regardless.
    """

    return f'<div class="text-center">{html.escape(message)}</div>'


def _spawn_error(message: str) -> web.HTTPError:
    """
    Returns an exception that aborts a spawn and is shown to the user.
    """

    error = web.HTTPError(403, message)
    error.jupyterhub_message = message  # type: ignore[attr-defined]
    return error


def _build_value(raw_value: Any, user: oidc.OSPoolPerson | None) -> Any:
    """
    Builds a Kubernetes Python API object or value.
    """

    if isinstance(raw_value, str):
        if user:
            # UID/GID as ints; the generic replace below stringifies.

            if raw_value == "{user.uid}":
                return user.uid

            if raw_value == "{user.gid}":
                return user.gid

            for field in dataclasses.fields(user):
                k = f"{{user.{field.name}}}"
                v = getattr(user, field.name)

                raw_value = raw_value.replace(k, str(v))

        # Assume any remaining templates are intentional.
        return raw_value

    if isinstance(raw_value, dict):
        # An API object or a plain dict; the cast recovers a concrete
        # element type from the untyped input.
        raw_dict = cast(dict[str, Any], raw_value)

        cls: Any
        if "_" in raw_dict:
            class_name = raw_dict["_"]
            try:
                cls = k8s.__dict__[class_name]
            except KeyError as exc:
                raise ConfigError(
                    f"unknown Kubernetes class {class_name!r} in a"
                    + " kubespawner_override `_` value"
                ) from exc
        else:
            cls = dict

        args: dict[str, Any] = {
            k: _build_value(v, user) for k, v in raw_dict.items() if k != "_"
        }

        # A valid class name paired with an unexpected keyword, a
        # wrong-typed argument, or a `_` that names a non-class attribute
        # of the client raises `TypeError`/`ValueError`. Re-raise as
        # `ConfigError` so `options_form` converts it to the standard
        # fail-closed spawn error rather than an opaque 500.
        try:
            return cls(**args)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"cannot build {getattr(cls, '__name__', cls)!r} from a"
                + f" kubespawner_override: {exc}"
            ) from exc

    if isinstance(raw_value, list):
        raw_list = cast(list[object], raw_value)
        return [_build_value(x, user) for x in raw_list]

    return raw_value  # assume that this is a scalar to be used as-is


def _merge_override(
    target: dict[str, Any],
    source: dict[str, Any],
    user: oidc.OSPoolPerson | None,
) -> None:
    """
    Merges one set of `kubespawner_override` keys into another.

    Unlike `KubeSpawner`, list values are concatenated, not replaced.
    """

    for k, raw_v in source.items():
        v = _build_value(raw_v, user)  # substitute user.username, etc.

        if isinstance(v, dict):
            if not isinstance(target.setdefault(k, {}), dict):
                raise ConfigError(
                    f"conflicting types for kubespawner_override key {k!r}: "
                    + f"cannot merge a mapping onto a {type(target[k]).__name__}"
                )
            target[k].update(v)
        elif isinstance(v, list):
            if not isinstance(target.setdefault(k, []), list):
                raise ConfigError(
                    f"conflicting types for kubespawner_override key {k!r}: "
                    + f"cannot concatenate a list onto a {type(target[k]).__name__}"
                )
            target[k].extend(v)
        else:
            if isinstance(target.get(k), (dict, list)):
                raise ConfigError(
                    f"conflicting types for kubespawner_override key {k!r}: "
                    + f"cannot overwrite a {type(target[k]).__name__} with a scalar"
                )
            target[k] = v

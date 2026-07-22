"""
Shared helpers for loading YAML configuration files.

Both the KubeSpawner hooks and the code-entry service load a single YAML
file into a dataclass, wrap the same load-time failures as a `ConfigError`,
and then perform their own semantic validation. This module holds the
common load/wrap logic and the shared `ConfigError`.
"""

import asyncio
import concurrent.futures
import logging
import os
import pathlib
import time
from collections.abc import Callable
from typing import Generic, TypeVar

import baydemir.parsing
import yaml

__all__ = [
    "CachedConfigLoader",
    "ConfigError",
    "ConfigUnavailableError",
    #
    "bool_from_env",
    "load_yaml_config",
    "positive_int_from_env",
]

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


class ConfigError(Exception):
    """
    Raised when a configuration cannot be loaded or is invalid.

    An instance's message is safe both to log and to show to a user.
    """


class ConfigUnavailableError(ConfigError):
    """
    Raised when a fresh read times out and no previously loaded config
    is available to serve (a cold start).

    A subclass of `ConfigError` so existing fail-closed handlers still
    catch it, but distinct so a caller may special-case the transient
    case if it wants a different, "try again shortly" message.
    """


def load_yaml_config(path: pathlib.Path, spec: type[T]) -> T:
    """
    Loads and structurally parses a YAML configuration file.

    A missing, malformed, or wrongly structured file raises `ConfigError`;
    the caller is expected to fail closed. A missing file is treated as an
    error, not as an empty configuration. Semantic (value-level) validation
    is the caller's responsibility.
    """

    try:
        return baydemir.parsing.load_yaml(path, spec)
    except FileNotFoundError as exn:
        raise ConfigError(f"Configuration file {path} does not exist") from exn
    except (OSError, UnicodeDecodeError) as exn:
        raise ConfigError(f"Configuration file {path} could not be read") from exn
    except (baydemir.parsing.ParseError, yaml.YAMLError) as exn:
        raise ConfigError(
            f"Configuration file {path} is malformed or has" + " the wrong structure"
        ) from exn


def bool_from_env(name: str, default: bool) -> bool:
    """
    Reads a boolean from the environment variable `name`.

    An unset variable yields `default`; otherwise the value is true only
    when it equals `"true"` (case-insensitively).
    """

    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() == "true"


def positive_int_from_env(name: str, default: int) -> int:
    """
    Reads a positive integer from the environment variable `name`.

    Raises `ConfigError` for a non-numeric or non-positive value so that
    a bad tuning knob is reported like any other config error at startup
    rather than crash-looping the service on a raw import-time traceback.
    """

    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, not {raw!r}") from None
    if value < 1:
        raise ConfigError(f"{name} must be at least 1, not {value}")
    return value


class CachedConfigLoader(
    Generic[T]
):  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """
    Wraps a synchronous config loader with a TTL cache, a single-flight
    refresh, and serve-stale-on-timeout.

    The target is a service on a single asyncio event loop (the
    KubeSpawner hooks, the code-entry service) that re-reads a mounted
    YAML config frequently so that a change to an auto-updating Secret
    takes effect without a restart. Reading on every request has two
    hazards this class removes:

      1. During a request/spawn storm, offloading every read to the
         loop's shared default thread pool can exhaust it. The TTL cache
         plus single-flight refresh bounds reads to one at a time, at
         most one per `ttl_seconds`.
      2. A hung mount (stalled NFS, unmounted volume) makes an untimed
         read block its thread forever. The `timeout_seconds` cap lets a
         caller fall back to the last-good config instead of hanging, so
         a client always gets *some* response in a timely fashion.

    A slow or hung read is treated as transient (serve stale, or raise
    `ConfigUnavailableError` on a cold start), whereas a read that
    succeeds but yields an invalid config raises `ConfigError` from the
    loader: an invalid config fails closed and stays loud, even when a
    good config is cached. Such a failure is itself cached for
    `ttl_seconds` — the same `ConfigError` is re-raised for later
    requests in that window — so an invalid-config storm is throttled to
    one read per TTL, just like the success path.

    Only *validated* configs are cached (the loader is expected to
    validate before returning), so a cache hit can never yield something
    the loader would have rejected. Callers must treat the returned
    object as read-only, since it is shared across concurrent requests.
    """

    def __init__(
        self,
        load: Callable[[], T],
        *,
        ttl_seconds: float = 15.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._load = load
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        # A `time.monotonic()` timestamp paired with the last good value.
        self._cache: tuple[float, T] | None = None
        # A `time.monotonic()` timestamp paired with the most recent
        # loader failure, so an invalid-config window throttles re-reads
        # to one per TTL (like the success path) while still failing
        # closed by re-raising the same `ConfigError`.
        self._error: tuple[float, ConfigError] | None = None
        # Serializes refreshes so a storm triggers one read, not many.
        # A module/instance-level `asyncio.Lock` is safe on Python 3.10+:
        # it binds to the running loop lazily on first `await`, well after
        # this loader is constructed at import time.
        self._lock = asyncio.Lock()
        # Config reads run here, never on the loop's shared default
        # executor. A hung mount leaves this one worker stuck; because a
        # timed-out read is kept as `_inflight` and reused rather than
        # resubmitted (see `get`), a persistent hang orphans at most one
        # thread and one queued job, and can never starve the shared pool.
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="config-loader"
        )
        # The read currently running on `_executor`, if any. Reused across
        # refreshes so a `wait_for` timeout does not resubmit a fresh job
        # that would queue behind (and never overtake) a stuck worker.
        self._inflight: asyncio.Future[T] | None = None

    async def get(self, *, log: logging.Logger | None = None) -> T:
        """
        Returns the configuration, reloading it at most once per TTL.

        Serves the cached config when it is fresh or when a refresh is
        already in flight; on a read that exceeds the timeout, serves the
        last-good config if there is one, else raises
        `ConfigUnavailableError`. A `ConfigError` from the loader (an
        invalid config) is re-raised on every request but cached for
        `ttl_seconds`, so re-reads are throttled like the success path.
        """

        _log = log or _LOGGER

        cached = self._cache
        now = time.monotonic()

        # 1. Fresh cache: serve immediately, with no I/O and no lock.
        if cached is not None and now - cached[0] < self._ttl:
            return cached[1]

        # 1b. Recent invalid-config failure: re-raise it without another
        #     read, throttling re-reads to one per TTL while failing
        #     closed. A fresh good cache and a fresh error are mutually
        #     exclusive (a successful read clears the error), so a stale
        #     good cache never masks a fresh failure here.
        error = self._error
        if error is not None and now - error[0] < self._ttl:
            raise error[1]

        # 2. A refresh is already in flight and we have something to
        #    serve: return the stale value rather than queue behind the
        #    (possibly slow) read that holds the lock.
        if self._lock.locked() and cached is not None:
            return cached[1]

        async with self._lock:
            # 3. Re-check: another coroutine may have refreshed while we
            #    waited for the lock (matters mainly on a cold start,
            #    when step 2 could not short-circuit).
            cached = self._cache
            error = self._error
            now = time.monotonic()
            if cached is not None and now - cached[0] < self._ttl:
                return cached[1]
            if error is not None and now - error[0] < self._ttl:
                raise error[1]

            loop = asyncio.get_running_loop()
            # Reuse a read that a previous refresh started but timed out
            # on, rather than submitting a fresh job that would queue
            # behind (and never overtake) the stuck single worker.
            fut = self._inflight
            if fut is None:
                fut = self._inflight = loop.run_in_executor(self._executor, self._load)
            try:
                # `shield` so a `wait_for` timeout cancels only this wait,
                # not the underlying read: the thread keeps running and
                # `_inflight` stays valid for the next refresh to reuse.
                config = await asyncio.wait_for(asyncio.shield(fut), self._timeout)
            except TimeoutError:
                # The read is still running on this loader's dedicated
                # single-worker pool (see `__init__`); we keep it as
                # `_inflight` so later refreshes await the same job. So
                # even a run of timed-out reads orphans at most one thread
                # and one queued job, and never touches the shared pool.
                if cached is not None:
                    _log.warning(
                        "Config read exceeded %.0fs; serving cached config"
                        + " (up to %.0fs stale)",
                        self._timeout,
                        self._ttl,
                    )
                    return cached[1]
                raise ConfigUnavailableError(
                    "Timed out reading the configuration and none is cached to serve"
                ) from None
            except ConfigError as exn:
                # Invalid config: fail closed and stay loud, but record
                # the failure so requests within the TTL re-raise it
                # instead of re-reading on every request. Leave any
                # existing good cache untouched. (A `ConfigUnavailableError`
                # from the timeout branch above is raised from within that
                # handler, so it is never intercepted here.)
                self._inflight = None
                self._error = (time.monotonic(), exn)
                raise

            self._inflight = None
            self._cache = (time.monotonic(), config)
            self._error = None
            return config

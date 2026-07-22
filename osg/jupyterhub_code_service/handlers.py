"""
Request handlers for the code-entry service.
"""

import enum
import logging
from typing import Any, cast

# pylint: disable=import-error,no-name-in-module
from jupyterhub.services.auth import HubOAuthenticated

# jupyterhub types `url_path_join`'s `*pieces` as unknown, which pyright
# surfaces on the imported symbol; the call site is well defined.
from jupyterhub.utils import url_path_join  # pyright: ignore[reportUnknownVariableType]

# pylint: enable=import-error,no-name-in-module
from tornado.httpclient import HTTPClientError
from tornado.web import RequestHandler, authenticated
from typing_extensions import override

from osg.jupyterhub_code_service import config as config_module
from osg.jupyterhub_code_service import grants as grants_module
from osg.jupyterhub_code_service import limiter as limiter_module

__all__ = [
    "CodeFormHandler",
    "HealthHandler",
]

log = logging.getLogger(__name__)

# 4xx statuses that mean a transient, retryable condition rather than a
# misconfiguration: 408 Request Timeout and 429 Too Many Requests.
RETRYABLE_HTTP_CODES = frozenset({408, 429})


class _GrantOutcome(enum.Enum):
    """
    The result of attempting to apply a matched code's grants.
    """

    SUCCESS = enum.auto()
    TRANSIENT_FAILURE = enum.auto()
    MISCONFIGURATION = enum.auto()


# The mixin overrides `xsrf_token` with a signature pyright dislikes, but
# it is the supported way to authenticate a Hub service.
class CodeFormHandler(  # pylint: disable=abstract-method  # pyright: ignore[reportIncompatibleMethodOverride]
    HubOAuthenticated, RequestHandler
):
    """
    Serves the code-entry form and applies a submitted code.
    """

    # Underscore-prefixed so they do not shadow
    # `HubOAuthenticated.hub_groups` (its allowed-groups attribute).
    _hub_groups: grants_module.HubGroups
    _limiter: limiter_module.Limiter

    @override
    def initialize(
        self,
        hub_groups: grants_module.HubGroups,
        limiter: limiter_module.Limiter,
    ) -> None:
        """
        Stores the shared services used to apply a submitted code.
        """

        self._hub_groups = hub_groups
        self._limiter = limiter

    def _render_error(self, message: str, status: int = 200) -> None:
        self.set_status(status)
        self.render("enter_code.html", error=message)

    def _render_transient_failure(self) -> None:
        self._render_error(
            (
                "Could not fully apply your code. Please submit it"
                " again; if the problem persists, contact an organizer."
            ),
            status=502,
        )

    def _render_config_unavailable(self) -> None:
        self._render_error(
            "The service is temporarily unavailable. Please try again shortly.",
            status=503,
        )

    def _render_misconfiguration(self) -> None:
        self._render_error(
            (
                "Your code is valid but is not configured correctly."
                " Please contact an organizer."
            ),
            status=500,
        )

    @authenticated
    def get(self) -> None:
        """
        Renders the empty code-entry form.
        """

        self.render("enter_code.html", error="")

    async def _apply_grants(self, grants: list[str], username: str) -> _GrantOutcome:
        """
        Adds `username` to every group in `grants`, classifying failures.

        `add_user` is idempotent, so a partial failure recovers by
        resubmitting the same code: already-added groups no-op and the
        failed one completes.
        """

        try:
            for group in grants:
                await self._hub_groups.add_user(group, username)
        except HTTPClientError as exn:
            code = exn.code or 0
            if 400 <= code < 500 and code not in RETRYABLE_HTTP_CODES:
                # A non-retryable 4xx (404 missing group, 403 missing
                # `admin:groups`, etc.) is a permanent misconfiguration;
                # retrying is futile.
                log.error("Code valid but misconfigured for %r", username, exc_info=True)
                return _GrantOutcome.MISCONFIGURATION
            # A retryable 4xx (408, 429) or any other HTTP error is
            # transient (e.g. Hub under load); resubmitting may work.
            log.exception("Failed to apply code for %r", username)
            return _GrantOutcome.TRANSIENT_FAILURE
        except Exception:  # pylint: disable=broad-except
            log.exception("Failed to apply code for %r", username)
            return _GrantOutcome.TRANSIENT_FAILURE

        return _GrantOutcome.SUCCESS

    @authenticated
    async def post(self) -> None:  # pylint: disable=too-many-return-statements
        """
        Validates a submitted code and grants the resulting groups.
        """

        # `@authenticated` guarantees a user; the cast/disable satisfy the
        # checkers for the untyped `get_current_user`.
        user = cast(dict[str, Any], self.get_current_user())
        username = str(user["name"])  # pylint: disable=unsubscriptable-object

        # Charge a token synchronously, before the first `await`, so
        # concurrent submissions from one user each contend on the same
        # pre-await state instead of all slipping through a stale read.
        if not self._limiter.try_acquire(username):
            self._render_error("Too many attempts. Please slow down and try again.")
            return

        # Reload so a newly issued code takes effect without a restart;
        # fail closed on an invalid edit. The cached loader re-reads at
        # most once per TTL, offloads the read to a thread, and serves
        # the last-good config if storage is slow, so a slow or hung
        # mount can't stall the event loop for other users.
        try:
            config = await config_module.config_loader.get(log=log)
        except config_module.ConfigUnavailableError as exn:
            # A transient cold-start timeout, not a misconfiguration: tell
            # the user to retry rather than surfacing a permanent 500.
            log.warning("Configuration temporarily unavailable: %s", exn)
            self._render_config_unavailable()
            return
        except config_module.ConfigError as exn:
            log.error("Configuration is invalid: %s", exn)
            self._render_error(
                (
                    "Something went wrong. Please try again later; if the"
                    " problem persists, contact an organizer."
                ),
                status=500,
            )
            return

        result = grants_module.match_code(config, self.get_argument("code", ""))

        if not result.matched:
            self._render_error("Invalid or expired code.")
            return

        if not result.grants:
            # Correct code, but every grant was dropped (e.g. missing
            # prefix): a misconfiguration, not a bad attempt. The token
            # charged above is not refunded, so this outcome still counts
            # toward lockout; we keep the implementation simple and accept
            # that a persistent misconfiguration can eventually lock a
            # user out.
            log.error("Code matched for %r but yielded no usable grants", username)
            self._render_misconfiguration()
            return

        grants = result.grants

        outcome = await self._apply_grants(grants, username)
        if outcome is _GrantOutcome.TRANSIENT_FAILURE:
            self._render_transient_failure()
            return
        if outcome is _GrantOutcome.MISCONFIGURATION:
            self._render_misconfiguration()
            return

        log.info("Granted %r to %r", grants, username)

        self.redirect(url_path_join(config_module.settings().base_url, "hub/spawn"))


class HealthHandler(RequestHandler):  # pylint: disable=abstract-method
    """
    An unauthenticated health-check endpoint for probes.
    """

    @override
    def get(self) -> None:
        """
        Responds with a static "ok" payload for liveness probes.
        """

        # `RequestHandler.write` is typed with a bare `dict`; pyright
        # flags it as partially unknown, but the call is well defined.
        self.write({"status": "ok"})  # pyright: ignore[reportUnknownMemberType]

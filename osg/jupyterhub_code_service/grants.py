"""
Validation of submitted codes and granting of group memberships.

This module holds the only code that knows the code secrets. Codes are
never logged; only a matched code's `id` is.
"""

import dataclasses
import datetime
import hmac
import json
import logging
from urllib.parse import quote

from tornado.httpclient import AsyncHTTPClient, HTTPClientError, HTTPRequest

from osg.jupyterhub_code_service import config as config_module

__all__ = [
    "HubGroups",
    "MatchResult",
    #
    "match_code",
]

log = logging.getLogger(__name__)


@dataclasses.dataclass
class MatchResult:
    """
    The outcome of checking a submitted code.

    `matched` is true when a non-expired code matched the submission,
    regardless of whether any grants survived the prefix filter.
    `grants` are the group names to apply; it may be empty even when
    `matched` is true, indicating a misconfigured code.
    """

    matched: bool
    grants: list[str]


def match_code(
    config: config_module.CodeServiceConfig,
    submitted: str,
    now: datetime.datetime | None = None,
) -> MatchResult:
    """
    Returns the outcome of checking the submitted code.

    Grants not beginning with the configured group prefix (see
    `config.CodeServiceSettings.group_prefix`) are dropped so a
    misconfigured code cannot grant an arbitrary (e.g. admin) group.
    See `MatchResult` for the matched/empty-grants contract.
    """

    if now is None:
        now = datetime.datetime.now(datetime.timezone.utc)

    submitted_value = submitted.strip()
    submitted_bytes = submitted_value.encode("utf-8")

    # Secrets are unique (see `config._validate`), so the linear scan
    # matches at most one code. Compare in constant time so the runtime
    # does not leak how many leading characters of a guess are correct.
    code = next(
        (
            c
            for c in config.codes
            if hmac.compare_digest(c.secret.encode("utf-8"), submitted_bytes)
        ),
        None,
    )
    if code is None:
        return MatchResult(False, [])
    if code.expires and config_module.parse_expires(code.expires) <= now:
        return MatchResult(False, [])

    log.info("Accepted code %r", code.id)

    group_prefix = config_module.settings().group_prefix

    grants: list[str] = []
    for group in code.grants:
        if group.startswith(group_prefix):
            grants.append(group)
        else:
            log.warning(
                "Code %r grants %r without the required prefix %r; dropping",
                code.id,
                group,
                group_prefix,
            )

    return MatchResult(True, sorted(set(grants)))


class HubGroups:  # pylint: disable=too-few-public-methods
    """
    A minimal client for the Hub API's group-membership endpoints.

    All calls authenticate with the service's own API token.
    """

    def __init__(self) -> None:
        self.api_url = config_module.settings().api_url
        self.token = config_module.settings().api_token
        self.client = AsyncHTTPClient()

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"token {self.token}",
            "Content-Type": "application/json",
        }

    async def add_user(self, group: str, username: str) -> None:
        """
        Adds a user to a group.

        Idempotent: re-adding a member is a no-op, so the grant loop is
        safe to retry.

        With `CODE_SERVICE_CREATE_MISSING_GROUPS` set, a 404 creates the
        group (needs the `admin:groups` scope) and retries the add.
        """

        url = f"{self.api_url}/groups/{quote(group, safe='')}/users"
        body = json.dumps({"users": [username]})

        async def _post_users() -> None:
            await self.client.fetch(
                HTTPRequest(url, method="POST", headers=self._headers(), body=body)
            )

        try:
            await _post_users()
        except HTTPClientError as exn:
            if exn.code == 404 and config_module.settings().create_missing_groups:
                try:
                    await self._create_group(group)
                except HTTPClientError as create_exn:
                    # Likely a concurrent create won the race; retry the
                    # add anyway.
                    log.info(
                        "Creating group %r failed (%s); retrying add anyway",
                        group,
                        create_exn.code,
                    )
                await _post_users()
            else:
                raise

    async def _create_group(self, group: str) -> None:
        url = f"{self.api_url}/groups/{quote(group, safe='')}"
        await self.client.fetch(
            HTTPRequest(url, method="POST", headers=self._headers(), body=b"")
        )

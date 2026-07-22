"""
The Tornado application for the code-entry service.
"""

import logging
import os
import sys
import urllib.parse

# pylint: disable=import-error,no-name-in-module
from jupyterhub.services.auth import HubOAuthCallbackHandler

# jupyterhub types `url_path_join`'s `*pieces` as unknown, which pyright
# surfaces on the imported symbol; the call site is well defined.
from jupyterhub.utils import url_path_join  # pyright: ignore[reportUnknownVariableType]

# pylint: enable=import-error,no-name-in-module
from tornado.ioloop import IOLoop
from tornado.web import Application

from osg.jupyterhub_code_service import config as config_module
from osg.jupyterhub_code_service import grants as grants_module
from osg.jupyterhub_code_service import handlers as handlers_module
from osg.jupyterhub_code_service import limiter as limiter_module

__all__ = [
    "main",
    "make_app",
]

log = logging.getLogger(__name__)


def make_app() -> Application:
    """
    Builds the Tornado application for the code-entry service.
    """

    prefix = config_module.settings().service_prefix

    hub_groups = grants_module.HubGroups()
    limiter = limiter_module.Limiter()

    return Application(
        [
            (
                url_path_join(prefix, "oauth_callback"),
                HubOAuthCallbackHandler,
            ),
            (
                url_path_join(prefix, "health"),
                handlers_module.HealthHandler,
            ),
            (
                prefix.rstrip("/") + "/?",
                handlers_module.CodeFormHandler,
                {"hub_groups": hub_groups, "limiter": limiter},
            ),
        ],
        # Per-process secret for the OAuth/session/XSRF cookies.
        # Assumes a single replica (see the rate limiter): a second
        # replica cannot validate the first's cookies.
        # A restart only forces a transparent re-login; a multi-replica
        # deployment must pin to one replica or share a stable secret.
        cookie_secret=os.urandom(32),
        xsrf_cookies=True,
        template_path=os.path.join(os.path.dirname(__file__), "templates"),
    )


def main() -> None:
    """
    Runs the code-entry service.
    """

    logging.basicConfig(level=logging.INFO)

    # Validate once at startup so a broken config crash-loops at deploy
    # time, not on a user's submission; the handlers re-read per request.
    # `make_app` is inside the guard because the rate limiter parses its
    # env-var thresholds and can also raise `ConfigError`.
    try:
        config_module.load_config()
        app = make_app()
    except config_module.ConfigError as exn:
        log.error("%s", exn)
        sys.exit(1)
    url = urllib.parse.urlparse(config_module.settings().service_url)
    app.listen(url.port or 8080, address=url.hostname or "127.0.0.1")

    IOLoop.current().start()

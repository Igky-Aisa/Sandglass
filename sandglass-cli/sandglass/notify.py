"""Optional ntfy.sh push notifications for long-running `sandglass execute` waits.

Configured entirely via environment variables -- no CLI flags, no persisted
settings -- so a queue run started in an unattended/headless context (the
whole reason this tool exists) doesn't need any extra setup beyond what's
already in the shell environment. If unconfigured, every call here is a
silent no-op: a missing notification is never a reason to fail or slow down
a queue run.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# The ntfy.sh topic to publish to (required for anything to be sent) and,
# optionally, a different server (e.g. a self-hosted ntfy instance).
NTFY_TOPIC_ENV = "SANDGLASS_NTFY_TOPIC"
NTFY_SERVER_ENV = "SANDGLASS_NTFY_SERVER"
DEFAULT_NTFY_SERVER = "https://ntfy.sh"

_REQUEST_TIMEOUT_SECONDS = 10


def _ntfy_url() -> str | None:
    topic = os.environ.get(NTFY_TOPIC_ENV)
    if not topic:
        return None
    server = os.environ.get(NTFY_SERVER_ENV, DEFAULT_NTFY_SERVER).rstrip("/")
    return f"{server}/{topic}"


def is_configured() -> bool:
    """Whether a topic is set -- lets a caller print a one-time hint instead
    of silently doing nothing, without checking env vars itself."""
    return _ntfy_url() is not None


def send(message: str, title: str = "Sandglass", priority: str = "default") -> bool:
    """Best-effort push notification. Returns True if actually sent, False if
    skipped (no topic configured) or failed (network/server error).

    Never raises -- a notification is a nice-to-have, not something that
    should ever break or stall a queue run over a flaky network.
    """
    url = _ntfy_url()
    if url is None:
        return False
    try:
        request = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            response.read()
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning("ntfy notification failed: %s", exc)
        return False

"""ntfy push notifications.

A thin POST wrapper around an ntfy topic URL (https://ntfy.sh/<topic> or a
self-hosted instance). Shaped like ``UnifiClient``: it takes an injected
``httpx.AsyncClient`` and owns nothing but the request.

The topic name *is* the credential on ntfy.sh — anyone who guesses it can
read and publish. Use a long random topic, or a self-hosted instance with
``NTFY_TOKEN``.
"""
from __future__ import annotations

import logging

import httpx

_LOGGER = logging.getLogger(__name__)

# ntfy priorities: 1=min, 2=low, 3=default, 4=high, 5=urgent.
PRIORITY_LOW = "low"
PRIORITY_DEFAULT = "default"
PRIORITY_HIGH = "high"
PRIORITY_URGENT = "urgent"


def _header_safe(text: str) -> str:
    """HTTP headers are latin-1 at best; ntfy titles must survive that.

    Z-Wave device names are user-supplied and may contain anything, so
    drop what cannot be encoded rather than raising mid-alert.
    """
    return text.encode("ascii", "replace").decode("ascii")


class Notifier:
    """Best-effort push sender. Never raises into the caller."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        *,
        token: str | None = None,
    ) -> None:
        self._http = http
        self._url = url
        self._token = token

    async def send(
        self,
        *,
        title: str,
        message: str,
        priority: str = PRIORITY_DEFAULT,
        tags: str | None = None,
    ) -> None:
        headers = {"Title": _header_safe(title), "Priority": priority}
        if tags:
            headers["Tags"] = tags
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        try:
            r = await self._http.post(
                self._url,
                content=message.encode("utf-8"),
                headers=headers,
            )
            r.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            # A push failure must never take down the firewall logic, and a
            # retry loop here would just queue behind the next event.
            _LOGGER.warning("notification failed (%s): %s", title, exc)
        else:
            _LOGGER.info("notified: %s - %s", title, message)

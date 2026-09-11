"""The ntfy client must deliver correctly and fail quietly."""
from __future__ import annotations

import httpx
import pytest

from zwave_controller.notify import Notifier

URL = "https://ntfy.example/topic"


async def test_send_posts_body_and_headers():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await Notifier(http, URL, token="secret").send(
            title="Front Door opened",
            message="Door/window opened.",
            priority="high",
            tags="door",
        )

    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == URL
    assert request.content == b"Door/window opened."
    assert request.headers["Title"] == "Front Door opened"
    assert request.headers["Priority"] == "high"
    assert request.headers["Tags"] == "door"
    assert request.headers["Authorization"] == "Bearer secret"


async def test_send_omits_auth_without_token():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await Notifier(http, URL).send(title="t", message="m")


async def test_non_ascii_title_does_not_raise():
    """Header values can't carry arbitrary unicode; device names can."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await Notifier(http, URL).send(title="Café door 🚪", message="opened")

    assert len(seen) == 1
    assert seen[0].content == "opened".encode("utf-8")


@pytest.mark.parametrize(
    "handler",
    [
        lambda request: httpx.Response(500),
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("boom")),
    ],
)
async def test_failures_are_swallowed(handler):
    """An ntfy outage must never propagate into the firewall logic."""
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await Notifier(http, URL).send(title="t", message="m")

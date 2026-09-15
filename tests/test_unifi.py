"""UniFi client-list lookup, and the firewall toggle's idempotence."""
from __future__ import annotations

import httpx
import pytest

from zwave_controller.unifi import UnifiClient

BASE = "https://unifi.test"
STA_PATH = "/proxy/network/api/s/default/stat/sta"


def client_with(handler) -> tuple[httpx.AsyncClient, UnifiClient]:
    http = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler))
    return http, UnifiClient(http, "default", "policy-1")


async def test_connected_macs_hits_stat_sta_and_lowercases():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "meta": {"rc": "ok"},
                "data": [
                    {"mac": "3E:90:21:A9:71:A2", "name": "Seth's iPhone"},
                    {"mac": "aa:bb:cc:dd:ee:ff"},
                    {"name": "no mac here"},
                    "garbage",
                ],
            },
        )

    http, unifi = client_with(handler)
    async with http:
        macs = await unifi.connected_macs()

    assert seen[0].method == "GET"
    assert seen[0].url.path == STA_PATH
    assert macs == {"3e:90:21:a9:71:a2", "aa:bb:cc:dd:ee:ff"}


async def test_connected_macs_http_error_propagates():
    http, unifi = client_with(lambda request: httpx.Response(500, text="boom"))
    async with http:
        with pytest.raises(httpx.HTTPStatusError):
            await unifi.connected_macs()


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"meta": {"rc": "error"}, "data": []},
        {"meta": {"rc": "ok"}, "data": "nope"},
        {"data": []},
    ],
)
async def test_connected_macs_rejects_malformed_payload(payload):
    """A wrong-shaped answer is a failed poll, not an empty house."""
    http, unifi = client_with(lambda request: httpx.Response(200, json=payload))
    async with http:
        with pytest.raises(ValueError):
            await unifi.connected_macs()


async def test_set_policy_enabled_is_a_noop_when_already_set():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"_id": "policy-1", "enabled": True})

    http, unifi = client_with(handler)
    async with http:
        await unifi.set_policy_enabled(True)

    assert [r.method for r in seen] == ["GET"]


async def test_set_policy_enabled_puts_full_object_back():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(
                200, json={"_id": "policy-1", "enabled": True, "name": "Block X"}
            )
        return httpx.Response(200, json={})

    http, unifi = client_with(handler)
    async with http:
        await unifi.set_policy_enabled(False)

    assert [r.method for r in seen] == ["GET", "PUT"]
    body = seen[1].read()
    assert b'"enabled": false' in body or b'"enabled":false' in body
    assert b"Block X" in body

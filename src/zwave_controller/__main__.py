"""Service entry point."""
from __future__ import annotations

import asyncio
import logging
import signal

import aiohttp
import httpx
from zwave_js_server.client import Client

from .config import Config
from .keypad_feedback import KeypadLed
from .unifi import UnifiClient
from .zwave import KeypadHandler, connect_keypad

_LOGGER = logging.getLogger("zwave_controller")


async def _run() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _LOGGER.info(
        "starting: zwave=%s node=%d unifi=%s policy=%s",
        cfg.zwave_ws_url,
        cfg.keypad_node_id,
        cfg.unifi_host,
        cfg.unifi_policy_id,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    unifi_headers = {
        "X-API-KEY": cfg.unifi_api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with (
        aiohttp.ClientSession() as zw_session,
        httpx.AsyncClient(
            base_url=cfg.unifi_host,
            headers=unifi_headers,
            verify=cfg.unifi_verify_tls,
            timeout=10.0,
        ) as http,
    ):
        unifi = UnifiClient(http, cfg.unifi_site, cfg.unifi_policy_id)
        client = Client(cfg.zwave_ws_url, zw_session)
        await client.connect()

        node = await connect_keypad(client, cfg.keypad_node_id)
        led = KeypadLed(client, cfg.keypad_node_id)
        handler = KeypadHandler(node, unifi, led, cfg.disarm_pin)
        handler.attach()

        try:
            enabled = await unifi.get_policy_enabled()
            _LOGGER.info("initial policy state enabled=%s; syncing LED", enabled)
            await led.set_armed(enabled)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("initial LED sync failed")

        await stop.wait()
        _LOGGER.info("shutdown signal received")
        await client.disconnect()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

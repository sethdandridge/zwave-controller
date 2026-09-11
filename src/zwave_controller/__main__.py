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
from .notify import Notifier
from .sensors import SensorHandler
from .unifi import UnifiClient
from .zwave import KeypadHandler, connect_keypad

_LOGGER = logging.getLogger("zwave_controller")


async def wait_for_shutdown(stop: asyncio.Event, listen_task: asyncio.Task) -> None:
    """Block until SIGTERM, or until the zwave-js-server listener dies.

    ``Client.listen()`` swallows ``ConnectionClosed`` and simply returns, so
    watching only the signal would leave the service parked on a dead socket:
    alerts silently stopped, but the container still healthy as far as systemd
    is concerned, so ``Restart=on-failure`` would never fire. Losing the
    listener raises ``SystemExit(1)`` instead, which gets the unit restarted.
    """
    stop_task = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        {stop_task, listen_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if not stop_task.done():
        stop_task.cancel()

    if listen_task not in done:
        # Deliberately left running: Client.disconnect() closes the socket and
        # waits for the listener's own cleanup to acknowledge it. Cancelling
        # the listener here would race that handshake.
        _LOGGER.info("shutdown signal received")
        return

    exc = None if listen_task.cancelled() else listen_task.exception()
    if exc is not None:
        _LOGGER.error("zwave-js-server listener failed: %s", exc)
    else:
        _LOGGER.error("zwave-js-server closed the connection")
    raise SystemExit(1)


async def _run() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _LOGGER.info(
        "starting: zwave=%s node=%d unifi=%s policy=%s notifications=%s",
        cfg.zwave_ws_url,
        cfg.keypad_node_id,
        cfg.unifi_host,
        cfg.unifi_policy_id,
        "on" if cfg.notifications_enabled else "off",
    )
    if not cfg.notifications_enabled:
        _LOGGER.warning("NTFY_URL is unset; door/motion/health alerts are disabled")

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
        # Separate client: the UniFi one carries X-API-KEY and a base_url, so
        # reusing it would leak the key to ntfy.
        httpx.AsyncClient(timeout=10.0) as notify_http,
    ):
        unifi = UnifiClient(http, cfg.unifi_site, cfg.unifi_policy_id)
        client = Client(cfg.zwave_ws_url, zw_session)
        await client.connect()

        node, listen_task = await connect_keypad(client, cfg.keypad_node_id)
        led = KeypadLed(client, cfg.keypad_node_id)
        handler = KeypadHandler(node, unifi, led, cfg.disarm_pin)
        handler.attach()

        if cfg.ntfy_url is not None:
            assert client.driver is not None
            notifier = Notifier(notify_http, cfg.ntfy_url, token=cfg.ntfy_token)
            sensors = SensorHandler(
                client.driver,
                notifier,
                cooldown_seconds=cfg.notify_cooldown_seconds,
                door_cooldown_seconds=cfg.notify_door_cooldown_seconds,
                battery_threshold=cfg.notify_battery_threshold,
                motion_enabled=cfg.notify_motion,
            )
            sensors.attach()

        try:
            enabled = await unifi.get_policy_enabled()
            _LOGGER.info("initial policy state enabled=%s; syncing LED", enabled)
            await led.set_armed(enabled)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("initial LED sync failed")

        try:
            await wait_for_shutdown(stop, listen_task)
        finally:
            await client.disconnect()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

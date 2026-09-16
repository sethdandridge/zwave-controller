"""Service entry point."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import aiohttp
import httpx
from zwave_js_server.client import Client

from .config import Config
from .keypad_feedback import KeypadLed
from .notify import PRIORITY_LOW, Notifier
from .presence import PresenceMonitor
from .sensors import SensorHandler
from .unifi import UnifiClient
from .zwave import KeypadHandler, connect_keypad

_LOGGER = logging.getLogger("zwave_controller")


async def wait_for_shutdown(
    stop: asyncio.Event, listen_task: asyncio.Task, *extra_tasks: asyncio.Task
) -> None:
    """Block until SIGTERM, or until a watched background task dies.

    ``Client.listen()`` swallows ``ConnectionClosed`` and simply returns, so
    watching only the signal would leave the service parked on a dead socket:
    alerts silently stopped, but the container still healthy as far as systemd
    is concerned, so ``Restart=on-failure`` would never fire. Losing the
    listener raises ``SystemExit(1)`` instead, which gets the unit restarted.

    ``extra_tasks`` get the same treatment: a dead presence poller frozen at
    "home" would mute door alerts forever while looking perfectly healthy.
    They are not cancelled here; the caller owns their shutdown.
    """
    stop_task = asyncio.create_task(stop.wait())
    watched = {listen_task, *extra_tasks}
    done, _ = await asyncio.wait(
        {stop_task, *watched}, return_when=asyncio.FIRST_COMPLETED
    )
    if not stop_task.done():
        stop_task.cancel()

    if not done & watched:
        # Deliberately left running: Client.disconnect() closes the socket and
        # waits for the listener's own cleanup to acknowledge it. Cancelling
        # the listener here would race that handshake.
        _LOGGER.info("shutdown signal received")
        return

    for task in done & watched:
        exc = None if task.cancelled() else task.exception()
        if task is listen_task:
            if exc is not None:
                _LOGGER.error("zwave-js-server listener failed: %s", exc)
            else:
                _LOGGER.error("zwave-js-server closed the connection")
        else:
            _LOGGER.error(
                "%s task exited unexpectedly: %s", task.get_name(), exc or "returned"
            )
    raise SystemExit(1)


async def _run() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs every request at INFO; the presence poller would turn that
    # into two journal lines a minute. Our own modules log what matters.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    _LOGGER.info(
        "starting: zwave=%s node=%d unifi=%s policy=%s notifications=%s presence=%s",
        cfg.zwave_ws_url,
        cfg.keypad_node_id,
        cfg.unifi_host,
        cfg.unifi_policy_id,
        "on" if cfg.notifications_enabled else "off",
        (
            f"on ({len(cfg.presence_macs)} MACs, poll={cfg.presence_poll_seconds}s, "
            f"grace={cfg.presence_away_grace_seconds}s)"
        )
        if cfg.presence_enabled
        else "off",
    )
    if not cfg.notifications_enabled:
        _LOGGER.warning("NTFY_URL is unset; door/motion/health alerts are disabled")
        if cfg.presence_enabled:
            _LOGGER.warning("PRESENCE_MACS is set but NTFY_URL is unset; nothing to mute")

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

        presence_task: asyncio.Task | None = None
        if cfg.ntfy_url is not None:
            assert client.driver is not None
            notifier = Notifier(notify_http, cfg.ntfy_url, token=cfg.ntfy_token)

            presence: PresenceMonitor | None = None
            if cfg.presence_enabled:

                async def welcome_home(seen) -> None:
                    await notifier.send(
                        title="Welcome home",
                        message=f"{', '.join(seen)} is back on the WiFi. "
                        "Door and motion alerts are muted.",
                        priority=PRIORITY_LOW,
                        tags="house",
                    )

                presence = PresenceMonitor(
                    unifi.connected_macs,
                    cfg.presence_macs,
                    away_grace_seconds=cfg.presence_away_grace_seconds,
                    poll_seconds=cfg.presence_poll_seconds,
                    on_return=welcome_home,
                )
                # Prime the state before any sensor can fire, so a door
                # opened right after startup is judged on real data.
                await presence.poll_once()
                presence_task = asyncio.create_task(presence.run(), name="presence")

            sensors = SensorHandler(
                client.driver,
                notifier,
                cooldown_seconds=cfg.notify_cooldown_seconds,
                door_cooldown_seconds=cfg.notify_door_cooldown_seconds,
                battery_threshold=cfg.notify_battery_threshold,
                motion_enabled=cfg.notify_motion,
                muted=presence.is_home if presence is not None else None,
            )
            sensors.attach()

        try:
            enabled = await unifi.get_policy_enabled()
            _LOGGER.info("initial policy state enabled=%s; syncing LED", enabled)
            await led.set_armed(enabled)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("initial LED sync failed")

        watched = [presence_task] if presence_task is not None else []
        try:
            await wait_for_shutdown(stop, listen_task, *watched)
        finally:
            # The poller must be finished before the httpx client's context
            # exits, so cancel and reap it before anything else.
            if presence_task is not None and not presence_task.done():
                presence_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await presence_task
            await client.disconnect()


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

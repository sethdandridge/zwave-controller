"""Z-Wave keypad listener and action dispatcher."""
from __future__ import annotations

import asyncio
import hmac
import logging

from zwave_js_server.client import Client
from zwave_js_server.const import CommandClass
from zwave_js_server.model.node import Node
from zwave_js_server.model.notification import EntryControlNotification

from .keypad_feedback import KeypadLed
from .unifi import UnifiClient

_LOGGER = logging.getLogger(__name__)

# Entry Control CC event types (zwave-js EntryControlEventTypes enum).
_EVT_DISARM_ALL = 3
_EVT_ARM_AWAY = 5
_EVT_ARM_HOME = 6


class KeypadHandler:
    def __init__(
        self,
        node: Node,
        unifi: UnifiClient,
        led: KeypadLed,
        disarm_pin: str,
    ) -> None:
        self._node = node
        self._unifi = unifi
        self._led = led
        self._disarm_pin = disarm_pin
        self._lock = asyncio.Lock()
        self._loop = asyncio.get_running_loop()

    def attach(self) -> None:
        self._node.on("notification", self._on_notification)
        _LOGGER.info("subscribed to notifications on node %d", self._node.node_id)

    def _on_notification(self, event: dict) -> None:
        notification = event.get("notification")
        if not isinstance(notification, EntryControlNotification):
            return
        if notification.command_class != CommandClass.ENTRY_CONTROL:
            return
        self._loop.create_task(self._dispatch(notification))

    async def _dispatch(self, n: EntryControlNotification) -> None:
        async with self._lock:
            if n.event_type == _EVT_DISARM_ALL:
                await self._handle(
                    label="disarm",
                    pin_required=True,
                    event_data=n.event_data,
                    enabled=False,
                )
            elif n.event_type == _EVT_ARM_AWAY:
                await self._handle(
                    label="arm_away",
                    pin_required=False,
                    event_data=None,
                    enabled=True,
                )
            elif n.event_type == _EVT_ARM_HOME:
                await self._handle(
                    label="arm_home",
                    pin_required=False,
                    event_data=None,
                    enabled=True,
                )
            else:
                _LOGGER.debug(
                    "ignoring entry-control event_type=%d (%s)",
                    n.event_type,
                    n.event_type_label,
                )

    async def _handle(
        self,
        *,
        label: str,
        pin_required: bool,
        event_data: object,
        enabled: bool,
    ) -> None:
        if pin_required:
            pin = _normalize_pin(event_data)
            if pin is None:
                _LOGGER.warning("%s rejected: no PIN provided", label)
                return
            if not hmac.compare_digest(pin, self._disarm_pin):
                _LOGGER.warning("%s rejected: bad PIN", label)
                return

        _LOGGER.info("%s accepted; setting policy enabled=%s", label, enabled)

        # Fire LED write first while the keypad is provably awake, then the
        # UniFi toggle. LED is best-effort; a failure there must not block
        # the firewall action.
        await self._led.set_armed(enabled)
        try:
            await self._unifi.set_policy_enabled(enabled)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("UniFi policy toggle failed for %s", label)


def _normalize_pin(event_data: object) -> str | None:
    """Return the ASCII PIN from Entry Control eventData, tolerating junk.

    Ring 1st-gen firmware sometimes emits trailing NUL/whitespace; zwave-js
    has ``disableStrictEntryControlDataValidation`` set for this device, so
    we must be equally lenient.
    """
    if isinstance(event_data, str):
        cleaned = event_data.strip().strip("\x00")
        return cleaned or None
    return None


async def connect_keypad(
    client: Client, node_id: int
) -> tuple[Node, asyncio.Task[None]]:
    """Start listening, wait for the initial state dump, return node + task.

    The listen task is handed back rather than fired and forgotten:
    ``Client.listen()`` swallows ``ConnectionClosed`` and simply returns, so
    the caller must watch it to notice that zwave-js-server went away.
    Keeping a reference also stops the task being garbage-collected.
    """
    driver_ready = asyncio.Event()
    listen_task = asyncio.create_task(client.listen(driver_ready))

    # Race the handshake against the listener: if the socket drops before the
    # state dump arrives, waiting on the event alone would hang forever.
    ready_task = asyncio.create_task(driver_ready.wait())
    done, _ = await asyncio.wait(
        {ready_task, listen_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if ready_task not in done:
        ready_task.cancel()
        await listen_task  # re-raises the real connection error, if any
        raise RuntimeError("zwave-js-server closed the connection during startup")

    assert client.driver is not None
    try:
        node = client.driver.controller.nodes[node_id]
    except KeyError as exc:
        raise RuntimeError(f"node {node_id} not present on controller") from exc

    label = node.name or node.device_config.description or "unknown"
    _LOGGER.info("keypad node %d: %s (status=%s)", node.node_id, label, node.status)
    return node, listen_task

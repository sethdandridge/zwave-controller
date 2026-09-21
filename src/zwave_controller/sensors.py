"""Door / motion / leak / health alerting for every node on the controller.

Subscribes to *all* nodes rather than a configured allowlist: new sensors
paired in zwave-js start alerting after a restart with no config change, and
events that don't match a rule below (the keypad's Entry Control traffic,
for instance) simply fall through.

Everything is suppressed by a per-(node, category) cooldown, because a
motion sensor re-fires every few seconds and an unfiltered feed makes the
phone unusable. Low battery adds edge detection on top: it is re-reported
on every wake-up, so the cooldown alone would need to be impractically long,
and it is capped at one push per node per day either way.

Door and motion are additionally muted while ``muted()`` says someone is
home (see presence.py). Leak and health alerts -- water, tamper, battery,
offline/online -- are never muted: being home is no reason not to hear about
a burst pipe or a dead sensor.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.notification import (
    AccessControlNotificationEvent,
    HomeSecurityNotificationEvent,
    NotificationType,
    WaterAlarmNotificationEvent,
)
from zwave_js_server.model.driver import Driver
from zwave_js_server.model.node import Node
from zwave_js_server.model.notification import NotificationNotification
from zwave_js_server.model.value import Value

from .notify import (
    PRIORITY_DEFAULT,
    PRIORITY_HIGH,
    PRIORITY_LOW,
    PRIORITY_URGENT,
    Notifier,
)

_LOGGER = logging.getLogger(__name__)

_DOOR_OPEN = AccessControlNotificationEvent.DOOR_STATE_WINDOW_DOOR_IS_OPEN  # 22
_DOOR_CLOSED = AccessControlNotificationEvent.DOOR_STATE_WINDOW_DOOR_IS_CLOSED  # 23

_MOTION_EVENTS = frozenset(
    {
        HomeSecurityNotificationEvent.MOTION_SENSOR_STATUS_MOTION_DETECTION,  # 8
        HomeSecurityNotificationEvent.MOTION_SENSOR_STATUS_MOTION_DETECTION_LOCATION_PROVIDED,  # 7
    }
)
# Contact sensors signal the reed switch through Home Security "Sensor
# status" (intrusion) rather than Access Control door state -- the Ring
# contact sensor on this network reports only notification type 7, with no
# Access Control variable at all. Both spellings mean "the thing this sensor
# guards is now open", so both map to the door alert.
_INTRUSION_EVENTS = frozenset(
    {
        HomeSecurityNotificationEvent.SENSOR_STATUS_INTRUSION_LOCATION_PROVIDED,  # 1
        HomeSecurityNotificationEvent.SENSOR_STATUS_INTRUSION,  # 2
    }
)
_TAMPER_EVENTS = frozenset(
    {
        HomeSecurityNotificationEvent.COVER_STATUS_TAMPERING_PRODUCT_COVER_REMOVED,  # 3
        HomeSecurityNotificationEvent.TAMPERING_PRODUCT_MOVED,  # 9
    }
)
# Leak detectors report through Water Alarm "Sensor status". The "dropped"
# events are the sensor drying out again -- the counterpart of door-closed.
_LEAK_EVENTS = frozenset(
    {
        WaterAlarmNotificationEvent.SENSOR_STATUS_WATER_LEAK_DETECTED_LOCATION_PROVIDED,  # 1
        WaterAlarmNotificationEvent.SENSOR_STATUS_WATER_LEAK_DETECTED,  # 2
    }
)
_LEAK_CLEARED_EVENTS = frozenset(
    {
        WaterAlarmNotificationEvent.IDLE,  # 0
        WaterAlarmNotificationEvent.WATER_LEVEL_DROPPED_LOCATION_PROVIDED,  # 3
        WaterAlarmNotificationEvent.WATER_LEVEL_DROPPED,  # 4
    }
)

# Categories are the cooldown keys, so a door open never suppresses a
# tamper alert on the same node.
_CAT_DOOR = "door"
_CAT_MOTION = "motion"
_CAT_TAMPER = "tamper"
_CAT_LEAK = "leak"
_CAT_BATTERY = "battery"
_CAT_OFFLINE = "offline"
_CAT_ONLINE = "online"

# The only categories presence is allowed to silence. A leak is deliberately
# absent: it is exactly the alert you want while sitting on the couch.
_MUTED_WHEN_HOME = frozenset({_CAT_DOOR, _CAT_MOTION})

# A low battery is an edge, but a device whose ``level`` hovers around the
# threshold while ``isLow`` stays set would re-edge on every wake-up. Cap it
# at one push per node per day regardless.
_BATTERY_REALERT_SECONDS = 24 * 60 * 60


def node_label(node: Node) -> str:
    """Human-readable device name, matching the pattern in zwave.py."""
    return node.name or node.device_config.description or f"node {node.node_id}"


class SensorHandler:
    def __init__(
        self,
        driver: Driver,
        notifier: Notifier,
        *,
        cooldown_seconds: int = 300,
        door_cooldown_seconds: int = 15,
        battery_threshold: int = 20,
        motion_enabled: bool = True,
        muted: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._driver = driver
        self._notifier = notifier
        self._cooldown = cooldown_seconds
        self._door_cooldown = door_cooldown_seconds
        self._battery_threshold = battery_threshold
        self._motion_enabled = motion_enabled
        # ``None`` means no presence source is configured; a callable
        # returning True mutes door/motion for that event.
        self._muted = muted
        self._clock = clock
        self._loop = asyncio.get_running_loop()
        self._last_alert: dict[tuple[int, str], float] = {}
        self._battery_low: dict[tuple[int, str], bool] = {}
        # asyncio only holds weak references to tasks; without this a push
        # can be garbage-collected mid-flight.
        self._tasks: set[asyncio.Task] = set()

    # -- wiring ---------------------------------------------------------

    def attach(self) -> None:
        nodes = self._driver.controller.nodes.values()
        for node in nodes:
            node.on("notification", self._on_notification)
            node.on("value updated", self._on_value_updated)
            node.on("dead", self._on_dead)
            node.on("alive", self._on_alive)
            _LOGGER.info(
                "watching node %d: %s (status=%s)",
                node.node_id,
                node_label(node),
                node.status,
            )
        _LOGGER.info(
            "sensor alerts active on %d node(s): door/tamper cooldown=%ds "
            "motion cooldown=%ds motion=%s battery<%d%% presence mute=%s",
            len(nodes),
            self._door_cooldown,
            self._cooldown,
            self._motion_enabled,
            self._battery_threshold,
            "on" if self._muted is not None else "off",
        )

    # -- event callbacks (synchronous; the emitter is not async) --------

    def _on_notification(self, event: dict) -> None:
        notification = event.get("notification")
        if not isinstance(notification, NotificationNotification):
            return
        matched = self._match_notification(notification)
        if matched is not None:
            category, alert = matched
            self._fire(notification.node_id, category, alert)

    def _on_value_updated(self, event: dict) -> None:
        value = event.get("value")
        node = event.get("node")
        if value is None or node is None:
            return

        if value.command_class == CommandClass.BATTERY:
            alert = self._match_battery(node, value.property_, value.value)
            if alert is not None:
                self._fire(node.node_id, _CAT_BATTERY, alert)
            return

        if value.command_class == CommandClass.NOTIFICATION:
            self._on_notification_value(node, value)
            return

    def _on_notification_value(self, node: Node, value: Value) -> None:
        """Handle door/motion reported as a Notification CC *value*.

        node-zwave-js routes "state"-style notifications (door open/closed,
        motion detected/idle) into a value rather than the ``notification``
        event, so a device that only updates its state variable would
        otherwise never alert. Both paths are wired; when a device happens to
        fire both, the per-(node, category) cooldown collapses them into one
        push.
        """
        notification_type = value.metadata.cc_specific.get("notificationType")
        if not isinstance(notification_type, int) or not isinstance(value.value, int):
            return
        matched = self._classify(
            node, notification_type, value.value, detail=value.property_key_name
        )
        if matched is not None:
            category, alert = matched
            self._fire(node.node_id, category, alert)

    def _on_dead(self, event: dict) -> None:
        node = event.get("node")
        if node is None:
            return
        self._fire(
            node.node_id,
            _CAT_OFFLINE,
            {
                "title": f"{node_label(node)} offline",
                "message": "Sensor stopped responding to the Z-Wave controller.",
                "priority": PRIORITY_HIGH,
                "tags": "warning",
            },
        )

    def _on_alive(self, event: dict) -> None:
        node = event.get("node")
        if node is None:
            return
        self._fire(
            node.node_id,
            _CAT_ONLINE,
            {
                "title": f"{node_label(node)} back online",
                "message": "Sensor is responding again.",
                "priority": PRIORITY_LOW,
                "tags": "white_check_mark",
            },
        )

    # -- matching -------------------------------------------------------

    def _match_notification(
        self, n: NotificationNotification
    ) -> tuple[str, dict[str, str]] | None:
        return self._classify(n.node, n.type_, n.event, detail=n.event_label)

    def _classify(
        self,
        node: Node,
        type_: int,
        event: int,
        *,
        detail: str | None = None,
    ) -> tuple[str, dict[str, str]] | None:
        """Map a (notification type, event) pair to (cooldown category, alert)."""
        name = node_label(node)

        if type_ == NotificationType.ACCESS_CONTROL:
            if event == _DOOR_OPEN:
                return _CAT_DOOR, {
                    "title": f"{name} opened",
                    "message": "Door/window opened.",
                    "priority": PRIORITY_HIGH,
                    "tags": "door",
                }
            if event == _DOOR_CLOSED:
                _LOGGER.info("event: %s closed [not notifying]", name)
                return None

        elif type_ == NotificationType.HOME_SECURITY:
            if event in _INTRUSION_EVENTS:
                return _CAT_DOOR, {
                    "title": f"{name} opened",
                    "message": "Door/window opened.",
                    "priority": PRIORITY_HIGH,
                    "tags": "door",
                }
            if event == HomeSecurityNotificationEvent.IDLE:
                _LOGGER.info("event: %s back to idle [not notifying]", name)
                return None
            if event in _MOTION_EVENTS:
                if not self._motion_enabled:
                    _LOGGER.info("event: motion on %s [NOTIFY_MOTION off]", name)
                    return None
                return _CAT_MOTION, {
                    "title": f"Motion: {name}",
                    "message": "Motion detected.",
                    "priority": PRIORITY_DEFAULT,
                    "tags": "runner",
                }
            if event in _TAMPER_EVENTS:
                return _CAT_TAMPER, {
                    "title": f"Tamper: {name}",
                    "message": detail or "Tamper or intrusion reported.",
                    "priority": PRIORITY_URGENT,
                    "tags": "rotating_light",
                }

        elif type_ == NotificationType.WATER_ALARM:
            if event in _LEAK_EVENTS:
                return _CAT_LEAK, {
                    "title": f"Leak: {name}",
                    "message": "Water detected.",
                    "priority": PRIORITY_URGENT,
                    "tags": "droplet",
                }
            if event in _LEAK_CLEARED_EVENTS:
                _LOGGER.info("event: %s leak cleared [not notifying]", name)
                return None

        _LOGGER.debug(
            "ignoring notification from %s: type=%s event=%s (%s)",
            name,
            type_,
            event,
            detail,
        )
        return None

    def _match_battery(
        self, node: Node, property_: object, value: object
    ) -> dict[str, str] | None:
        """Edge-detect a low battery from either ``isLow`` or ``level``."""
        if property_ == "isLow":
            low = bool(value)
        elif property_ == "level":
            if not isinstance(value, (int, float)):
                return None
            low = value <= self._battery_threshold
        else:
            return None

        # Only a not-low -> low edge alerts, tracked per property so a
        # device reporting both ``level`` and ``isLow`` doesn't have one
        # signal clear the other's flag. The first reading after a restart
        # counts as an edge, so a restart can repeat one alert; that beats
        # missing one.
        key = (node.node_id, str(property_))
        was_low = self._battery_low.get(key)
        self._battery_low[key] = low
        if not low or was_low:
            return None

        name = node_label(node)
        detail = f" ({value}%)" if property_ == "level" else ""
        return {
            "title": f"Low battery: {name}",
            "message": f"Replace the battery{detail}.",
            "priority": PRIORITY_DEFAULT,
            "tags": "battery",
        }

    # -- delivery -------------------------------------------------------

    def _cooldown_for(self, category: str) -> int:
        """How long to stay quiet after alerting, by kind of event.

        A door is not a chatty device -- it only fires on the open
        transition -- and it is the one event where a miss matters most:
        with the motion window applied, a second entry minutes after a
        legitimate one would go unreported. Motion keeps the long window
        because that is the sensor that actually floods. A leak gets the
        short window too: it is rare, and if the sensor re-reports while
        still wet, being nagged is the point.
        """
        if category in (_CAT_DOOR, _CAT_TAMPER, _CAT_LEAK):
            return self._door_cooldown
        if category == _CAT_BATTERY:
            return _BATTERY_REALERT_SECONDS
        return self._cooldown

    def _fire(self, node_id: int, category: str, alert: dict[str, str]) -> None:
        """Send an alert unless muted by presence or still cooling down.

        Every event reaches the log at INFO whether or not it is pushed:
        the suppressed paths log here with the reason, and ``Notifier``
        logs the ones that go out. The presence check comes first and does
        not touch the cooldown: a door opened while home must not start a
        window that hides a real open seconds after the phone leaves.
        """
        if self._muted is not None and category in _MUTED_WHEN_HOME and self._muted():
            _LOGGER.info(
                "event: %s [muted, someone is home] (node %d, %s)",
                alert["title"],
                node_id,
                category,
            )
            return

        cooldown = self._cooldown_for(category)
        key = (node_id, category)
        now = self._clock()
        last = self._last_alert.get(key)
        if last is not None and now - last < cooldown:
            _LOGGER.info(
                "event: %s [suppressed, %.0fs into %ds %s cooldown] (node %d)",
                alert["title"],
                now - last,
                cooldown,
                category,
                node_id,
            )
            return
        self._last_alert[key] = now
        self._send(**alert)

    def _send(self, **alert: str) -> None:
        task = self._loop.create_task(self._notifier.send(**alert))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

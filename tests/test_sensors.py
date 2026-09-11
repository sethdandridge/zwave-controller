"""Event -> alert mapping, cooldown, and battery edge detection."""
from __future__ import annotations

import asyncio

import pytest

from zwave_controller.sensors import SensorHandler, node_label

from .conftest import FakeClock, FakeNotifier, notification

ACCESS_CONTROL = 6
HOME_SECURITY = 7
ENTRY_CONTROL_CC = 111

DOOR_OPEN = 22
DOOR_CLOSED = 23
MOTION = 8
INTRUSION = 2
COVER_REMOVED = 3
IDLE = 0

BATTERY_CC = 128


async def build(driver, notifier, **kwargs) -> tuple[SensorHandler, FakeClock]:
    clock = FakeClock()
    handler = SensorHandler(
        driver,
        notifier,
        cooldown_seconds=kwargs.pop("cooldown_seconds", 300),
        door_cooldown_seconds=kwargs.pop("door_cooldown_seconds", 15),
        battery_threshold=kwargs.pop("battery_threshold", 20),
        motion_enabled=kwargs.pop("motion_enabled", True),
        clock=clock,
    )
    handler.attach()
    return handler, clock


async def drain() -> None:
    """Let the tasks scheduled by the sync callbacks run."""
    await asyncio.sleep(0)


async def test_door_open_notifies(driver, door):
    notifier = FakeNotifier()
    await build(driver, notifier)

    door.emit("notification", {"notification": notification(door, type_=ACCESS_CONTROL, event=DOOR_OPEN)})
    await drain()

    assert len(notifier.sent) == 1
    alert = notifier.sent[0]
    assert alert["title"] == "Front Door opened"
    assert alert["priority"] == "high"


async def test_door_close_is_silent(driver, door):
    notifier = FakeNotifier()
    await build(driver, notifier)

    door.emit("notification", {"notification": notification(door, type_=ACCESS_CONTROL, event=DOOR_CLOSED)})
    await drain()

    assert notifier.sent == []


async def test_motion_cooldown(driver, motion):
    notifier = FakeNotifier()
    _, clock = await build(driver, notifier, cooldown_seconds=300)

    def trip():
        motion.emit("notification", {"notification": notification(motion, type_=HOME_SECURITY, event=MOTION)})

    trip()
    await drain()
    assert len(notifier.sent) == 1

    # Repeats inside the window are dropped.
    clock.advance(30)
    trip()
    clock.advance(200)
    trip()
    await drain()
    assert len(notifier.sent) == 1

    # ...and it re-arms once the window has passed.
    clock.advance(101)
    trip()
    await drain()
    assert len(notifier.sent) == 2


async def test_motion_can_be_disabled(driver, motion):
    notifier = FakeNotifier()
    await build(driver, notifier, motion_enabled=False)

    motion.emit("notification", {"notification": notification(motion, type_=HOME_SECURITY, event=MOTION)})
    await drain()

    assert notifier.sent == []


async def test_door_falls_back_to_device_description(driver, motion):
    notifier = FakeNotifier()
    await build(driver, notifier)

    motion.emit("notification", {"notification": notification(motion, type_=HOME_SECURITY, event=MOTION)})
    await drain()

    assert notifier.sent[0]["title"] == "Motion: Motion Sensor v2"


async def test_tamper_has_its_own_cooldown_bucket(driver, motion):
    """A motion alert must not swallow a tamper alert on the same node."""
    notifier = FakeNotifier()
    await build(driver, notifier)

    motion.emit("notification", {"notification": notification(motion, type_=HOME_SECURITY, event=MOTION)})
    motion.emit("notification", {"notification": notification(motion, type_=HOME_SECURITY, event=COVER_REMOVED)})
    await drain()

    assert len(notifier.sent) == 2
    assert notifier.sent[1]["priority"] == "urgent"
    assert notifier.sent[1]["title"].startswith("Tamper:")


async def test_home_security_intrusion_is_a_door_open(driver, door):
    """Contact sensors report the reed switch as Home Security intrusion.

    The Ring contact sensor exposes no Access Control variable at all, so
    this -- not event 22 -- is what actually fires when the door opens.
    """
    notifier = FakeNotifier()
    await build(driver, notifier)

    door.emit("notification", {"notification": notification(door, type_=HOME_SECURITY, event=INTRUSION)})
    await drain()

    assert notifier.titles == ["Front Door opened"]
    assert notifier.sent[0]["priority"] == "high"
    assert notifier.sent[0]["tags"] == "door"


async def test_home_security_idle_is_silent(driver, door):
    """Closing the door resets the variable to idle; no push for that."""
    notifier = FakeNotifier()
    await build(driver, notifier)

    door.emit("notification", {"notification": notification(door, type_=HOME_SECURITY, event=IDLE)})
    await drain()

    assert notifier.sent == []


async def test_contact_sensor_open_close_via_value(driver, door):
    """The real path for this hardware: a Notification CC value, type 7."""
    from .conftest import FakeValue

    notifier = FakeNotifier()
    await build(driver, notifier)

    def report(value: int):
        door.emit(
            "value updated",
            {
                "value": FakeValue(
                    113,
                    "Home Security",
                    value,
                    notification_type=7,
                    property_key_name="Sensor status",
                )
            },
        )

    report(INTRUSION)  # door opened
    report(IDLE)       # door closed
    await drain()

    assert notifier.titles == ["Front Door opened"]


async def test_unrelated_notification_ignored(driver, door):
    notifier = FakeNotifier()
    await build(driver, notifier)

    # Home Security "idle" (0) and an unmapped Access Control event.
    door.emit("notification", {"notification": notification(door, type_=HOME_SECURITY, event=0)})
    door.emit("notification", {"notification": notification(door, type_=ACCESS_CONTROL, event=1)})
    # Entry Control notifications arrive as a different class entirely.
    door.emit("notification", {"notification": object()})
    await drain()

    assert notifier.sent == []


async def test_door_window_is_short_enough_to_catch_a_follow_in(driver, door):
    """The whole point: a second entry after a legitimate one must alert.

    Doors only fire on the open transition, so they cannot flood the way
    motion does -- giving them the motion window would mean an intruder
    walking in minutes after the homeowner goes unreported.
    """
    notifier = FakeNotifier()
    _, clock = await build(driver, notifier, cooldown_seconds=300, door_cooldown_seconds=15)

    def open_door():
        door.emit(
            "notification",
            {"notification": notification(door, type_=HOME_SECURITY, event=INTRUSION)},
        )

    open_door()
    clock.advance(20)
    open_door()
    await drain()

    assert len(notifier.sent) == 2


async def test_door_still_debounces_a_chattering_reed_switch(driver, door):
    notifier = FakeNotifier()
    _, clock = await build(driver, notifier, door_cooldown_seconds=15)

    for _ in range(4):
        door.emit(
            "notification",
            {"notification": notification(door, type_=HOME_SECURITY, event=INTRUSION)},
        )
        clock.advance(2)
    await drain()

    assert len(notifier.sent) == 1


async def test_motion_keeps_the_long_window(driver, door, motion):
    """Tightening doors must not make the chatty sensor chatty again."""
    notifier = FakeNotifier()
    _, clock = await build(driver, notifier, cooldown_seconds=300, door_cooldown_seconds=15)

    motion.emit("notification", {"notification": notification(motion, type_=HOME_SECURITY, event=MOTION)})
    clock.advance(20)
    motion.emit("notification", {"notification": notification(motion, type_=HOME_SECURITY, event=MOTION)})
    await drain()

    assert len(notifier.sent) == 1


async def test_cooldown_is_per_node(driver, door, motion):
    notifier = FakeNotifier()
    await build(driver, notifier)

    door.emit("notification", {"notification": notification(door, type_=ACCESS_CONTROL, event=DOOR_OPEN)})
    motion.emit("notification", {"notification": notification(motion, type_=HOME_SECURITY, event=MOTION)})
    await drain()

    assert len(notifier.sent) == 2


# -- battery ----------------------------------------------------------------


async def test_battery_is_low_alerts_once_per_edge(driver, door):
    from .conftest import FakeValue

    notifier = FakeNotifier()
    _, clock = await build(driver, notifier)

    def report(is_low: bool):
        door.emit("value updated", {"value": FakeValue(BATTERY_CC, "isLow", is_low)})

    report(True)
    report(True)  # re-reported on every wake-up; must not re-alert
    await drain()
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["title"] == "Low battery: Front Door"

    # Battery replaced and low again the same day: still capped at one push.
    report(False)
    report(True)
    await drain()
    assert len(notifier.sent) == 1

    # A day later, a fresh low-battery edge is worth hearing about again.
    clock.advance(24 * 60 * 60 + 1)
    report(False)
    report(True)
    await drain()
    assert len(notifier.sent) == 2


async def test_battery_level_does_not_clear_is_low_flag(driver, door):
    """A device reporting both signals must not re-edge on every wake-up."""
    from .conftest import FakeValue

    notifier = FakeNotifier()
    _, clock = await build(driver, notifier, battery_threshold=20)

    # isLow trips at a higher level than our threshold, so `level` keeps
    # reporting "not low" alongside it.
    for _ in range(3):
        door.emit("value updated", {"value": FakeValue(BATTERY_CC, "isLow", True)})
        door.emit("value updated", {"value": FakeValue(BATTERY_CC, "level", 25)})
        clock.advance(3600)
    await drain()

    assert len(notifier.sent) == 1


async def test_battery_level_threshold(driver, door):
    from .conftest import FakeValue

    notifier = FakeNotifier()
    await build(driver, notifier, battery_threshold=20)

    door.emit("value updated", {"value": FakeValue(BATTERY_CC, "level", 55)})
    await drain()
    assert notifier.sent == []

    door.emit("value updated", {"value": FakeValue(BATTERY_CC, "level", 15)})
    await drain()
    assert len(notifier.sent) == 1
    assert "15%" in notifier.sent[0]["message"]


async def test_unrelated_command_class_values_ignored(driver, door):
    from .conftest import FakeValue

    notifier = FakeNotifier()
    await build(driver, notifier)

    # Multilevel Sensor, nothing to do with doors or batteries.
    door.emit("value updated", {"value": FakeValue(49, "Air temperature", 21)})
    await drain()

    assert notifier.sent == []


# -- notification-as-value path ---------------------------------------------
#
# node-zwave-js routes "state" notifications (door open/closed, motion) into
# a Notification CC value rather than the `notification` event, so this path
# is what actually fires for most contact and motion sensors.


async def test_door_open_via_notification_value(driver, door):
    from .conftest import FakeValue

    notifier = FakeNotifier()
    await build(driver, notifier)

    door.emit(
        "value updated",
        {"value": FakeValue(113, "Access Control", DOOR_OPEN, notification_type=6)},
    )
    await drain()

    assert notifier.titles == ["Front Door opened"]


async def test_door_idle_via_notification_value_is_silent(driver, door):
    from .conftest import FakeValue

    notifier = FakeNotifier()
    await build(driver, notifier)

    for event in (DOOR_CLOSED, 0):
        door.emit(
            "value updated",
            {"value": FakeValue(113, "Access Control", event, notification_type=6)},
        )
    await drain()

    assert notifier.sent == []


async def test_motion_via_notification_value(driver, motion):
    from .conftest import FakeValue

    notifier = FakeNotifier()
    await build(driver, notifier)

    motion.emit(
        "value updated",
        {"value": FakeValue(113, "Home Security", MOTION, notification_type=7)},
    )
    await drain()

    assert notifier.titles == ["Motion: Motion Sensor v2"]


async def test_both_paths_collapse_into_one_alert(driver, door):
    """A device firing the event *and* the value must not double-notify."""
    from .conftest import FakeValue

    notifier = FakeNotifier()
    await build(driver, notifier)

    door.emit("notification", {"notification": notification(door, type_=ACCESS_CONTROL, event=DOOR_OPEN)})
    door.emit(
        "value updated",
        {"value": FakeValue(113, "Access Control", DOOR_OPEN, notification_type=6)},
    )
    await drain()

    assert len(notifier.sent) == 1


async def test_notification_value_without_metadata_ignored(driver, door):
    from .conftest import FakeValue

    notifier = FakeNotifier()
    await build(driver, notifier)

    # No ccSpecific.notificationType -> we can't tell what it means.
    door.emit("value updated", {"value": FakeValue(113, "Access Control", DOOR_OPEN)})
    await drain()

    assert notifier.sent == []


# -- health -----------------------------------------------------------------


async def test_dead_and_alive(driver, door):
    notifier = FakeNotifier()
    await build(driver, notifier)

    door.emit("dead", {})
    door.emit("alive", {})
    await drain()

    assert notifier.titles == ["Front Door offline", "Front Door back online"]
    assert notifier.sent[0]["priority"] == "high"


async def test_flapping_node_is_damped(driver, door):
    """A sensor at the edge of range must not produce an alert storm."""
    notifier = FakeNotifier()
    _, clock = await build(driver, notifier, cooldown_seconds=300)

    for _ in range(5):
        door.emit("dead", {})
        door.emit("alive", {})
        clock.advance(20)
    await drain()

    assert notifier.titles == ["Front Door offline", "Front Door back online"]


def test_node_label_prefers_name_then_description_then_id():
    from .conftest import FakeNode

    assert node_label(FakeNode(1, name="Kitchen")) == "Kitchen"
    assert node_label(FakeNode(2, description="Contact Sensor")) == "Contact Sensor"
    assert node_label(FakeNode(3)) == "node 3"

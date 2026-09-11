"""Lightweight stand-ins for zwave-js-server objects.

The real ``Node``/``Driver`` need a live websocket and a full state dump;
the handler only touches a handful of attributes, so stubs keep the tests
fast and free of network setup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from zwave_js_server.model.notification import NotificationNotification


@dataclass
class FakeDeviceConfig:
    description: str | None = None


@dataclass
class FakeNode:
    node_id: int
    name: str | None = None
    description: str | None = None
    status: str = "alive"
    handlers: dict[str, list] = field(default_factory=dict)

    @property
    def device_config(self) -> FakeDeviceConfig:
        return FakeDeviceConfig(self.description)

    def on(self, event: str, callback) -> None:
        self.handlers.setdefault(event, []).append(callback)

    def emit(self, event: str, data: dict) -> None:
        payload = {"node": self, **data}
        for callback in self.handlers.get(event, []):
            callback(payload)


@dataclass
class FakeController:
    nodes: dict[int, FakeNode]


@dataclass
class FakeDriver:
    controller: FakeController


@dataclass
class FakeMetadata:
    cc_specific: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeValue:
    command_class: int
    property_: str
    value: Any
    notification_type: int | None = None
    property_key_name: str | None = None

    @property
    def metadata(self) -> FakeMetadata:
        if self.notification_type is None:
            return FakeMetadata()
        return FakeMetadata({"notificationType": self.notification_type})


class FakeNotifier:
    """Records ``send`` calls instead of hitting the network."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, **kwargs) -> None:
        self.sent.append(kwargs)

    @property
    def titles(self) -> list[str]:
        return [s["title"] for s in self.sent]


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def notification(
    node: FakeNode,
    *,
    type_: int,
    event: int,
    label: str = "label",
    event_label: str = "event label",
) -> NotificationNotification:
    """Build a real NotificationNotification around a stub node."""
    return NotificationNotification(
        node,  # type: ignore[arg-type]
        {
            "source": "node",
            "event": "notification",
            "nodeId": node.node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": type_,
                "label": label,
                "event": event,
                "eventLabel": event_label,
            },
        },
    )


@pytest.fixture
def door() -> FakeNode:
    return FakeNode(node_id=5, name="Front Door")


@pytest.fixture
def motion() -> FakeNode:
    return FakeNode(node_id=6, description="Motion Sensor v2")


@pytest.fixture
def driver(door: FakeNode, motion: FakeNode) -> FakeDriver:
    return FakeDriver(FakeController({5: door, 6: motion}))

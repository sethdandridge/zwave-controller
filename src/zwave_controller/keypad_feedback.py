"""Best-effort Indicator CC V1 LED feedback on the Ring keypad.

The 1st-gen Ring keypad reports Indicator CC V1 only, which exposes a
single generic indicator value (0 = off, non-zero = on). There's no way
to distinguish Armed Away vs Armed Home at the LED — only armed vs
disarmed. The keypad is a sleeping battery device with an Awake Timeout
of 1-5 s after activity; writes fired immediately inside a notification
callback generally land, writes outside that window may be deferred
until the next wake. Failures are logged and swallowed.
"""
from __future__ import annotations

import logging

from zwave_js_server.client import Client

_LOGGER = logging.getLogger(__name__)

_INDICATOR_CC = 135  # 0x87
# Device-specific Indicator V1 values for the Ring keypad: 3 shows the
# disarmed LED, 1 shows the armed LED. Both Arm Away and Arm Home map to 1
# — V1 has no way to distinguish them.
_V1_VALUE_DISARMED = 3
_V1_VALUE_ARMED = 1


class KeypadLed:
    def __init__(self, client: Client, node_id: int) -> None:
        self._client = client
        self._node_id = node_id
        self._last: bool | None = None

    async def set_armed(self, armed: bool) -> None:
        if self._last == armed:
            return
        self._last = armed
        value = _V1_VALUE_ARMED if armed else _V1_VALUE_DISARMED
        try:
            await self._client.async_send_command(
                {
                    "command": "node.set_value",
                    "nodeId": self._node_id,
                    "valueId": {
                        "commandClass": _INDICATOR_CC,
                        "endpoint": 0,
                        "property": "value",
                    },
                    "value": value,
                }
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("indicator write failed (armed=%s): %s", armed, exc)

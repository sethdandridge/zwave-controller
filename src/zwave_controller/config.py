"""Environment-variable configuration."""
from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be a boolean (true/false), got {raw!r}")


@dataclass(frozen=True)
class Config:
    zwave_ws_url: str
    keypad_node_id: int

    unifi_host: str
    unifi_site: str
    unifi_api_key: str
    unifi_policy_id: str
    unifi_verify_tls: bool

    disarm_pin: str
    log_level: str

    @classmethod
    def from_env(cls) -> "Config":
        disarm_pin = _require("DISARM_PIN")
        if not disarm_pin.isdigit():
            raise ConfigError("DISARM_PIN must contain digits only")

        try:
            node_id = int(_require("KEYPAD_NODE_ID"))
        except ValueError as exc:
            raise ConfigError("KEYPAD_NODE_ID must be an integer") from exc

        return cls(
            zwave_ws_url=_require("ZWAVE_WS_URL"),
            keypad_node_id=node_id,
            unifi_host=_require("UNIFI_HOST").rstrip("/"),
            unifi_site=os.environ.get("UNIFI_SITE", "default").strip() or "default",
            unifi_api_key=_require("UNIFI_API_KEY"),
            unifi_policy_id=_require("UNIFI_POLICY_ID"),
            unifi_verify_tls=_bool("UNIFI_VERIFY_TLS", default=True),
            disarm_pin=disarm_pin,
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )

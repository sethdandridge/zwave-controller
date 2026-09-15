"""UniFi Zone-Based Firewall Policy toggle and client-list lookup."""
from __future__ import annotations

import logging

import httpx

_LOGGER = logging.getLogger(__name__)


class UnifiClient:
    def __init__(self, http: httpx.AsyncClient, site: str, policy_id: str) -> None:
        self._http = http
        self._site = site
        self._policy_id = policy_id
        self._path = (
            f"/proxy/network/v2/api/site/{site}/firewall-policies/{policy_id}"
        )
        self._clients_path = f"/proxy/network/api/s/{site}/stat/sta"

    async def connected_macs(self) -> set[str]:
        """MACs of every client currently associated with the site.

        Lowercase, colon-separated. Raises on HTTP errors and on a payload
        that isn't the expected ``{"meta": {"rc": "ok"}, "data": [...]}``
        shape, so the presence poller can count the poll as failed rather
        than mistaking an empty answer for "nobody home".
        """
        r = await self._http.get(self._clients_path)
        r.raise_for_status()
        body = r.json()
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("meta"), dict)
            or body["meta"].get("rc") != "ok"
            or not isinstance(body.get("data"), list)
        ):
            raise ValueError("unexpected stat/sta payload")
        return {
            entry["mac"].lower()
            for entry in body["data"]
            if isinstance(entry, dict) and isinstance(entry.get("mac"), str)
        }

    async def get_policy_enabled(self) -> bool:
        r = await self._http.get(self._path)
        r.raise_for_status()
        return bool(r.json().get("enabled"))

    async def set_policy_enabled(self, enabled: bool) -> None:
        """GET the policy, flip ``enabled``, PUT the full object back.

        Idempotent: no request is sent if the policy is already in the target
        state. Retries once on 401/403 in case of a transient auth blip.
        """
        for attempt in (1, 2):
            try:
                await self._apply(enabled)
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (401, 403) and attempt == 1:
                    _LOGGER.warning(
                        "UniFi auth error %s on attempt %d; retrying once", status, attempt
                    )
                    continue
                _LOGGER.error(
                    "UniFi request failed (%s): %s", status, exc.response.text[:200]
                )
                raise

    async def _apply(self, enabled: bool) -> None:
        r = await self._http.get(self._path)
        r.raise_for_status()
        policy = r.json()
        if policy.get("enabled") == enabled:
            _LOGGER.info("policy %s already enabled=%s; no-op", self._policy_id, enabled)
            return
        policy["enabled"] = enabled
        r = await self._http.put(self._path, json=policy)
        r.raise_for_status()
        _LOGGER.info("policy %s set enabled=%s", self._policy_id, enabled)

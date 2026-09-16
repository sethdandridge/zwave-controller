"""Is anyone home? Answered by polling the UniFi client list.

Door and motion alerts are muted while a configured phone is on the WiFi.
The state machine is deliberately asymmetric: one sighting flips to *home*
immediately, but *away* needs the phone to be unseen for a grace period,
because an iPhone drops off WiFi for a minute or two whenever it sleeps.

A failed poll never counts as "nobody home" on its own. It does, however,
let the away timer keep running: an outage that lasts longer than the grace
period ends with alerts *un*muted, which is the safe direction for a
security feature. If the gateway is unreachable from the LAN for that long,
the WiFi is probably down and "phone not on WiFi" is literally true.

``on_return`` fires on a genuine return: an away -> home flip where "away"
was established by evidence (a successful poll without the phone, or the
grace period expiring), not merely by startup ignorance. A restart while
you are home does not greet you; a restart while you are out, followed by
you walking in, does.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable

_LOGGER = logging.getLogger(__name__)


class PresenceMonitor:
    def __init__(
        self,
        fetch: Callable[[], Awaitable[set[str]]],
        macs: frozenset[str],
        *,
        away_grace_seconds: int,
        poll_seconds: int,
        on_return: Callable[[Iterable[str]], Awaitable[None]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch
        self._macs = macs
        self._grace = away_grace_seconds
        self._poll_seconds = poll_seconds
        self._on_return = on_return
        self._clock = clock
        # Unknown counts as away: until the first successful sighting,
        # alerts fire.
        self._home = False
        self._last_seen: float | None = None
        self._failures = 0
        # True once "away" has been confirmed by evidence rather than
        # assumed at startup; gates the welcome-home callback.
        self._confirmed_away = False

    def is_home(self) -> bool:
        return self._home

    async def poll_once(self) -> None:
        """One poll and state update. Never raises."""
        now = self._clock()
        try:
            connected = await self._fetch()
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            # One warning per outage; the rest at DEBUG so a long outage
            # doesn't fill the journal.
            level = logging.WARNING if self._failures == 1 else logging.DEBUG
            _LOGGER.log(level, "presence: UniFi client poll failed: %s", exc)
            self._check_away(now)
            return

        if self._failures:
            _LOGGER.info(
                "presence: UniFi polling recovered after %d failure(s)", self._failures
            )
            self._failures = 0

        seen = self._macs & {mac.lower() for mac in connected}
        if seen:
            self._last_seen = now
            if not self._home:
                self._home = True
                _LOGGER.info("presence: home (%s on WiFi)", ", ".join(sorted(seen)))
                if self._confirmed_away:
                    self._confirmed_away = False
                    await self._greet(sorted(seen))
            return

        if not self._home:
            # A successful poll with nobody here: that is real evidence, so
            # the next arrival counts as a return.
            self._confirmed_away = True
        self._check_away(now)

    def _check_away(self, now: float) -> None:
        if not self._home or self._last_seen is None:
            return
        unseen = now - self._last_seen
        if unseen >= self._grace:
            self._home = False
            self._confirmed_away = True
            _LOGGER.info("presence: away (unseen for %.0fs)", unseen)

    async def _greet(self, seen: list[str]) -> None:
        if self._on_return is None:
            return
        try:
            await self._on_return(seen)
        except Exception:  # noqa: BLE001
            # The poller must survive anything the callback does.
            _LOGGER.exception("presence: on_return callback failed")

    async def run(self) -> None:
        """Poll forever. The caller primes state with ``poll_once`` first."""
        while True:
            await asyncio.sleep(self._poll_seconds)
            await self.poll_once()

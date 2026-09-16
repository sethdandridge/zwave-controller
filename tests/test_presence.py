"""Presence state machine: home on first sight, away only after the grace."""
from __future__ import annotations

import asyncio

from zwave_controller.presence import PresenceMonitor

from .conftest import FakeClock

PHONE = "3e:90:21:a9:71:a2"
OTHER = "aa:bb:cc:dd:ee:ff"
GRACE = 300


class FakeFetch:
    def __init__(self, macs: set[str] | None = None) -> None:
        self.macs: set[str] = macs or set()
        self.fail = False
        self.calls = 0

    async def __call__(self) -> set[str]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("unifi down")
        return set(self.macs)


class Greeter:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    async def __call__(self, seen) -> None:
        self.calls.append(list(seen))
        if self.fail:
            raise RuntimeError("ntfy exploded")


def build(
    fetch: FakeFetch, macs=(PHONE,), on_return=None
) -> tuple[PresenceMonitor, FakeClock]:
    clock = FakeClock()
    monitor = PresenceMonitor(
        fetch,
        frozenset(macs),
        away_grace_seconds=GRACE,
        poll_seconds=30,
        on_return=on_return,
        clock=clock,
    )
    return monitor, clock


async def test_starts_away():
    monitor, _ = build(FakeFetch({PHONE}))
    assert monitor.is_home() is False


async def test_first_sighting_is_home_immediately():
    monitor, _ = build(FakeFetch({PHONE}))
    await monitor.poll_once()
    assert monitor.is_home() is True


async def test_unseen_within_grace_stays_home():
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch)
    await monitor.poll_once()

    fetch.macs = set()
    clock.advance(GRACE - 1)
    await monitor.poll_once()
    assert monitor.is_home() is True


async def test_unseen_for_grace_is_away():
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch)
    await monitor.poll_once()

    fetch.macs = set()
    clock.advance(GRACE)
    await monitor.poll_once()
    assert monitor.is_home() is False


async def test_reappearance_resets_timer():
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch)
    await monitor.poll_once()  # t=0 seen

    fetch.macs = set()
    clock.advance(200)
    await monitor.poll_once()  # t=200 unseen, still home

    fetch.macs = {PHONE}
    clock.advance(50)
    await monitor.poll_once()  # t=250 seen again

    fetch.macs = set()
    clock.advance(250)
    await monitor.poll_once()  # t=500, only 250s unseen
    assert monitor.is_home() is True

    clock.advance(51)
    await monitor.poll_once()  # t=551, 301s unseen
    assert monitor.is_home() is False


async def test_any_configured_mac_counts():
    monitor, _ = build(FakeFetch({OTHER}), macs=(PHONE, OTHER))
    await monitor.poll_once()
    assert monitor.is_home() is True


async def test_matching_is_case_insensitive():
    monitor, _ = build(FakeFetch({PHONE.upper()}))
    await monitor.poll_once()
    assert monitor.is_home() is True


async def test_failed_poll_does_not_flip_state_or_raise():
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch)

    fetch.fail = True
    await monitor.poll_once()  # away stays away
    assert monitor.is_home() is False

    fetch.fail = False
    await monitor.poll_once()
    assert monitor.is_home() is True

    fetch.fail = True
    clock.advance(GRACE - 1)
    await monitor.poll_once()  # home stays home inside the grace
    assert monitor.is_home() is True


async def test_sustained_failures_decay_to_away():
    """An outage must not leave door alerts muted indefinitely."""
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch)
    await monitor.poll_once()

    fetch.fail = True
    for _ in range(10):
        clock.advance(GRACE // 10)
        await monitor.poll_once()
    assert monitor.is_home() is False


async def test_recovery_after_failures_sees_phone():
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch)
    await monitor.poll_once()

    fetch.fail = True
    clock.advance(GRACE)
    await monitor.poll_once()
    assert monitor.is_home() is False

    fetch.fail = False
    await monitor.poll_once()
    assert monitor.is_home() is True


async def test_transitions_are_logged(caplog):
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch)
    with caplog.at_level("INFO", logger="zwave_controller.presence"):
        await monitor.poll_once()
        fetch.macs = set()
        clock.advance(GRACE)
        await monitor.poll_once()
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("presence: home") and PHONE in m for m in messages)
    assert any(m.startswith("presence: away") for m in messages)


async def test_run_loop_survives_a_failing_poll():
    fetch = FakeFetch({PHONE})
    monitor = PresenceMonitor(
        fetch, frozenset({PHONE}), away_grace_seconds=GRACE, poll_seconds=0
    )

    original = fetch.__call__

    async def flaky() -> set[str]:
        if fetch.calls == 1:
            fetch.calls += 1
            raise ConnectionError("blip")
        return await original()

    monitor._fetch = flaky  # noqa: SLF001

    task = asyncio.create_task(monitor.run())

    async def until_three_calls() -> None:
        while fetch.calls < 3:
            await asyncio.sleep(0)

    await asyncio.wait_for(until_three_calls(), timeout=2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert monitor.is_home() is True


# -- welcome home -------------------------------------------------------------


async def test_startup_while_home_does_not_greet():
    greeter = Greeter()
    monitor, _ = build(FakeFetch({PHONE}), on_return=greeter)
    await monitor.poll_once()
    assert monitor.is_home() is True
    assert greeter.calls == []


async def test_startup_while_out_then_arriving_greets():
    greeter = Greeter()
    fetch = FakeFetch(set())
    monitor, _ = build(fetch, on_return=greeter)
    await monitor.poll_once()  # confirmed nobody home

    fetch.macs = {PHONE}
    await monitor.poll_once()
    assert greeter.calls == [[PHONE]]


async def test_leaving_and_returning_greets_once():
    greeter = Greeter()
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch, on_return=greeter)
    await monitor.poll_once()

    fetch.macs = set()
    clock.advance(GRACE)
    await monitor.poll_once()
    assert monitor.is_home() is False

    fetch.macs = {PHONE}
    await monitor.poll_once()
    await monitor.poll_once()
    assert greeter.calls == [[PHONE]]


async def test_brief_wifi_drop_does_not_greet():
    """Inside the grace period the phone never went 'away', so no greeting."""
    greeter = Greeter()
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch, on_return=greeter)
    await monitor.poll_once()

    fetch.macs = set()
    clock.advance(60)
    await monitor.poll_once()
    fetch.macs = {PHONE}
    clock.advance(60)
    await monitor.poll_once()
    assert greeter.calls == []


async def test_startup_poll_failure_then_sighting_does_not_greet():
    """Unknown -> home is not a return; we never saw you leave."""
    greeter = Greeter()
    fetch = FakeFetch({PHONE})
    monitor, _ = build(fetch, on_return=greeter)

    fetch.fail = True
    await monitor.poll_once()
    fetch.fail = False
    await monitor.poll_once()
    assert monitor.is_home() is True
    assert greeter.calls == []


async def test_outage_decay_then_return_greets():
    greeter = Greeter()
    fetch = FakeFetch({PHONE})
    monitor, clock = build(fetch, on_return=greeter)
    await monitor.poll_once()

    fetch.fail = True
    clock.advance(GRACE)
    await monitor.poll_once()
    assert monitor.is_home() is False

    fetch.fail = False
    await monitor.poll_once()
    assert greeter.calls == [[PHONE]]


async def test_greeter_failure_does_not_break_presence():
    greeter = Greeter(fail=True)
    fetch = FakeFetch(set())
    monitor, _ = build(fetch, on_return=greeter)
    await monitor.poll_once()

    fetch.macs = {PHONE}
    await monitor.poll_once()  # must not raise
    assert monitor.is_home() is True
    assert len(greeter.calls) == 1

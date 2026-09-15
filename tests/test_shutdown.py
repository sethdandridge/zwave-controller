"""The listener watchdog.

`Client.listen()` catches ConnectionClosed and returns, so a zwave-js-server
restart looks like a clean exit. Without this watchdog the service would sit
on a dead socket forever with alerts silently stopped, and systemd's
Restart=on-failure would never fire because nothing failed.
"""
from __future__ import annotations

import asyncio

import pytest

from zwave_controller.__main__ import wait_for_shutdown


async def test_signal_shuts_down_cleanly():
    stop = asyncio.Event()
    listen_task = asyncio.create_task(asyncio.Event().wait())  # never finishes

    stop.set()
    await wait_for_shutdown(stop, listen_task)  # returns, does not raise

    # The listener must survive: Client.disconnect() closes the socket and
    # then waits for the listener's cleanup to acknowledge it.
    await asyncio.sleep(0)
    assert not listen_task.done()
    listen_task.cancel()


async def test_silent_listener_exit_is_a_failure():
    """listen() returning None means the websocket dropped."""
    stop = asyncio.Event()

    async def listener() -> None:
        return None

    listen_task = asyncio.create_task(listener())

    with pytest.raises(SystemExit) as excinfo:
        await wait_for_shutdown(stop, listen_task)
    assert excinfo.value.code == 1


async def test_listener_exception_is_a_failure():
    stop = asyncio.Event()

    async def listener() -> None:
        raise ConnectionResetError("server went away")

    listen_task = asyncio.create_task(listener())

    with pytest.raises(SystemExit) as excinfo:
        await wait_for_shutdown(stop, listen_task)
    assert excinfo.value.code == 1


async def test_extra_task_returning_is_a_failure():
    """A presence poller that quietly exits would freeze the mute state."""
    stop = asyncio.Event()
    listen_task = asyncio.create_task(asyncio.Event().wait())

    async def poller() -> None:
        return None

    extra = asyncio.create_task(poller(), name="presence")
    with pytest.raises(SystemExit) as excinfo:
        await wait_for_shutdown(stop, listen_task, extra)
    assert excinfo.value.code == 1
    listen_task.cancel()


async def test_extra_task_exception_is_a_failure():
    stop = asyncio.Event()
    listen_task = asyncio.create_task(asyncio.Event().wait())

    async def poller() -> None:
        raise RuntimeError("poller crashed")

    extra = asyncio.create_task(poller(), name="presence")
    with pytest.raises(SystemExit):
        await wait_for_shutdown(stop, listen_task, extra)
    listen_task.cancel()


async def test_signal_leaves_extra_task_to_the_caller():
    stop = asyncio.Event()
    listen_task = asyncio.create_task(asyncio.Event().wait())
    extra = asyncio.create_task(asyncio.Event().wait(), name="presence")

    stop.set()
    await wait_for_shutdown(stop, listen_task, extra)

    await asyncio.sleep(0)
    assert not extra.done()
    extra.cancel()
    listen_task.cancel()


async def test_pending_stop_task_is_cancelled():
    """No 'Task was destroyed but it is pending' noise on the failure path."""
    stop = asyncio.Event()

    async def listener() -> None:
        return None

    listen_task = asyncio.create_task(listener())

    with pytest.raises(SystemExit):
        await wait_for_shutdown(stop, listen_task)

    await asyncio.sleep(0)
    leftover = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    assert leftover == []

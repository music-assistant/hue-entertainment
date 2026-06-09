"""Tests for EntertainmentSession lifecycle management."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from hue_entertainment import EntertainmentSession
from hue_entertainment.models import EntertainmentArea, LightColorCommand

if TYPE_CHECKING:
    from collections.abc import Callable


class _FakeStreamer:
    """Minimal stand-in for HueDtlsStreamer that records calls (no real sockets)."""

    def __init__(self) -> None:
        """Initialize the fake in a disconnected state."""
        self.connected = False
        self.connect_calls: list[tuple[str, str, str, str]] = []
        self.sent: list[list[LightColorCommand]] = []
        self.fail_connect = False

    @property
    def is_connected(self) -> bool:
        """Return whether the fake is currently 'connected'."""
        return self.connected

    def connect(self, host: str, username: str, clientkey: str, area_id: str) -> None:
        """Record a connect call and flip to connected (or raise if configured)."""
        if self.fail_connect:
            msg = "handshake failed"
            raise OSError(msg)
        self.connect_calls.append((host, username, clientkey, area_id))
        self.connected = True

    def disconnect(self) -> None:
        """Flip back to disconnected."""
        self.connected = False

    def send_colors(self, commands: list[LightColorCommand]) -> None:
        """Record a frame if connected."""
        if self.connected:
            self.sent.append(commands)


def _make_session(
    streamer: _FakeStreamer,
    *,
    idle_timeout: float = 0.0,
    areas: list[EntertainmentArea] | None = None,
) -> tuple[EntertainmentSession, AsyncMock]:
    """Build a session with its API and streamer replaced by fakes."""
    session = EntertainmentSession("1.2.3.4", "user", "deadbeef", idle_timeout=idle_timeout)
    api = AsyncMock()
    api.host = "1.2.3.4"
    api.get_entertainment_areas.return_value = areas or []
    session._api = api
    session._streamer = streamer
    return session, api


async def _wait_until(predicate: Callable[[], bool], max_wait: float = 2.0) -> bool:
    """Poll ``predicate`` until true or timeout (avoids flaky fixed sleeps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


async def test_start_activates_and_connects() -> None:
    """start() activates the area via CLIP and opens the DTLS stream."""
    streamer = _FakeStreamer()
    session, api = _make_session(streamer)
    await session.start("area-1")
    api.start_entertainment.assert_awaited_once_with("area-1")
    assert streamer.connect_calls == [("1.2.3.4", "user", "deadbeef", "area-1")]
    assert session.is_streaming
    assert session.active_area == "area-1"


async def test_start_is_idempotent_for_same_area() -> None:
    """Starting the same area twice connects only once."""
    streamer = _FakeStreamer()
    session, api = _make_session(streamer)
    await session.start("area-1")
    await session.start("area-1")
    assert len(streamer.connect_calls) == 1
    assert api.start_entertainment.await_count == 1


async def test_start_switches_area() -> None:
    """Starting a different area tears down the old stream and starts the new one."""
    streamer = _FakeStreamer()
    session, api = _make_session(streamer)
    await session.start("area-1", stop_others=False)
    await session.start("area-2", stop_others=False)
    assert len(streamer.connect_calls) == 2
    assert streamer.connect_calls[-1][3] == "area-2"
    api.stop_entertainment.assert_any_await("area-1")
    assert session.active_area == "area-2"


async def test_send_forwards_only_when_connected() -> None:
    """send() is a no-op before start and forwards frames after."""
    streamer = _FakeStreamer()
    session, _api = _make_session(streamer)
    cmd = LightColorCommand(channel_id=0, red=255, green=0, blue=0)
    session.send([cmd])
    assert streamer.sent == []
    await session.start("area-1")
    session.send([cmd])
    assert streamer.sent == [[cmd]]


async def test_stop_deactivates() -> None:
    """stop() disconnects and deactivates the area."""
    streamer = _FakeStreamer()
    session, api = _make_session(streamer)
    await session.start("area-1")
    await session.stop()
    assert not session.is_streaming
    assert session.active_area is None
    api.stop_entertainment.assert_any_await("area-1")


async def test_start_rolls_back_on_connect_failure() -> None:
    """A DTLS connect failure rolls back the CLIP activation and re-raises."""
    streamer = _FakeStreamer()
    streamer.fail_connect = True
    session, api = _make_session(streamer)
    with pytest.raises(OSError, match="handshake failed"):
        await session.start("area-1")
    api.start_entertainment.assert_awaited_once_with("area-1")
    api.stop_entertainment.assert_any_await("area-1")
    assert not session.is_streaming


async def test_stop_others_keeps_target_area() -> None:
    """stop_others stops other areas but never the target."""
    streamer = _FakeStreamer()
    areas = [
        EntertainmentArea(id="other", name="Other"),
        EntertainmentArea(id="area-1", name="TV"),
    ]
    session, api = _make_session(streamer, areas=areas)
    await session.start("area-1", stop_others=True)
    api.stop_entertainment.assert_any_await("other")
    stopped = [call.args[0] for call in api.stop_entertainment.await_args_list]
    assert "area-1" not in stopped


async def test_idle_timeout_tears_down() -> None:
    """The stream is torn down after the idle timeout with no sends."""
    streamer = _FakeStreamer()
    session, api = _make_session(streamer, idle_timeout=0.1)
    await session.start("area-1")
    assert session.is_streaming
    assert await _wait_until(lambda: not session.is_streaming)
    api.stop_entertainment.assert_any_await("area-1")


async def test_sends_keep_stream_alive() -> None:
    """Regular sends keep the stream alive past the idle timeout, then it idles out."""
    streamer = _FakeStreamer()
    session, _api = _make_session(streamer, idle_timeout=0.3)
    cmd = LightColorCommand(channel_id=0)
    await session.start("area-1")
    for _ in range(6):
        session.send([cmd])
        await asyncio.sleep(0.05)
    assert session.is_streaming
    assert await _wait_until(lambda: not session.is_streaming)

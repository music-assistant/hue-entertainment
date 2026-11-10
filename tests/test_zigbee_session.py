"""Tests for ZigbeeEntertainmentSession against a fake ziggurat-server."""

from __future__ import annotations

import asyncio
import itertools
import struct
from typing import TYPE_CHECKING

import pytest

from hue_entertainment import LightColorCommand, ZigbeeLight
from hue_entertainment.zigbee import ZigbeeEntertainmentSession, ZigguratError
from hue_entertainment.zigbee import session as zigbee_session
from hue_entertainment.zigbee.frames import (
    CLUSTER_COLOR,
    CLUSTER_ENTERTAINMENT,
    CLUSTER_LEVEL,
    CLUSTER_ON_OFF,
    light_record,
    move_to_color,
    move_to_color_temperature,
    move_to_level_with_on_off,
    off,
    smoothing_from_seconds,
)
from hue_entertainment.zigbee.ziggurat import STATUS_NOT_CONFIGURED

from .fake_ziggurat import SEND_NWK_NO_ACK, FakeLight, Unicast, wait_until

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from .fake_ziggurat import FakeZiggurat

    SessionFactory = Callable[..., ZigbeeEntertainmentSession]

RED = LightColorCommand(channel_id=0, red=65535)
BLUE = LightColorCommand(channel_id=1, blue=65535)


def _fake_xy(command: LightColorCommand) -> tuple[float, float, float]:
    """Stand-in colour conversion, so tests pin the frame encoding and not the colour maths."""
    return (command.red / 65535 * 0.6, command.green / 65535 * 0.6, max(0.1, command.blue / 65535))


def _record(nwk: int, command: LightColorCommand) -> bytes:
    """Return the light record the session should stream for ``command``."""
    x, y, brightness = _fake_xy(command)
    return light_record(nwk, round(brightness * 2047), x, y)


def _frame_fields(frame: Unicast) -> tuple[int, int, bytes]:
    """Decode an entertainment frame into (counter, smoothing, records)."""
    counter, smoothing = struct.unpack_from("<IH", frame.zcl_payload)
    return counter, smoothing, frame.zcl_payload[6:]


def _hue_light(
    nwk: int,
    ieee: str,
    *,
    rssi: int = -60,
    on: bool = True,
    level: int = 200,
    color_mode: int = 1,
    x: int = 0x6000,
    y: int = 0x5000,
    mireds: int = 366,
    balance: int = 0xFE,
) -> FakeLight:
    """Return a fake Hue colour light with the given state."""
    return FakeLight(
        nwk,
        ieee,
        rssi=rssi,
        attributes={
            (CLUSTER_ON_OFF, 0x0000): (0x10, int(on)),
            (CLUSTER_LEVEL, 0x0000): (0x20, level),
            (CLUSTER_COLOR, 0x0008): (0x30, color_mode),
            (CLUSTER_COLOR, 0x0003): (0x21, x),
            (CLUSTER_COLOR, 0x0004): (0x21, y),
            (CLUSTER_COLOR, 0x0007): (0x21, mireds),
            (CLUSTER_ENTERTAINMENT, 0x0005): (0x20, balance),
        },
    )


def _zigbee_light(fake: FakeLight, channel_id: int) -> ZigbeeLight:
    """Return the ZigbeeLight a consumer would pass for ``fake``."""
    return ZigbeeLight(
        channel_id=channel_id, ieee=fake.ieee, nwk=fake.nwk, name=f"light {channel_id}"
    )


def _requests_to(ziggurat: FakeZiggurat, nwk: int, after: int = 0) -> list[Unicast]:
    """Return the non-frame requests sent to ``nwk``, from index ``after`` on."""
    return [req for req in ziggurat.requests[after:] if req.nwk == nwk and not req.is_frame]


@pytest.fixture(autouse=True)
def _fast_session(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the session timeouts and replace the colour conversion."""
    monkeypatch.setattr(zigbee_session, "_command_to_xy", _fake_xy)
    monkeypatch.setattr(zigbee_session, "_REQUEST_TIMEOUT_S", 0.3)
    monkeypatch.setattr(zigbee_session, "_FRAME_CONFIRM_TIMEOUT_S", 0.2)
    monkeypatch.setattr(zigbee_session, "_STREAM_LOSS_S", 0.2)


@pytest.fixture
async def make_session(ziggurat: FakeZiggurat) -> AsyncIterator[SessionFactory]:
    """Build sessions against the fake server and close them after the test."""
    sessions: list[ZigbeeEntertainmentSession] = []

    def factory(lights: list[ZigbeeLight], idle_timeout: float = 0.0) -> ZigbeeEntertainmentSession:
        session = ZigbeeEntertainmentSession(ziggurat.url, lights, idle_timeout=idle_timeout)
        sessions.append(session)
        return session

    yield factory
    for session in sessions:
        await session.aclose()


def test_rejects_invalid_light_counts() -> None:
    """A session needs 1 to 10 lights (one frame holds at most 10 records)."""
    lights = [ZigbeeLight(i, f"00:00:00:00:00:00:00:{i:02x}", i + 1) for i in range(11)]
    with pytest.raises(ValueError, match="1-10"):
        ZigbeeEntertainmentSession("ws://127.0.0.1:1/", [])
    with pytest.raises(ValueError, match="1-10"):
        ZigbeeEntertainmentSession("ws://127.0.0.1:1/", lights)


async def test_start_snapshots_resets_and_picks_proxy_by_rssi(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """start() reads each light, resets it, and streams via the best-heard light."""
    far = ziggurat.add_light(_hue_light(0x1001, "00:17:88:01:00:00:00:01", rssi=-82))
    near = ziggurat.add_light(_hue_light(0x1002, "00:17:88:01:00:00:00:02", rssi=-48))
    session = make_session([_zigbee_light(far, 0), _zigbee_light(near, 1)])
    await session.start()
    assert session.is_streaming

    for fake in (far, near):
        sent = _requests_to(ziggurat, fake.nwk)
        assert all(req.aps_ack for req in sent)
        assert [req.cluster for req in sent if not req.zcl.cluster_specific] == [
            CLUSTER_ON_OFF,
            CLUSTER_LEVEL,
            CLUSTER_COLOR,
            CLUSTER_ENTERTAINMENT,
        ]
        assert sent[-1].is_reset

    session.send([RED, BLUE])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    frame = ziggurat.frames[0]
    assert frame.nwk == near.nwk
    assert not frame.aps_ack
    assert _frame_fields(frame)[2] == _record(far.nwk, RED) + _record(near.nwk, BLUE)


async def test_frames_only_go_to_proxy_and_merge_latest_values(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """Frames go to the proxy only; lights without a new command keep their last colour."""
    a = ziggurat.add_light(_hue_light(0x2001, "00:17:88:01:00:00:00:0a"))
    b = ziggurat.add_light(_hue_light(0x2002, "00:17:88:01:00:00:00:0b"))
    session = make_session([_zigbee_light(a, 0), _zigbee_light(b, 1)])
    await session.start()

    session.send([RED])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert _frame_fields(ziggurat.frames[0])[2] == _record(a.nwk, RED)

    session.send([BLUE])
    assert await wait_until(lambda: len(ziggurat.frames) == 2)
    assert _frame_fields(ziggurat.frames[1])[2] == _record(a.nwk, RED) + _record(b.nwk, BLUE)

    assert {frame.nwk for frame in ziggurat.frames} == {a.nwk}
    counters = [_frame_fields(frame)[0] for frame in ziggurat.frames]
    assert counters[1] == counters[0] + 1


async def test_latest_frame_wins_with_one_frame_in_flight(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """While a frame is unconfirmed nothing else is sent; then only the newest frame goes."""
    a = ziggurat.add_light(_hue_light(0x3001, "00:17:88:01:00:00:00:31"))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    ziggurat.hold_frames = True

    session.send([LightColorCommand(0, red=1000)])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    for red in (2000, 3000, 4000):
        session.send([LightColorCommand(0, red=red)])
        await asyncio.sleep(0.02)
    assert len(ziggurat.frames) == 1

    await ziggurat.release_held()
    assert await wait_until(lambda: len(ziggurat.frames) == 2)
    assert _frame_fields(ziggurat.frames[1])[2] == _record(a.nwk, LightColorCommand(0, red=4000))
    assert ziggurat.max_frames_in_flight == 1


async def test_frame_rate_is_capped(ziggurat: FakeZiggurat, make_session: SessionFactory) -> None:
    """Commands faster than 25 Hz are coalesced: frames stay at least 40 ms apart."""
    a = ziggurat.add_light(_hue_light(0x3101, "00:17:88:01:00:00:00:32"))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    for red in range(60):
        session.send([LightColorCommand(0, red=red * 1000)])
        await asyncio.sleep(0.005)
    starts = [frame.at for frame in ziggurat.frames]
    assert 3 <= len(starts) <= 10
    assert min(b - a for a, b in itertools.pairwise(starts)) >= 0.035


async def test_keepalive_resends_last_frame(
    ziggurat: FakeZiggurat, make_session: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without new commands the last frame is repeated with a fresh counter."""
    monkeypatch.setattr(zigbee_session, "_KEEPALIVE_S", 0.05)
    a = ziggurat.add_light(_hue_light(0x4001, "00:17:88:01:00:00:00:41"))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    session.send([RED])
    assert await wait_until(lambda: len(ziggurat.frames) >= 3)
    fields = [_frame_fields(frame) for frame in ziggurat.frames[:3]]
    assert {records for _, _, records in fields} == {_record(a.nwk, RED)}
    assert [counter for counter, _, _ in fields] == [fields[0][0] + i for i in range(3)]


async def test_no_frames_before_the_first_command(
    ziggurat: FakeZiggurat, make_session: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """send() before start() is ignored, and nothing streams until a command arrives."""
    monkeypatch.setattr(zigbee_session, "_KEEPALIVE_S", 0.02)
    a = ziggurat.add_light(_hue_light(0x4101, "00:17:88:01:00:00:00:42"))
    session = make_session([_zigbee_light(a, 0)])
    session.send([RED])
    await session.start()
    await asyncio.sleep(0.1)
    assert ziggurat.frames == []


async def test_smoothing_follows_the_send_interval(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """The smoothing tracks the caller's frame interval, clamped to 100 ms."""
    a = ziggurat.add_light(_hue_light(0x4201, "00:17:88:01:00:00:00:43"))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    for _ in range(40):
        session._last_send -= 0.5  # a long gap between commands
        session.send([RED])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    smoothing = _frame_fields(ziggurat.frames[0])[1]
    assert abs(smoothing - smoothing_from_seconds(0.1)) < 5


async def test_stop_restores_captured_state(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """stop() resets every light, then restores off / xy / colour-temperature state."""
    was_off = ziggurat.add_light(_hue_light(0x5001, "00:17:88:01:00:00:00:51", on=False))
    xy = ziggurat.add_light(_hue_light(0x5002, "00:17:88:01:00:00:00:52", level=120))
    ct = ziggurat.add_light(
        _hue_light(0x5003, "00:17:88:01:00:00:00:53", color_mode=2, mireds=250, level=80)
    )
    session = make_session([_zigbee_light(was_off, 0), _zigbee_light(xy, 1), _zigbee_light(ct, 2)])
    await session.start()
    session.send([RED, BLUE, LightColorCommand(2, green=65535)])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    mark = len(ziggurat.requests)

    await session.stop()
    assert not session.is_streaming

    def restored(nwk: int) -> list[tuple[int, int, bytes]]:
        sent = _requests_to(ziggurat, nwk, mark)
        assert all(req.aps_ack for req in sent)
        assert sent[0].is_reset
        return [(req.cluster, req.zcl.command, req.zcl_payload) for req in sent[1:]]

    def expected(cluster: int, asdu: bytes) -> tuple[int, int, bytes]:
        return (cluster, asdu[2], asdu[3:])

    assert restored(was_off.nwk) == [
        expected(CLUSTER_COLOR, move_to_color(0, 0x6000, 0x5000, 0)),
        expected(CLUSTER_ON_OFF, off(0)),
    ]
    assert restored(xy.nwk) == [
        expected(CLUSTER_COLOR, move_to_color(0, 0x6000, 0x5000, 4)),
        expected(CLUSTER_LEVEL, move_to_level_with_on_off(0, 120, 4)),
    ]
    assert restored(ct.nwk) == [
        expected(CLUSTER_COLOR, move_to_color_temperature(0, 250, 4)),
        expected(CLUSTER_LEVEL, move_to_level_with_on_off(0, 80, 4)),
    ]


async def test_stop_is_idempotent(ziggurat: FakeZiggurat, make_session: SessionFactory) -> None:
    """A second stop() (or a stop before start) sends nothing."""
    a = ziggurat.add_light(_hue_light(0x5101, "00:17:88:01:00:00:00:54"))
    session = make_session([_zigbee_light(a, 0)])
    await session.stop()
    assert ziggurat.requests == []
    await session.start()
    await session.stop()
    mark = len(ziggurat.requests)
    await session.stop()
    assert len(ziggurat.requests) == mark


async def test_unanswered_light_is_streamed_but_not_restored(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A light that does not answer keeps its record in the frames but gets no restore."""
    a = ziggurat.add_light(_hue_light(0x6001, "00:17:88:01:00:00:00:61"))
    gone = FakeLight(0x6002, "00:17:88:01:00:00:00:62", online=False)
    ziggurat.add_light(gone)
    session = make_session([_zigbee_light(a, 0), _zigbee_light(gone, 1)])
    await session.start()
    assert session.is_streaming
    assert len(_requests_to(ziggurat, gone.nwk)) == 2  # the OnOff read, then the reset

    session.send([RED, BLUE])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert ziggurat.frames[0].nwk == a.nwk
    assert _frame_fields(ziggurat.frames[0])[2] == _record(a.nwk, RED) + _record(gone.nwk, BLUE)

    gone.online = True  # back by the time we stop, but there is nothing to restore it to
    mark = len(ziggurat.requests)
    await session.stop()
    sent = _requests_to(ziggurat, gone.nwk, mark)
    assert len(sent) == 1
    assert sent[0].is_reset


async def test_balance_attribute_written_only_when_different(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """FC01 attribute 0x0005 is written to 0xFE only on lights that report another value."""
    ready = ziggurat.add_light(_hue_light(0x7001, "00:17:88:01:00:00:00:71"))
    unset = ziggurat.add_light(_hue_light(0x7002, "00:17:88:01:00:00:00:72", balance=0x00))
    session = make_session([_zigbee_light(ready, 0), _zigbee_light(unset, 1)])
    await session.start()

    def writes(nwk: int) -> list[Unicast]:
        return [
            req
            for req in _requests_to(ziggurat, nwk)
            if not req.zcl.cluster_specific and req.zcl.command == 0x02
        ]

    assert writes(ready.nwk) == []
    assert len(writes(unset.nwk)) == 1
    assert writes(unset.nwk)[0].zcl.manufacturer == 0x100B
    assert unset.attributes[(CLUSTER_ENTERTAINMENT, 0x0005)] == (0x20, 0xFE)


async def test_start_fails_when_stack_is_down(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """NotConfigured during start raises; a later start captures the state afresh."""
    a = ziggurat.add_light(_hue_light(0x7101, "00:17:88:01:00:00:00:73"))
    session = make_session([_zigbee_light(a, 0)])
    ziggurat.status_for = lambda _req: STATUS_NOT_CONFIGURED
    with pytest.raises(ZigguratError, match="NotConfigured"):
        await session.start()
    assert not session.is_streaming

    ziggurat.status_for = lambda _req: 0
    mark = len(ziggurat.requests)
    await session.start()
    assert session.is_streaming
    assert _requests_to(ziggurat, a.nwk, mark)[0].cluster == CLUSTER_ON_OFF


async def test_not_configured_marks_stream_lost(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """ZHA reconfiguring the stack (NotConfigured on a frame) ends the stream."""
    a = ziggurat.add_light(_hue_light(0x8001, "00:17:88:01:00:00:00:81"))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    ziggurat.status_for = lambda req: STATUS_NOT_CONFIGURED if req.is_frame else 0
    session.send([RED])
    # Immediately, not after the failing-frames window (the next try is a keepalive away).
    assert await wait_until(lambda: not session.is_streaming, max_wait=0.5)


async def test_disconnect_marks_stream_lost(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A dropped websocket ends the stream."""
    a = ziggurat.add_light(_hue_light(0x8101, "00:17:88:01:00:00:00:82"))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    await ziggurat.drop()
    assert await wait_until(lambda: not session.is_streaming)


async def test_failing_confirms_mark_stream_lost(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """Frames that keep failing for the loss window end the stream; one failure does not."""
    a = ziggurat.add_light(_hue_light(0x8201, "00:17:88:01:00:00:00:83"))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    failures = iter([True])
    ziggurat.confirm_for = lambda req: (
        SEND_NWK_NO_ACK if req.is_frame and next(failures, False) else 0
    )
    session.send([RED])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    session.send([BLUE])
    assert await wait_until(lambda: len(ziggurat.frames) == 2)
    assert session.is_streaming

    ziggurat.confirm_for = lambda req: SEND_NWK_NO_ACK if req.is_frame else 0

    async def keep_sending() -> None:
        while session.is_streaming:
            session.send([RED])
            await asyncio.sleep(0.01)

    await asyncio.wait_for(keep_sending(), 2)
    assert not session.is_streaming


async def test_restart_after_loss_keeps_the_original_snapshot(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A restart keeps the first snapshot, so stop restores the pre-stream state."""
    a = ziggurat.add_light(_hue_light(0x9001, "00:17:88:01:00:00:00:91", level=150))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    a.attributes[(CLUSTER_LEVEL, 0x0000)] = (0x20, 254)
    await ziggurat.drop()
    assert await wait_until(lambda: not session.is_streaming)

    mark = len(ziggurat.requests)
    await session.start()
    assert session.is_streaming
    assert [req.is_reset for req in _requests_to(ziggurat, a.nwk, mark)] == [True]

    mark = len(ziggurat.requests)
    await session.stop()
    level = [req for req in _requests_to(ziggurat, a.nwk, mark) if req.cluster == CLUSTER_LEVEL]
    assert level[0].zcl_payload == move_to_level_with_on_off(0, 150, 4)[3:]


async def test_restart_replaces_a_proxy_that_went_away(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A restart streams via another light when the old proxy no longer acknowledges."""
    far = ziggurat.add_light(_hue_light(0x9101, "00:17:88:01:00:00:00:a1", rssi=-82))
    near = ziggurat.add_light(_hue_light(0x9102, "00:17:88:01:00:00:00:a2", rssi=-48))
    session = make_session([_zigbee_light(far, 0), _zigbee_light(near, 1)])
    await session.start()
    session.send([RED, BLUE])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert ziggurat.frames[0].nwk == near.nwk

    near.online = False  # e.g. switched off at the wall
    await ziggurat.drop()
    assert await wait_until(lambda: not session.is_streaming)
    await session.start()
    session.send([RED, BLUE])
    assert await wait_until(lambda: len(ziggurat.frames) == 2)
    assert ziggurat.frames[1].nwk == far.nwk


async def test_device_joined_updates_nwk(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A light that rejoins under a new NWK keeps streaming at its new address."""
    a = ziggurat.add_light(_hue_light(0xA001, "00:17:88:01:00:00:00:a1"))
    light = ZigbeeLight(channel_id=0, ieee="00:17:88:01:00:00:00:A1", nwk=a.nwk)
    session = make_session([light])
    await session.start()
    await ziggurat.inject_joined(0xA0FF, a.ieee)
    await asyncio.sleep(0.02)
    session.send([RED])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert ziggurat.frames[0].nwk == 0xA0FF
    assert _frame_fields(ziggurat.frames[0])[2] == _record(0xA0FF, RED)


async def test_stop_never_raises_when_server_is_gone(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """stop() completes quietly when ziggurat-server disappeared."""
    a = ziggurat.add_light(_hue_light(0xB001, "00:17:88:01:00:00:00:b1"))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    session.send([RED])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    await ziggurat.close()
    await asyncio.wait_for(session.stop(), 5)
    assert not session.is_streaming


async def test_idle_timeout_stops_and_restores(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """Without send() for ``idle_timeout`` seconds the session stops and restores the lights."""
    a = ziggurat.add_light(_hue_light(0xC001, "00:17:88:01:00:00:00:c1"))
    session = make_session([_zigbee_light(a, 0)], idle_timeout=0.15)
    await session.start()
    session.send([RED])
    assert await wait_until(lambda: not session.is_streaming)
    assert await wait_until(
        lambda: any(req.cluster == CLUSTER_LEVEL for req in ziggurat.requests[-3:])
    )

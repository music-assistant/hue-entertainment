"""Tests for ZigbeeEntertainmentSession against a fake ziggurat-server."""

from __future__ import annotations

import asyncio
import itertools
import struct
import time
from typing import TYPE_CHECKING

import pytest

from hue_entertainment import LightColorCommand, ZigbeeLight
from hue_entertainment.zigbee import (
    ZigbeeEntertainmentSession,
    ZigguratError,
    read_segment_counts,
)
from hue_entertainment.zigbee import session as zigbee_session
from hue_entertainment.zigbee.frames import (
    CLUSTER_COLOR,
    CLUSTER_ENTERTAINMENT,
    CLUSTER_HUE_STATE,
    CLUSTER_LEVEL,
    CLUSTER_ON_OFF,
    light_record,
    move_to_color,
    move_to_color_temperature,
    move_to_level_with_on_off,
    off,
    parse_zcl,
    segment_record,
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
# The Hue state (0xFC03 attribute 0x0002) of a real Signe showing five gradient colours.
_SIGNE_COLOURS = "1350000000848322eed32a90142b12962f170945"
_SIGNE_STATE = bytes.fromhex("cb030140" + "5629181d" + _SIGNE_COLOURS + "c0280000")


def _fake_xy(command: LightColorCommand) -> tuple[float, float, float]:
    """Stand-in colour conversion, so tests pin the frame encoding and not the colour maths."""
    return (command.red / 65535 * 0.6, command.green / 65535 * 0.6, max(0.1, command.blue / 65535))


def _record(nwk: int, command: LightColorCommand) -> bytes:
    """Return the light record the session should stream for ``command``."""
    x, y, brightness = _fake_xy(command)
    return light_record(nwk, round(brightness * 2047), x, y)


def _segment(address: int, command: LightColorCommand) -> bytes:
    """Return the segment record the session should stream for ``command``."""
    x, y, brightness = _fake_xy(command)
    return segment_record(address, round(brightness * 2047), x, y)


def _colours(count: int, first_channel: int = 0) -> list[LightColorCommand]:
    """Return a distinct colour command for each of ``count`` channels."""
    return [LightColorCommand(first_channel + i, red=1000 * (i + 1)) for i in range(count)]


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
    segments: int | None = None,
    hue_state: bytes | None = None,
) -> FakeLight:
    """
    Return a fake Hue colour light with the given state; a gradient one with ``segments``.

    Without ``hue_state`` it reports no 0xFC03 state attribute.
    """
    light = FakeLight(
        nwk,
        ieee,
        rssi=rssi,
        segments=segments,
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
    if hue_state is not None:
        light.attributes[(CLUSTER_HUE_STATE, 0x0002)] = (0x41, hue_state)
    return light


def _zigbee_light(fake: FakeLight, channel_id: int) -> ZigbeeLight:
    """Return the ZigbeeLight a consumer would pass for ``fake``."""
    return ZigbeeLight(
        channel_id=channel_id, ieee=fake.ieee, nwk=fake.nwk, name=f"light {channel_id}"
    )


def _zones(fake: FakeLight, count: int, first_channel: int = 0) -> list[ZigbeeLight]:
    """Return the entries a consumer would pass to stream ``fake`` in ``count`` zones."""
    return [
        ZigbeeLight(first_channel + zone, fake.ieee, fake.nwk, "gradient", zone=zone)
        for zone in range(count)
    ]


def _requests_to(ziggurat: FakeZiggurat, nwk: int, after: int = 0) -> list[Unicast]:
    """Return the non-frame requests sent to ``nwk``, from index ``after`` on."""
    return [req for req in ziggurat.requests[after:] if req.nwk == nwk and not req.is_frame]


def _command(cluster: int, asdu: bytes) -> tuple[int, int, int | None, int, bytes]:
    """Return a ZCL frame's cluster, frame control, manufacturer, command and payload."""
    header, payload = parse_zcl(asdu)
    return (cluster, header.frame_control, header.manufacturer, header.command, payload)


def _kinds(ziggurat: FakeZiggurat, nwk: int, after: int = 0) -> list[str]:
    """Name the non-frame requests sent to ``nwk``: read, write, layout, map or reset."""
    names = {(False, 0x00): "read", (False, 0x02): "write", (True, 0x03): "reset"}
    names |= {(True, 0x04): "layout", (True, 0x07): "map"}
    return [
        names.get((req.zcl.cluster_specific, req.zcl.command), "other")
        for req in _requests_to(ziggurat, nwk, after)
    ]


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

    def factory(
        lights: list[ZigbeeLight], idle_timeout: float = 0.0, max_rate_hz: float = 25.0
    ) -> ZigbeeEntertainmentSession:
        session = ZigbeeEntertainmentSession(
            ziggurat.url, lights, idle_timeout=idle_timeout, max_rate_hz=max_rate_hz
        )
        sessions.append(session)
        return session

    yield factory
    for session in sessions:
        await session.aclose()


_IEEE_A = "00:17:88:01:00:00:00:aa"
_IEEE_B = "00:17:88:01:00:00:00:bb"


@pytest.mark.parametrize(
    ("lights", "match"),
    [
        ([], "at least one"),
        ([ZigbeeLight(0, _IEEE_A, 1), ZigbeeLight(0, _IEEE_B, 2)], "own channel id"),
        ([ZigbeeLight(0, _IEEE_A, 1), ZigbeeLight(1, _IEEE_A, 1, zone=2)], "numbered 0 to 1"),
        ([ZigbeeLight(0, _IEEE_A, 1, zone=1)], "numbered 0 to 0"),
        ([ZigbeeLight(0, _IEEE_A, 1), ZigbeeLight(1, _IEEE_A, 1)], "numbered 0 to 1"),
        ([ZigbeeLight(0, _IEEE_A, 1), ZigbeeLight(1, _IEEE_A, 2, zone=1)], "network addresses"),
    ],
)
def test_rejects_invalid_light_sets(lights: list[ZigbeeLight], match: str) -> None:
    """Channel ids are unique; the zones of one light are 0..n-1 and share its NWK."""
    with pytest.raises(ValueError, match=match):
        ZigbeeEntertainmentSession("ws://127.0.0.1:1/", lights)


def test_accepts_more_than_ten_channels_and_groups_zones() -> None:
    """There is no channel limit; zones group by ieee in any case and order."""
    lights = [ZigbeeLight(i, f"00:00:00:00:00:00:00:{i:02x}", i + 1) for i in range(11)]
    lights += [ZigbeeLight(11, _IEEE_A.upper(), 0x50, zone=1), ZigbeeLight(12, _IEEE_A, 0x50)]
    session = ZigbeeEntertainmentSession("ws://127.0.0.1:1/", lights)
    assert len(session._lights) == 12
    assert [zone.channel_id for zone in session._lights[-1].zones] == [12, 11]


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
        # Reads rely on their response; only the reset asks for an APS ack.
        assert [req.aps_ack for req in sent] == [req.is_reset for req in sent]
        assert [req.cluster for req in sent if not req.zcl.cluster_specific] == [
            CLUSTER_ON_OFF,
            CLUSTER_LEVEL,
            CLUSTER_COLOR,
            CLUSTER_HUE_STATE,
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


async def test_commands_during_pacing_are_not_sent_twice(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """Commands that arrive while a frame waits for its slot go out once, in that frame."""
    a = ziggurat.add_light(_hue_light(0x3201, "00:17:88:01:00:00:00:33"))
    session = make_session([_zigbee_light(a, 0)], max_rate_hz=1 / 0.15)
    await session.start()
    session.send([RED])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    blue = LightColorCommand(channel_id=0, blue=65535)
    session.send([blue])
    await asyncio.sleep(0.03)
    session.send([blue])
    await asyncio.sleep(0.4)
    assert [_frame_fields(frame)[2] for frame in ziggurat.frames] == [
        _record(a.nwk, RED),
        _record(a.nwk, blue),
    ]


async def test_low_rate_session_sends_slowly_and_fades_over_the_gap(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """With max_rate_hz=5 frames stay 200 ms apart and the lights fade over those 200 ms."""
    a = ziggurat.add_light(_hue_light(0x3301, "00:17:88:01:00:00:00:34"))
    session = make_session([_zigbee_light(a, 0)], max_rate_hz=5)
    await session.start()
    for red in range(40):
        session.send([LightColorCommand(0, red=red * 1000)])
        await asyncio.sleep(0.02)
    starts = [frame.at for frame in ziggurat.frames]
    assert 3 <= len(starts) <= 6
    assert min(b - a for a, b in itertools.pairwise(starts)) >= 0.19
    assert abs(_frame_fields(ziggurat.frames[-1])[1] - smoothing_from_seconds(0.2)) < 5


def test_rejects_a_rate_below_one_frame_per_second() -> None:
    """Below 1 Hz the frames would come too rarely to keep the lights in entertainment mode."""
    light = ZigbeeLight(0, "00:00:00:00:00:00:00:01", 1)
    with pytest.raises(ValueError, match="max_rate_hz"):
        ZigbeeEntertainmentSession("ws://127.0.0.1:1/", [light], max_rate_hz=0.5)
    with pytest.raises(ValueError, match="max_rate_hz"):
        ZigbeeEntertainmentSession("ws://127.0.0.1:1/", [light], max_rate_hz=float("nan"))
    ZigbeeEntertainmentSession("ws://127.0.0.1:1/", [light], max_rate_hz=1)


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
        expected(CLUSTER_LEVEL, move_to_level_with_on_off(0, 200, 0)),
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


async def test_restore_continues_when_the_colour_fails(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A lost colour restore does not keep a light that was off from switching off."""
    a = ziggurat.add_light(_hue_light(0x5201, "00:17:88:01:00:00:00:55", on=False, level=90))
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    ziggurat.confirm_for = lambda req: (
        SEND_NWK_NO_ACK if req.cluster == CLUSTER_COLOR and req.zcl.cluster_specific else 0
    )
    mark = len(ziggurat.requests)
    await session.stop()
    assert [req.cluster for req in _requests_to(ziggurat, a.nwk, mark)] == [
        CLUSTER_ENTERTAINMENT,
        CLUSTER_COLOR,
        CLUSTER_LEVEL,
        CLUSTER_ON_OFF,
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


async def test_read_refused_with_a_default_response_is_unanswered_at_once(
    ziggurat: FakeZiggurat, make_session: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Default Response error to the snapshot read ends it right away: streamed, not restored."""
    monkeypatch.setattr(zigbee_session, "_REQUEST_TIMEOUT_S", 2.0)
    a = ziggurat.add_light(_hue_light(0x7201, "00:17:88:01:00:00:00:74"))
    a.read_default_status = {CLUSTER_ON_OFF: 0xC3}  # unsupported cluster
    session = make_session([_zigbee_light(a, 0)])
    started = time.monotonic()
    await session.start()
    assert time.monotonic() - started < 1.0
    assert _kinds(ziggurat, a.nwk) == ["read", "reset"]
    mark = len(ziggurat.requests)
    await session.stop()
    assert _kinds(ziggurat, a.nwk, mark) == ["reset"]


async def test_refused_balance_read_skips_the_write(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A light that refuses the balance read with a Default Response gets no balance write."""
    a = ziggurat.add_light(_hue_light(0x7202, "00:17:88:01:00:00:00:75", balance=0x00))
    a.read_default_status = {CLUSTER_ENTERTAINMENT: 0x84}  # unsupported manufacturer command
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    assert _kinds(ziggurat, a.nwk) == ["read"] * 5 + ["reset"]


async def test_success_default_response_is_not_the_answer(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A success Default Response ahead of the read response does not end the read."""
    a = ziggurat.add_light(_hue_light(0x7203, "00:17:88:01:00:00:00:76", level=120))
    a.read_default_status = {CLUSTER_LEVEL: 0x00}
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    mark = len(ziggurat.requests)
    await session.stop()
    level = [req for req in _requests_to(ziggurat, a.nwk, mark) if req.cluster == CLUSTER_LEVEL]
    assert [req.zcl_payload for req in level] == [move_to_level_with_on_off(0, 120, 4)[3:]]


async def test_default_response_for_another_command_is_not_the_answer(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A Default Response error with the read's tsn but naming another command does not end it."""
    a = ziggurat.add_light(_hue_light(0x7204, "00:17:88:01:00:00:00:77", level=120))
    a.read_default_status = {CLUSTER_LEVEL: 0x81}  # unsupported cluster command
    a.default_response_command = 0x04  # e.g. ZHA's Move to Level that reused the tsn
    session = make_session([_zigbee_light(a, 0)])
    await session.start()
    mark = len(ziggurat.requests)
    await session.stop()
    level = [req for req in _requests_to(ziggurat, a.nwk, mark) if req.cluster == CLUSTER_LEVEL]
    assert [req.zcl_payload for req in level] == [move_to_level_with_on_off(0, 120, 4)[3:]]


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


async def test_start_fails_when_no_light_acknowledges(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """With every light unreachable (e.g. switched off at the wall) start() raises."""
    gone = ziggurat.add_light(FakeLight(0x7101, "00:17:88:01:00:00:00:71", online=False))
    session = make_session([_zigbee_light(gone, 0)])
    with pytest.raises(ZigguratError, match="No light acknowledged"):
        await session.start()
    assert not session.is_streaming
    session.send([RED])
    await asyncio.sleep(0.1)
    assert ziggurat.frames == []


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


@pytest.mark.parametrize(
    ("segments", "zone_of_segment"),
    [(4, [0, 1, 1, 2]), (7, [0, 0, 1, 1, 1, 2, 2]), (3, [0, 1, 2])],
)
async def test_gradient_zones_spread_over_the_segments(
    ziggurat: FakeZiggurat,
    make_session: SessionFactory,
    segments: int,
    zone_of_segment: list[int],
) -> None:
    """Each zone gets an address; segments map to the zone under their centre and may share it."""
    signe = ziggurat.add_light(_hue_light(0x1000, "00:17:88:01:00:00:10:00", segments=segments))
    session = make_session(_zones(signe, 3))
    await session.start()
    addresses = [0x1000, 0x1001, 0x1002]
    assert signe.segment_map == [addresses[zone] for zone in zone_of_segment]

    commands = _colours(3)
    session.send(commands)
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert _frame_fields(ziggurat.frames[0])[2] == b"".join(
        _segment(address, command) for address, command in zip(addresses, commands, strict=True)
    )


async def test_gradient_start_order(ziggurat: FakeZiggurat, make_session: SessionFactory) -> None:
    """A gradient light gets the balance write, then layout read and map, then the reset."""
    signe = ziggurat.add_light(
        _hue_light(0x1100, "00:17:88:01:00:00:11:00", segments=4, balance=0x00)
    )
    spot = ziggurat.add_light(_hue_light(0x1101, "00:17:88:01:00:00:11:01", segments=4))
    session = make_session([*_zones(signe, 3), _zigbee_light(spot, 3)])
    await session.start()
    assert _kinds(ziggurat, signe.nwk) == ["read"] * 5 + ["write", "layout", "map", "reset"]
    # A gradient light given one entry streams as one light: no segment commands.
    assert _kinds(ziggurat, spot.nwk) == ["read"] * 5 + ["reset"]
    assert spot.segment_map is None


async def test_single_zone_gradient_streams_one_device_record(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A light with one entry streams a device-mode record to its own NWK."""
    signe = ziggurat.add_light(_hue_light(0x1200, "00:17:88:01:00:00:12:00", segments=4))
    session = make_session([_zigbee_light(signe, 0)])
    await session.start()
    session.send([RED])
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert _frame_fields(ziggurat.frames[0])[2] == _record(signe.nwk, RED)


async def test_zone_addresses_skip_other_lights(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """Zone addresses skip the NWK of every light in the session and addresses already taken."""
    first = ziggurat.add_light(_hue_light(0x2000, "00:17:88:01:00:00:20:00", segments=3))
    spot = ziggurat.add_light(_hue_light(0x2001, "00:17:88:01:00:00:20:01"))
    second = ziggurat.add_light(_hue_light(0x2003, "00:17:88:01:00:00:20:03", segments=2))
    session = make_session([*_zones(first, 3), _zigbee_light(spot, 3), *_zones(second, 2, 4)])
    await session.start()
    assert first.segment_map == [0x2000, 0x2002, 0x2004]
    assert second.segment_map == [0x2003, 0x2005]

    commands = _colours(6)
    session.send(commands)
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert _frame_fields(ziggurat.frames[0])[2] == (
        _segment(0x2000, commands[0])
        + _segment(0x2002, commands[1])
        + _segment(0x2004, commands[2])
        + _record(spot.nwk, commands[3])
        + _segment(0x2003, commands[4])
        + _segment(0x2005, commands[5])
    )


async def test_zone_addresses_stay_in_the_unicast_range(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """Past 0xFFF7 (the last unicast address) the allocation wraps to 0x0001."""
    signe = ziggurat.add_light(_hue_light(0xFFF6, "00:17:88:01:00:00:ff:f6", segments=3))
    session = make_session(_zones(signe, 3))
    await session.start()
    assert signe.segment_map == [0xFFF6, 0xFFF7, 0x0001]


@pytest.mark.parametrize(
    ("segments", "answers", "map_status", "sent", "reason"),
    [
        (4, False, 0, ["layout"], "did not report its segments"),
        (None, True, 0, ["layout"], "has no gradient segments"),
        (4, True, 1, ["layout", "map"], "did not take its segment map"),
    ],
    ids=["layout-timeout", "default-response", "map-refused"],
)
async def test_gradient_falls_back_to_one_colour(
    ziggurat: FakeZiggurat,
    make_session: SessionFactory,
    caplog: pytest.LogCaptureFixture,
    segments: int | None,
    *,
    answers: bool,
    map_status: int,
    sent: list[str],
    reason: str,
) -> None:
    """Without a segment map the light streams in device mode, coloured by its zone 0."""
    signe = ziggurat.add_light(_hue_light(0x3000, "00:17:88:01:00:00:30:00", segments=segments))
    signe.answers_segments = answers
    signe.map_status = map_status
    session = make_session(_zones(signe, 3))
    await session.start()
    assert session.is_streaming
    assert [kind for kind in _kinds(ziggurat, signe.nwk) if kind in ("layout", "map")] == sent
    assert reason in caplog.text

    commands = _colours(3)
    session.send(commands)
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert _frame_fields(ziggurat.frames[0])[2] == _record(signe.nwk, commands[0])


async def test_zones_without_a_segment_are_ignored(
    ziggurat: FakeZiggurat, make_session: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """With more zones than segments, a zone that no segment maps to gets no record."""
    signe = ziggurat.add_light(_hue_light(0x3100, "00:17:88:01:00:00:31:00", segments=4))
    session = make_session(_zones(signe, 5))
    await session.start()
    assert "4 segments for 5 zones" in caplog.text
    assert signe.segment_map == [0x3100, 0x3101, 0x3103, 0x3104]

    commands = _colours(5)
    session.send(commands)
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert _frame_fields(ziggurat.frames[0])[2] == b"".join(
        _segment(0x3100 + zone, commands[zone]) for zone in (0, 1, 3, 4)
    )


def _plain_lights(ziggurat: FakeZiggurat, count: int) -> list[ZigbeeLight]:
    """Add ``count`` plain lights (NWK 0x4400 on) to the fake and return their entries."""
    fakes = [_hue_light(0x4400 + i, f"00:17:88:01:00:00:44:{i:02x}") for i in range(count)]
    return [_zigbee_light(ziggurat.add_light(fake), i) for i, fake in enumerate(fakes)]


@pytest.mark.parametrize(
    ("count", "sizes"), [(10, [10]), (11, [6, 5]), (12, [6, 6]), (21, [7, 7, 7])]
)
async def test_updates_are_split_evenly_over_the_fewest_frames(
    ziggurat: FakeZiggurat, make_session: SessionFactory, count: int, sizes: list[int]
) -> None:
    """An update's records fill the fewest frames evenly, in order, with consecutive counters."""
    session = make_session(_plain_lights(ziggurat, count))
    await session.start()
    commands = _colours(count)
    session.send(commands)
    assert await wait_until(lambda: len(ziggurat.frames) == len(sizes))
    fields = [_frame_fields(frame) for frame in ziggurat.frames]
    assert [len(records) // 7 for _, _, records in fields] == sizes
    assert [counter for counter, _, _ in fields] == [fields[0][0] + i for i in range(len(sizes))]
    assert b"".join(records for _, _, records in fields) == b"".join(
        _record(0x4400 + i, command) for i, command in enumerate(commands)
    )
    assert len({frame.nwk for frame in ziggurat.frames}) == 1
    assert ziggurat.max_frames_in_flight == 1


async def test_a_frame_that_keeps_failing_loses_the_stream(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """An update fails when any of its frames fails, so delivered sibling frames mask nothing."""
    session = make_session(_plain_lights(ziggurat, 12))
    await session.start()
    second_frame = range(0x4406, 0x440C)

    def reject_second_frame(req: Unicast) -> int:
        if not req.is_frame:
            return 0
        first_record_nwk = struct.unpack_from("<H", req.zcl_payload, 6)[0]
        return 0x22 if first_record_nwk in second_frame else 0  # PayloadTooLong

    ziggurat.status_for = reject_second_frame
    started = time.monotonic()

    async def keep_sending() -> None:
        while session.is_streaming:
            session.send(_colours(12))
            await asyncio.sleep(0.01)

    await asyncio.wait_for(keep_sending(), 2)
    assert time.monotonic() - started >= zigbee_session._STREAM_LOSS_S
    assert any(
        struct.unpack_from("<H", frame.zcl_payload, 6)[0] == 0x4400
        for frame in ziggurat.frames
        if frame.at >= started
    )


async def test_keepalive_resends_every_frame_of_the_tick(
    ziggurat: FakeZiggurat, make_session: SessionFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without new commands both frames of the last update repeat with fresh counters."""
    monkeypatch.setattr(zigbee_session, "_KEEPALIVE_S", 0.05)
    session = make_session(_plain_lights(ziggurat, 12))
    await session.start()
    session.send(_colours(12))
    assert await wait_until(lambda: len(ziggurat.frames) >= 6)
    fields = [_frame_fields(frame) for frame in ziggurat.frames[:6]]
    assert [records for _, _, records in fields[2:]] == [fields[0][2], fields[1][2]] * 2
    assert [counter for counter, _, _ in fields] == [fields[0][0] + i for i in range(6)]


async def test_rate_cap_paces_updates_not_frames(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """The frames of one update go back to back; updates stay 1 / max_rate_hz apart."""
    session = make_session(_plain_lights(ziggurat, 12), max_rate_hz=5)
    await session.start()
    for red in range(30):
        session.send([LightColorCommand(i, red=red * 1000 + i) for i in range(12)])
        await asyncio.sleep(0.02)
    starts = [frame.at for frame in ziggurat.frames]
    ticks = len(starts) // 2
    assert ticks >= 2
    assert max(starts[2 * i + 1] - starts[2 * i] for i in range(ticks)) < 0.1
    assert min(starts[2 * i + 2] - starts[2 * i] for i in range(ticks - 1)) >= 0.19


async def test_gradient_light_is_captured_and_restored_once(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A light streamed in three zones is read and restored once, not once per zone."""
    signe = ziggurat.add_light(_hue_light(0x5500, "00:17:88:01:00:00:55:00", segments=3))
    session = make_session(_zones(signe, 3))
    await session.start()
    assert _kinds(ziggurat, signe.nwk).count("read") == 5
    session.send(_colours(3))
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    mark = len(ziggurat.requests)
    await session.stop()
    assert [req.cluster for req in _requests_to(ziggurat, signe.nwk, mark)] == [
        CLUSTER_ENTERTAINMENT,
        CLUSTER_COLOR,
        CLUSTER_LEVEL,
    ]


async def test_stop_restores_the_gradient_after_colour_and_level(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A light that showed a gradient gets it back after its colour and level, before any off."""
    lit = ziggurat.add_light(
        _hue_light(0x5601, "00:17:88:01:00:00:56:01", segments=5, hue_state=_SIGNE_STATE)
    )
    was_off = ziggurat.add_light(
        _hue_light(0x5602, "00:17:88:01:00:00:56:02", on=False, hue_state=_SIGNE_STATE)
    )
    session = make_session([*_zones(lit, 2), _zigbee_light(was_off, 2)])
    await session.start()
    for fake in (lit, was_off):
        [read] = [
            req for req in _requests_to(ziggurat, fake.nwk) if req.cluster == CLUSTER_HUE_STATE
        ]
        assert (read.zcl.cluster_specific, read.zcl.manufacturer, read.zcl.command) == (
            False,
            0x100B,
            0x00,
        )
        assert read.zcl_payload == bytes.fromhex("0200")
    session.send(_colours(3))
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    mark = len(ziggurat.requests)
    await session.stop()

    def restored(nwk: int) -> list[tuple[int, int, int | None, int, bytes]]:
        sent = _requests_to(ziggurat, nwk, mark)
        assert all(req.aps_ack for req in sent)
        assert sent[0].is_reset
        return [_command(req.cluster, req.asdu) for req in sent[1:]]

    def gradient(transition: str) -> tuple[int, int, int | None, int, bytes]:
        payload = bytes.fromhex("5001" + transition + _SIGNE_COLOURS + "2800")
        return (CLUSTER_HUE_STATE, 0x15, 0x100B, 0x00, payload)

    assert restored(lit.nwk) == [
        _command(CLUSTER_COLOR, move_to_color(0, 0x6000, 0x5000, 4)),
        _command(CLUSTER_LEVEL, move_to_level_with_on_off(0, 200, 4)),
        gradient("0400"),
    ]
    assert restored(was_off.nwk) == [
        _command(CLUSTER_COLOR, move_to_color(0, 0x6000, 0x5000, 0)),
        _command(CLUSTER_LEVEL, move_to_level_with_on_off(0, 200, 0)),
        gradient("0000"),
        _command(CLUSTER_ON_OFF, off(0)),
    ]


async def test_lights_without_a_gradient_get_no_gradient_restore(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A light without a gradient state, or that cannot report one, is restored as before."""
    plain = ziggurat.add_light(_hue_light(0x5701, "00:17:88:01:00:00:57:01"))
    one_colour = ziggurat.add_light(
        _hue_light(0x5702, "00:17:88:01:00:00:57:02", hue_state=bytes.fromhex("0b00010460b09c4e"))
    )
    refused = ziggurat.add_light(_hue_light(0x5703, "00:17:88:01:00:00:57:03"))
    refused.read_default_status = {CLUSTER_HUE_STATE: 0xC3}  # unsupported cluster
    malformed = ziggurat.add_light(
        _hue_light(0x5704, "00:17:88:01:00:00:57:04", hue_state=_SIGNE_STATE[:-8])
    )
    fakes = (plain, one_colour, refused, malformed)
    session = make_session([_zigbee_light(fake, i) for i, fake in enumerate(fakes)])
    await session.start()
    session.send(_colours(4))
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    mark = len(ziggurat.requests)
    await session.stop()
    for fake in fakes:
        assert [req.cluster for req in _requests_to(ziggurat, fake.nwk, mark)] == [
            CLUSTER_ENTERTAINMENT,
            CLUSTER_COLOR,
            CLUSTER_LEVEL,
        ]


async def test_refused_gradient_restore_is_a_warning(
    ziggurat: FakeZiggurat, make_session: SessionFactory, caplog: pytest.LogCaptureFixture
) -> None:
    """A gradient the light does not take is logged, and a light that was off still goes off."""
    signe = ziggurat.add_light(
        _hue_light(0x5801, "00:17:88:01:00:00:58:01", on=False, hue_state=_SIGNE_STATE)
    )
    session = make_session([_zigbee_light(signe, 0)])
    await session.start()
    ziggurat.confirm_for = lambda req: SEND_NWK_NO_ACK if req.cluster == CLUSTER_HUE_STATE else 0
    mark = len(ziggurat.requests)
    await session.stop()
    assert "Could not restore the gradient of light 0" in caplog.text
    assert [req.cluster for req in _requests_to(ziggurat, signe.nwk, mark)] == [
        CLUSTER_ENTERTAINMENT,
        CLUSTER_COLOR,
        CLUSTER_LEVEL,
        CLUSTER_HUE_STATE,
        CLUSTER_ON_OFF,
    ]


async def test_restart_sends_the_segment_map_again(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """Every start sends the segment map before the reset; the snapshot is kept."""
    signe = ziggurat.add_light(_hue_light(0x5600, "00:17:88:01:00:00:56:00", segments=4))
    session = make_session(_zones(signe, 3))
    await session.start()
    await ziggurat.drop()
    assert await wait_until(lambda: not session.is_streaming)
    signe.segment_map = None  # e.g. the light lost power

    mark = len(ziggurat.requests)
    await session.start()
    assert _kinds(ziggurat, signe.nwk, mark) == ["layout", "map", "reset"]
    assert signe.segment_map == [0x5600, 0x5601, 0x5601, 0x5602]


async def test_restart_falls_back_to_one_colour_without_a_map(
    ziggurat: FakeZiggurat, make_session: SessionFactory
) -> None:
    """A light mapped on the first start streams one colour after a restart that cannot map it."""
    signe = ziggurat.add_light(_hue_light(0x5700, "00:17:88:01:00:00:57:00", segments=4))
    session = make_session(_zones(signe, 3))
    await session.start()
    commands = _colours(3)
    session.send(commands)
    assert await wait_until(lambda: len(ziggurat.frames) == 1)
    assert _frame_fields(ziggurat.frames[0])[2] == b"".join(
        _segment(0x5700 + zone, commands[zone]) for zone in range(3)
    )

    await ziggurat.drop()
    assert await wait_until(lambda: not session.is_streaming)
    signe.answers_segments = False
    await session.start()
    mark = len(ziggurat.frames)
    session.send(commands)
    assert await wait_until(lambda: len(ziggurat.frames) > mark)
    assert _frame_fields(ziggurat.frames[-1])[2] == _record(signe.nwk, commands[0])


async def test_read_segment_counts(ziggurat: FakeZiggurat) -> None:
    """Gradient lights report their segments, others 1; silent lights are left out."""
    gradient = ziggurat.add_light(FakeLight(0x6001, "00:17:88:01:00:00:60:01", segments=7))
    plain = ziggurat.add_light(FakeLight(0x6002, "00:17:88:01:00:00:60:02"))
    silent = ziggurat.add_light(
        FakeLight(0x6003, "00:17:88:01:00:00:60:03", segments=4, answers_segments=False)
    )
    nwks = [gradient.nwk, plain.nwk, silent.nwk, 0x6004]  # 0x6004: no such device
    assert await read_segment_counts(ziggurat.url, nwks) == {gradient.nwk: 7, plain.nwk: 1}
    assert all(req.is_segment_command and not req.aps_ack for req in ziggurat.requests)
    assert await wait_until(lambda: not ziggurat._sockets)


async def test_read_segment_counts_raises_when_unreachable(ziggurat: FakeZiggurat) -> None:
    """An unreachable ziggurat-server raises ZigguratError."""
    url = ziggurat.url
    await ziggurat.close()
    with pytest.raises(ZigguratError, match="Cannot connect"):
        await read_segment_counts(url, [0x6001])

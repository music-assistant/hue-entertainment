"""A local fake ziggurat-server that enforces the client's safety rules and simulates lights."""

from __future__ import annotations

import asyncio
import struct
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestServer

from hue_entertainment.zigbee.frames import CLUSTER_ENTERTAINMENT, ZclHeader, parse_zcl

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

_REQUEST, _RESPONSE, _NOTIFICATION = 0, 1, 3
_SEND_UNICAST = struct.Struct("<BH8sHHBBBBbBH")
_RECEIVED_APS = struct.Struct("<HHBHHHBBBbH")

SEND_SUCCESS = 0
SEND_NWK_NO_ACK = 4
SEND_APS_ACK_TIMEOUT = 7
UNSUPPORTED_ATTRIBUTE = 0x86


@dataclass
class FakeLight:
    """A simulated light: answers attribute reads/writes when online."""

    nwk: int
    ieee: str
    rssi: int = -60
    online: bool = True
    # (cluster, attribute) -> (ZCL data type, value)
    attributes: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)


@dataclass
class Unicast:
    """One SendUnicast request as received by the fake server."""

    rid: int
    nwk: int
    cluster: int
    dst_ep: int
    aps_ack: bool
    asdu: bytes
    at: float

    @property
    def zcl(self) -> ZclHeader:
        """Return the ZCL header of the payload."""
        return parse_zcl(self.asdu)[0]

    @property
    def zcl_payload(self) -> bytes:
        """Return the ZCL command payload."""
        return parse_zcl(self.asdu)[1]

    @property
    def is_frame(self) -> bool:
        """Return True for an entertainment frame (0xFC01 command 0x01)."""
        return self.cluster == CLUSTER_ENTERTAINMENT and self.zcl.command == 0x01

    @property
    def is_reset(self) -> bool:
        """Return True for an entertainment reset (0xFC01 command 0x03)."""
        return (
            self.cluster == CLUSTER_ENTERTAINMENT
            and self.zcl.cluster_specific
            and self.zcl.command == 0x03
        )


class FakeZiggurat:
    """Speaks the Ziggurat wire protocol v2 to one or more clients on 127.0.0.1."""

    def __init__(self) -> None:
        """Initialize with protocol version 2 and no lights."""
        self.version = 2
        self.send_hello = True
        self.close_after_hello = False
        self.lights: dict[int, FakeLight] = {}
        self.requests: list[Unicast] = []
        self.violations: list[str] = []
        self.connections = 0
        self.confirm_delay = 0.002
        # Response status per request (0 = accepted).
        self.status_for: Callable[[Unicast], int] = lambda _req: 0
        # SendConfirm status per accepted request; None never confirms.
        self.confirm_for: Callable[[Unicast], int | None] = lambda _req: SEND_SUCCESS
        self.hold_frames = False
        self.held: list[tuple[web.WebSocketResponse, Unicast]] = []
        self.frames_in_flight = 0
        self.max_frames_in_flight = 0
        self._sockets: set[web.WebSocketResponse] = set()
        self._tasks: set[asyncio.Task[None]] = set()
        app = web.Application()
        app.router.add_get("/", self._handler)
        self._server = TestServer(app)

    @property
    def url(self) -> str:
        """Return the websocket URL of the fake server."""
        return f"ws://127.0.0.1:{self._server.port}/"

    @property
    def frames(self) -> list[Unicast]:
        """Return the entertainment frames received so far."""
        return [req for req in self.requests if req.is_frame]

    async def start(self) -> None:
        """Start listening."""
        await self._server.start_server()

    async def close(self) -> None:
        """Drop all clients and stop listening."""
        for task in list(self._tasks):
            task.cancel()
        for ws in list(self._sockets):
            await ws.close()
        await self._server.close()

    def add_light(self, light: FakeLight) -> FakeLight:
        """Add a simulated light."""
        self.lights[light.nwk] = light
        return light

    async def drop(self) -> None:
        """Close every client connection from the server side."""
        for ws in list(self._sockets):
            await ws.close()

    async def release_held(self) -> None:
        """Confirm the frames held back by ``hold_frames``."""
        held, self.held = self.held, []
        for ws, req in held:
            await self._complete(ws, req, delay=0)

    async def inject_joined(self, nwk: int, ieee: str) -> None:
        """Send a DeviceJoined notification to every client."""
        raw = bytes(reversed(bytes.fromhex(ieee.replace(":", ""))))
        body = struct.pack("<H8sHB", nwk, raw, 0x0000, 0x01)
        for ws in list(self._sockets):
            await ws.send_bytes(_header(0x20, _NOTIFICATION, 0) + body)

    async def _handler(self, request: web.Request) -> web.WebSocketResponse:
        """Serve one client connection."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._sockets.add(ws)
        self.connections += 1
        try:
            if self.send_hello:
                await ws.send_bytes(_header(0x00, _NOTIFICATION, 0) + bytes([self.version, 0x01]))
            if self.close_after_hello:
                await ws.close()
                return ws
            async for msg in ws:
                if msg.type is WSMsgType.BINARY:
                    await self._on_request(ws, msg.data)
        finally:
            self._sockets.discard(ws)
        return ws

    async def _on_request(self, ws: web.WebSocketResponse, data: bytes) -> None:
        """Validate a request against the safety rules and answer it."""
        command = data[0]
        word = int.from_bytes(data[1:3], "little")
        frame_type, rid = word & 0x03, word >> 2
        if command != 0x30 or frame_type != _REQUEST:
            self.violations.append(f"forbidden request cmd=0x{command:02x} type={frame_type}")
            return
        (flags, nwk, eui, profile, cluster, src_ep, dst_ep, _seq, radius, prio, route, length) = (
            _SEND_UNICAST.unpack_from(data, 3)
        )
        asdu = data[3 + _SEND_UNICAST.size :]
        checks = {
            "priority": prio == -2,
            "dst_ep": dst_ep != 0,
            "radius": radius == 30,
            "route": route == 0,
            "profile": profile == 0x0104,
            "src_ep": src_ep == 0x01,
            "flags": flags & ~0x02 == 0,
            "eui64": eui == bytes(8),
            "length": len(asdu) == length,
        }
        for name, ok in checks.items():
            if not ok:
                self.violations.append(f"bad {name} in request to 0x{nwk:04x}")
        req = Unicast(rid, nwk, cluster, dst_ep, bool(flags & 0x02), asdu, time.monotonic())
        self.requests.append(req)
        status = self.status_for(req)
        await ws.send_bytes(_header(0x30, _RESPONSE, rid) + bytes([status]))
        if status:
            return
        if req.is_frame:
            self.frames_in_flight += 1
            self.max_frames_in_flight = max(self.max_frames_in_flight, self.frames_in_flight)
            if self.hold_frames:
                self.held.append((ws, req))
                return
        self._spawn(self._complete(ws, req, delay=self.confirm_delay))

    async def _complete(self, ws: web.WebSocketResponse, req: Unicast, *, delay: float) -> None:
        """Send the confirms (and a light's reply) for an accepted request."""
        await asyncio.sleep(delay)
        if req.is_frame:
            self.frames_in_flight -= 1
        status = self.confirm_for(req)
        if status is None or ws.closed:
            return
        await ws.send_bytes(_header(0x11, _NOTIFICATION, req.rid) + bytes([status]))
        if status != SEND_SUCCESS:
            return
        light = self.lights.get(req.nwk)
        online = light is not None and light.online
        if req.aps_ack:
            verdict = SEND_SUCCESS if online else SEND_APS_ACK_TIMEOUT
            await ws.send_bytes(_header(0x12, _NOTIFICATION, req.rid) + bytes([verdict]))
        if light is None or not online:
            return
        reply = _reply(light, req)
        if reply is not None:
            body = _RECEIVED_APS.pack(
                light.nwk, 0, 0, 0, 0x0104, req.cluster, 0x0B, 0x01, 200, light.rssi, len(reply)
            )
            await ws.send_bytes(_header(0x10, _NOTIFICATION, 0) + body + reply)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a background task that is cancelled on close."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def wait_until(predicate: Callable[[], bool], max_wait: float = 2.0) -> bool:
    """Poll ``predicate`` until true or timeout (avoids flaky fixed sleeps)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()


def _header(command: int, frame_type: int, rid: int) -> bytes:
    """Encode a Ziggurat frame header."""
    return struct.pack("<BH", command, (rid << 2) | frame_type)


def _reply(light: FakeLight, req: Unicast) -> bytes | None:
    """Answer a global Read/Write Attributes request like a light would."""
    header, payload = parse_zcl(req.asdu)
    if header.cluster_specific:
        return None
    mfr = header.manufacturer is not None
    response_fc = 0x18 | (0x04 if mfr else 0)
    prefix = (
        struct.pack("<BHB", response_fc, header.manufacturer, header.tsn)
        if mfr
        else bytes([response_fc, header.tsn])
    )
    if header.command == 0x00:
        records = b""
        for (attribute,) in struct.iter_unpack("<H", payload):
            value = light.attributes.get((req.cluster, attribute))
            if value is None:
                records += struct.pack("<HB", attribute, UNSUPPORTED_ATTRIBUTE)
                continue
            data_type, raw = value
            size = 2 if data_type in (0x19, 0x21, 0x29) else 1
            records += struct.pack("<HBB", attribute, 0, data_type) + raw.to_bytes(size, "little")
        return prefix + bytes([0x01]) + records
    if header.command == 0x02:
        attribute, data_type, raw = struct.unpack_from("<HBB", payload)
        light.attributes[(req.cluster, attribute)] = (data_type, raw)
        return prefix + bytes([0x04, 0x00])
    return None

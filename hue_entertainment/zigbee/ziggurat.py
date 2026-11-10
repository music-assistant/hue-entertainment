"""
Minimal client for the Ziggurat websocket server (binary wire protocol v2).

It connects as an extra client next to Home Assistant's ZHA and can only send unicast APS
frames to a non-ZDO endpoint, at background priority so everything ZHA sends goes first. No
other Ziggurat request exists in this module, so it can never reconfigure the stack or the
network.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import struct
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import aiohttp

from .frames import HUE_ENDPOINT, PROFILE_HOME_AUTOMATION

if TYPE_CHECKING:
    from collections.abc import Callable

LOGGER = logging.getLogger(__name__)

PROTOCOL_VERSION: Final[int] = 2
CONNECT_TIMEOUT_S: Final[float] = 5.0
RESPONSE_TIMEOUT_S: Final[float] = 5.0

# Response status codes (the first byte of every response).
STATUS_OK: Final[int] = 0x00
STATUS_NOT_CONFIGURED: Final[int] = 0x10
STATUS_NOT_STARTED: Final[int] = 0x11

# Send statuses carried by SendConfirm / ApsAckConfirm.
SEND_SUCCESS: Final[int] = 0
SEND_CANCELLED: Final[int] = 11

_FRAME_REQUEST: Final[int] = 0
_FRAME_RESPONSE: Final[int] = 1
_FRAME_NOTIFICATION: Final[int] = 3

_CMD_SEND_UNICAST: Final[int] = 0x30
_NTF_HELLO: Final[int] = 0x00
_NTF_RECEIVED_APS: Final[int] = 0x10
_NTF_SEND_CONFIRM: Final[int] = 0x11
_NTF_APS_ACK_CONFIRM: Final[int] = 0x12
_NTF_DEVICE_JOINED: Final[int] = 0x20

_SRC_ENDPOINT: Final[int] = 0x01
_RADIUS: Final[int] = 30
_PRIORITY_BACKGROUND: Final[int] = -2
_ROUTE_STACK_DECIDES: Final[int] = 0
_FLAG_APS_ACK: Final[int] = 0x02
_MAX_REQUEST_ID: Final[int] = 0x3FFF
# 0xFFF8 and above are broadcast addresses.
_MAX_UNICAST_NWK: Final[int] = 0xFFF7

# flags, nwk, eui64, profile, cluster, src_ep, dst_ep, aps_seq, radius, priority, route, asdu_len
_SEND_UNICAST: Final = struct.Struct("<BH8sHHBBBBbBH")
# source, destination, flags, group, profile, cluster, src_ep, dst_ep, lqi, rssi, data_len
_RECEIVED_APS: Final = struct.Struct("<HHBHHHBBBbH")
# nwk, ieee (least significant byte first), parent, flags
_DEVICE_JOINED: Final = struct.Struct("<H8sHB")

_STATUS_NAMES: Final[dict[int, str]] = {
    0x01: "MalformedPayload",
    0x02: "UnknownCommand",
    0x03: "InvalidRequest",
    STATUS_NOT_CONFIGURED: "NotConfigured",
    STATUS_NOT_STARTED: "NotStarted",
    0x12: "AlreadyStarted",
    0x20: "RateLimited",
    0x21: "BudgetExhausted",
    0x22: "PayloadTooLong",
    0x23: "SecurityUnavailable",
    0x24: "NoRoute",
    0x30: "RadioError",
}


class ZigguratError(Exception):
    """A Ziggurat connection failure, or a request the server rejected."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """
        Initialize the error.

        :param message: Human-readable description.
        :param status: The response status of a rejected request; None for connection and
            protocol failures.
        """
        super().__init__(message)
        self.status = status

    @property
    def stack_down(self) -> bool:
        """Return True when the Zigbee stack is not running (e.g. while ZHA restarts it)."""
        return self.status in (STATUS_NOT_CONFIGURED, STATUS_NOT_STARTED)


@dataclass(frozen=True)
class ReceivedAps:
    """
    An APS frame the coordinator received from the network.

    :param source: Network address of the sender.
    :param profile: Profile id.
    :param cluster: Cluster id.
    :param src_ep: Sender endpoint.
    :param dst_ep: Destination endpoint on the coordinator.
    :param lqi: Link quality of the last hop.
    :param rssi: Signal strength of the last hop, in dBm.
    :param data: The APS payload (a ZCL frame for ZCL clusters).
    """

    source: int
    profile: int
    cluster: int
    src_ep: int
    dst_ep: int
    lqi: int
    rssi: int
    data: bytes


@dataclass(frozen=True)
class DeviceJoined:
    """
    A device joined or rejoined the network.

    :param nwk: The device's (new) network address.
    :param ieee: The device's IEEE address, e.g. ``"00:17:88:01:0c:bf:17:c8"``.
    :param parent: Network address of its parent.
    """

    nwk: int
    ieee: str
    parent: int


@dataclass(frozen=True)
class UnicastSend:
    """
    Delivery tracking of a unicast that Ziggurat accepted.

    Both futures resolve to a send status: ``SEND_SUCCESS`` or a failure code, and
    ``SEND_CANCELLED`` when the connection closes before the verdict arrives.

    :param confirmed: Verdict of the first hop (a MAC-level ack).
    :param acked: End-to-end APS ack verdict; only for sends with ``aps_ack``.
    """

    confirmed: asyncio.Future[int]
    acked: asyncio.Future[int] | None


class ZigguratClient:
    """Websocket client for ziggurat-server that can only send unicast frames."""

    def __init__(
        self,
        url: str,
        *,
        on_received: Callable[[ReceivedAps], None] | None = None,
        on_joined: Callable[[DeviceJoined], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        """
        Initialize the client (no network I/O until :meth:`connect`).

        Callbacks run on the event loop and must not block.

        :param url: Websocket URL of ziggurat-server, e.g. ``ws://homeassistant.local:9999``.
        :param on_received: Called for every APS frame the coordinator receives.
        :param on_joined: Called when a device joins or rejoins the network.
        :param on_disconnect: Called when an established connection drops (not on :meth:`close`).
        """
        self._url = url
        self._on_received = on_received
        self._on_joined = on_joined
        self._on_disconnect = on_disconnect
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task[None] | None = None
        self._connected = False
        self._request_ids = itertools.cycle(range(1, _MAX_REQUEST_ID + 1))
        self._responses: dict[int, asyncio.Future[bytes]] = {}
        self._confirms: dict[int, asyncio.Future[int]] = {}
        self._acks: dict[int, asyncio.Future[int]] = {}

    @property
    def url(self) -> str:
        """Return the ziggurat-server URL."""
        return self._url

    @property
    def connected(self) -> bool:
        """Return True while connected to a server speaking a supported protocol version."""
        return self._connected

    async def connect(self, timeout: float = CONNECT_TIMEOUT_S) -> None:
        """
        Connect and wait for the server's Hello (a no-op when already connected).

        :param timeout: Seconds to wait for the connection and the Hello.
        :raises ZigguratError: When the server is unreachable or speaks another protocol version.
        """
        if self._connected:
            return
        await self.close()
        hello: asyncio.Future[tuple[int, bool]] = asyncio.get_running_loop().create_future()
        self._session = aiohttp.ClientSession()
        try:
            async with asyncio.timeout(timeout):
                self._ws = ws = await self._session.ws_connect(self._url)
                self._reader = asyncio.create_task(self._read_loop(ws, hello))
                version, configured = await hello
        except (aiohttp.ClientError, OSError, TimeoutError, ZigguratError) as err:
            await self.close()
            msg = f"Cannot connect to Ziggurat at {self._url}: {err or type(err).__name__}"
            raise ZigguratError(msg) from err
        except asyncio.CancelledError:
            await self.close()
            raise
        if version != PROTOCOL_VERSION:
            await self.close()
            msg = f"Unsupported Ziggurat protocol version {version} (need {PROTOCOL_VERSION})"
            raise ZigguratError(msg)
        self._connected = True
        LOGGER.debug("Connected to Ziggurat at %s (stack configured: %s)", self._url, configured)

    async def close(self) -> None:
        """Close the connection; pending sends resolve as cancelled. Safe to call repeatedly."""
        self._connected = False
        ws, self._ws = self._ws, None
        reader, self._reader = self._reader, None
        session, self._session = self._session, None
        if reader is not None:
            reader.cancel()
            with suppress(asyncio.CancelledError):
                await reader
        if ws is not None:
            await ws.close()
        if session is not None:
            await session.close()
        self._fail_pending()

    async def send_unicast(
        self,
        nwk: int,
        cluster: int,
        asdu: bytes,
        *,
        aps_ack: bool,
        dst_ep: int = HUE_ENDPOINT,
    ) -> UnicastSend:
        """
        Send one unicast APS frame (Home Automation profile) at background priority.

        Returns once Ziggurat accepted the frame for transmission; the delivery verdicts
        arrive later on the returned :class:`UnicastSend`.

        :param nwk: Network address of the destination device.
        :param cluster: Cluster id.
        :param asdu: The APS payload, typically a ZCL frame.
        :param aps_ack: Request an end-to-end APS acknowledgement.
        :param dst_ep: Destination endpoint; the ZDO endpoint 0 is refused.
        :raises ValueError: For a ZDO endpoint or a broadcast address.
        :raises ZigguratError: When not connected, the connection drops, or Ziggurat rejects
            the frame (``status`` holds the reason).
        """
        if dst_ep == 0:
            msg = "Sending to the ZDO endpoint is not allowed"
            raise ValueError(msg)
        if not 0 <= nwk <= _MAX_UNICAST_NWK:
            msg = f"Not a unicast address: 0x{nwk:04x}"
            raise ValueError(msg)
        ws = self._ws
        if not self._connected or ws is None:
            msg = "Not connected to Ziggurat"
            raise ZigguratError(msg)

        rid = next(self._request_ids)
        payload = _SEND_UNICAST.pack(
            _FLAG_APS_ACK if aps_ack else 0,
            nwk,
            bytes(8),
            PROFILE_HOME_AUTOMATION,
            cluster,
            _SRC_ENDPOINT,
            dst_ep,
            0,
            _RADIUS,
            _PRIORITY_BACKGROUND,
            _ROUTE_STACK_DECIDES,
            len(asdu),
        )
        frame = _header(_CMD_SEND_UNICAST, _FRAME_REQUEST, rid) + payload + asdu
        loop = asyncio.get_running_loop()
        confirmed: asyncio.Future[int] = loop.create_future()
        acked: asyncio.Future[int] | None = loop.create_future() if aps_ack else None
        # Register the verdicts before sending: a confirm may arrive before the response.
        self._track(self._confirms, rid, confirmed)
        if acked is not None:
            self._track(self._acks, rid, acked)
        try:
            body = await self._request(ws, rid, frame)
        except BaseException:
            self._forget(rid)
            raise
        status = body[0] if body else 0xFF
        if status != STATUS_OK:
            self._forget(rid)
            msg = f"Ziggurat rejected the send: {_STATUS_NAMES.get(status, f'0x{status:02x}')}"
            raise ZigguratError(msg, status)
        return UnicastSend(confirmed, acked)

    # -- internals --

    async def _request(self, ws: aiohttp.ClientWebSocketResponse, rid: int, frame: bytes) -> bytes:
        """Send a request frame and return the body of its response."""
        response: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()
        self._responses[rid] = response
        try:
            await ws.send_bytes(frame)
            async with asyncio.timeout(RESPONSE_TIMEOUT_S):
                return await response
        except TimeoutError as err:
            msg = "No response from Ziggurat"
            raise ZigguratError(msg) from err
        except (ConnectionError, aiohttp.ClientError) as err:
            msg = f"Connection to Ziggurat lost: {err}"
            raise ZigguratError(msg) from err
        finally:
            self._responses.pop(rid, None)

    async def _read_loop(
        self, ws: aiohttp.ClientWebSocketResponse, hello: asyncio.Future[tuple[int, bool]]
    ) -> None:
        """Dispatch incoming frames until the connection closes."""
        try:
            async for msg in ws:
                if msg.type is aiohttp.WSMsgType.BINARY:
                    self._handle_frame(msg.data, hello)
        finally:
            lost = self._connected and self._ws is ws
            if lost:
                self._connected = False
            if not hello.done():
                hello.set_exception(ZigguratError("Connection closed before Hello"))
            self._fail_pending()
            if lost:
                LOGGER.warning("Connection to Ziggurat at %s lost", self._url)
                if self._on_disconnect is not None:
                    _call(self._on_disconnect)

    def _handle_frame(self, data: bytes, hello: asyncio.Future[tuple[int, bool]]) -> None:
        """Route a response to its request, or handle a notification."""
        if len(data) < 3:
            return
        command = data[0]
        word = int.from_bytes(data[1:3], "little")
        frame_type, rid, body = word & 0x03, word >> 2, data[3:]
        if frame_type == _FRAME_RESPONSE:
            response = self._responses.pop(rid, None)
            if response is not None and not response.done():
                response.set_result(body)
        elif frame_type == _FRAME_NOTIFICATION:
            try:
                self._handle_notification(command, rid, body, hello)
            except (struct.error, IndexError):
                LOGGER.debug("Ignoring malformed Ziggurat notification 0x%02x", command)

    def _handle_notification(
        self, command: int, rid: int, body: bytes, hello: asyncio.Future[tuple[int, bool]]
    ) -> None:
        """Handle one notification frame."""
        if command == _NTF_HELLO:
            if not hello.done():
                hello.set_result((body[0], bool(body[1] & 0x01)))
        elif command == _NTF_SEND_CONFIRM:
            status = body[0]
            _resolve(self._confirms.pop(rid, None), status)
            if status != SEND_SUCCESS:
                # A failed first hop is final: no ApsAckConfirm follows.
                _resolve(self._acks.pop(rid, None), status)
        elif command == _NTF_APS_ACK_CONFIRM:
            _resolve(self._acks.pop(rid, None), body[0])
        elif command == _NTF_RECEIVED_APS and self._on_received is not None:
            source, _dst, _flags, _group, profile, cluster, src_ep, dst_ep, lqi, rssi, length = (
                _RECEIVED_APS.unpack_from(body)
            )
            data = body[_RECEIVED_APS.size : _RECEIVED_APS.size + length]
            received = ReceivedAps(source, profile, cluster, src_ep, dst_ep, lqi, rssi, data)
            _call(self._on_received, received)
        elif command == _NTF_DEVICE_JOINED and self._on_joined is not None:
            nwk, ieee, parent, _flags = _DEVICE_JOINED.unpack_from(body)
            ieee_str = ":".join(f"{byte:02x}" for byte in reversed(ieee))
            _call(self._on_joined, DeviceJoined(nwk, ieee_str, parent))

    def _track(
        self, pending: dict[int, asyncio.Future[int]], rid: int, future: asyncio.Future[int]
    ) -> None:
        """Register a verdict future, dropping a stale one left under the same request id."""
        stale = pending.pop(rid, None)
        if stale is not None:
            stale.cancel()
        pending[rid] = future

    def _forget(self, rid: int) -> None:
        """Drop the verdict futures of a send that was not accepted."""
        for pending in (self._confirms, self._acks):
            future = pending.pop(rid, None)
            if future is not None:
                future.cancel()

    def _fail_pending(self) -> None:
        """Fail outstanding requests and resolve outstanding verdicts as cancelled."""
        responses, self._responses = self._responses, {}
        for response in responses.values():
            if not response.done():
                response.set_exception(ZigguratError("Connection to Ziggurat lost"))
        for pending in (self._confirms, self._acks):
            for future in pending.values():
                _resolve(future, SEND_CANCELLED)
            pending.clear()


def _header(command: int, frame_type: int, rid: int) -> bytes:
    """Encode the 3-byte frame header."""
    return struct.pack("<BH", command, (rid << 2) | frame_type)


def _resolve(future: asyncio.Future[int] | None, status: int) -> None:
    """Resolve a verdict future unless it is gone or already done."""
    if future is not None and not future.done():
        future.set_result(status)


def _call(callback: Callable[..., None], *args: object) -> None:
    """Invoke a consumer callback, logging instead of propagating its errors."""
    try:
        callback(*args)
    except Exception:
        LOGGER.exception("Error in Ziggurat callback")

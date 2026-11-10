"""High-level async session that streams Hue Entertainment frames over Zigbee via Ziggurat."""

from __future__ import annotations

import asyncio
import itertools
import logging
import statistics
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, TypeVar, cast

from hue_entertainment.color import GAMUT_C, rgb_to_xy
from hue_entertainment.session import DEFAULT_IDLE_TIMEOUT_S

from .frames import (
    ATTR_COLOR_MODE,
    ATTR_COLOR_TEMPERATURE,
    ATTR_CURRENT_LEVEL,
    ATTR_CURRENT_X,
    ATTR_CURRENT_Y,
    ATTR_ENTERTAINMENT_BALANCE,
    ATTR_ON_OFF,
    CLUSTER_COLOR,
    CLUSTER_ENTERTAINMENT,
    CLUSTER_LEVEL,
    CLUSTER_ON_OFF,
    CMD_SEGMENT_LAYOUT,
    CMD_SEGMENT_MAP,
    COLOR_MODE_TEMPERATURE,
    ENTERTAINMENT_BALANCE_VALUE,
    HUE_MANUFACTURER_CODE,
    MAX_BRIGHTNESS,
    MAX_RECORDS,
    ZCL_CMD_DEFAULT_RESPONSE,
    ZCL_CMD_READ_ATTRIBUTES_RESPONSE,
    entertainment_frame,
    entertainment_reset,
    light_record,
    move_to_color,
    move_to_color_temperature,
    move_to_level_with_on_off,
    off,
    parse_default_response,
    parse_read_attributes_response,
    parse_segment_layout,
    parse_segment_map_response,
    parse_zcl,
    read_attributes,
    segment_layout_request,
    segment_map,
    segment_record,
    smoothing_from_seconds,
    write_attribute_uint8,
)
from .ziggurat import MAX_UNICAST_NWK, SEND_SUCCESS, ZigguratClient, ZigguratError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Sequence

    from hue_entertainment.models import LightColorCommand, ZigbeeLight

    from .ziggurat import DeviceJoined, ReceivedAps

LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# The Hue bridge streams at 25 Hz too; a cap leaves airtime for ZHA (each frame is also relayed).
DEFAULT_MAX_RATE_HZ: Final[float] = 25.0
# Resend the last frames after this long without new ones; lights otherwise leave
# entertainment mode a few seconds after the frames stop.
_KEEPALIVE_S: Final[float] = 1.0
_FRAME_CONFIRM_TIMEOUT_S: Final[float] = 1.0
# Updates with a failing frame for this long mean the stream is lost.
_STREAM_LOSS_S: Final[float] = 1.0
_REQUEST_TIMEOUT_S: Final[float] = 3.0
_RESTORE_TRANSITION: Final[int] = 4  # tenths of a second
_SMOOTHING_MAX_S: Final[float] = 0.1
_SMOOTHING_EMA_WEIGHT: Final[float] = 0.2


class ZigbeeEntertainmentSession:
    """
    Asyncio facade that streams Hue Entertainment frames to Zigbee lights through Ziggurat.

    Connects to ziggurat-server as an extra websocket client next to ZHA; no Hue bridge is
    involved. Every frame goes to one proxy light that relays it to the others, so the lights
    should be Hue lights within radio range of each other. Each light's state is captured on
    the first :meth:`start` and restored on :meth:`stop`. Like :class:`.EntertainmentSession`,
    a lost stream turns :attr:`is_streaming` False and the caller decides whether to restart.
    """

    def __init__(
        self,
        url: str,
        lights: Sequence[ZigbeeLight],
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_S,
        max_rate_hz: float = DEFAULT_MAX_RATE_HZ,
    ) -> None:
        """
        Initialize the session (no network I/O until :meth:`start`).

        :param url: Websocket URL of ziggurat-server, e.g. ``ws://homeassistant.local:9999``.
        :param lights: The channels to stream to, at least one, each with its own channel id;
            entries sharing an ``ieee`` are the zones of one gradient light (see
            :class:`.ZigbeeLight`).
        :param idle_timeout: Seconds without :meth:`send` after which the stream is stopped;
            0 disables.
        :param max_rate_hz: Most frames per second to send, at least 1 (the lights leave
            entertainment mode a few seconds after the frames stop); the lights fade over the
            time between frames, so a low rate (e.g. 5 for slowly changing colours) still looks
            smooth while using a fraction of the Zigbee airtime. Beyond 10 channels each update
            takes several frames, sent back to back; the cap then applies to the updates.
        :raises ValueError: For an invalid set of lights or a rate below 1.
        """
        self._lights = _group_lights(lights)
        if not max_rate_hz >= 1:  # also rejects NaN
            msg = "max_rate_hz must be at least 1"
            raise ValueError(msg)
        self._client = _ZclClient(url, on_joined=self._on_joined, on_disconnect=self._on_disconnect)
        self._by_channel = {zone.channel_id: zone for light in self._lights for zone in light.zones}
        self._proxy = self._lights[0]
        self._idle_timeout = idle_timeout
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._streaming = False
        # True from the first start until stop() restored the lights.
        self._engaged = False
        self._sender_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._counter = 1
        self._min_frame_interval = 1.0 / max_rate_hz
        self._interval = self._min_frame_interval
        self._last_send = 0.0

    @property
    def is_streaming(self) -> bool:
        """Return True while frames are being streamed."""
        return self._streaming

    async def start(self) -> None:
        """
        Connect to Ziggurat and start streaming (a no-op while already streaming).

        The first start captures each light's state for :meth:`stop` to restore; a restart
        after a lost stream keeps that capture. A light that does not answer is still streamed
        to, but not restored. A light given several zones that turns out to have no segments,
        or does not take its segment map, streams one colour: that of its zone 0.

        :raises ZigguratError: When Ziggurat is unreachable or its Zigbee stack is not running.
        """
        async with self._lock:
            if self._streaming:
                return
            await self._stop_sender()
            try:
                await self._client.connect()
                if not self._engaged:
                    await self._for_each_light(self._prepare_light)
                addresses = self._allocate_addresses()
                await self._for_each_light(
                    lambda light: self._map_segments(light, addresses[light.ieee])
                )
                self._proxy = await self._reset_lights()
            except BaseException:
                await self._client.close()
                if not self._engaged:
                    self._forget_lights()
                raise
            self._engaged = True
            self._streaming = True
            self._last_send = time.monotonic()
            self._sender_task = asyncio.create_task(self._sender())
            if self._idle_timeout > 0 and self._idle_task is None:
                self._idle_task = asyncio.create_task(self._idle_monitor())
            LOGGER.info(
                "Zigbee entertainment started for %d lights via %s (proxy %s)",
                len(self._lights),
                self._client.url,
                self._proxy.label,
            )

    def send(self, commands: list[LightColorCommand]) -> None:
        """
        Queue an update of light colours; the newest update wins when the network is busy.

        Non-blocking and safe to call from the event loop; a no-op when not streaming.

        :param commands: Colour commands for the lights to update; other lights keep their colour.
        """
        now = time.monotonic()
        if self._streaming:
            # The smoothing (fade time) follows the interval between frames: the caller's, but
            # never below the rate cap.
            interval = min(
                max(_SMOOTHING_MAX_S, self._min_frame_interval),
                max(self._min_frame_interval, now - self._last_send),
            )
            self._interval += _SMOOTHING_EMA_WEIGHT * (interval - self._interval)
            for command in commands:
                zone = self._by_channel.get(command.channel_id)
                if zone is None:
                    continue
                x, y, brightness = _command_to_xy(command)
                zone.color = (round(brightness * MAX_BRIGHTNESS), x, y)
            self._wake.set()
        self._last_send = now

    async def stop(self) -> None:
        """Stop streaming and restore the lights' captured state. Never raises; idempotent."""
        async with self._lock:
            await self._teardown_locked()

    async def aclose(self) -> None:
        """Stop streaming and release the connection."""
        await self.stop()

    # -- internals --

    async def _teardown_locked(self) -> None:
        """Stop the stream and restore the lights. The caller must hold ``self._lock``."""
        # Cancel the idle monitor (unless we are being called from within it).
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
        self._idle_task = None
        self._streaming = False
        await self._stop_sender()
        if self._engaged:
            with suppress(ZigguratError):
                await self._client.connect()
            if self._client.connected:
                results = await asyncio.gather(
                    *(self._restore_light(light) for light in self._lights),
                    return_exceptions=True,
                )
                for result in results:
                    if isinstance(result, Exception):
                        LOGGER.error("Restoring a light failed", exc_info=result)
            else:
                LOGGER.warning("Ziggurat unreachable; the lights are not restored")
            LOGGER.info("Zigbee entertainment stopped")
        self._engaged = False
        self._forget_lights()
        await self._client.close()

    async def _prepare_light(self, light: _Light) -> None:
        """Capture a light's state and make sure it accepts entertainment frames."""
        on_off = await self._read(light, CLUSTER_ON_OFF, [ATTR_ON_OFF])
        if on_off is None or ATTR_ON_OFF not in on_off:
            LOGGER.warning("%s did not answer; it is streamed to but not restored", light.label)
            return
        level = await self._read(light, CLUSTER_LEVEL, [ATTR_CURRENT_LEVEL]) or {}
        color = (
            await self._read(
                light,
                CLUSTER_COLOR,
                [ATTR_COLOR_MODE, ATTR_CURRENT_X, ATTR_CURRENT_Y, ATTR_COLOR_TEMPERATURE],
            )
            or {}
        )
        light.snapshot = _Snapshot(
            on=bool(on_off[ATTR_ON_OFF]),
            level=level.get(ATTR_CURRENT_LEVEL),
            color_mode=color.get(ATTR_COLOR_MODE),
            x=color.get(ATTR_CURRENT_X),
            y=color.get(ATTR_CURRENT_Y),
            mireds=color.get(ATTR_COLOR_TEMPERATURE),
        )
        balance = await self._read(
            light,
            CLUSTER_ENTERTAINMENT,
            [ATTR_ENTERTAINMENT_BALANCE],
            manufacturer=HUE_MANUFACTURER_CODE,
        )
        if balance is not None and balance.get(ATTR_ENTERTAINMENT_BALANCE) != (
            ENTERTAINMENT_BALANCE_VALUE
        ):
            asdu = write_attribute_uint8(
                self._client.next_tsn(),
                ATTR_ENTERTAINMENT_BALANCE,
                ENTERTAINMENT_BALANCE_VALUE,
                manufacturer=HUE_MANUFACTURER_CODE,
            )
            await self._client.deliver(light.nwk, CLUSTER_ENTERTAINMENT, asdu, aps_ack=True)

    def _allocate_addresses(self) -> dict[str, list[int]]:
        """Return the segment address of each zone, per light ieee; zone 0 keeps the light's NWK."""
        taken = {light.nwk for light in self._lights}
        allocated: dict[str, list[int]] = {}
        for light in self._lights:
            addresses = [light.nwk]
            candidate = light.nwk
            while len(addresses) < len(light.zones):
                # The next unicast address, wrapping around past the coordinator's 0x0000.
                candidate = candidate % MAX_UNICAST_NWK + 1
                if candidate not in taken:
                    taken.add(candidate)
                    addresses.append(candidate)
            allocated[light.ieee] = addresses
        return allocated

    async def _map_segments(self, light: _Light, addresses: list[int]) -> None:
        """Spread a light's segments over its zones; without a map it streams one colour."""
        for zone in light.zones:
            zone.address = None
        zones = len(light.zones)
        if zones == 1:
            return
        segments = await _read_segment_count(self._client, light.nwk)
        if segments is None:
            LOGGER.warning("%s did not report its segments; it streams one colour", light.label)
            return
        if segments == 1:
            LOGGER.warning("%s has no gradient segments; it streams one colour", light.label)
            return
        if zones > segments:
            LOGGER.warning(
                "%s has %d segments for %d zones; zones without a segment are ignored",
                light.label,
                segments,
                zones,
            )
        zone_of_segment = _zone_of_segments(zones, segments)
        asdu = segment_map(self._client.next_tsn(), [addresses[zone] for zone in zone_of_segment])
        reply = await self._client.request(light.nwk, CLUSTER_ENTERTAINMENT, asdu, CMD_SEGMENT_MAP)
        if reply is None or reply.status or not parse_segment_map_response(reply.payload):
            LOGGER.warning("%s did not take its segment map; it streams one colour", light.label)
            return
        for index in set(zone_of_segment):
            light.zones[index].address = addresses[index]

    async def _reset_lights(self) -> _Light:
        """Reset every light for streaming and return the proxy to stream through."""
        acked = await self._for_each_light(self._reset_light)
        if not any(acked):
            msg = "No light acknowledged the entertainment start"
            raise ZigguratError(msg)
        # Re-picked on every (re)start, so a proxy that went away is replaced.
        return self._choose_proxy(
            [light for light, ok in zip(self._lights, acked, strict=True) if ok]
        )

    async def _reset_light(self, light: _Light) -> bool:
        """Send the entertainment reset that precedes a stream; return whether it was acked."""
        asdu = entertainment_reset(self._client.next_tsn(), self._counter)
        if await self._client.deliver(light.nwk, CLUSTER_ENTERTAINMENT, asdu, aps_ack=True):
            return True
        LOGGER.warning("%s did not acknowledge the entertainment start", light.label)
        return False

    async def _restore_light(self, light: _Light) -> None:
        """End entertainment mode on a light and restore its captured state, best effort."""
        client = self._client
        try:
            reset = entertainment_reset(client.next_tsn(), self._counter)
            if not await client.deliver(light.nwk, CLUSTER_ENTERTAINMENT, reset, aps_ack=True):
                LOGGER.warning("%s did not acknowledge the entertainment stop", light.label)
                return
            snapshot = light.snapshot
            if snapshot is None:
                return
            # Lights that were off get their colour and level back too (instantly, right before
            # they switch off), so they later switch on as they were, not in the streamed state.
            transition = _RESTORE_TRANSITION if snapshot.on else 0
            color = snapshot.color_command(client.next_tsn(), transition)
            if color is not None and not await client.deliver(
                light.nwk, CLUSTER_COLOR, color, aps_ack=True
            ):
                LOGGER.warning("Could not restore the colour of %s", light.label)
            if snapshot.level is not None:
                level = move_to_level_with_on_off(client.next_tsn(), snapshot.level, transition)
                await client.deliver(light.nwk, CLUSTER_LEVEL, level, aps_ack=True)
            if not snapshot.on:
                await client.deliver(
                    light.nwk, CLUSTER_ON_OFF, off(client.next_tsn()), aps_ack=True
                )
        except ZigguratError as err:
            LOGGER.warning("Could not restore %s: %s", light.label, err)

    async def _read(
        self,
        light: _Light,
        cluster: int,
        attributes: list[int],
        *,
        manufacturer: int | None = None,
    ) -> dict[int, int] | None:
        """Read attributes from a light; None when it does not answer."""
        asdu = read_attributes(self._client.next_tsn(), attributes, manufacturer=manufacturer)
        reply = await self._client.request(
            light.nwk, cluster, asdu, ZCL_CMD_READ_ATTRIBUTES_RESPONSE
        )
        if reply is None or reply.status:
            return None
        light.rssi.append(reply.rssi)
        return parse_read_attributes_response(reply.payload)

    async def _sender(self) -> None:
        """Stream the newest colours to the proxy light, with at most one frame in flight."""
        failing_since: float | None = None
        next_tick_at = 0.0
        try:
            while self._streaming:
                with suppress(TimeoutError):
                    async with asyncio.timeout(_KEEPALIVE_S):
                        await self._wake.wait()
                await asyncio.sleep(max(0.0, next_tick_at - time.monotonic()))
                # Cleared after the pacing sleep: commands that arrived during it are in this tick.
                self._wake.clear()
                records = [record for light in self._lights for record in light.records()]
                if not self._streaming or not records:
                    continue
                smoothing = smoothing_from_seconds(self._interval)
                next_tick_at = time.monotonic() + self._min_frame_interval
                delivered = True
                for chunk in _split_evenly(records):
                    if not self._streaming:
                        break
                    asdu = entertainment_frame(
                        self._client.next_tsn(), self._counter, smoothing, chunk
                    )
                    self._counter += 1
                    if not await self._client.deliver(
                        self._proxy.nwk, CLUSTER_ENTERTAINMENT, asdu, aps_ack=False
                    ):
                        delivered = False
                if delivered:
                    failing_since = None
                    continue
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                elif now - failing_since >= _STREAM_LOSS_S:
                    self._lose_stream("frames are no longer delivered")
        except ZigguratError as err:
            self._lose_stream(str(err))
        except Exception:
            LOGGER.exception("Zigbee entertainment sender failed")
            self._lose_stream("sender failed")

    async def _stop_sender(self) -> None:
        """Cancel the sender task, if any."""
        task, self._sender_task = self._sender_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _idle_monitor(self) -> None:
        """Stop the stream after ``idle_timeout`` seconds without a :meth:`send`."""
        while True:
            remaining = self._idle_timeout - (time.monotonic() - self._last_send)
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            async with self._lock:
                if time.monotonic() - self._last_send < self._idle_timeout:
                    # Activity resumed while we were acquiring the lock.
                    continue
                LOGGER.debug("Zigbee entertainment idle for %.1fs - stopping", self._idle_timeout)
                await self._teardown_locked()
                return

    async def _for_each_light(self, action: Callable[[_Light], Awaitable[_T]]) -> list[_T]:
        """Run ``action`` for all lights concurrently; return the results in light order."""
        return await _gather(action(light) for light in self._lights)

    def _choose_proxy(self, candidates: list[_Light]) -> _Light:
        """Pick the candidate heard with the strongest signal (else the first one) as proxy."""
        heard = [light for light in candidates if light.rssi]
        if not heard:
            return candidates[0]
        return max(heard, key=lambda light: statistics.fmean(light.rssi))

    def _forget_lights(self) -> None:
        """Drop the captured state and streamed colours of all lights."""
        for light in self._lights:
            light.snapshot = None
            light.rssi.clear()
            for zone in light.zones:
                zone.color = None

    def _lose_stream(self, reason: str) -> None:
        """Mark the stream as lost; the caller restarts or stops the session."""
        if self._streaming:
            LOGGER.warning("Zigbee entertainment stream lost: %s", reason)
        self._streaming = False
        self._wake.set()

    def _on_joined(self, joined: DeviceJoined) -> None:
        """Follow a light that rejoined the network under a new network address."""
        for light in self._lights:
            if light.ieee == joined.ieee and light.nwk != joined.nwk:
                LOGGER.info(
                    "%s rejoined as 0x%04x (was 0x%04x)", light.label, joined.nwk, light.nwk
                )
                light.nwk = joined.nwk

    def _on_disconnect(self) -> None:
        """Handle the connection to Ziggurat dropping."""
        self._lose_stream("connection to Ziggurat lost")


async def read_segment_counts(url: str, nwks: Iterable[int]) -> dict[int, int]:
    """
    Ask Hue lights how many gradient segments they have, e.g. to offer their zones as channels.

    Opens its own connection to ziggurat-server and closes it again.

    :param url: Websocket URL of ziggurat-server, e.g. ``ws://homeassistant.local:9999``.
    :param nwks: Network addresses of the lights to ask.
    :return: The segment count per network address, 1 for a light without segments; a light
        that does not answer is left out.
    :raises ZigguratError: When Ziggurat is unreachable or its Zigbee stack is not running.
    """
    targets = list(nwks)
    client = _ZclClient(url)
    await client.connect()
    try:
        counts = await _gather(_read_segment_count(client, nwk) for nwk in targets)
    finally:
        await client.close()
    return {nwk: count for nwk, count in zip(targets, counts, strict=True) if count is not None}


def _command_to_xy(command: LightColorCommand) -> tuple[float, float, float]:
    """Convert a 16-bit RGB command to CIE ``(x, y, brightness)`` with brightness 0.0-1.0."""
    return rgb_to_xy(
        command.red / 65535, command.green / 65535, command.blue / 65535, GAMUT_C, vivid=True
    )


def _group_lights(lights: Sequence[ZigbeeLight]) -> list[_Light]:
    """Group the entries into lights with their zones; raise ValueError for an invalid set."""
    if not lights:
        msg = "A Zigbee entertainment session needs at least one light"
        raise ValueError(msg)
    if len({light.channel_id for light in lights}) != len(lights):
        msg = "Every entry needs its own channel id"
        raise ValueError(msg)
    entries_by_ieee: dict[str, list[ZigbeeLight]] = {}
    for light in lights:
        entries_by_ieee.setdefault(light.ieee.lower(), []).append(light)
    grouped: list[_Light] = []
    for ieee, entries in entries_by_ieee.items():
        entries.sort(key=lambda entry: entry.zone)
        if [entry.zone for entry in entries] != list(range(len(entries))):
            msg = f"The zones of {ieee} must be numbered 0 to {len(entries) - 1}"
            raise ValueError(msg)
        if len({entry.nwk for entry in entries}) > 1:
            msg = f"The zones of {ieee} have different network addresses"
            raise ValueError(msg)
        zones = [_Zone(entry.channel_id) for entry in entries]
        grouped.append(_Light(ieee, entries[0].nwk, entries[0].name, zones))
    return grouped


def _split_evenly(records: list[bytes]) -> list[list[bytes]]:
    """Split an update's records, in order, over the fewest frames and as evenly as possible."""
    frames = -(-len(records) // MAX_RECORDS)
    # Frame i ends at ceil(len * (i + 1) / frames), so earlier frames take the remainder.
    bounds = [-(-len(records) * i // frames) for i in range(frames + 1)]
    return [records[start:end] for start, end in itertools.pairwise(bounds)]


def _zone_of_segments(zones: int, segments: int) -> list[int]:
    """Return the zone each segment shows, spreading the segments evenly over the zones."""
    # The zone under each segment's centre: (i + 0.5) * zones / segments, rounded down.
    return [(2 * i + 1) * zones // (2 * segments) for i in range(segments)]


async def _read_segment_count(client: _ZclClient, nwk: int) -> int | None:
    """Return a light's segment count: 1 for a light without segments, None without an answer."""
    asdu = segment_layout_request(client.next_tsn())
    reply = await client.request(nwk, CLUSTER_ENTERTAINMENT, asdu, CMD_SEGMENT_LAYOUT)
    if reply is None:
        return None
    if reply.status:
        return 1
    try:
        return max(1, len(parse_segment_layout(reply.payload)))
    except ValueError:
        LOGGER.debug("Malformed segment layout from 0x%04x: %s", nwk, reply.payload.hex())
        return None


async def _gather(awaitables: Iterable[Awaitable[_T]]) -> list[_T]:
    """Await all concurrently and return their results in order; re-raise the first failure."""
    results = await asyncio.gather(*awaitables, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return cast("list[_T]", results)


class _ZclClient(ZigguratClient):
    """Ziggurat client that also runs ZCL request/response exchanges with lights."""

    def __init__(
        self,
        url: str,
        *,
        on_joined: Callable[[DeviceJoined], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        """Initialize the client (no network I/O until :meth:`connect`)."""
        super().__init__(
            url, on_received=self._on_received_aps, on_joined=on_joined, on_disconnect=on_disconnect
        )
        self._tsn = itertools.count()
        # (nwk, cluster, tsn) -> (cluster-specific, command id, response command id, reply)
        self._pending: dict[
            tuple[int, int, int], tuple[bool, int, int, asyncio.Future[_Reply]]
        ] = {}

    def next_tsn(self) -> int:
        """Return the next ZCL transaction sequence number."""
        return next(self._tsn) & 0xFF

    async def deliver(self, nwk: int, cluster: int, asdu: bytes, *, aps_ack: bool) -> bool:
        """
        Send one unicast and wait for its delivery verdict.

        :return: True once delivered (to the first hop, or acknowledged by the light with
            ``aps_ack``); False when delivery failed or timed out.
        :raises ZigguratError: When the connection is lost or the Zigbee stack is down.
        """
        try:
            send = await self.send_unicast(nwk, cluster, asdu, aps_ack=aps_ack)
        except ZigguratError as err:
            if err.status is None or err.stack_down:
                raise
            LOGGER.debug("Send to 0x%04x rejected: %s", nwk, err)
            return False
        if send.acked is None:
            verdict, timeout = send.confirmed, _FRAME_CONFIRM_TIMEOUT_S
        else:
            verdict, timeout = send.acked, _REQUEST_TIMEOUT_S
        try:
            status = await asyncio.wait_for(verdict, timeout)
        except TimeoutError:
            return False
        return status == SEND_SUCCESS

    async def request(self, nwk: int, cluster: int, asdu: bytes, response: int) -> _Reply | None:
        """
        Send a ZCL command and wait for the light's answer.

        :param response: Command id of the expected response, of the same kind (global or
            cluster-specific) as the command.
        :return: The answer, which may be a Default Response error; None without an answer.
        :raises ZigguratError: When the connection is lost or the Zigbee stack is down.
        """
        sent = parse_zcl(asdu)[0]
        key = (nwk, cluster, sent.tsn)
        reply: asyncio.Future[_Reply] = asyncio.get_running_loop().create_future()
        self._pending[key] = (sent.cluster_specific, sent.command, response, reply)
        try:
            # The response itself proves delivery, so no APS ack is requested.
            if not await self.deliver(nwk, cluster, asdu, aps_ack=False):
                return None
            return await asyncio.wait_for(reply, _REQUEST_TIMEOUT_S)
        except TimeoutError:
            return None
        finally:
            self._pending.pop(key, None)

    def _on_received_aps(self, received: ReceivedAps) -> None:
        """Resolve the pending request that a light's frame answers, if any."""
        try:
            header, payload = parse_zcl(received.data)
        except ValueError:
            return
        pending = self._pending.get((received.source, received.cluster, header.tsn))
        if pending is None or pending[3].done() or not header.from_server:
            return
        cluster_specific, command, response, reply = pending
        if header.cluster_specific == cluster_specific and header.command == response:
            reply.set_result(_Reply(payload, received.rssi))
        elif not header.cluster_specific and header.command == ZCL_CMD_DEFAULT_RESPONSE:
            with suppress(ValueError):
                answered, status = parse_default_response(payload)
                if answered == command and status:
                    reply.set_result(_Reply(payload, received.rssi, status))


@dataclass(frozen=True)
class _Reply:
    """A light's answer to a request."""

    payload: bytes
    rssi: int
    status: int = 0  # the ZCL status of a Default Response error; 0 for the expected response


@dataclass
class _Snapshot:
    """A light's state captured before streaming (raw ZCL attribute values)."""

    on: bool
    level: int | None = None
    color_mode: int | None = None
    x: int | None = None
    y: int | None = None
    mireds: int | None = None

    def color_command(self, tsn: int, transition: int) -> bytes | None:
        """Return the ZCL command that restores the captured colour, if it is known."""
        if self.color_mode == COLOR_MODE_TEMPERATURE and self.mireds is not None:
            return move_to_color_temperature(tsn, self.mireds, transition)
        if self.x is not None and self.y is not None:
            return move_to_color(tsn, self.x, self.y, transition)
        return None


@dataclass
class _Zone:
    """Runtime state of one channel: a whole light, or one zone of a gradient light."""

    channel_id: int
    address: int | None = None  # segment address, while its light streams in segment mode
    color: tuple[int, float, float] | None = None  # brightness, x, y of the latest command


@dataclass
class _Light:
    """Runtime state of one streamed light."""

    ieee: str
    nwk: int
    name: str
    zones: list[_Zone]
    snapshot: _Snapshot | None = None
    rssi: list[int] = field(default_factory=list)

    @property
    def label(self) -> str:
        """Return a name for logs."""
        return self.name or self.ieee

    def records(self) -> list[bytes]:
        """Return the light's records for the next update; none before its first colour."""
        if any(zone.address is not None for zone in self.zones):
            return [
                segment_record(zone.address, *zone.color)
                for zone in self.zones
                if zone.address is not None and zone.color is not None
            ]
        color = self.zones[0].color
        return [] if color is None else [light_record(self.nwk, *color)]

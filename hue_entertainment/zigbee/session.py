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
    COLOR_MODE_TEMPERATURE,
    ENTERTAINMENT_BALANCE_VALUE,
    HUE_MANUFACTURER_CODE,
    MAX_BRIGHTNESS,
    MAX_RECORDS,
    ZCL_CMD_READ_ATTRIBUTES_RESPONSE,
    entertainment_frame,
    entertainment_reset,
    light_record,
    move_to_color,
    move_to_color_temperature,
    move_to_level_with_on_off,
    off,
    parse_read_attributes_response,
    parse_zcl,
    read_attributes,
    smoothing_from_seconds,
    write_attribute_uint8,
)
from .ziggurat import SEND_SUCCESS, ZigguratClient, ZigguratError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from hue_entertainment.models import LightColorCommand, ZigbeeLight

    from .ziggurat import DeviceJoined, ReceivedAps

LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# The Hue bridge streams at 25 Hz too; a cap leaves airtime for ZHA (each frame is also relayed).
DEFAULT_MAX_RATE_HZ: Final[float] = 25.0
# Resend the last frame after this long without a new one; lights otherwise leave
# entertainment mode a few seconds after the frames stop.
_KEEPALIVE_S: Final[float] = 1.0
_FRAME_CONFIRM_TIMEOUT_S: Final[float] = 1.0
# Frames failing for this long mean the stream is lost.
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
        :param lights: The lights to stream to, 1 to 10.
        :param idle_timeout: Seconds without :meth:`send` after which the stream is stopped;
            0 disables.
        :param max_rate_hz: Most frames per second to send, at least 1 (the lights leave
            entertainment mode a few seconds after the frames stop); the lights fade over the
            time between frames, so a low rate (e.g. 5 for slowly changing colours) still looks
            smooth while using a fraction of the Zigbee airtime.
        """
        if not 0 < len(lights) <= MAX_RECORDS:
            msg = f"A Zigbee entertainment session streams to 1-{MAX_RECORDS} lights"
            raise ValueError(msg)
        if not max_rate_hz >= 1:  # also rejects NaN
            msg = "max_rate_hz must be at least 1"
            raise ValueError(msg)
        self._client = ZigguratClient(
            url,
            on_received=self._on_received,
            on_joined=self._on_joined,
            on_disconnect=self._on_disconnect,
        )
        self._lights = [_Light(lt.channel_id, lt.ieee.lower(), lt.nwk, lt.name) for lt in lights]
        self._by_channel = {light.channel_id: light for light in self._lights}
        self._proxy = self._lights[0]
        self._idle_timeout = idle_timeout
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._streaming = False
        # True from the first start until stop() restored the lights.
        self._engaged = False
        self._sender_task: asyncio.Task[None] | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._pending_reads: dict[
            tuple[int, int, int], tuple[_Light, asyncio.Future[dict[int, int]]]
        ] = {}
        self._tsn = itertools.count()
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
        to, but not restored.

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
        Queue a frame of light colours; the newest frame wins when the network is busy.

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
                light = self._by_channel.get(command.channel_id)
                if light is None:
                    continue
                x, y, brightness = _command_to_xy(command)
                light.color = (round(brightness * MAX_BRIGHTNESS), x, y)
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
                self._next_tsn(),
                ATTR_ENTERTAINMENT_BALANCE,
                ENTERTAINMENT_BALANCE_VALUE,
                manufacturer=HUE_MANUFACTURER_CODE,
            )
            await self._deliver(light.nwk, CLUSTER_ENTERTAINMENT, asdu, aps_ack=True)

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
        asdu = entertainment_reset(self._next_tsn(), self._counter)
        if await self._deliver(light.nwk, CLUSTER_ENTERTAINMENT, asdu, aps_ack=True):
            return True
        LOGGER.warning("%s did not acknowledge the entertainment start", light.label)
        return False

    async def _restore_light(self, light: _Light) -> None:
        """End entertainment mode on a light and restore its captured state, best effort."""
        try:
            reset = entertainment_reset(self._next_tsn(), self._counter)
            if not await self._deliver(light.nwk, CLUSTER_ENTERTAINMENT, reset, aps_ack=True):
                LOGGER.warning("%s did not acknowledge the entertainment stop", light.label)
                return
            snapshot = light.snapshot
            if snapshot is None:
                return
            # Lights that were off get their colour and level back too (instantly, right before
            # they switch off), so they later switch on as they were, not in the streamed state.
            transition = _RESTORE_TRANSITION if snapshot.on else 0
            color = snapshot.color_command(self._next_tsn(), transition)
            if color is not None and not await self._deliver(
                light.nwk, CLUSTER_COLOR, color, aps_ack=True
            ):
                LOGGER.warning("Could not restore the colour of %s", light.label)
            if snapshot.level is not None:
                level = move_to_level_with_on_off(self._next_tsn(), snapshot.level, transition)
                await self._deliver(light.nwk, CLUSTER_LEVEL, level, aps_ack=True)
            if not snapshot.on:
                await self._deliver(light.nwk, CLUSTER_ON_OFF, off(self._next_tsn()), aps_ack=True)
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
        tsn = self._next_tsn()
        key = (light.nwk, cluster, tsn)
        response: asyncio.Future[dict[int, int]] = asyncio.get_running_loop().create_future()
        self._pending_reads[key] = (light, response)
        try:
            asdu = read_attributes(tsn, attributes, manufacturer=manufacturer)
            # The response itself proves delivery, so no APS ack is requested.
            if not await self._deliver(light.nwk, cluster, asdu, aps_ack=False):
                return None
            return await asyncio.wait_for(response, _REQUEST_TIMEOUT_S)
        except TimeoutError:
            return None
        finally:
            self._pending_reads.pop(key, None)

    async def _deliver(self, nwk: int, cluster: int, asdu: bytes, *, aps_ack: bool) -> bool:
        """
        Send one unicast and wait for its delivery verdict.

        :return: True once delivered (to the first hop, or acknowledged by the light with
            ``aps_ack``); False when delivery failed or timed out.
        :raises ZigguratError: When the connection is lost or the Zigbee stack is down.
        """
        try:
            send = await self._client.send_unicast(nwk, cluster, asdu, aps_ack=aps_ack)
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

    async def _sender(self) -> None:
        """Stream the newest frame to the proxy light, with at most one frame in flight."""
        failing_since: float | None = None
        next_frame_at = 0.0
        try:
            while self._streaming:
                with suppress(TimeoutError):
                    async with asyncio.timeout(_KEEPALIVE_S):
                        await self._wake.wait()
                await asyncio.sleep(max(0.0, next_frame_at - time.monotonic()))
                # Cleared after the pacing sleep: commands that arrived during it are in this frame.
                self._wake.clear()
                records = [
                    light_record(light.nwk, *light.color)
                    for light in self._lights
                    if light.color is not None
                ]
                if not self._streaming or not records:
                    continue
                asdu = entertainment_frame(
                    self._next_tsn(),
                    self._counter,
                    smoothing_from_seconds(self._interval),
                    records,
                )
                self._counter += 1
                next_frame_at = time.monotonic() + self._min_frame_interval
                if await self._deliver(self._proxy.nwk, CLUSTER_ENTERTAINMENT, asdu, aps_ack=False):
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
        results = await asyncio.gather(
            *(action(light) for light in self._lights), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result
        return cast("list[_T]", results)

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
            light.color = None

    def _lose_stream(self, reason: str) -> None:
        """Mark the stream as lost; the caller restarts or stops the session."""
        if self._streaming:
            LOGGER.warning("Zigbee entertainment stream lost: %s", reason)
        self._streaming = False
        self._wake.set()

    def _next_tsn(self) -> int:
        """Return the next ZCL transaction sequence number."""
        return next(self._tsn) & 0xFF

    def _on_received(self, received: ReceivedAps) -> None:
        """Match a Read Attributes Response to its pending read."""
        try:
            header, payload = parse_zcl(received.data)
        except ValueError:
            return
        if (
            header.cluster_specific
            or not header.from_server
            or header.command != ZCL_CMD_READ_ATTRIBUTES_RESPONSE
        ):
            return
        pending = self._pending_reads.get((received.source, received.cluster, header.tsn))
        if pending is None or pending[1].done():
            return
        light, response = pending
        light.rssi.append(received.rssi)
        response.set_result(parse_read_attributes_response(payload))

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


def _command_to_xy(command: LightColorCommand) -> tuple[float, float, float]:
    """Convert a 16-bit RGB command to CIE ``(x, y, brightness)`` with brightness 0.0-1.0."""
    return rgb_to_xy(
        command.red / 65535, command.green / 65535, command.blue / 65535, GAMUT_C, vivid=True
    )


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
class _Light:
    """Runtime state of one streamed light."""

    channel_id: int
    ieee: str
    nwk: int
    name: str
    snapshot: _Snapshot | None = None
    rssi: list[int] = field(default_factory=list)
    color: tuple[int, float, float] | None = None  # brightness, x, y of the latest command

    @property
    def label(self) -> str:
        """Return a name for logs."""
        return self.name or self.ieee

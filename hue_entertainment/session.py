"""High-level async session that manages a Hue Entertainment stream lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress

from .api import HueEntertainmentAPI
from .dtls import HueDtlsStreamer
from .models import EntertainmentArea, LightColorCommand

LOGGER = logging.getLogger(__name__)

DEFAULT_IDLE_TIMEOUT_S = 10.0


class EntertainmentSession:
    """
    Asyncio facade that manages the lifecycle of a single Hue Entertainment stream.

    It activates the entertainment configuration and opens the DTLS stream on demand,
    enforces the bridge's single-active-stream constraint, optionally tears the stream
    down after a period of inactivity, and never blocks the event loop (the blocking DTLS
    work runs in an executor). The PSK identity is the bridge application key and the PSK
    itself is the client key, both obtained from :meth:`HueEntertainmentAPI.pair`.
    """

    def __init__(
        self,
        host: str,
        app_key: str,
        client_key: str,
        *,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT_S,
    ) -> None:
        """
        Initialize the session for a single bridge (no network I/O until :meth:`start`).

        :param host: IP address or hostname of the Hue bridge.
        :param app_key: Bridge application key (the CLIP v2 username); also the DTLS PSK identity.
        :param client_key: The hex client key from pairing, used as the DTLS pre-shared key.
        :param idle_timeout: Seconds of inactivity after which the stream is torn down; 0 disables.
        """
        self._api = HueEntertainmentAPI(host, app_key)
        self._streamer = HueDtlsStreamer()
        self._app_key = app_key
        self._client_key = client_key
        self._idle_timeout = idle_timeout
        self._area_id: str | None = None
        self._lock = asyncio.Lock()
        self._idle_task: asyncio.Task[None] | None = None
        self._last_send = 0.0

    @property
    def host(self) -> str:
        """Return the bridge host."""
        return self._api.host

    @host.setter
    def host(self, value: str) -> None:
        """Update the bridge host (e.g. after a DHCP renewal); applied on the next stream."""
        self._api.host = value

    @property
    def is_streaming(self) -> bool:
        """Return True while a DTLS stream is active."""
        return self._streamer.is_connected

    @property
    def active_area(self) -> str | None:
        """Return the id of the currently streaming entertainment area, if any."""
        return self._area_id

    async def get_entertainment_areas(self) -> list[EntertainmentArea]:
        """Return the entertainment configurations available on the bridge."""
        return await self._api.get_entertainment_areas()

    async def start(self, area_id: str, *, stop_others: bool = True) -> None:
        """
        Activate the entertainment area and open the DTLS stream (idempotent per area).

        If a different area is currently streaming it is stopped first. A Hue bridge allows
        only one active stream at a time, so with ``stop_others`` any other entertainment
        configuration on the bridge is stopped before this one is started.

        :param area_id: Id of the entertainment configuration to stream to.
        :param stop_others: Stop any other active entertainment configuration first.
        """
        async with self._lock:
            if self._streamer.is_connected and self._area_id == area_id:
                return
            await self._teardown_locked()
            if stop_others:
                await self._stop_other_areas(area_id)

            await self._api.start_entertainment(area_id)
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    self._streamer.connect,
                    self._api.host,
                    self._app_key,
                    self._client_key,
                    area_id,
                )
            except Exception:
                await self._api.stop_entertainment(area_id)
                raise

            self._area_id = area_id
            self._last_send = time.monotonic()
            if self._idle_timeout > 0:
                self._idle_task = asyncio.create_task(self._idle_monitor())
            LOGGER.info("Entertainment session started for area %s on %s", area_id, self._api.host)

    def send(self, commands: list[LightColorCommand]) -> None:
        """
        Queue a frame of channel colors for the active stream.

        Non-blocking and safe to call from the event loop; a no-op when not streaming.

        :param commands: One color command per entertainment channel to update this frame.
        """
        self._streamer.send_colors(commands)
        self._last_send = time.monotonic()

    async def stop(self) -> None:
        """Stop the DTLS stream and deactivate the entertainment area."""
        async with self._lock:
            await self._teardown_locked()

    async def aclose(self) -> None:
        """Stop streaming and release the underlying HTTP session."""
        await self.stop()
        await self._api.close()

    async def _teardown_locked(self) -> None:
        """Tear down an active stream. The caller must hold ``self._lock``."""
        # Cancel the idle monitor (unless we are being called from within it).
        if self._idle_task is not None and self._idle_task is not asyncio.current_task():
            self._idle_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._idle_task
            self._idle_task = None

        if not self._streamer.is_connected and self._area_id is None:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._streamer.disconnect)
        if self._area_id is not None:
            await self._api.stop_entertainment(self._area_id)
            self._area_id = None

    async def _stop_other_areas(self, keep: str) -> None:
        """Stop any other (possibly orphaned) entertainment configuration on the bridge."""
        with suppress(Exception):
            for area in await self._api.get_entertainment_areas():
                if area.id and area.id != keep:
                    await self._api.stop_entertainment(area.id)

    async def _idle_monitor(self) -> None:
        """Tear the stream down after ``idle_timeout`` seconds without a :meth:`send`."""
        while True:
            remaining = self._idle_timeout - (time.monotonic() - self._last_send)
            if remaining > 0:
                await asyncio.sleep(remaining)
                continue
            async with self._lock:
                if time.monotonic() - self._last_send < self._idle_timeout:
                    # Activity resumed while we were acquiring the lock.
                    continue
                LOGGER.debug("Entertainment session idle for %.1fs - stopping", self._idle_timeout)
                await self._teardown_locked()
                return

"""mDNS discovery of Philips Hue bridges on the local network."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from zeroconf import ServiceStateChange
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

_HUE_SERVICE = "_hue._tcp.local."
_DEFAULT_TIMEOUT = 5.0
_RESOLVE_TIMEOUT_MS = 3000.0
_DEFAULT_PORT = 443


@dataclass
class DiscoveredBridge:
    """A Hue bridge found on the local network via mDNS."""

    id: str
    host: str
    name: str
    port: int = _DEFAULT_PORT


async def discover_bridges(timeout: float = _DEFAULT_TIMEOUT) -> list[DiscoveredBridge]:
    """
    Discover Philips Hue bridges on the local network via mDNS.

    :param timeout: How long to listen for bridge announcements, in seconds.
    """
    aiozc = AsyncZeroconf()
    bridges: dict[str, DiscoveredBridge] = {}
    pending: list[asyncio.Task[None]] = []

    def on_change(
        zeroconf: object,  # noqa: ARG001 - name must match zeroconf's keyword call
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change is ServiceStateChange.Added:
            task = asyncio.ensure_future(_resolve(aiozc, service_type, name, bridges))
            pending.append(task)

    browser = AsyncServiceBrowser(aiozc.zeroconf, _HUE_SERVICE, handlers=[on_change])
    try:
        await asyncio.sleep(timeout)
    finally:
        await browser.async_cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await aiozc.async_close()
    return sorted(bridges.values(), key=lambda bridge: bridge.host)


async def _resolve(
    aiozc: AsyncZeroconf,
    service_type: str,
    name: str,
    bridges: dict[str, DiscoveredBridge],
) -> None:
    """Resolve a discovered mDNS service into a bridge entry (host, id, name)."""
    info = AsyncServiceInfo(service_type, name)
    if not await info.async_request(aiozc.zeroconf, _RESOLVE_TIMEOUT_MS):
        return
    addresses = info.parsed_addresses()
    if not addresses:
        return
    properties = {
        key.decode(): (value.decode() if value else "")
        for key, value in (info.properties or {}).items()
    }
    bridges[name] = DiscoveredBridge(
        id=properties.get("bridgeid", ""),
        host=addresses[0],
        name=(info.server or name).rstrip("."),
        port=info.port or _DEFAULT_PORT,
    )

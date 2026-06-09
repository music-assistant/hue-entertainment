"""Tests for mDNS bridge discovery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from zeroconf import ServiceStateChange

from hue_entertainment.discovery import DiscoveredBridge, _resolve, discover_bridges


class _FakeServiceInfo:
    """Stand-in for zeroconf's AsyncServiceInfo with a fixed resolved result."""

    def __init__(self, service_type: str, name: str) -> None:
        """Record the requested service type and name."""
        self.type = service_type
        self.name = name
        self.server = "Philips-hue.local."
        self.port = 443
        self.properties: dict[bytes, bytes | None] = {
            b"bridgeid": b"ABCDEF1234",
            b"modelid": b"BSB002",
        }

    async def async_request(self, _zc: object, _timeout: float) -> bool:
        """Pretend the service resolved successfully."""
        return True

    def parsed_addresses(self) -> list[str]:
        """Return the resolved IP address."""
        return ["192.168.1.50"]


def test_discovered_bridge_defaults() -> None:
    """A DiscoveredBridge defaults to the HTTPS port."""
    bridge = DiscoveredBridge(id="x", host="1.2.3.4", name="hue")
    assert bridge.port == 443


async def test_resolve_populates_bridge(monkeypatch: Any) -> None:
    """_resolve turns a resolved service into a DiscoveredBridge entry."""
    monkeypatch.setattr("hue_entertainment.discovery.AsyncServiceInfo", _FakeServiceInfo)
    aiozc = SimpleNamespace(zeroconf=object())
    bridges: dict[str, DiscoveredBridge] = {}
    await _resolve(aiozc, "_hue._tcp.local.", "bridge._hue._tcp.local.", bridges)
    assert len(bridges) == 1
    bridge = next(iter(bridges.values()))
    assert bridge.host == "192.168.1.50"
    assert bridge.id == "ABCDEF1234"
    assert bridge.name == "Philips-hue.local"


class _FakeBrowser:
    """Captures the state-change handler passed by discover_bridges."""

    captured: Any = None

    def __init__(self, _zc: object, _service_type: str, *, handlers: list[Any]) -> None:
        """Record the handler so the test can invoke it like zeroconf does."""
        type(self).captured = handlers[0]

    async def async_cancel(self) -> None:
        """No-op cancel."""


class _FakeAioZc:
    """Minimal stand-in for AsyncZeroconf."""

    def __init__(self) -> None:
        """Expose a dummy zeroconf attribute."""
        self.zeroconf = object()

    async def async_close(self) -> None:
        """No-op close."""


async def test_browse_handler_accepts_zeroconf_keyword_call(monkeypatch: Any) -> None:
    """Zeroconf invokes the handler with keyword args; the names must match (regression)."""
    resolved: list[str] = []

    async def fake_resolve(_aiozc: Any, _service_type: str, name: str, _bridges: Any) -> None:
        resolved.append(name)

    monkeypatch.setattr("hue_entertainment.discovery.AsyncZeroconf", _FakeAioZc)
    monkeypatch.setattr("hue_entertainment.discovery.AsyncServiceBrowser", _FakeBrowser)
    monkeypatch.setattr("hue_entertainment.discovery._resolve", fake_resolve)

    task = asyncio.ensure_future(discover_bridges(0.2))
    await asyncio.sleep(0.05)
    # This mirrors exactly how zeroconf calls the handler (all keyword arguments).
    _FakeBrowser.captured(
        zeroconf=None,
        service_type="_hue._tcp.local.",
        name="bridge._hue._tcp.local.",
        state_change=ServiceStateChange.Added,
    )
    await task
    assert resolved == ["bridge._hue._tcp.local."]

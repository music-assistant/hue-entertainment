"""Tests for mDNS bridge discovery."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from hue_entertainment.discovery import DiscoveredBridge, _resolve


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

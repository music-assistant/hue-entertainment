"""
Async Python client for the Philips Hue Entertainment streaming API.

Provides bridge discovery (mDNS), a pure-Python DTLS 1.2 PSK streaming client and a
HueStream encoder for the Hue Entertainment API, working with both the Hue V2 ("square")
bridge and the Hue Pro bridge. No openssl subprocess and no C bindings beyond the
`cryptography` package.
"""

from .api import HueEntertainmentAPI
from .color import ColorMode, Gamut, gamut_for_type
from .discovery import DiscoveredBridge, discover_bridges
from .dtls import HueDtlsStreamer
from .models import EntertainmentArea, LightChannel, LightColorCommand
from .session import EntertainmentSession

__all__ = [
    "ColorMode",
    "DiscoveredBridge",
    "EntertainmentArea",
    "EntertainmentSession",
    "Gamut",
    "HueDtlsStreamer",
    "HueEntertainmentAPI",
    "LightChannel",
    "LightColorCommand",
    "discover_bridges",
    "gamut_for_type",
]

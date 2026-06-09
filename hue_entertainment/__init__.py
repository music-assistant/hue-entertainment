"""
Async Python client for the Philips Hue Entertainment streaming API.

Provides a pure-Python DTLS 1.2 PSK streaming client and HueStream encoder for the
Hue Entertainment API, working with both the Hue V2 ("square") bridge and the Hue Pro
bridge. No openssl subprocess and no C bindings beyond the `cryptography` package.

Extracted from Music Assistant's `hue_entertainment` provider for reuse across projects.
"""

from .api import HueEntertainmentAPI
from .dtls import HueDtlsStreamer
from .models import EntertainmentArea, LightChannel, LightColorCommand
from .session import EntertainmentSession

__all__ = [
    "EntertainmentArea",
    "EntertainmentSession",
    "HueDtlsStreamer",
    "HueEntertainmentAPI",
    "LightChannel",
    "LightColorCommand",
]

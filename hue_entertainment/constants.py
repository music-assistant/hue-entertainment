"""Protocol constants for the Hue Entertainment streaming API."""

from __future__ import annotations

from typing import Final

# Hue Entertainment DTLS streaming endpoint.
HUE_ENTERTAINMENT_PORT: Final[int] = 2100

# HueStream protocol framing.
HUESTREAM_HEADER: Final[bytes] = b"HueStream"
HUESTREAM_VERSION: Final[bytes] = bytes([0x02, 0x00])  # protocol version 2.0
COLOR_SPACE_RGB: Final[int] = 0x00
COLOR_SPACE_XY: Final[int] = 0x01

# Streaming behaviour.
MAX_LIGHTS_PER_MESSAGE: Final[int] = 20
TARGET_UPDATE_RATE_HZ: Final[int] = 25
UPDATE_INTERVAL_S: Final[float] = 1.0 / TARGET_UPDATE_RATE_HZ
KEEPALIVE_INTERVAL_S: Final[float] = 5.0

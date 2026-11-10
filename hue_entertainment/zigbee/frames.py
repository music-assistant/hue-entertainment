"""
ZCL frame encoders and decoders for Hue Entertainment over Zigbee.

Pure functions, no I/O. Covers the Hue manufacturer-specific entertainment cluster (0xFC01)
and the handful of standard ZCL commands needed to snapshot and restore a light's state.
Every function returns or accepts a complete ZCL frame (the APS payload), little-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

PROFILE_HOME_AUTOMATION: Final[int] = 0x0104
HUE_ENDPOINT: Final[int] = 0x0B
HUE_MANUFACTURER_CODE: Final[int] = 0x100B

CLUSTER_ON_OFF: Final[int] = 0x0006
CLUSTER_LEVEL: Final[int] = 0x0008
CLUSTER_COLOR: Final[int] = 0x0300
CLUSTER_ENTERTAINMENT: Final[int] = 0xFC01

ATTR_ON_OFF: Final[int] = 0x0000
ATTR_CURRENT_LEVEL: Final[int] = 0x0000
ATTR_CURRENT_X: Final[int] = 0x0003
ATTR_CURRENT_Y: Final[int] = 0x0004
ATTR_COLOR_TEMPERATURE: Final[int] = 0x0007
ATTR_COLOR_MODE: Final[int] = 0x0008
# Hue entertainment attribute ("light balance") that is set to 0xFE before streaming.
ATTR_ENTERTAINMENT_BALANCE: Final[int] = 0x0005
ENTERTAINMENT_BALANCE_VALUE: Final[int] = 0xFE

COLOR_MODE_TEMPERATURE: Final[int] = 2  # ColorMode attribute: 0 = hue/sat, 1 = xy, 2 = mireds

# A frame must fit one radio frame, which caps it at 10 light records.
MAX_RECORDS: Final[int] = 10
MAX_BRIGHTNESS: Final[int] = 0x7FF

ZCL_CMD_READ_ATTRIBUTES: Final[int] = 0x00
ZCL_CMD_READ_ATTRIBUTES_RESPONSE: Final[int] = 0x01
ZCL_CMD_WRITE_ATTRIBUTES: Final[int] = 0x02

# ZCL frame control bits.
_FC_CLUSTER_SPECIFIC: Final[int] = 0x01
_FC_MANUFACTURER_SPECIFIC: Final[int] = 0x04
_FC_SERVER_TO_CLIENT: Final[int] = 0x08
_FC_DISABLE_DEFAULT_RESPONSE: Final[int] = 0x10

_CMD_ENTERTAINMENT_FRAME: Final[int] = 0x01
_CMD_ENTERTAINMENT_RESET: Final[int] = 0x03
_CMD_OFF: Final[int] = 0x00
_CMD_MOVE_TO_LEVEL_WITH_ON_OFF: Final[int] = 0x04
_CMD_MOVE_TO_COLOR: Final[int] = 0x07
_CMD_MOVE_TO_COLOR_TEMPERATURE: Final[int] = 0x0A

_TYPE_UINT8: Final[int] = 0x20
# Fixed-size ZCL data types we can decode: type id -> (size, signed).
_DATA_TYPES: Final[dict[int, tuple[int, bool]]] = {
    0x10: (1, False),  # boolean
    0x18: (1, False),  # bitmap8
    0x19: (2, False),  # bitmap16
    0x20: (1, False),  # uint8
    0x21: (2, False),  # uint16
    0x28: (1, True),  # int8
    0x29: (2, True),  # int16
    0x30: (1, False),  # enum8
}

# Bounds of the quantised xy space of a light record (the Hue "wide gamut" corner).
_WIDE_GAMUT_MAX_X: Final[float] = 0.7347
_WIDE_GAMUT_MAX_Y: Final[float] = 0.8264
_RECORD_MODE_DEVICE: Final[int] = 0b01011
# A smoothing value of 0xFFFF corresponds to a 2.56 s fade.
_SMOOTHING_MAX_US: Final[int] = 2_560_000


@dataclass(frozen=True)
class ZclHeader:
    """
    A decoded ZCL frame header.

    :param frame_control: The raw frame control byte.
    :param manufacturer: Manufacturer code of a manufacturer-specific frame, else None.
    :param tsn: Transaction sequence number.
    :param command: Command id (global or cluster-specific, see ``cluster_specific``).
    """

    frame_control: int
    manufacturer: int | None
    tsn: int
    command: int

    @property
    def cluster_specific(self) -> bool:
        """Return True for a cluster-specific command, False for a global (profile) command."""
        return bool(self.frame_control & _FC_CLUSTER_SPECIFIC)

    @property
    def from_server(self) -> bool:
        """Return True when the frame travels server -> client (e.g. a response from a light)."""
        return bool(self.frame_control & _FC_SERVER_TO_CLIENT)


def parse_zcl(data: bytes) -> tuple[ZclHeader, bytes]:
    """
    Split a ZCL frame into its header and command payload.

    :param data: A complete ZCL frame.
    :raises ValueError: When the frame is too short for its header.
    """
    if len(data) < 3:
        msg = "ZCL frame too short"
        raise ValueError(msg)
    frame_control = data[0]
    if frame_control & _FC_MANUFACTURER_SPECIFIC:
        if len(data) < 5:
            msg = "ZCL frame too short"
            raise ValueError(msg)
        manufacturer, tsn, command = struct.unpack_from("<HBB", data, 1)
        return ZclHeader(frame_control, manufacturer, tsn, command), data[5:]
    return ZclHeader(frame_control, None, data[1], data[2]), data[3:]


def parse_read_attributes_response(payload: bytes) -> dict[int, int]:
    """
    Decode the records of a Read Attributes Response.

    Attributes reported with a failure status are left out. Decoding stops at the first
    record whose data type is not a supported fixed-size type.

    :param payload: The command payload (after the ZCL header).
    :return: The successfully read attribute values, keyed by attribute id.
    """
    values: dict[int, int] = {}
    offset = 0
    while offset + 3 <= len(payload):
        attribute, status = struct.unpack_from("<HB", payload, offset)
        offset += 3
        if status != 0:
            continue
        if offset >= len(payload):
            break
        data_type = payload[offset]
        offset += 1
        if data_type not in _DATA_TYPES:
            break
        size, signed = _DATA_TYPES[data_type]
        if offset + size > len(payload):
            break
        values[attribute] = int.from_bytes(payload[offset : offset + size], "little", signed=signed)
        offset += size
    return values


def read_attributes(
    tsn: int, attributes: Sequence[int], *, manufacturer: int | None = None
) -> bytes:
    """
    Encode a ZCL Read Attributes command.

    :param tsn: Transaction sequence number, echoed in the response.
    :param attributes: Attribute ids to read.
    :param manufacturer: Manufacturer code for manufacturer-specific attributes.
    """
    payload = b"".join(struct.pack("<H", attribute) for attribute in attributes)
    return _zcl(0, tsn, ZCL_CMD_READ_ATTRIBUTES, payload, manufacturer=manufacturer)


def write_attribute_uint8(tsn: int, attribute: int, value: int, *, manufacturer: int) -> bytes:
    """
    Encode a manufacturer-specific ZCL Write Attributes command for one uint8 attribute.

    :param tsn: Transaction sequence number.
    :param attribute: Attribute id to write.
    :param value: The uint8 value.
    :param manufacturer: Manufacturer code of the attribute.
    """
    payload = struct.pack("<HBB", attribute, _TYPE_UINT8, value)
    return _zcl(0, tsn, ZCL_CMD_WRITE_ATTRIBUTES, payload, manufacturer=manufacturer)


def entertainment_frame(tsn: int, counter: int, smoothing: int, records: Sequence[bytes]) -> bytes:
    """
    Encode an entertainment frame (cluster 0xFC01, command 0x01).

    :param tsn: Transaction sequence number.
    :param counter: Frame counter; increment it for every frame of a stream.
    :param smoothing: Fade time towards the new colours, see :func:`smoothing_from_seconds`.
    :param records: Up to :data:`MAX_RECORDS` light records from :func:`light_record`.
    """
    if len(records) > MAX_RECORDS:
        msg = f"An entertainment frame holds at most {MAX_RECORDS} lights"
        raise ValueError(msg)
    payload = struct.pack("<IH", counter & 0xFFFFFFFF, smoothing) + b"".join(records)
    return _entertainment_command(tsn, _CMD_ENTERTAINMENT_FRAME, payload)


def entertainment_reset(tsn: int, counter: int) -> bytes:
    """
    Encode the entertainment reset command (cluster 0xFC01, command 0x03).

    Sent to every light before streaming starts and again when it stops.

    :param tsn: Transaction sequence number.
    :param counter: The stream's current frame counter.
    """
    payload = struct.pack("<BBI", 0x00, 0x01, counter & 0xFFFFFFFF)
    return _entertainment_command(tsn, _CMD_ENTERTAINMENT_RESET, payload)


def light_record(nwk: int, brightness: int, x: float, y: float) -> bytes:
    """
    Encode one light's 7-byte record of an entertainment frame.

    :param nwk: Network address of the light the record is for.
    :param brightness: 11-bit brightness, clamped to 1..2047 (a record cannot switch a light off).
    :param x: CIE x chromaticity; values outside the record's range are clamped.
    :param y: CIE y chromaticity; values outside the record's range are clamped.
    """
    brightness = max(1, min(MAX_BRIGHTNESS, brightness))
    return struct.pack("<HH", nwk, (brightness << 5) | _RECORD_MODE_DEVICE) + quantize_xy(x, y)


def quantize_xy(x: float, y: float) -> bytes:
    """
    Pack a CIE xy chromaticity into the 3-byte (12 bits per axis) record form.

    :param x: CIE x chromaticity; clamped to the representable range.
    :param y: CIE y chromaticity; clamped to the representable range.
    """
    qx = max(0, min(0xFFF, int(x * 0xFFF / _WIDE_GAMUT_MAX_X)))
    qy = max(0, min(0xFFF, int(y * 0xFFF / _WIDE_GAMUT_MAX_Y)))
    return bytes([qx & 0xFF, ((qx >> 8) & 0x0F) | ((qy & 0x0F) << 4), (qy >> 4) & 0xFF])


def smoothing_from_seconds(seconds: float) -> int:
    """
    Convert a fade duration to the entertainment frame's smoothing value.

    :param seconds: Fade duration; clamped to the 0..2.56 s the field can express.
    """
    return max(0, min(0xFFFF, int(seconds * 1_000_000 * 0x10000 / _SMOOTHING_MAX_US)))


def off(tsn: int) -> bytes:
    """
    Encode the On/Off cluster's Off command (cluster 0x0006).

    :param tsn: Transaction sequence number.
    """
    return _cluster_command(tsn, _CMD_OFF, b"")


def move_to_level_with_on_off(tsn: int, level: int, transition: int) -> bytes:
    """
    Encode Move to Level (with On/Off) of the Level Control cluster (cluster 0x0008).

    :param tsn: Transaction sequence number.
    :param level: Target level, 0-254.
    :param transition: Transition time in tenths of a second.
    """
    return _cluster_command(
        tsn, _CMD_MOVE_TO_LEVEL_WITH_ON_OFF, struct.pack("<BH", level, transition)
    )


def move_to_color(tsn: int, x: int, y: int, transition: int) -> bytes:
    """
    Encode Move to Color of the Color Control cluster (cluster 0x0300).

    :param tsn: Transaction sequence number.
    :param x: Target x chromaticity as its raw 16-bit attribute value.
    :param y: Target y chromaticity as its raw 16-bit attribute value.
    :param transition: Transition time in tenths of a second.
    """
    return _cluster_command(tsn, _CMD_MOVE_TO_COLOR, struct.pack("<HHH", x, y, transition))


def move_to_color_temperature(tsn: int, mireds: int, transition: int) -> bytes:
    """
    Encode Move to Color Temperature of the Color Control cluster (cluster 0x0300).

    :param tsn: Transaction sequence number.
    :param mireds: Target colour temperature in mireds.
    :param transition: Transition time in tenths of a second.
    """
    return _cluster_command(
        tsn, _CMD_MOVE_TO_COLOR_TEMPERATURE, struct.pack("<HH", mireds, transition)
    )


# -- internals --


def _zcl(
    frame_control: int, tsn: int, command: int, payload: bytes, *, manufacturer: int | None = None
) -> bytes:
    """Encode a ZCL frame; a manufacturer code sets the manufacturer-specific bit."""
    if manufacturer is None:
        return struct.pack("<BBB", frame_control, tsn & 0xFF, command) + payload
    frame_control |= _FC_MANUFACTURER_SPECIFIC
    return struct.pack("<BHBB", frame_control, manufacturer, tsn & 0xFF, command) + payload


def _cluster_command(tsn: int, command: int, payload: bytes) -> bytes:
    """Encode a standard cluster-specific command without a default response."""
    return _zcl(_FC_CLUSTER_SPECIFIC | _FC_DISABLE_DEFAULT_RESPONSE, tsn, command, payload)


def _entertainment_command(tsn: int, command: int, payload: bytes) -> bytes:
    """Encode a Hue entertainment (0xFC01) command without a default response."""
    return _zcl(
        _FC_CLUSTER_SPECIFIC | _FC_DISABLE_DEFAULT_RESPONSE,
        tsn,
        command,
        payload,
        manufacturer=HUE_MANUFACTURER_CODE,
    )

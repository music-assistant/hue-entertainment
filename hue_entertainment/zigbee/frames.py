"""
ZCL frame encoders and decoders for Hue Entertainment over Zigbee.

Pure functions, no I/O. Covers the Hue manufacturer-specific entertainment cluster (0xFC01)
and the handful of standard ZCL commands (plus the Hue state cluster 0xFC03 for gradients)
needed to snapshot and restore a light's state.
Every function returns or accepts a complete ZCL frame (the APS payload), little-endian.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

PROFILE_HOME_AUTOMATION: Final[int] = 0x0104
HUE_ENDPOINT: Final[int] = 0x0B
HUE_MANUFACTURER_CODE: Final[int] = 0x100B

CLUSTER_ON_OFF: Final[int] = 0x0006
CLUSTER_LEVEL: Final[int] = 0x0008
CLUSTER_COLOR: Final[int] = 0x0300
CLUSTER_ENTERTAINMENT: Final[int] = 0xFC01
# Hue cluster that holds a light's combined state, its gradient colours included.
CLUSTER_HUE_STATE: Final[int] = 0xFC03

ATTR_ON_OFF: Final[int] = 0x0000
ATTR_CURRENT_LEVEL: Final[int] = 0x0000
ATTR_CURRENT_X: Final[int] = 0x0003
ATTR_CURRENT_Y: Final[int] = 0x0004
ATTR_COLOR_TEMPERATURE: Final[int] = 0x0007
ATTR_COLOR_MODE: Final[int] = 0x0008
ATTR_HUE_STATE: Final[int] = 0x0002
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
ZCL_CMD_DEFAULT_RESPONSE: Final[int] = 0x0B

# Gradient commands of the entertainment cluster; a light answers each with the same command id.
CMD_SEGMENT_LAYOUT: Final[int] = 0x04
CMD_SEGMENT_MAP: Final[int] = 0x07

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
_CMD_HUE_STATE: Final[int] = 0x00

_TYPE_UINT8: Final[int] = 0x20
_TYPE_OCTET_STRING: Final[int] = 0x41
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
_RECORD_MODE_SEGMENT: Final[int] = 0b00000
# A smoothing value of 0xFFFF corresponds to a 2.56 s fade.
_SMOOTHING_MAX_US: Final[int] = 2_560_000

# Fields of the Hue state (cluster 0xFC03): a flag word, then the field of each set flag in
# this order, which is not the order of the flags. Higher flags are unknown and trail them.
_STATE_FADE_SPEED: Final[int] = 0x0010
_STATE_GRADIENT_PARAMS: Final[int] = 0x0040
_STATE_GRADIENT_COLORS: Final[int] = 0x0100
_STATE_FIELDS: Final[tuple[tuple[int, int], ...]] = (
    (0x0001, 1),  # on/off
    (0x0002, 1),  # brightness
    (0x0004, 2),  # colour temperature
    (0x0008, 4),  # xy
    (_STATE_FADE_SPEED, 2),
    (0x0020, 1),  # effect
    (_STATE_GRADIENT_COLORS, 1),  # a size byte, then that many bytes
    (0x0080, 1),  # effect speed
    (_STATE_GRADIENT_PARAMS, 2),  # scale and offset
)


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


@dataclass(frozen=True)
class Gradient:
    """
    The gradient a Hue gradient light shows, as the raw fields of its Hue state.

    :param colors: The gradient colours field: its size, the colour count, style and colours.
    :param params: The gradient scale and offset field; None when the state has none.
    """

    colors: bytes
    params: bytes | None = None


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
    Decode the numeric records of a Read Attributes Response.

    Attributes reported with a failure status are left out, as are octet strings (see
    :func:`parse_read_octet_strings`). Decoding stops at the first record whose data type is
    not supported.

    :param payload: The command payload (after the ZCL header).
    :return: The successfully read attribute values, keyed by attribute id.
    """
    return {
        attribute: int.from_bytes(raw, "little", signed=_DATA_TYPES[data_type][1])
        for attribute, data_type, raw in _read_records(payload)
        if data_type in _DATA_TYPES
    }


def parse_read_octet_strings(payload: bytes) -> dict[int, bytes]:
    """
    Decode the octet string records of a Read Attributes Response.

    :param payload: The command payload (after the ZCL header).
    :return: The successfully read octet strings, keyed by attribute id.
    """
    return {
        attribute: raw
        for attribute, data_type, raw in _read_records(payload)
        if data_type == _TYPE_OCTET_STRING
    }


def parse_default_response(payload: bytes) -> tuple[int, int]:
    """
    Decode a ZCL Default Response.

    :param payload: The command payload (after the ZCL header).
    :return: The id of the command it answers and its ZCL status (0 = success).
    :raises ValueError: When the payload is too short.
    """
    if len(payload) < 2:
        msg = "Default Response too short"
        raise ValueError(msg)
    return payload[0], payload[1]


def parse_segment_layout(payload: bytes) -> list[tuple[int, int]]:
    """
    Decode a gradient light's answer to :func:`segment_layout_request`.

    :param payload: The command payload (after the ZCL header).
    :return: The ``(start, length)`` of each segment, in segment order.
    :raises ValueError: When the payload is truncated.
    """
    if len(payload) < 3 or len(payload) < 3 + 2 * payload[2]:
        msg = "Segment layout response too short"
        raise ValueError(msg)
    return [(payload[3 + 2 * i], payload[4 + 2 * i]) for i in range(payload[2])]


def parse_segment_map_response(payload: bytes) -> bool:
    """
    Return True when a gradient light accepted the map sent by :func:`segment_map`.

    :param payload: The command payload (after the ZCL header).
    """
    return payload[:2] == b"\x00\x00"


def parse_gradient(state: bytes) -> Gradient | None:
    """
    Return the gradient held in a light's Hue state (cluster 0xFC03, attribute 0x0002).

    :param state: The attribute value.
    :return: The gradient; None when the light shows no gradient.
    :raises ValueError: When the state is truncated.
    """
    if len(state) < 2:
        msg = "Hue state too short"
        raise ValueError(msg)
    (flags,) = struct.unpack_from("<H", state)
    fields: dict[int, bytes] = {}
    offset = 2
    for flag, field_size in _STATE_FIELDS:
        if not flags & flag:
            continue
        size = field_size
        if flag == _STATE_GRADIENT_COLORS and offset < len(state):
            size += state[offset]
        if offset + size > len(state):
            msg = "Hue state truncated"
            raise ValueError(msg)
        fields[flag] = state[offset : offset + size]
        offset += size
    colors = fields.get(_STATE_GRADIENT_COLORS)
    if colors is None:
        return None
    return Gradient(colors, fields.get(_STATE_GRADIENT_PARAMS))


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
    :param records: Up to :data:`MAX_RECORDS` records from :func:`light_record` or
        :func:`segment_record`.
    """
    if len(records) > MAX_RECORDS:
        msg = f"An entertainment frame holds at most {MAX_RECORDS} records"
        raise ValueError(msg)
    payload = struct.pack("<IH", counter & 0xFFFFFFFF, smoothing) + b"".join(records)
    return _hue_command(tsn, _CMD_ENTERTAINMENT_FRAME, payload)


def entertainment_reset(tsn: int, counter: int) -> bytes:
    """
    Encode the entertainment reset command (cluster 0xFC01, command 0x03).

    Sent to every light before streaming starts and again when it stops.

    :param tsn: Transaction sequence number.
    :param counter: The stream's current frame counter.
    """
    payload = struct.pack("<BBI", 0x00, 0x01, counter & 0xFFFFFFFF)
    return _hue_command(tsn, _CMD_ENTERTAINMENT_RESET, payload)


def segment_layout_request(tsn: int) -> bytes:
    """
    Encode the request for a gradient light's segment layout (cluster 0xFC01, command 0x04).

    A light without segments answers with a Default Response error.

    :param tsn: Transaction sequence number, echoed in the response.
    """
    return _hue_command(tsn, CMD_SEGMENT_LAYOUT, b"\x00")


def segment_map(tsn: int, addresses: Sequence[int]) -> bytes:
    """
    Encode a gradient light's segment map (cluster 0xFC01, command 0x07).

    It gives each segment the address of the :func:`segment_record` it shows; segments that
    share an address show the same record.

    :param tsn: Transaction sequence number, echoed in the response.
    :param addresses: One address per segment of the light, in segment order.
    """
    payload = struct.pack(">H", len(addresses)) + b"".join(
        struct.pack("<H", address) for address in addresses
    )
    return _hue_command(tsn, CMD_SEGMENT_MAP, payload)


def light_record(nwk: int, brightness: int, x: float, y: float) -> bytes:
    """
    Encode one light's 7-byte record of an entertainment frame.

    :param nwk: Network address of the light the record is for.
    :param brightness: 11-bit brightness, clamped to 1..2047 (a record cannot switch a light off).
    :param x: CIE x chromaticity; values outside the record's range are clamped.
    :param y: CIE y chromaticity; values outside the record's range are clamped.
    """
    return _record(nwk, _RECORD_MODE_DEVICE, brightness, x, y)


def segment_record(address: int, brightness: int, x: float, y: float) -> bytes:
    """
    Encode the 7-byte record of gradient segments in an entertainment frame.

    :param address: The address that the light's :func:`segment_map` gives the segments.
    :param brightness: 11-bit brightness, clamped to 1..2047 (a record cannot switch a light off).
    :param x: CIE x chromaticity; values outside the record's range are clamped.
    :param y: CIE y chromaticity; values outside the record's range are clamped.
    """
    return _record(address, _RECORD_MODE_SEGMENT, brightness, x, y)


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


def set_gradient(tsn: int, gradient: Gradient, transition: int) -> bytes:
    """
    Encode the Hue state command (cluster 0xFC03, command 0x00) that shows a gradient.

    :param tsn: Transaction sequence number.
    :param gradient: The gradient to show, see :func:`parse_gradient`.
    :param transition: Transition time in tenths of a second.
    """
    flags = _STATE_FADE_SPEED | _STATE_GRADIENT_COLORS
    if gradient.params is not None:
        flags |= _STATE_GRADIENT_PARAMS
    payload = struct.pack("<HH", flags, transition) + gradient.colors + (gradient.params or b"")
    return _hue_command(tsn, _CMD_HUE_STATE, payload)


# -- internals --


def _zcl(
    frame_control: int, tsn: int, command: int, payload: bytes, *, manufacturer: int | None = None
) -> bytes:
    """Encode a ZCL frame; a manufacturer code sets the manufacturer-specific bit."""
    if manufacturer is None:
        return struct.pack("<BBB", frame_control, tsn & 0xFF, command) + payload
    frame_control |= _FC_MANUFACTURER_SPECIFIC
    return struct.pack("<BHBB", frame_control, manufacturer, tsn & 0xFF, command) + payload


def _read_records(payload: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Yield the successful records of a Read Attributes Response as (attribute, type, value)."""
    offset = 0
    while offset + 3 <= len(payload):
        attribute, status = struct.unpack_from("<HB", payload, offset)
        offset += 3
        if status != 0:
            continue
        if offset >= len(payload):
            return
        data_type = payload[offset]
        offset += 1
        if data_type == _TYPE_OCTET_STRING and offset < len(payload):
            size = payload[offset]
            offset += 1
        elif data_type in _DATA_TYPES:
            size = _DATA_TYPES[data_type][0]
        else:
            return
        if offset + size > len(payload):
            return
        yield attribute, data_type, payload[offset : offset + size]
        offset += size


def _record(address: int, mode: int, brightness: int, x: float, y: float) -> bytes:
    """Encode a 7-byte entertainment record for a device or segment address."""
    brightness = max(1, min(MAX_BRIGHTNESS, brightness))
    return struct.pack("<HH", address, (brightness << 5) | mode) + quantize_xy(x, y)


def _cluster_command(tsn: int, command: int, payload: bytes) -> bytes:
    """Encode a standard cluster-specific command without a default response."""
    return _zcl(_FC_CLUSTER_SPECIFIC | _FC_DISABLE_DEFAULT_RESPONSE, tsn, command, payload)


def _hue_command(tsn: int, command: int, payload: bytes) -> bytes:
    """Encode a command of a Hue cluster (0xFC01, 0xFC03) without a default response."""
    return _zcl(
        _FC_CLUSTER_SPECIFIC | _FC_DISABLE_DEFAULT_RESPONSE,
        tsn,
        command,
        payload,
        manufacturer=HUE_MANUFACTURER_CODE,
    )

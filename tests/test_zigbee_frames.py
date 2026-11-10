"""Tests for the Hue Zigbee entertainment ZCL encoders and decoders."""

from __future__ import annotations

import pytest

from hue_entertainment.zigbee.frames import (
    MAX_RECORDS,
    entertainment_frame,
    entertainment_reset,
    light_record,
    move_to_color,
    move_to_color_temperature,
    move_to_level_with_on_off,
    off,
    parse_default_response,
    parse_read_attributes_response,
    parse_segment_layout,
    parse_segment_map_response,
    parse_zcl,
    quantize_xy,
    read_attributes,
    segment_layout_request,
    segment_map,
    segment_record,
    smoothing_from_seconds,
    write_attribute_uint8,
)

_MAX_X, _MAX_Y = 0.7347, 0.8264


def _xy_for(qx: int, qy: int) -> tuple[float, float]:
    """Return the xy at the centre of a quantisation bucket."""
    return ((qx + 0.5) * _MAX_X / 0xFFF, (qy + 0.5) * _MAX_Y / 0xFFF)


def test_light_record_matches_bifrost_vector() -> None:
    """Device-mode record at full brightness packs like bifrost's reference (2211ebffaabbcc)."""
    x, y = _xy_for(0xBAA, 0xCCB)
    assert light_record(0x1122, 0x7FF, x, y).hex() == "2211ebffaabbcc"


def test_light_record_xy_vector() -> None:
    """A red-corner xy quantises to the expected 12-bit pair."""
    assert light_record(0x1122, 0x7FF, 0.73469, 0.26531).hex() == "2211ebfffe2f52"


@pytest.mark.parametrize(
    ("x", "y", "expected"),
    [
        (0.735, 0.0, "ff0f00"),  # just past the x range: clamps instead of wrapping to 0
        (-0.1, 1.0, "00f0ff"),
        (0.0, 0.0, "000000"),
    ],
)
def test_quantize_xy_clamps(x: float, y: float, expected: str) -> None:
    """Out-of-range chromaticities clamp to the record's 12-bit range."""
    assert quantize_xy(x, y).hex() == expected


def test_segment_record_uses_segment_mode() -> None:
    """A segment record differs from a device record only in its mode bits (00000)."""
    x, y = _xy_for(0xBAA, 0xCCB)
    assert segment_record(0x1122, 0x7FF, x, y).hex() == "2211e0ffaabbcc"
    assert segment_record(0x1122, 0, x, y)[2:4] == (1 << 5).to_bytes(2, "little")


def test_light_record_brightness_is_clamped() -> None:
    """Brightness stays within 1..2047: zero would not be a valid record."""
    assert light_record(0x0001, 0, 0.3, 0.3)[2:4] == (1 << 5 | 0b01011).to_bytes(2, "little")
    assert light_record(0x0001, 5000, 0.3, 0.3)[2:4] == bytes.fromhex("ebff")


def test_entertainment_frame_layout() -> None:
    """Frame = mfr-specific ZCL header (fc 0x15) + counter + smoothing + records."""
    record = bytes.fromhex("2211ebffaabbcc")
    asdu = entertainment_frame(0x42, 0x11223344, 0xAABB, [record])
    assert asdu.hex() == "150b104201" + "44332211" + "bbaa" + record.hex()


def test_entertainment_frame_rejects_too_many_records() -> None:
    """A frame holds at most ten records."""
    with pytest.raises(ValueError, match="at most"):
        entertainment_frame(0, 1, 0, [bytes(7)] * (MAX_RECORDS + 1))


def test_entertainment_reset_layout() -> None:
    """The reset carries 00 01 and the stream counter."""
    assert entertainment_reset(0x07, 0x1122).hex() == "150b100703" + "0001" + "22110000"


def test_segment_layout_request() -> None:
    """The layout request is mfr-specific cluster command 0x04 with payload 00."""
    assert segment_layout_request(0x42).hex() == "150b104204" + "00"


def test_parse_segment_layout_real_sample() -> None:
    """The answer of a Signe table lamp: 4 segments of length 1."""
    payload = bytes.fromhex("00 00 04 00 01 01 01 02 01 03 01")
    assert parse_segment_layout(payload) == [(0, 1), (1, 1), (2, 1), (3, 1)]


@pytest.mark.parametrize("data", ["", "0000", "000002000101"])
def test_parse_segment_layout_rejects_truncated(data: str) -> None:
    """A payload shorter than its segment count raises ValueError."""
    with pytest.raises(ValueError, match="too short"):
        parse_segment_layout(bytes.fromhex(data))


def test_segment_map_matches_probe() -> None:
    """Count is big-endian, the addresses little-endian (bytes sent to a Signe lamp)."""
    addresses = [0xD297, 0xD298, 0xD299, 0xD29A, 0xD29B, 0xD29C, 0xD29D]
    assert segment_map(0x42, addresses) == bytes.fromhex(
        "15 0b 10 42 07 00 07 97 d2 98 d2 99 d2 9a d2 9b d2 9c d2 9d d2"
    )


@pytest.mark.parametrize(
    ("data", "accepted"), [("0000", True), ("0100", False), ("0001", False), ("", False)]
)
def test_parse_segment_map_response(data: str, *, accepted: bool) -> None:
    """Only a 00 00 status accepts the map."""
    assert parse_segment_map_response(bytes.fromhex(data)) is accepted


def test_parse_default_response() -> None:
    """A Default Response carries the answered command id and its status."""
    assert parse_default_response(bytes.fromhex("0483")) == (0x04, 0x83)
    with pytest.raises(ValueError, match="too short"):
        parse_default_response(b"\x04")


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0.04, 0x0400), (1.28, 0x8000), (10.0, 0xFFFF), (-1.0, 0), (0.0, 0)],
)
def test_smoothing_from_seconds(seconds: float, expected: int) -> None:
    """Smoothing scales 0..2.56 s onto 0..0xFFFF and clamps outside that."""
    assert smoothing_from_seconds(seconds) == expected


def test_read_attributes_standard_and_manufacturer() -> None:
    """Standard reads use fc 0x00; manufacturer reads set 0x04 and carry the code."""
    assert read_attributes(0x05, [0x0008, 0x0003]).hex() == "00050008000300"
    assert read_attributes(0x05, [0x0005], manufacturer=0x100B).hex() == "040b1005000500"


def test_write_attribute_uint8() -> None:
    """Manufacturer-specific write of one uint8 attribute."""
    assert write_attribute_uint8(0x09, 0x0005, 0xFE, manufacturer=0x100B).hex() == (
        "040b1009020500" + "20fe"
    )


def test_restore_commands() -> None:
    """Standard cluster commands set cluster-specific + disable-default-response (fc 0x11)."""
    assert off(0x03).hex() == "110300"
    assert move_to_level_with_on_off(0x04, 200, 4).hex() == "110404c80400"
    assert move_to_color(0x05, 0x1234, 0x5678, 4).hex() == "110507341278560400"
    assert move_to_color_temperature(0x06, 366, 4).hex() == "11060a6e010400"


def test_parse_zcl_standard_and_manufacturer() -> None:
    """Both header forms split into header fields and payload."""
    header, payload = parse_zcl(bytes.fromhex("18aa01" + "0000"))
    assert (header.manufacturer, header.tsn, header.command) == (None, 0xAA, 0x01)
    assert header.from_server
    assert not header.cluster_specific
    assert payload == bytes(2)

    header, payload = parse_zcl(bytes.fromhex("150b10bb03" + "0001"))
    assert (header.manufacturer, header.tsn, header.command) == (0x100B, 0xBB, 0x03)
    assert header.cluster_specific
    assert not header.from_server
    assert payload == bytes.fromhex("0001")


@pytest.mark.parametrize("data", ["", "1801", "1c0b10"])
def test_parse_zcl_rejects_short_frames(data: str) -> None:
    """Frames shorter than their header raise ValueError."""
    with pytest.raises(ValueError, match="too short"):
        parse_zcl(bytes.fromhex(data))


def test_parse_read_attributes_response() -> None:
    """Successful records decode by type; failed ones are skipped."""
    payload = bytes.fromhex(
        "0800" + "00" + "30" + "01"  # ColorMode enum8 = 1
        "0300" + "00" + "21" + "3412"  # CurrentX uint16 = 0x1234
        "0700" + "86"  # ColorTemperature unsupported
        "0000" + "00" + "10" + "01"  # boolean true
    )
    assert parse_read_attributes_response(payload) == {0x0008: 1, 0x0003: 0x1234, 0x0000: 1}


def test_parse_read_attributes_response_stops_at_unknown_type() -> None:
    """An unknown data type ends decoding (its size is unknown)."""
    payload = bytes.fromhex("0000" + "00" + "20" + "fe" + "0100" + "00" + "42" + "0361" + "6263")
    assert parse_read_attributes_response(payload) == {0x0000: 0xFE}

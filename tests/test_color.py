"""Tests for CIE xy colour-gamut mapping."""

from __future__ import annotations

import pytest

from hue_entertainment.color import (
    GAMUT_A,
    GAMUT_B,
    GAMUT_C,
    _clamp_to_gamut,
    _in_triangle,
    gamut_for_type,
    rgb_to_xy,
)


def test_white_maps_to_d65_white_point() -> None:
    """Full-scale white converts to the CIE D65 white point at full brightness."""
    x, y, brightness = rgb_to_xy(1.0, 1.0, 1.0)
    assert x == pytest.approx(0.3127, abs=0.001)
    assert y == pytest.approx(0.3290, abs=0.001)
    assert brightness == pytest.approx(1.0, abs=0.001)


def test_black_has_zero_brightness() -> None:
    """Pure black yields zero brightness; chromaticity stays a valid in-gamut point."""
    x, y, brightness = rgb_to_xy(0.0, 0.0, 0.0)
    assert brightness == 0.0
    assert _in_triangle((x, y), GAMUT_C.red, GAMUT_C.green, GAMUT_C.blue)


def test_pure_red_is_reddish_and_full_brightness() -> None:
    """Pure sRGB red lands in the red region with full brightness (max channel, not luminance)."""
    x, _, brightness = rgb_to_xy(1.0, 0.0, 0.0)
    assert x > 0.55
    assert brightness == pytest.approx(1.0, abs=0.001)


def test_output_is_always_in_gamut() -> None:
    """Every conversion result is inside the target gamut (clamping is a no-op on it)."""
    for gamut in (GAMUT_A, GAMUT_B, GAMUT_C):
        for rgb in (
            (1.0, 0.0, 0.0),
            (0.0, 1.0, 0.0),
            (0.0, 0.0, 1.0),
            (0.0, 1.0, 1.0),
            (1.0, 0.2, 0.6),
        ):
            x, y, _ = rgb_to_xy(*rgb, gamut=gamut)
            reclamped = _clamp_to_gamut((x, y), gamut)
            assert reclamped == pytest.approx((x, y), abs=1e-9)


def test_clamp_inside_point_is_identity() -> None:
    """A point already inside the gamut is returned unchanged."""
    inside = (0.35, 0.36)
    assert _clamp_to_gamut(inside, GAMUT_C) == inside


def test_clamp_outside_point_moves_onto_gamut() -> None:
    """A point well outside the gamut is pulled onto the triangle and is then stable."""
    outside = (0.95, 0.05)
    clamped = _clamp_to_gamut(outside, GAMUT_C)
    assert clamped != outside
    assert _clamp_to_gamut(clamped, GAMUT_C) == pytest.approx(clamped, abs=1e-9)


def test_gamut_for_type_lookup() -> None:
    """Bridge gamut types map to the right triangle; unknown/None default to the widest (C)."""
    assert gamut_for_type("A") is GAMUT_A
    assert gamut_for_type("b") is GAMUT_B
    assert gamut_for_type("C") is GAMUT_C
    assert gamut_for_type(None) is GAMUT_C
    assert gamut_for_type("other") is GAMUT_C


def test_vivid_pushes_saturated_colour_to_gamut_edge() -> None:
    """Vivid mode sends a fully saturated colour to the gamut corner, not the dimmer sRGB point."""
    ax, _, _ = rgb_to_xy(1.0, 0.0, 0.0, GAMUT_C)
    vx, vy, vbri = rgb_to_xy(1.0, 0.0, 0.0, GAMUT_C, vivid=True)
    assert (vx, vy) == pytest.approx(GAMUT_C.red, abs=0.01)
    assert abs(vx - ax) > 0.02  # more saturated than the colour-accurate point
    assert vbri == pytest.approx(1.0)


def test_vivid_keeps_greyscale_neutral() -> None:
    """A greyscale colour has no saturation to stretch, so vivid leaves it at the white point."""
    vx, vy, _ = rgb_to_xy(1.0, 1.0, 1.0, GAMUT_C, vivid=True)
    assert vx == pytest.approx(0.3127, abs=0.005)
    assert vy == pytest.approx(0.3290, abs=0.005)

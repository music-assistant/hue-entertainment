"""Tests for CIE xy colour-gamut mapping."""

from __future__ import annotations

import colorsys
import math

import pytest

from hue_entertainment.color import (
    GAMUT_A,
    GAMUT_B,
    GAMUT_C,
    Gamut,
    _clamp_to_gamut,
    _in_triangle,
    gamut_for_type,
    rgb_to_xy,
)

D65 = (0.3127, 0.3290)


def _barycentric(point: tuple[float, float], gamut: Gamut) -> tuple[float, float, float]:
    """Return the barycentric coordinates of ``point`` in the gamut triangle."""
    (rx, ry), (gx, gy), (bx, by) = gamut.red, gamut.green, gamut.blue
    det = (gy - by) * (rx - bx) + (bx - gx) * (ry - by)
    w_red = ((gy - by) * (point[0] - bx) + (bx - gx) * (point[1] - by)) / det
    w_green = ((by - ry) * (point[0] - bx) + (rx - bx) * (point[1] - by)) / det
    return (w_red, w_green, 1.0 - w_red - w_green)


def test_white_maps_to_d65_white_point() -> None:
    """Full-scale white converts to the CIE D65 white point at full brightness."""
    x, y, brightness = rgb_to_xy(1.0, 1.0, 1.0)
    assert x == pytest.approx(0.3127, abs=0.001)
    assert y == pytest.approx(0.3290, abs=0.001)
    assert brightness == pytest.approx(1.0, abs=0.001)


def test_black_has_zero_brightness() -> None:
    """Pure black yields zero brightness; chromaticity stays a valid in-gamut point."""
    for vivid in (False, True):
        x, y, brightness = rgb_to_xy(0.0, 0.0, 0.0, vivid=vivid)
        assert brightness == 0.0
        assert _in_triangle((x, y), GAMUT_C.red, GAMUT_C.green, GAMUT_C.blue)


def test_xy_known_answer_for_non_primary_colour() -> None:
    """
    XY mode matches the IEC 61966-2-1 sRGB -> CIE xy result for an in-gamut orange.

    Reference: sRGB (1.0, 0.5, 0.0) -> linear (1.0, 0.21404, 0.0) -> xy (0.54356, 0.40658),
    computed with the sRGB matrix derived from its primaries and D65 white.
    """
    x, y, brightness = rgb_to_xy(1.0, 0.5, 0.0, GAMUT_C)
    assert x == pytest.approx(0.54356, abs=1e-4)
    assert y == pytest.approx(0.40658, abs=1e-4)
    assert brightness == pytest.approx(1.0)


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


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_C])
def test_vivid_maps_primaries_onto_gamut_corners(gamut: Gamut) -> None:
    """Vivid mode puts pure R, G and B exactly on the gamut's corners at full brightness."""
    for rgb, corner in (
        ((1.0, 0.0, 0.0), gamut.red),
        ((0.0, 1.0, 0.0), gamut.green),
        ((0.0, 0.0, 1.0), gamut.blue),
    ):
        x, y, brightness = rgb_to_xy(*rgb, gamut=gamut, vivid=True)
        assert (x, y) == pytest.approx(corner, abs=1e-9)
        assert brightness == pytest.approx(1.0)


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_C])
@pytest.mark.parametrize("level", [1.0, 0.5, 0.1])
def test_vivid_maps_greys_onto_d65(gamut: Gamut, level: float) -> None:
    """Vivid mode keeps every grey level on the D65 white point."""
    x, y, brightness = rgb_to_xy(level, level, level, gamut, vivid=True)
    assert (x, y) == pytest.approx(D65, abs=1e-9)
    assert brightness == pytest.approx(level)


@pytest.mark.parametrize("gamut", [GAMUT_A, GAMUT_C])
def test_vivid_hue_sweep_is_continuous_and_in_gamut(gamut: Gamut) -> None:
    """
    A full-saturation hue sweep stays inside the gamut and turns steadily around D65.

    Every 5-degree hue step must advance the chromaticity's angle around D65 in the same
    direction, so no range of hues collapses onto one point (e.g. a gamut corner).
    """
    angles = []
    for step in range(72):
        rgb = colorsys.hsv_to_rgb(step / 72, 1.0, 1.0)
        x, y, _ = rgb_to_xy(*rgb, gamut=gamut, vivid=True)
        assert min(_barycentric((x, y), gamut)) >= -1e-9
        angles.append(math.atan2(y - D65[1], x - D65[0]))
    deltas = [
        (angles[(i + 1) % 72] - angles[i] + math.pi) % (2 * math.pi) - math.pi for i in range(72)
    ]
    assert all(math.radians(0.1) < delta < math.radians(30) for delta in deltas)
    assert sum(deltas) == pytest.approx(2 * math.pi)


def test_vivid_keeps_hues_between_primaries_apart() -> None:
    """Orange and yellow stay distinct from the red corner and from each other."""
    orange = rgb_to_xy(1.0, 0.5, 0.0, GAMUT_C, vivid=True)[:2]
    yellow = rgb_to_xy(1.0, 1.0, 0.0, GAMUT_C, vivid=True)[:2]
    assert orange == pytest.approx((0.59204, 0.38307), abs=1e-4)
    assert yellow == pytest.approx((0.41779, 0.51392), abs=1e-4)


def test_vivid_falls_back_to_xy_when_gamut_excludes_d65() -> None:
    """Gamut B does not contain D65, so vivid gives the colour-accurate xy result there."""
    assert min(_barycentric(D65, GAMUT_B)) < 0.0
    for rgb in ((1.0, 0.0, 0.0), (1.0, 0.5, 0.0), (0.2, 0.9, 0.4), (1.0, 1.0, 1.0)):
        assert rgb_to_xy(*rgb, gamut=GAMUT_B, vivid=True) == rgb_to_xy(*rgb, gamut=GAMUT_B)

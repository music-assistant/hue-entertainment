"""
CIE xy colour-gamut mapping for Hue Entertainment streaming.

A Hue Entertainment stream can carry colour either as raw RGB (``COLOR_SPACE_RGB``)
or as CIE xy chromaticity + brightness (``COLOR_SPACE_XY``). The bridge does not
gamut-correct an entertainment stream, so a client that wants accurate, consistent
colour across different Hue light models should convert RGB into the light's own
colour gamut here before streaming.

Pure functions, no I/O. The maths is the standard sRGB companding + sRGB -> CIE XYZ
(D65) linear transform, followed by a point-to-triangle projection onto the light's
gamut. These are public colour-science formulas (IEC 61966-2-1 / CIE 1931); the
per-model gamut triangles are the documented Hue gamut A/B/C primaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# A 2D CIE xy point.
Point = tuple[float, float]


class ColorMode(StrEnum):
    """
    How a client's RGB colours are encoded into the Hue Entertainment stream.

    - ``RGB``: send raw RGB. The bridge maps it to each bulb's full native gamut, giving the
      widest colour range and the most dynamic, organic fades - but the same RGB can look
      slightly different across mixed Hue models. Best for vivid, expressive light shows.
    - ``XY``: convert to the bulb's CIE xy gamut (colour-accurate). Hardware-independent, so
      colour is consistent across mixed models, at a narrower range and with steadier (less
      organic) fades. Best when exact, matching colour across different lights matters.
    - ``VIVID``: like ``XY`` but saturated colours are stretched to the gamut edge, recovering
      most of RGB's punch while keeping cross-model consistency.
    """

    RGB = "rgb"
    XY = "xy"
    VIVID = "vivid"


@dataclass(frozen=True)
class Gamut:
    """
    A Hue light's reproducible colour gamut, as a CIE xy triangle.

    :param red: xy chromaticity of the red primary.
    :param green: xy chromaticity of the green primary.
    :param blue: xy chromaticity of the blue primary.
    """

    red: Point
    green: Point
    blue: Point


# Documented Hue per-model gamuts. Gamut C is the widest (modern colour bulbs and
# lightstrips) and is the safe default when a light's gamut type is unknown.
GAMUT_A: Final[Gamut] = Gamut(red=(0.704, 0.296), green=(0.2151, 0.7106), blue=(0.138, 0.08))
GAMUT_B: Final[Gamut] = Gamut(red=(0.675, 0.322), green=(0.4091, 0.518), blue=(0.167, 0.04))
GAMUT_C: Final[Gamut] = Gamut(red=(0.692, 0.308), green=(0.17, 0.7), blue=(0.153, 0.048))

_GAMUTS: Final[dict[str, Gamut]] = {"A": GAMUT_A, "B": GAMUT_B, "C": GAMUT_C}

# CIE D65 white point - the neutral anchor that saturated colours are stretched away from.
_WHITE_D65: Final[Point] = (0.3127, 0.3290)


def gamut_for_type(gamut_type: str | None) -> Gamut:
    """
    Return the gamut triangle for a bridge-reported gamut type ("A"/"B"/"C").

    :param gamut_type: The light's gamut type as reported by the bridge, or None.
    """
    if gamut_type is None:
        return GAMUT_C
    return _GAMUTS.get(gamut_type.strip().upper(), GAMUT_C)


def rgb_to_xy(
    red: float, green: float, blue: float, gamut: Gamut = GAMUT_C, *, vivid: bool = False
) -> tuple[float, float, float]:
    """
    Convert an sRGB colour to a gamut-clamped CIE xy chromaticity + brightness.

    :param red: Red channel, 0.0-1.0.
    :param green: Green channel, 0.0-1.0.
    :param blue: Blue channel, 0.0-1.0.
    :param gamut: The target light's colour gamut; out-of-gamut colours are clamped to it.
    :param vivid: Stretch saturated colours out to the gamut edge (the light's most saturated
        rendering of that hue) instead of the colour-accurate sRGB point, for a punchier show.
    :return: ``(x, y, brightness)`` with x/y in 0.0-1.0 chromaticity and brightness 0.0-1.0.
    """
    rr = max(0.0, min(1.0, red))
    gg = max(0.0, min(1.0, green))
    bb = max(0.0, min(1.0, blue))
    r = _gamma(rr)
    g = _gamma(gg)
    b = _gamma(bb)

    # Linear sRGB -> CIE XYZ (D65); used only for the chromaticity (xy).
    x_val = r * 0.4124 + g * 0.3576 + b * 0.1805
    y_val = r * 0.2126 + g * 0.7152 + b * 0.0722
    z_val = r * 0.0193 + g * 0.1192 + b * 0.9505

    # Brightness is the colour's value (max channel), NOT its luminance Y: saturated reds
    # and blues have very low Y, so using Y would dim vivid colours - max keeps full punch.
    brightness = max(rr, gg, bb)

    total = x_val + y_val + z_val
    if total <= 0.0:
        # Pure black: park chromaticity at the gamut centre (brightness carries the result).
        cx = (gamut.red[0] + gamut.green[0] + gamut.blue[0]) / 3.0
        cy = (gamut.red[1] + gamut.green[1] + gamut.blue[1]) / 3.0
        return (cx, cy, brightness)

    point = (x_val / total, y_val / total)
    cx, cy = _clamp_to_gamut(point, gamut)
    if vivid:
        cx, cy = _saturate_to_edge((cx, cy), gamut, max(rr, gg, bb), min(rr, gg, bb))
    return (cx, cy, brightness)


# -- internals --


def _saturate_to_edge(point: Point, gamut: Gamut, mx: float, mn: float) -> Point:
    """
    Push a chromaticity out toward the gamut edge, scaled by the colour's saturation.

    A fully saturated colour lands on the gamut boundary (the light's most saturated
    rendering of that hue); a greyscale colour stays at the gamut's white point.
    """
    saturation = (mx - mn) / mx if mx > 0.0 else 0.0
    wx, wy = _WHITE_D65
    # Exaggerate the hue direction far past the gamut, then clamp back: the clamp lands on
    # the boundary point for this hue (its most saturated reproducible chromaticity).
    far = (wx + (point[0] - wx) * 100.0, wy + (point[1] - wy) * 100.0)
    ex, ey = _clamp_to_gamut(far, gamut)
    return (wx + (ex - wx) * saturation, wy + (ey - wy) * saturation)


def _gamma(channel: float) -> float:
    """Apply inverse sRGB companding (gamma) to a 0.0-1.0 channel value."""
    if channel > 0.04045:
        return float(((channel + 0.055) / 1.055) ** 2.4)
    return channel / 12.92


def _clamp_to_gamut(point: Point, gamut: Gamut) -> Point:
    """Return ``point`` if inside the gamut triangle, else the closest point on its edge."""
    if _in_triangle(point, gamut.red, gamut.green, gamut.blue):
        return point
    candidates = (
        _closest_on_segment(point, gamut.red, gamut.green),
        _closest_on_segment(point, gamut.green, gamut.blue),
        _closest_on_segment(point, gamut.blue, gamut.red),
    )
    return min(candidates, key=lambda c: _distance_sq(point, c))


def _in_triangle(p: Point, a: Point, b: Point, c: Point) -> bool:
    """Return True if ``p`` lies inside (or on) triangle ``a, b, c``."""
    d1 = _sign(p, a, b)
    d2 = _sign(p, b, c)
    d3 = _sign(p, c, a)
    has_neg = d1 < 0.0 or d2 < 0.0 or d3 < 0.0
    has_pos = d1 > 0.0 or d2 > 0.0 or d3 > 0.0
    return not (has_neg and has_pos)


def _sign(p: Point, a: Point, b: Point) -> float:
    """Signed area (cross product) of the (a->b, a->p) ordering — side-of-line test."""
    return (p[0] - b[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (p[1] - b[1])


def _closest_on_segment(p: Point, a: Point, b: Point) -> Point:
    """Return the closest point to ``p`` on the line segment ``a``-``b``."""
    abx, aby = b[0] - a[0], b[1] - a[1]
    length_sq = abx * abx + aby * aby
    if length_sq <= 0.0:
        return a
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / length_sq
    t = max(0.0, min(1.0, t))
    return (a[0] + abx * t, a[1] + aby * t)


def _distance_sq(p: Point, q: Point) -> float:
    """Squared Euclidean distance between two points."""
    return (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2

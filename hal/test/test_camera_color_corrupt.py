"""Tests for the camera ISP color-corruption detector.

The detector flags the wedged-ISP failure mode where frames keep changing
but chroma is garbage: posterized oversaturated green plus complementary
magenta/pink patches. Thresholds were calibrated against a live corrupt
specimen (green frac 0.19, magenta frac 0.012 at sat>=100) versus clean
office scenes (0.000 / 0.000) — these synthetic fixtures mirror those
measured distributions.
"""
import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from hal.drivers.camera.video_capture_device import LocalVideoCaptureDevice


def _bgr_from_hsv(h: int, s: int, v: int) -> tuple[int, int, int]:
    px = np.array([[[h, s, v]]], dtype=np.uint8)
    b, g, r = cv2.cvtColor(px, cv2.COLOR_HSV2BGR)[0, 0]
    return int(b), int(g), int(r)


def _frame(fill_bgr: tuple[int, int, int]) -> np.ndarray:
    frame = np.zeros((23, 40, 3), dtype=np.uint8)
    frame[:] = fill_bgr
    return frame


def test_corrupt_green_plus_magenta_detected():
    # Mirror the live specimen: ~19% saturated acid green, ~1.5% magenta,
    # rest a desaturated office-like gray.
    frame = _frame((120, 128, 125))
    frame[0:5, :] = _bgr_from_hsv(60, 220, 180)  # acid green rows (~22%)
    frame[6, 0:1] = _bgr_from_hsv(150, 140, 180)  # magenta speck
    frame[7, 0:1] = _bgr_from_hsv(155, 140, 170)
    frame[8, 0:12] = _bgr_from_hsv(150, 140, 180)  # magenta patch (~1.5%)
    assert LocalVideoCaptureDevice._looks_color_corrupt(frame)


def test_clean_desaturated_scene_not_detected():
    assert not LocalVideoCaptureDevice._looks_color_corrupt(_frame((120, 128, 125)))


def test_single_hue_green_wall_not_detected():
    # A green wall / plant / LED spill saturates ONE hue family only — the
    # complementary-magenta requirement must keep this from triggering.
    assert not LocalVideoCaptureDevice._looks_color_corrupt(
        _frame(_bgr_from_hsv(60, 220, 180))
    )


def test_single_hue_magenta_flood_not_detected():
    assert not LocalVideoCaptureDevice._looks_color_corrupt(
        _frame(_bgr_from_hsv(150, 220, 180))
    )


def test_dark_frame_not_detected():
    # Near-black pixels are excluded by the value floor regardless of hue.
    assert not LocalVideoCaptureDevice._looks_color_corrupt(
        _frame(_bgr_from_hsv(60, 255, 20))
    )

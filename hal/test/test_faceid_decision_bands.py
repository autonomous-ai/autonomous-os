"""Where the face-ID decision bands sit, and why (#429, #299).

Three banks, three bars. The enrolled UPLOADS are a phone photo matched against
the device camera, so the same person scores lower there than camera-to-camera;
they match at FACE_MATCH_THRESHOLD. The two auto-captured banks — a user's
.extended views and the stranger bank — are camera-to-camera and were admitted
by the device's own guess, so both need the higher FACE_EXTENDED_THRESHOLD /
FACE_STRANGER_THRESHOLD bar. Below the owner banks' negative_threshold and below
the stranger bar, a face is corroborated and minted; the stranger bank is NOT
part of the negative test, because with N rows some row is nearly always above
0.2 and a genuinely new person could never mint (#429).
"""

import hal.config as config


def test_stranger_bank_is_matched_at_least_as_strictly_as_the_extended_bank():
    """Both are auto-captured, same-camera, single-view banks (#429)."""
    assert config.FACE_STRANGER_THRESHOLD >= config.FACE_EXTENDED_THRESHOLD


def test_upload_match_bar_sits_in_the_gap_between_three_quarter_and_frontal():
    """Measured on orange-lamp 2026-09-16: owner frontal 0.60-0.85, owner 3/4
    pose 0.29-0.34, strangers <= 0.28. 0.40 sits in the gap; 0.30 sat inside
    the 3/4 cluster where genuine and impostor overlap."""
    assert 0.35 <= config.FACE_MATCH_THRESHOLD <= 0.45


def test_becoming_a_reference_view_is_stricter_than_being_recognised():
    """Being recognised must not be enough to become a reference view (#299)."""
    assert config.FACE_EXTEND_MIN_ENROLL_SIM > config.FACE_MATCH_THRESHOLD


def test_stranger_bar_leaves_headroom_over_the_measured_false_accepts():
    """#429: stale rows false-accepted the same person at 0.346 / 0.385 / 0.318."""
    assert config.FACE_STRANGER_THRESHOLD - 0.385 >= 0.05

"""Regression checks for normalized servo recordings."""

import csv
import math
from pathlib import Path


RECORDINGS_DIR = Path(__file__).parents[1] / "recordings"
POSITION_MIN = -100.0
POSITION_MAX = 100.0


def test_builtin_recordings_stay_within_motor_normalized_range():
    """The motor bus silently clamps normalized positions outside [-100, 100]."""
    for recording in sorted(RECORDINGS_DIR.glob("*.csv")):
        with recording.open(newline="") as source:
            reader = csv.DictReader(source)
            assert reader.fieldnames is not None, f"{recording.name}: missing header"

            for line_number, frame in enumerate(reader, start=2):
                for joint, raw_position in frame.items():
                    if joint == "timestamp":
                        continue

                    position = float(raw_position)
                    assert math.isfinite(position), (
                        f"{recording.name}:{line_number}: {joint} is not finite"
                    )
                    assert POSITION_MIN <= position <= POSITION_MAX, (
                        f"{recording.name}:{line_number}: {joint}={position} is outside "
                        f"[{POSITION_MIN}, {POSITION_MAX}]"
                    )

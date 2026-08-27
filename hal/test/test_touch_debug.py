"""TOUCH-DEBUG tracer: inert when off, correct arithmetic when on.

The first test is the important one. The tracer sits inside the lgpio callback
path, so "disabled costs nothing and touches no disk" is the guarantee that lets
it ship on by default in the repo and off on every device.
"""

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path


def _fresh(enabled: bool, out_dir: str = "", pads: str = ""):
    """Re-import the module with a given env. `_enabled` is resolved once and
    cached, so each case needs a clean module."""
    os.environ["HAL_TOUCH_DEBUG"] = "true" if enabled else "false"
    if out_dir:
        os.environ["HAL_TOUCH_DEBUG_DIR"] = out_dir
    else:
        os.environ.pop("HAL_TOUCH_DEBUG_DIR", None)
    if pads:
        os.environ["HAL_TOUCH_DEBUG_PADS"] = pads
    else:
        os.environ.pop("HAL_TOUCH_DEBUG_PADS", None)
    import hal.drivers.touch_debug as td

    importlib.reload(td)
    return td


class TestDisabled(unittest.TestCase):
    def test_every_entry_point_is_a_noop_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(False, tmp)
            td.start_cycle(0, [96, 98, 100])
            td.note_edge(96, 0)
            td.note_session_end(1)
            td.note_decision("TAP", "reason", 1)
            td.note_action("single_click_action", "TTP223")
            td.finish("TAP")
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_disabled_never_opens_a_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(False, tmp)
            td.start_cycle(0, [96])
            self.assertIsNone(td._current)


class TestSessionArithmetic(unittest.TestCase):
    """The Phase 2 measurement. Deltas are what decide swipe viability, so they
    are worth pinning even before a classifier exists."""

    def _trace(self, tmp):
        return json.loads(next(Path(tmp).glob("*.json")).read_text())

    def test_cross_talk_burst_records_deltas_and_primary_pad(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp, pads="96=S1,98=S2,100=S4")
            td.start_cycle(0, [96, 98, 100])
            # One finger press: three pads fire close together, then release.
            td.note_edge(96, 0)
            td.note_edge(98, 0)
            td.note_edge(100, 0)
            td.note_edge(96, 1)
            td.note_session_end(1)
            td.note_decision("TAP", "decision window expired at count=1", 1)
            td.finish("TAP")
            import time as _t
            _t.sleep(0.2)  # writer is a daemon thread

            tr = self._trace(tmp)
            s = tr["sessions"][0]
            self.assertEqual(s["pads_touched"], ["S1", "S2", "S4"])
            self.assertEqual(s["primary_pad"], "S1")
            # Release edge (level 1) must not appear in first_touch_order —
            # it is FastMode's auto-drop, not a second contact.
            self.assertEqual(len(s["first_touch_order"]), 3)
            self.assertEqual(len(s["adjacent_deltas_ms"]), 2)

    def test_suppressed_edges_are_recorded_but_do_not_form_contacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp)
            td.start_cycle(0, [96, 98])
            td.note_edge(96, 0, suppressed=True)
            td.note_edge(98, 0, suppressed=True)
            td.note_session_end(0)
            td.finish("IGNORED-settle")
            import time as _t
            _t.sleep(0.2)

            tr = self._trace(tmp)
            self.assertEqual(len(tr["edges"]), 2)
            self.assertTrue(all(e["suppressed"] for e in tr["edges"]))
            # Suppressed edges never enter a contact, so no pad was "touched".
            self.assertEqual(tr["sessions"][0]["pads_touched"], [])


class TestTraversal(unittest.TestCase):
    """Reversal count is what separates a swipe from a stroke."""

    def _traversal(self, tmp):
        tr = json.loads(next(Path(tmp).glob("*.json")).read_text())
        return tr["traversal"]

    def _run(self, tmp, pad_lines):
        td = _fresh(True, tmp, pads="96=S1,98=S2,100=S4")
        td.start_cycle(0, [96, 98, 100])
        for n, line in enumerate(pad_lines, 1):
            td.note_edge(line, 0)
            td.note_session_end(n)
        td.finish("TEST")
        import time as _t
        _t.sleep(0.2)

    def test_monotonic_traversal_is_swipe_shaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, [96, 98, 100])
            tv = self._traversal(tmp)
            self.assertEqual(tv["reversals"], 0)
            self.assertTrue(tv["monotonic"])
            self.assertEqual(tv["distinct_pads"], 3)

    def test_turning_around_is_stroke_shaped(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, [96, 98, 100, 98, 96])
            tv = self._traversal(tmp)
            self.assertEqual(tv["reversals"], 1)
            self.assertFalse(tv["monotonic"])

    def test_no_contacts_is_distinct_from_one_pad(self):
        """A settle burst forms no contact at all. Reporting that as "stayed on
        one pad" would imply a finger landed, which is exactly the wrong read
        when the question is whether a pad fired spuriously."""
        with tempfile.TemporaryDirectory() as tmp:
            td = _fresh(True, tmp)
            td.start_cycle(0, [96, 98])
            td.note_edge(96, 1, suppressed=True)
            td.note_session_end(0)
            td.finish("IGNORED-settle")
            import time as _t
            _t.sleep(0.2)
            tv = json.loads(next(Path(tmp).glob("*.json")).read_text())["traversal"]
            self.assertEqual(tv["distinct_pads"], 0)
            self.assertEqual(tv["verdict"], "no contacts recorded")

    def test_repeated_same_pad_has_no_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, [96, 96, 96])
            tv = self._traversal(tmp)
            self.assertEqual(tv["distinct_pads"], 1)
            self.assertIn("no traversal", tv["verdict"])

    def test_axis_source_is_reported_as_assumed_without_boards_json_axis(self):
        """Reversal against a guessed axis is still evidence, but the reader
        has to know it is provisional until the pads are physically labelled."""
        with tempfile.TemporaryDirectory() as tmp:
            self._run(tmp, [96, 98, 100])
            self.assertEqual(self._traversal(tmp)["axis_source"], "assumed line order")


class TestPadLabels(unittest.TestCase):
    """`_pad` reads the map `_init` resolves, so these call it first — every
    real caller reaches `_pad` only from an entry point that has already
    initialised."""

    def test_defaults_to_line_number_when_unmapped(self):
        td = _fresh(True, tempfile.gettempdir())
        td._init()
        self.assertEqual(td._pad(97), "L97")

    def test_malformed_env_entries_are_skipped_not_raised(self):
        td = _fresh(True, tempfile.gettempdir(), pads="96=S1,garbage,=X,98=S2")
        td._init()
        self.assertEqual(td._pad(96), "S1")
        self.assertEqual(td._pad(98), "S2")
        # The two malformed entries are dropped, not raised on.
        self.assertEqual(td._pad(100), "L100")


if __name__ == "__main__":
    unittest.main()

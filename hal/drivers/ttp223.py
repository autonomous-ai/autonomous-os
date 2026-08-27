"""TTP223 capacitive touchpad handler (dog-head touch surface).

How many pads and which lines is board data, not a constant here — see the
`touch` entry in hal/board/boards.json. It has changed twice (the pads were
relocated to escape LED/servo/audio coupling), so any count written into this
docstring goes stale; ask the board profile.

Two gestures:
- Single tap   → stop speaker / unmute mic (same as GPIO button single click)
- Pet / stroke → playful TTS response ("hihi nhột quá!", etc.)

Destructive gestures (reboot / shutdown) are intentionally OFF on TTP223
because the IC on this board runs in FastMode: output drops LOW within
~50ms of touch even with finger still on the pad, so a true "hold 5s"
is impossible without rewiring the FM pin. GPIO button still owns those.

Gesture detection is two-layered:

1. Session: any edge (rising or falling, any pad) keeps a 200ms window
   alive. Coalesces the burst of cross-talk + FastMode auto-LOW edges
   from one physical touch into a single "session". One session = one
   touch event from the user's POV.

2. Pet vs tap: after a session ends, wait DECISION_WINDOW (1.2s) for
   more sessions. A second session inside the window = the user is
   stroking the head → head_pat_action. One session that's not followed
   by more = single tap → single_click_action.

The 1.2s decision delay is the cost of distinguishing the two gestures
on this hardware — TTP223 FastMode can't tell us "finger currently down",
so we infer continuous stroking from session frequency.

Two parts of the tap action escape the wait, both at the FIRST session
end of a burst (~0.2s after the finger lifts): in-flight TTS is stopped
immediately, and a short ack chime plays — stop latency and "did it hear
me" feedback are the parts of the gesture users actually feel. Deliberate
semantic change that comes with it: petting her head while she talks now
cuts her off (the pet giggle follows) — touch means "attention here"
either way. Only TTS is cut early; music keeps playing until the burst
actually resolves as a tap, so petting during music never kills the
playlist. Unmute + the listening cue also still wait for resolution.
"""

import logging
import os
import threading
import time

import hal.app_state as state
from hal.board.board import board_profile
from hal.drivers import touch_debug
from hal.drivers.button_actions import (
    head_pat_action,
    mic_toggle_action,
    play_ack_chime,
    single_click_action,
    swipe_action,
)

logger = logging.getLogger(__name__)

# TTP223 pad wiring (chip / lines) lives in the board platform layer —
# hal/board/board.py (BoardProfile.touch).

# Session gap: edges within this window of the previous edge belong to
# the same session. 200ms comfortably exceeds the observed burst length
# (~30-100ms across the pads a single touch reaches) while staying below a
# natural inter-tap gap. That figure was measured on green-lamp; HAL_TOUCH_DEBUG
# re-measures it per unit (see hal/drivers/touch_debug.py).
SESSION_GAP_S = 0.2

# Decision window: after a session ends, wait this long for more
# sessions before classifying as a single tap. Field-measured stroke
# pace on this hardware is 0.8-1.2s per beat (FastMode forces a
# tap-tap-tap rhythm rather than continuous motion). 1.2s catches the
# slowest natural stroke. Cost: single tap responds 1.2s after release
# — the price of preventing a spurious "single click" at the start of
# every pet motion.
DECISION_WINDOW_S = 1.2

# Number of sessions to qualify as pet. 2 keeps pet detection generous
# for continuous stroking — any second touch within DECISION_WINDOW
# fires pet immediately. The cost is that two intentional single taps
# spaced <0.9s apart will fire pet instead of two singles; this is
# acceptable because users who want two stops just need to space their
# taps slightly (1s+ apart).
PET_SESSION_THRESHOLD = 2

# After head_pat fires, swallow further sessions for this long so a
# continuous stroke doesn't produce stuttering "single click" interjections
# between pet responses. Every session inside the window extends the
# window — petting is finished only when the user stops touching for
# PET_COOLDOWN_S consecutively.
PET_COOLDOWN_S = 1.5

# Settle window after claiming the lines. lgpio reports each line's current
# level as an initial edge the moment the alert callback is registered. The
# pads rest HIGH, so without this guard those startup reports are read as a
# real touch and fire a phantom single_click ~DECISION_WINDOW after every HAL
# start (Restart=always makes it recur). Ignore all edges for this long after
# claim so the startup transient never starts a session.
SETTLE_S = 0.5

# --- Traversal classification (swipe / double tap / spatial pet) ------------
#
# DEFAULT OFF. The labelled measurement that was supposed to authorise this has
# not been run — see the build plan's Phase 2.6. With the flag off, every path
# below falls through to the two-gesture behaviour this driver has always had.
#
# A swipe and a stroke are the same motion until one of them turns around, so
# the discriminator is REVERSAL, not timing: a swipe is one monotonic pass, a
# pet turns around at least once. That also means a reversal can fire pet
# IMMEDIATELY — once the direction changes the gesture cannot be a swipe — which
# preserves the responsiveness trade documented above.
SWIPE_ENABLED = os.environ.get("HAL_TOUCH_SWIPE", "false").lower() in ("1", "true", "yes")

# Distinct pads a traversal must span to count as a swipe. With three wired this
# is all of them, so there is no margin: one dropped edge collapses the gesture.
SWIPE_MIN_PADS = 3

# Minimum gap between adjacent pads before a traversal counts as deliberate
# rather than cross-talk. PROVISIONAL — device data from orange-lamp on
# 2026-08-27 (n=36, unlabelled) put inter-pad deltas on one continuous
# 7-345 ms distribution with no gap in it, so this cannot yet be set from
# evidence. 40 ms excludes the tightest third of that set. Recalibrate from the
# labelled run before shipping the flag on; HAL_TOUCH_DEBUG records the deltas
# this is measured against.
SWIPE_MIN_GAP_MS = float(os.environ.get("HAL_TOUCH_SWIPE_MIN_GAP_MS", "40"))


def _board_label() -> str:
    return board_profile().id


def _resolve_board_config():
    """Return (chip, lines, axis) or None if TTP223 isn't wired on this board."""
    touch = board_profile().touch
    return (touch.chip, touch.lines, touch.axis) if touch else None


class TTP223Handler:
    def __init__(self):
        self._lgpio = None
        self._handle = None
        self._callbacks = []
        self._chip = 0
        self._lines = []
        # Physical pad order along the swipe axis; None until measured (2.2).
        self._axis = None
        # Per-contact first-touch order for the current gesture cycle:
        # [[(line, ts), ...], ...], one inner list per contact. NOT flattened —
        # device-measured 2026-08-27: several fingers landing in one place spread
        # across pads exactly like one finger moving, so a flat cross-contact
        # sequence cannot tell a 3-finger double tap from a stroke. The two
        # questions need different layers: whether a single contact TRAVERSED
        # (swipe), and whether successive contacts stayed in the same PLACE
        # (double tap).
        self._contacts = []
        self._contact = []
        self._lock = threading.Lock()
        # Session-end timer: fires SESSION_GAP_S after the last edge.
        self._session_end_timer = None
        # Decision timer: fires DECISION_WINDOW_S after the last session
        # ended, resolving how many sessions accumulated → tap vs pet.
        self._decision_timer = None
        self._session_count = 0
        # monotonic deadline before which incoming sessions are silently
        # eaten (cooldown after pet to avoid stuttering single_clicks
        # during a continuous stroke).
        self._pet_cooldown_until = 0.0
        # monotonic deadline before which edges are ignored (startup transient
        # from claiming the lines — see SETTLE_S).
        self._ignore_edges_until = 0.0

    def start(self):
        config = _resolve_board_config()
        if config is None:
            logger.info(
                "TTP223 disabled: board is %s (only wired on orangepi-sun60)",
                _board_label(),
            )
            return

        import lgpio

        self._chip, self._lines, self._axis = config
        self._lgpio = lgpio

        # Arm the settle window now, before claiming: the callback registration
        # below emits an initial edge per line (the current resting level).
        self._ignore_edges_until = time.monotonic() + SETTLE_S

        try:
            self._handle = lgpio.gpiochip_open(self._chip)
        except Exception as e:
            logger.warning("TTP223 gpiochip_open(%d) failed: %s", self._chip, e)
            return

        for line in self._lines:
            try:
                lgpio.gpio_claim_alert(
                    self._handle, line, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP
                )
                cb = lgpio.callback(
                    self._handle, line, lgpio.BOTH_EDGES, self._on_edge
                )
                self._callbacks.append(cb)
            except Exception as e:
                logger.warning("TTP223 claim line %d failed: %s", line, e)

        if not self._callbacks:
            logger.warning("TTP223 no lines claimed -- disabled")
            return

        logger.info(
            "TTP223 ready on gpiochip%d lines %s (session %dms, decision %dms, pet>=%d sessions)",
            self._chip,
            self._lines,
            int(SESSION_GAP_S * 1000),
            int(DECISION_WINDOW_S * 1000),
            PET_SESSION_THRESHOLD,
        )

    def _on_edge(self, chip, gpio, level, tick):
        # Drop the startup transient: lgpio reports each line's initial level
        # as an edge when the callback is registered. The resting-HIGH pads
        # would otherwise fire a phantom gesture on every HAL start.
        if time.monotonic() < self._ignore_edges_until:
            # Traced anyway, flagged: a suppressed edge and a pad that never
            # fired look identical in a trace that omits them.
            touch_debug.start_cycle(self._chip, self._lines)
            touch_debug.note_edge(gpio, level, suppressed=True)
            return
        touch_debug.start_cycle(self._chip, self._lines, self._axis)
        touch_debug.note_edge(gpio, level)
        # Pads rest HIGH (pull-up), so level 0 is the TOUCH edge; level 1 is
        # FastMode's auto-release and says nothing about where the finger is.
        # Recorded unconditionally, not just when SWIPE_ENABLED: it costs one
        # list append and it means HAL_TOUCH_DEBUG measures the same sequence
        # the classifier would see, which is the point of the instrument.
        if level == 0:
            with self._lock:
                if not self._contact or self._contact[-1][0] != gpio:
                    self._contact.append((gpio, time.monotonic()))
        # Any edge keeps the current session alive — cross-talk and
        # FastMode auto-LOW produce flurries of edges per physical
        # touch; coalesce them by resetting the session-end timer.
        with self._lock:
            if self._session_end_timer is not None:
                self._session_end_timer.cancel()
            self._session_end_timer = threading.Timer(
                SESSION_GAP_S, self._on_session_end
            )
            self._session_end_timer.daemon = True
            self._session_end_timer.start()

    def _close_contact(self):
        """Move the open contact into the cycle's list. Called at session end."""
        with self._lock:
            if self._contact:
                self._contacts.append(self._contact)
            self._contact = []

    def _classify(self):
        """Read the cycle as (is_swipe, moved, min_gap_ms, n_contacts).

        Two independent questions, because the hardware answers them at
        different layers (device-measured on orange-lamp, 2026-08-27):

        * **Did one contact traverse?** All four real swipes in that run arrived
          as `sessions=1` — a stroke across the pads completes inside a single
          contact, because FastMode's auto-release does not end the session.
          So swipe is an INTRA-contact question: one contact whose first-touch
          order runs monotonically across every pad with wide enough gaps.

        * **Did successive contacts stay in one place?** Several fingers landing
          together spread across pads exactly like one finger moving, so pad
          COUNT cannot answer this and neither can the first pad to fire — in
          the same run a 3-finger double tap reported primary L98 then L96.
          What does separate them is whether a pad is common to every contact:
          tapping one spot always re-touches its centre pad, a stroke moves off
          it. `moved` is the negation of that.
        """
        with self._lock:
            contacts = [list(c) for c in self._contacts]
            if self._contact:
                contacts.append(list(self._contact))
        axis = self._axis or self._lines
        pos_of = {line: i for i, line in enumerate(axis)}

        is_swipe = False
        min_gap = 0.0
        for c in contacts:
            positions = [pos_of[l] for l, _ in c if l in pos_of]
            if len(positions) < SWIPE_MIN_PADS:
                continue
            if len({l for l, _ in c}) < SWIPE_MIN_PADS:
                continue
            deltas = [b - a for a, b in zip(positions, positions[1:]) if b != a]
            if not deltas or any((a > 0) != (b > 0) for a, b in zip(deltas, deltas[1:])):
                continue  # turned around — a stroke, not a pass
            gaps = [(b - a) * 1000.0 for (_, a), (_, b) in zip(c, c[1:])]
            if gaps and min(gaps) >= SWIPE_MIN_GAP_MS:
                is_swipe = True
                min_gap = min(gaps)

        sets = [{l for l, _ in c} for c in contacts if c]
        # A pad present in EVERY contact means the hand never left that spot.
        common = set.intersection(*sets) if sets else set()
        moved = len(sets) >= 2 and not common
        return is_swipe, moved, min_gap, len(sets)

    def _reset_cycle(self):
        """Clear the per-gesture contacts once a gesture has resolved."""
        with self._lock:
            self._contacts = []
            self._contact = []

    def _on_session_end(self):
        # One physical touch ended.
        #
        # 1) If we're still inside the pet cooldown (user is mid-stroke,
        #    a head_pat fired recently), extend the cooldown and bail —
        #    don't count, don't fire. This prevents single_clicks from
        #    interleaving between pets during one continuous stroke.
        # 2) Otherwise increment the count. If it hits PET threshold,
        #    fire head_pat immediately and arm the cooldown.
        # 3) Else schedule the decision timer to classify accumulated
        #    sessions as a single tap when the user stops touching.
        fire_pet = False
        grab_floor = False
        swallowed = False
        # Close the contact first, then read the cycle. Both take the lock
        # themselves, so they run before entering the block below.
        self._close_contact()
        _is_swipe, _moved, _min_gap, _n = self._classify()
        pet_now = False
        with self._lock:
            self._session_end_timer = None
            now = time.monotonic()
            if now < self._pet_cooldown_until:
                # Still petting — swallow this session, extend cooldown.
                self._pet_cooldown_until = now + PET_COOLDOWN_S
                # Also cancel any pending decision_timer left over from
                # the pre-pet count: that count was already consumed
                # when pet fired, so no single_click should fire.
                if self._decision_timer is not None:
                    self._decision_timer.cancel()
                    self._decision_timer = None
                logger.debug("TTP223 session ignored (pet cooldown)")
                swallowed = True
                count = self._session_count
            else:
                self._session_count += 1
                count = self._session_count
                # Pet's fast path needs the hand to have MOVED between contacts,
                # and at least two of them. Gated on the SESSION count rather
                # than the number of contacts that recorded pads: a contact whose
                # touch edge fell the other side of a session boundary records
                # only a release and contributes nothing (device trace 154507).
                # Gated on >=2 at all because a single press's cross-talk
                # ordering can double back — device-measured, a one-contact trace
                # fired PET off nothing but noise.
                pet_now = (
                    SWIPE_ENABLED and _moved and count >= PET_SESSION_THRESHOLD
                )
                # First session of a burst: cut in-flight TTS NOW rather than
                # after the decision window (see module docstring). Checked
                # outside the lock — it does I/O.
                grab_floor = count == 1
                logger.debug("TTP223 session ended (count=%d)", count)
                if SWIPE_ENABLED:
                    # Reversal is decidable the moment it happens and cannot
                    # later become a swipe, so pet keeps its fast path. Every
                    # other outcome (swipe, double tap, tap, count-fallback pet)
                    # needs to see the whole gesture, so it waits for the
                    # decision window. Checked outside the lock below.
                    fire_pet = pet_now
                    if not fire_pet:
                        if self._decision_timer is not None:
                            self._decision_timer.cancel()
                        self._decision_timer = threading.Timer(
                            DECISION_WINDOW_S, self._on_decision
                        )
                        self._decision_timer.daemon = True
                        self._decision_timer.start()
                    else:
                        if self._decision_timer is not None:
                            self._decision_timer.cancel()
                            self._decision_timer = None
                        self._session_count = 0
                        self._pet_cooldown_until = now + PET_COOLDOWN_S
                elif count >= PET_SESSION_THRESHOLD:
                    if self._decision_timer is not None:
                        self._decision_timer.cancel()
                        self._decision_timer = None
                    self._session_count = 0
                    self._pet_cooldown_until = now + PET_COOLDOWN_S
                    fire_pet = True
                else:
                    if self._decision_timer is not None:
                        self._decision_timer.cancel()
                    self._decision_timer = threading.Timer(
                        DECISION_WINDOW_S, self._on_decision
                    )
                    self._decision_timer.daemon = True
                    self._decision_timer.start()
        # Tracing and actions stay outside the lock: both do I/O, and the
        # session state above is already committed.
        touch_debug.note_session_end(count)
        if swallowed:
            # Closed as its own cycle rather than folded into the pet that
            # armed the cooldown: one file per resolved outcome, and a stroke
            # that keeps extending the window would otherwise grow one file
            # without bound.
            touch_debug.note_decision(
                "IGNORED", "session inside pet cooldown; cooldown extended", count
            )
            touch_debug.finish("IGNORED-pet_cooldown")
            self._reset_cycle()
            return
        if grab_floor:
            self._ack_first_session()
        if fire_pet:
            reason = (
                f"{_n} contacts with no pad in common -- the hand moved"
                if pet_now
                else f"session count reached {PET_SESSION_THRESHOLD}"
            )
            # Same finish-before-dispatch rule as _dispatch: the trace closes
            # before the action can block or raise.
            touch_debug.note_decision("PET", reason, count)
            touch_debug.note_action("head_pat_action", "TTP223")
            touch_debug.finish("PET")
            self._reset_cycle()
            head_pat_action(source="TTP223")

    def _ack_first_session(self):
        """Instant ack for the first touch session of a burst: cut in-flight
        TTS (barge-in), then sound the ack chime so the user gets sub-250ms
        confirmation the touch registered. Chime is gesture-neutral, so it
        fires for taps AND the first stroke of a pet; the spoken cue / pet
        phrase still waits for tap-vs-pet resolution. TTS stop only — no
        unmute, no music stop; those wait for resolution too. Off-thread
        because stop_tts and the chime write do I/O and this is called from
        a Timer thread that must go on to arm the decision timer promptly."""

        def _run():
            try:
                tts = state.tts_service
                if tts is not None and tts.speaking:
                    logger.info("TTP223 first touch during TTS -- stopping speech early")
                    from hal.routes.voice import stop_tts
                    stop_tts()
                play_ack_chime(source="TTP223")
            except Exception as e:
                logger.warning("TTP223 first-session ack failed: %s", e)

        threading.Thread(
            target=_run, daemon=True, name="ttp223-touch-ack"
        ).start()

    def _on_decision(self):
        with self._lock:
            count = self._session_count
            self._session_count = 0
            self._decision_timer = None
        self._close_contact()
        is_swipe, moved, min_gap, _n = self._classify()

        if count < 1:
            # The decision timer outlived its count (pet consumed it inline).
            touch_debug.note_decision("NONE", "decision timer fired at count=0", count)
            touch_debug.finish("IGNORED-no_sessions")
            self._reset_cycle()
            return

        if SWIPE_ENABLED:
            # 1) SWIPE — one contact that ran cleanly across every pad in order.
            #    Checked first so a resolved swipe never also fires a tap.
            if is_swipe:
                self._dispatch(
                    "SWIPE", f"one contact traversed all pads, min gap {min_gap:.0f}ms",
                    count, "swipe_action", swipe_action,
                )
                return

            if count >= PET_SESSION_THRESHOLD:
                # 2) PET — successive contacts with no pad in common: the hand
                #    moved. The inline path above catches most of these; this
                #    covers a stroke slow enough to outlast the fast check.
                if moved:
                    self._dispatch(
                        "PET", f"{count} contacts with no pad in common",
                        count, "head_pat_action", head_pat_action,
                    )
                    return
                # 3) DOUBLE TAP — repeated contacts sharing a pad, i.e. the hand
                #    stayed put. Deliberately NOT "only one pad touched": several
                #    fingers land on several pads at once, and requiring a single
                #    pad made this reachable only with one fingertip.
                self._dispatch(
                    "DOUBLE_TAP", f"{count} contacts, no evidence the hand moved",
                    count, "mic_toggle_action", mic_toggle_action,
                )
                return

        # 4) TAP. Also the whole classifier when SWIPE_ENABLED is off, in which
        #    case count is 1 or 2 — 2 is tolerated because cross-talk
        #    occasionally splits one physical touch into two close sessions and
        #    treating both as one tap is friendlier than ignoring.
        #
        # The tap gesture IS live. A `Disabled:` comment and a bare `# pass`
        # survived here from 01d8ac24, which commented the call out while
        # phantom triggers were being chased; the call was restored but the
        # comment was not, so the file claimed the opposite of what it did.
        # chime=False: the ack chime already sounded at the first session end
        # (_ack_first_session) — don't ping twice.
        self._dispatch(
            "TAP", f"decision window expired at count={count}",
            count, "single_click_action",
            lambda source: single_click_action(source=source, chime=False),
            chime=False,
        )

    def _dispatch(self, gesture, reason, count, fn_name, fn, **trace_fields):
        """Record the verdict, CLOSE THE TRACE, then run the action.

        Finishing before dispatch is deliberate. `sleep_action` blocks ~5s
        waiting out its TTS clip, which outlived the tracer's idle flush and
        filed four correctly-classified swipes as `IGNORED-unresolved`
        (device-observed 2026-08-27). Writing first also means the trace
        survives an action that raises.
        """
        touch_debug.note_decision(gesture, reason, count)
        touch_debug.note_action(fn_name, "TTP223", **trace_fields)
        touch_debug.finish(gesture)
        self._reset_cycle()
        fn(source="TTP223")

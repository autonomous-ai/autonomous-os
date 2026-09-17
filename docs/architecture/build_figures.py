"""The Autonomous OS architecture figure — one drawing, house tokens.

    python3 docs/architecture/build_figures.py      # writes autonomous-stack.svg

then render it to PNG at 1.5× with headless Chrome (see DIAGRAMS notes in
build output) and look at the result before committing. READMEs embed the PNG.

The figure is a layered stack read top-down, and every layer names the folder
that implements it, so the drawing and the repository tree map 1:1. Green does
work, purple decides or holds state, coral is a terminal — the bodies the whole
stack exists to drive. Nothing is coloured for emphasis.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from figs import (Fig, H, PAD, ARROW, LABEL_TEXT, PURPLE_TEXT, GREEN_TEXT, CORAL_TEXT,
                  NODE_FS, LABEL_FS, box_w, text_w)

HERE = pathlib.Path(__file__).parent

# ---- layout ---------------------------------------------------------------
LEFT = 40           # left caption column starts here
COL = 360           # x where the first node begins (must clear the widest left caption)
RIGHT_PAD = 40      # gap after the last node
GAP = 18            # gap between sibling nodes in a row
ROW = 112           # centre-to-centre distance between rows
SUB = 84            # centre-to-centre distance between the two lines of one row


def row_width(labels):
    return sum(box_w(l) for l in labels) + GAP * (len(labels) - 1)


class Stack(Fig):
    """A Fig that knows how to lay out one row of the stack."""

    def caption_left(self, cy, name, folder=None, file=None, note=None):
        """Layer name, then the folder that implements it and the file that governs it."""
        self.parts.append(
            f'<text x="{LEFT}" y="{cy + 2:.0f}" fill="{LABEL_TEXT}" '
            f'font-size="{NODE_FS}" font-weight="600">{name}</text>')
        line = []
        if folder:
            line.append(f'<tspan fill="{LABEL_TEXT}">{folder}</tspan>')
        if file:
            line.append(f'<tspan fill="{PURPLE_TEXT}"> · {file}</tspan>')
        if line:
            plain = (folder or "") + (f" · {file}" if file else "")
            assert LEFT + len(plain) * (NODE_FS - 2) * 0.60 < COL - 24, \
                f"left caption {plain!r} runs under the node column — raise COL"
            self.parts.append(
                f'<text x="{LEFT}" y="{cy + 34:.0f}" font-size="{NODE_FS - 2}" '
                f'font-family="SF Mono, Menlo, Consolas, monospace">{"".join(line)}</text>')
        if note:
            ny = cy + 66 if line else cy + 34
            self.parts.append(
                f'<text x="{LEFT}" y="{ny:.0f}" fill="{ARROW}" font-size="{LABEL_FS}">{note}</text>')

    def caption_right(self, x, cy, *lines, colour=None):
        for i, ln in enumerate(lines):
            self.parts.append(
                f'<text x="{x:.0f}" y="{cy + 9 + i * 31:.0f}" fill="{colour or LABEL_TEXT}" '
                f'font-size="{LABEL_FS}" font-family="SF Mono, Menlo, Consolas, monospace">'
                f'{ln}</text>')
            self._saw(x + text_w(ln, LABEL_FS) * 1.15, cy + 9 + i * 31)

    def row(self, cy, labels, kind, x0=COL, term=False, dashed=()):
        x = x0
        for l in labels:
            w = box_w(l) if not term else max(H + 8, box_w(l) - 8)
            cx = x + w / 2
            if term:
                self.term(cx, cy, l, w=w, dashed=(l in dashed))
            else:
                self.box(cx, cy, l, kind=kind, w=w, dashed=(l in dashed))
            x += w + GAP
        return x - GAP  # right edge of the row


# ---- one hue per layer, the way Android's platform diagram reads ------------
# A reader should be able to tell two layers apart without reading a word.
# Same shape everywhere; only the hue changes, and it changes per layer.
APPS   = ("#fbf0ed", "#f0c5b6", "#ad5130")   # coral — people
SKILL  = ("#eff5ea", "#b8d7a6", "#5a912f")   # green
BRAIN  = ("#f3f2f9", "#a9a3c9", "#5d579d")   # purple
SYSTEM = ("#eef4fa", "#a5c3e2", "#2f6193")   # blue
VOICE  = ("#eaf5f3", "#9ecfc6", "#23786c")   # teal
CAPS   = ("#fdf4e6", "#e6c489", "#96660f")   # amber
SAFETY = ("#fceded", "#e7adad", "#a33b3b")   # rose — the one gate
DRIVER = ("#f1f3f7", "#b4bfcf", "#46566d")   # slate
BOARD  = ("#f4f4ea", "#c8c99e", "#67691f")   # olive
KERNEL = ("#f3f3f3", "#c6c6c6", "#565656")   # grey — not ours
BODY   = ("#fbf0ed", "#f0c5b6", "#ad5130")   # coral — the robot


def stack():
    f = Stack(1600, 1300)
    y = 30

    widest = 0

    # 0 — Apps: the surfaces a person touches. Android's top layer is System Apps;
    # ours is the app that adds a robot, installs skills and switches brains.
    y += 60
    labels = ["Autonomous app", "web setup", "live monitor"]
    f.caption_left(y, "Apps")
    r = f.row(y, labels, APPS); widest = max(widest, r)

    # 1 — Skills
    y += ROW
    labels = ["guard", "emotion", "face-enroll", "servo-tracking", "music", "+ 20 more", "your skill"]
    f.caption_left(y, "Skills")
    r = f.row(y, labels, SKILL, dashed=("your skill",)); widest = max(widest, r)

    # 2 — Agentic Runtime
    y += ROW
    labels = ["OpenClaw", "Hermes", "PicoClaw", "OpenCode", "Codex", "Claude Code", "your brain"]
    f.caption_left(y, "Agentic runtime")
    r = f.row(y, labels, BRAIN, dashed=("your brain",)); widest = max(widest, r)

    # 3 — System managers
    y += ROW
    labels = ["server", "intent", "agent", "skills", "device", "bootstrap", "+ 8 more"]
    f.caption_left(y, "System services")
    r = f.row(y, labels, SYSTEM); widest = max(widest, r)

    # 3b — Realtime voice: hosted in HAL, answers small talk or hands the turn to the brain
    y += ROW
    labels = ["Gemini Live", "OpenAI Realtime", "GPT-Live"]
    f.caption_left(y, "Realtime voice")
    r = f.row(y, labels, VOICE); widest = max(widest, r)

    # 4 — HAL capabilities, two lines
    y += ROW
    labels = ["audio", "vision", "motion", "light", "sensing", "display", "+ 7 more"]
    f.caption_left(y, "Capabilities")
    r = f.row(y, labels, CAPS); widest = max(widest, r)

    # 5 — Safety gate (band)
    y += ROW
    f.caption_left(y, "Safety gate")
    band_y = y

    # 6 — Drivers + boards
    y += ROW
    labels = ["motors", "camera", "voice", "sensing", "display", "+ 3 more", "your driver"]
    f.caption_left(y, "Drivers")
    r = f.row(y, labels, DRIVER, dashed=("your driver",)); widest = max(widest, r)

    y += SUB
    labels = ["Raspberry Pi 5", "Raspberry Pi CM4", "OrangePi 4 Pro", "your board"]
    f.caption_left(y, "Boards")
    r = f.row(y, labels, BOARD, dashed=("your board",)); widest = max(widest, r)

    # 7 — Linux (band)
    y += ROW
    f.caption_left(y, "Linux")
    linux_y = y

    # 8 — Bodies (terminals)
    y += ROW
    labels = ["Lamp", "Intern", "Reachy Mini", "Go2-W", "your robot"]
    f.caption_left(y, "Bodies")
    r = f.row(y, labels, BODY, term=True, dashed=("Go2-W", "your robot")); widest = max(widest, r)

    # bands span the widest row
    f.band(COL, widest, band_y, "brightness · quiet hours · explicit-move speed · thermal",
           kind=SAFETY)
    f.band(COL, widest, linux_y, "Raspberry Pi OS · OrangePi Debian · the robot's own image", kind=KERNEL)

    return f.write(HERE / "autonomous-stack.svg")


if __name__ == "__main__":
    p = stack()
    print(p)
    print("render: \"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\" --headless "
          "--disable-gpu --hide-scrollbars --force-device-scale-factor=1.5 "
          f"--window-size=W,H --screenshot={p.with_suffix('.png')} file://{p}")

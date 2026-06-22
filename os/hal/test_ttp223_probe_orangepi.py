#!/usr/bin/env python3
"""Probe each TTP223 line independently. Touch one pad at a time; you should see
the corresponding line print HIGH then LOW. Ctrl+C to exit."""
import time, gpiod
from gpiod.line import Bias, Direction, Value

CHIP = "/dev/gpiochip0"
LINES = [96, 97, 99]
NAMES = {96: "S1", 97: "S2", 99: "S4"}

settings = gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)
config = {l: settings for l in LINES}

with gpiod.request_lines(CHIP, consumer="ttp223-probe", config=config) as req:
    print("Probing", LINES, "(PULL_UP). Touch each pad. Ctrl+C to exit.")
    last = {l: None for l in LINES}
    while True:
        vals = req.get_values(LINES)
        for line, v in zip(LINES, vals):
            high = (v == Value.ACTIVE)
            if last[line] != high:
                print(f"{time.strftime('%H:%M:%S')}  {NAMES[line]} (line {line}) = {'HIGH' if high else 'LOW'}")
                last[line] = high
        time.sleep(0.02)

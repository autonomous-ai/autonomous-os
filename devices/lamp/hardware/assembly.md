# Assembly

Build order for one lamp. Skeleton — fill in photos and torque values as we build the next one.

## Tooling

- Phillips #1 + #2
- Hex 2 mm, 2.5 mm
- Wire stripper, crimper for JST
- Soldering iron (only if amp needs leads)
- Multimeter

## Build order

1. **Print enclosure** — see [`cad/README.md`](cad/README.md) for STL files. Estimated print time: TODO.
2. **Mount the 5 servos** in their bays.
   - Torque: TODO
   - Photo: `images/TODO-servo-mount.jpg`
3. **Daisy-chain the servo bus** — short jumpers between adjacent servos. Last servo connects to the USB control board.
4. **Mount the LED ring** on the top cap.
   - 3 wires out: 5 V, GND, DIN
   - Route through the strain-relief slot (don't pinch under the cap).
5. **Wire the button** to the chosen header pin (see [`wiring.md`](wiring.md)). Solder + heat-shrink, no breadboard joints.
6. **Mount speakers** in their grilles. Wire PAM8610 → speakers with twisted pair.
7. **Mount the SBC** (Pi 5 or OPi 4 Pro) on its standoffs.
   - Standoff height: TODO
   - HAT clearance: TODO
8. **Power wiring**:
   - 12 V adaptor → terminal block
   - 12 V → PAM8610
   - 12 V → MP2482 buck → 5 V to SBC and LED ring
   - All grounds to star point at buck output (see [`power.md`](power.md))
9. **Plug USB devices**: camera, mic, servo control board.
   - **Mic 2 (sensing) rework**: the OrangePi 4 Pro's onboard MEMS mic must be desoldered from its pads and re-mounted in the lamp base, wired back to the same pads with an extended (twisted) cord. Do this before mounting the SBC if possible — the pads are easier to reach with the board out.
10. **Smoke test before closing the body**:
    - Power on with current meter inline if possible.
    - Confirm SBC boots (LEDs on board).
    - Confirm fan spins.
    - SSH in, run `aplay -D plug:device_speaker /home/orangepi/tiger.wav` (or similar).
    - LED ring should breathe boot animation.
11. **Calibrate** — see [`calibration.md`](calibration.md).
12. **Close enclosure**.

## Screw / fastener spec

| Location | Screw | Qty |
|---|---|---|
| Servo to bracket | TODO | — |
| Bracket to body | TODO | — |
| SBC standoff | M2.5 × TODO | 4 |
| Top cap | TODO | — |
| Speaker grilles | TODO | — |

## Photos

Annotated build photos go in [`images/`](images/). Reference them from this file as you go.

## TODO

- [ ] Photograph each step on the next build
- [ ] Fill in torque values
- [ ] Confirm screw spec table
- [ ] Add a "common gotchas" section after the next build (servo direction, LED data direction, etc.)

# Power

Single 12 V input. One step-down to 5 V for the SBC and ring. 3.3 V comes from the SBC for sensors.

## Source

- 12 V / 5 A barrel-jack adaptor → screw terminal block on the chassis
- Total budget: **60 W**, but plan for ~30 % headroom (work to ~42 W max sustained)

## Rail tree

```
                 +12V (5A adaptor)
                       │
        ┌──────────────┼──────────────┐
        │              │              │
   PAM8610 amp    MP2482 buck     (12V fan, optional)
   (~1.5A peak)   (12V→5V, 3A)
                       │
        ┌──────────────┼──────────────┐
        │              │              │
     SBC USB-C      WS2812 ring     5V Nidec fan
    (Pi 5 / OPi)   (64 px, capped)
    (~2A typ)      (~1.0A capped)   (~0.2A)
                       │
                  3.3V (from SBC, sensors only)
                       │
                  TTP223 ×4 (~0.02A)
```

## Current budget

| Rail / Load | Typical | Worst case | Notes |
|---|---|---|---|
| **12 V total** | ~3.0 A | ~4.0 A | Adaptor is 5 A — keep ≥ 1 A headroom |
| PAM8610 amp | 0.3 A | 1.5 A peak | Class-D; peak only on transients |
| MP2482 input | 1.6 A | 2.5 A | At ~85 % efficiency for a 5 V/3 A draw |
| 12 V fan (if used) | 0.2 A | 0.3 A | Optional |
| **5 V total** (out of buck) | ~2.5 A | ~3.0 A | MP2482 limit |
| SBC (Pi 5 or OPi 4 Pro) | 1.8 A | 2.5 A | Spikes during boot + camera + servo activity |
| WS2812 ring (capped) | 0.5 A | 1.0 A | Brightness cap in software; full white 64 px = 3.84 A which is **not allowed** |
| Nidec 5 V fan | 0.15 A | 0.20 A | |
| **3.3 V** (from SBC) | < 0.05 A | < 0.10 A | TTP223 ×4 + headroom |

## Grounding

- Star-ground at the buck output (combined 12 V GND and 5 V GND tie point)
- Speaker amp GND returns to star ground via short, thick wire — avoids hum
- LED ring GND returns to star ground via its own conductor — do not piggy-back on the SBC USB-C ground
- Camera + USB mic ground via USB cable shield only

## Notes

- **Do not** test the LED ring at full brightness white — exceeds the buck's 3 A limit and will brown out the SBC. Software caps brightness; do not bypass.
- **Do not** feed PAM8610 with 5 V — it's specified for 7 V–18 V, runs under-power and distorts at 5 V.
- If you hear hum in the speakers, suspect ground loop first — add a short bonding wire from amp GND directly to the SBC analog GND (header pin 6 on Pi/OPi).
- MP2482 efficiency drops sharply below ~80 % load swing — keep the 5 V draw above 0.5 A or you'll see ripple. (LEDs idle at ~0.1 A — that's fine, the SBC alone keeps the buck in regulation.)

## See also

- [`wiring.md`](wiring.md) — pin-by-pin connections
- [`components.md`](components.md) — exact part models

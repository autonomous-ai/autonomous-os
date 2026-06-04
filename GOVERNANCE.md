# Governance

Autonomous follows the **BDFL** model, in the Linux tradition: open contribution, but a
single lead maintainer holds final say. Not a committee. This keeps the platform
coherent and fast while it is young.

- **Lead maintainer:** @deehw. Final decision on architecture, the frozen `contract/`,
  and releases.
- **Subsystem owners:** listed in `MAINTAINERS`. Own day-to-day review of their area.
- **Decisions:** proposed by PR or issue. Owners review; the lead maintainer breaks ties
  and rules on contract changes.

## Open vs. closed

Autonomous (the OS) is Apache 2.0 and fully open: core, HAL, drivers, board support,
device contracts, the skills system, setup, and OTA. The following are **not** part of
the open OS and ship separately:

- premium character packs (`soul_ref` targets)
- the memory continuity service
- Grid / distributed inference
- the skill store + distribution

This is the AOSP/GMS split: the OS is open so the platform spreads; the differentiated
services on top are how Autonomous sustains it. Anyone can build a device; the best
experience ships from Autonomous.

## Trademark

"Autonomous" and the device names are trademarks. You may build Autonomous-compatible
devices and say so; you may not ship a device as an official Autonomous product without
authorization. Compatibility is defined by the `contract/`.

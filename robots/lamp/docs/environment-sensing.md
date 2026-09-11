# Environmental sensing — SEN55 and SCD41

SEN55 is an environmental sensor: particulate matter, humidity, temperature,
and VOC/NOx indexes. HAL exposes it as an optional `environment` capability,
with its own driver, lifecycle, and HTTP snapshots alongside camera and audio.
The integration provides HAL acquisition, read-only web/MQTT snapshots, and
configurable OS detection of sustained environmental changes for the agent.
SEN55 does not measure touch, pressure, CO₂, CO or O₂. Optional SCD41 adds
measured `co2_ppm` to the same capability; its temperature/humidity wire values
are not published, so SEN55 remains their source.

## Current scope: readings and sustained-change events

HAL keeps its latest snapshot in RAM. The OS environment worker independently
polls that snapshot and sends qualifying `environment.update` events through
`POST /api/sensing/event`; it does not invoke the agent for every sensor sample.
The `environment` skill interprets measurements and consults `wellbeing` for
considerate advice. Web and MQTT reads remain read-only and do not trigger turns.
There is no environmental history store, scheduled follow-up service, automatic
actuator control, or medical alarm. Lamp still ships with this hardware disabled
and its capability commented out; the worker requires the declared capability.

## Wiring and mounting

Use the connector orientation in the [Sensirion SEN5x datasheet, sections 4
and 6](https://sensirion.com/media/documents/6791EFA0/62A1F68F/Sensirion_Datasheet_Environmental_Node_SEN5x.pdf),
not wire colors. A compatible cable plug is JST GHR-06V-S.

| SEN55 pin | Signal | Connection |
|---|---|---|
| 1 | VDD | 5 V supply |
| 2 | GND | Common host ground |
| 3 | SDA | Host I2C data |
| 4 | SCL | Host I2C clock |
| 5 | SEL | GND before power is applied |
| 6 | NC | Leave unconnected |

Hardware-team wiring note (reported by the owner): the **OrangePi host header**
uses physical **pin 3 for SDA** and **pin 5 for SCL**. These are host header
positions, not SEN55 connector pin numbers: connect OrangePi pin 3 to SEN55
pin 3 (SDA), and OrangePi pin 5 to SEN55 pin 4 (SCL). On the inspected OrangePi 4 Pro, `gpio readall` identifies pin 3 as
SDA.0 (PB3) and pin 5 as SCL.0 (PB2). The live device tree maps these pins
to `/dev/i2c-0`, with the `i2c0` overlay enabled. `sen55.json` therefore
records `bus: 0`; acquisition remains disabled.

`sen55.json` records these physical host header positions as `sda_pin: 3` and
`scl_pin: 5`. They are wiring metadata; the driver uses `bus` and does not
configure GPIO pin-mux from these fields.

SDA/SCL support 3.3 V logic. Use pull-ups to 3.3 V with a 3.3 V host;
5 V powers the sensor, not the host GPIO. The I2C address is `0x69`, with
bus speed at most 100 kHz. Confirm the actual board, header pin mapping,
pin multiplexing, available bus, and power budget before wiring. HAL does
not select board header pins or configure bus speed. Keep the air inlet and
outlet clear and avoid heat from the host when mounting.

Device inspection on 2026-09-11 confirmed the host bus mapping, but the
SEN55 product-name command at `0x69` received no address ACK (the Sunxi
driver returned `EINVAL`). The live bus clock was 400 kHz, above the SEN55
limit. No sensor identity or readings have been verified. Configure the bus
for at most 100 kHz and check power, common ground, SEL and wiring before
enabling acquisition. This inspection did not change device configuration.

## Enable in HAL

Lamp's `ROBOT.md` keeps the optional `environment` declaration commented out.
HAL does not mount its endpoints or load its wiring until that line is uncommented.
The prepared declaration uses driver `composite`, `routes: [environment]`, and
`required: false`.

Component selection belongs to `robots/<device>/environment.json`:

```json
{"components": ["sen55", "scd41"]}
```

Either component may be selected independently. A missing selection file keeps
the legacy SEN55-only behavior; unknown or duplicate names are rejected. Each
selected component has an independent worker and configuration.

SEN55 configuration belongs to `robots/<device>/sen55.json`, using a
`boards` map like `mpr121.json`. The target board is OrangePi (`orangepi_sun60`); Lamp ships its entry disabled
(`{"enabled": false}`), with the verified host-header bus `0`. A missing file
or selected-board entry disables the sensor. Disabled entries may omit `bus` or set it to `null` while the bus is unknown.
Enabling acquisition requires a nonnegative integer `bus`.
Invalid configuration, including unknown fields, is rejected at startup.

To enable it after wiring is confirmed, uncomment the capability in `ROBOT.md`,
edit the actual board's entry, and restart HAL. This template
uses placeholders and is not directly loadable JSON:

```text
{
  "boards": {
    "<actual board ID>": {"enabled": true, "bus": <actual bus number>}
  }
}
```

Replace the board ID with the detected board and the bus placeholder with its
actual nonnegative integer Linux bus number. There is no default bus. Enable
the kernel I2C interface and configure pin multiplexing and bus speed for that
board; the inspected OrangePi 4 Pro uses bus `0`, but sensor communication
and the required bus clock still need verification.
HAL accesses `/dev/i2c-N` through Python's standard library; the process needs
permission to open that device. No additional Python I2C package is needed.
Simulation never accesses the hardware, even with an enabled entry.

With default timing, the SEN55 worker polls every 1 second and retries hardware failures after 5 seconds.
No new data for more than 30 seconds triggers recovery through the same retry
path. Shutdown stops the worker and releases the bus. Acquisition runs
separately from the camera/microphone sensing loop. Camera/microphone privacy
controls and sleep do not stop environmental acquisition. This capability
adds no actuator policy or new `SAFETY.md` bounds; ambient temperature is not
the SoC thermal reading.

## SCD41 CO₂ component

`robots/lamp/scd41.json` uses the same `boards` map. Its OrangePi entry is
disabled with `bus`, `sda_pin`, and `scl_pin` set to `null` pending hardware
confirmation. Enabling requires a nonnegative Linux bus number; header pins
are optional metadata and do not configure pin-mux. Do not copy SEN55 wiring
or supply voltage without confirming the SCD41 board/breakout. SCD41 uses I2C
address `0x62`; SEN55 uses `0x69`, so sharing a bus is possible if the hardware
team confirms compatible wiring, logic, power and bus configuration.

The driver uses normal periodic measurement (one new result every 5 seconds),
checks data readiness and CRC, and exposes only `co2_ppm`. Defaults are
`poll_interval_s: 5`, `retry_interval_s: 5`, `stale_after_s: 15`, and
`no_data_timeout_s: 30`. Changing HAL polling does not change this internal
5-second measurement cadence.

`automatic_self_calibration: null` preserves the sensor setting; `true`/`false`
explicitly sets ASC in RAM before measurement. HAL does not persist settings
or perform forced recalibration. Review ASC exposure requirements for the
installation before choosing a value; a 60-second OS warm-up is not calibration.
See the [SCD4x datasheet](https://sensirion.com/media/documents/48C4B7FB/67FE0194/CD_DS_SCD4x_Datasheet_D1.pdf).
Actual SCD41 wiring and measurements have not been verified on hardware.

HAL lifecycle logs use component keys `[sen55]` and `[scd41]`: disabled/start,
measurement start, retry failures and stop are visible at INFO (failures may
be WARNING or ERROR). Every valid sample is logged at INFO with timestamp and
measured values; polls without a new sample log waiting state and last-data
age. `[environment]` records selected components/simulation or missing capability.

To follow logs on the device, use either its log file or systemd journal:

```sh
tail -F /var/log/hal/server.log | grep --line-buffered -E '\[(environment|sen55|scd41)\]'
journalctl -u hal -f | grep --line-buffered -E '\[(environment|sen55|scd41)\]'
```

These are operator instructions; no device connection is performed by setup
or by documenting these commands. Default root logging at INFO is sufficient.

## HAL API

These are HAL endpoints on port 5001, subject to the existing HAL access
controls. They are available when the robot loads the `environment` route.

| Endpoint | Behavior |
|---|---|
| `GET /environment/status` | Reports state, `last_error`, latest `sample`, `age_s`, `stale`, and per-component diagnostics, including when disabled or unavailable |
| `GET /environment/sample` | Returns a fresh snapshot; HTTP 503 when unavailable or stale |
| `GET /health` | The `environment` boolean reports whether a fresh sample is available |

States are `disabled`, `starting`, `ready`, `error`, and `stopped`. Each entry
in `components` retains its own state, enabled flag, sample, error, age, bus
and timing. Non-ready or expired component readings are unavailable; SEN55
with a nonzero status register is also excluded from the combined sample.

The group is `ready` and not stale when at least one component contributes a
fresh value. `partial: true` means another enabled component is unavailable;
a disabled component alone does not make the group partial. Healthy values
remain usable when another sensor fails. With no usable sample, group `sample`
and `age_s` are `null` and `stale` is true. The flat `sample` includes fields
owned by selected components, with unavailable values set to `null`.

`sources` maps each metric to its component; `metric_timestamps` records each
usable metric's Unix timestamp. Group `sample.timestamp` and `age_s` describe
the newest contributing data, not every metric. Consumers must check source
status and metric freshness independently. Bus/timing are also exposed at the
top level only for a single-component group. `ready` does not certify gas
sensor warm-up or calibration.

A sample contains:

| Field | Meaning |
|---|---|
| `timestamp` | Unix time in seconds |
| `pm1_0_ug_m3`, `pm2_5_ug_m3`, `pm4_0_ug_m3`, `pm10_ug_m3` | Particle mass concentration in µg/m³ |
| `humidity_pct` | Relative humidity in % |
| `temperature_c` | Temperature in °C |
| `voc_index`, `nox_index` | Unitless gas indexes, not ppm measurements |
| `co2_ppm` | Measured carbon dioxide concentration in ppm, from SCD41 only |

SEN55 `device_status` remains in `components.sen55.sample` for diagnostics;
a SEN55-only group also retains `sample.device_status` for compatibility.
It is not a combined CO₂ sensor status.

Unavailable measurement values are JSON `null`; callers must not treat them
as zero. OS change detection and agent interpretation are described below.

## Local web view

[Device → Sensing](../../../docs/web-ui.md#58-device--sensing) shows an
**Environment** card only when the device explicitly declares the
`environment` capability. Loading or missing capabilities do not trigger
sensor requests. The Sensing menu is available without debug mode and
supports devices with `vision`, `environment`, or both, and camera cards
remain gated by `vision`.

The browser polls `GET /api/hardware/environment/status` every 3 seconds via
the existing authenticated OS hardware proxy, which forwards to HAL
`GET /environment/status`. This refresh interval is separate from the HAL
polling configuration below. The card shows state, available component fields
(up to nine measurements including CO₂ in ppm), source labels, sample
time, stale status, and errors. Missing or stale values appear as `—` and
request failures are shown explicitly so old values are not presented as live
readings. Component failures do not hide healthy readings. Bus, device status
register, and timing configuration are in a collapsed technical section per
component under `status.components`; legacy single-sensor snapshots remain supported. This is a read-only display with no good/bad
thresholds or historical storage. OS → agent events come from the independent
worker below, not browser refreshes.

The card stays hidden with Lamp's current commented capability. Declaring the
capability while leaving `enabled: false` makes the card show the disabled state.

## MQTT reads

Mobile/backend clients send `{"cmd":"data","kind":"environment.status","data":{}}`
on `fa_channel`; the OS replies with `MQTTDataResponse` on `fd_channel`, the
same `kind`, `status: "success"`, and the HAL snapshot in `data`. The handler
requires the declared `environment` capability, not a SEN55 model. Local HAL
requests time out after 5 seconds. Disabled, error, and stale sensor states are
valid snapshots: callers must inspect `state`, `stale`, `sample`, and `last_error`
in `data`.

A missing capability returns `status: "failure"` with
`error: "environment capability not declared"`. HAL transport errors, non-200
HTTP responses, and invalid status JSON also return failure. Lamp's currently
commented capability therefore returns the missing-capability error. This is
request/reply, with no continuous stream, automatic events, or agent invocation.
See the [MQTT protocol](../../../docs/mqtt.md) for payloads and response rules.

## Timing configuration

Each board entry accepts these optional timing fields (seconds). The following defaults are for SEN55; SCD41 defaults are listed above. Values must be finite positive numbers; `stale_after_s` and `no_data_timeout_s` must exceed `poll_interval_s`. Restart HAL after editing. These control HAL polling, not the sensor’s internal sampling rate.

```json
{
  "poll_interval_s": 1.0,
  "retry_interval_s": 5.0,
  "stale_after_s": 5.0,
  "no_data_timeout_s": 30.0
}
```

## OS change policy and agent access

OS interpretation is configured under the top-level `environment` object in
`config/config.json`, through the existing admin `GET`/`PUT /api/device/config`.
This is separate from HAL timing in `sen55.json` and `scd41.json`. Defaults are:

```json
{
  "environment": {
    "enabled": true,
    "initial_report": true,
    "evaluate_interval_s": 10,
    "sustain_s": 60,
    "cooldown_s": 900,
    "retry_interval_s": 60,
    "max_sample_age_s": 10,
    "metrics": {
      "pm1_0_ug_m3": {"delta": 10, "warmup_s": 60},
      "pm2_5_ug_m3": {"delta": 10, "warmup_s": 60},
      "pm4_0_ug_m3": {"delta": 15, "warmup_s": 60},
      "pm10_ug_m3": {"delta": 15, "warmup_s": 60},
      "temperature_c": {"delta": 2, "warmup_s": 60},
      "humidity_pct": {"delta": 10, "warmup_s": 60},
      "voc_index": {"delta": 50, "warmup_s": 3600},
      "nox_index": {"delta": 20, "warmup_s": 21600},
      "co2_ppm": {"delta": 200, "warmup_s": 60}
    }
  }
}
```

`delta` uses each measurement's unit (humidity uses percentage points). These
are change-detection defaults, **not medical or absolute air-quality limits**.
Omitting the whole object uses defaults. Within a supplied object, omitted
top-level fields use defaults; supplying `metrics` replaces the entire metric
map, allowing a monitored subset. Each rule needs a positive finite `delta`;
omitted per-metric fields inherit that metric’s defaults. Explicit null fields
or maps, an empty map, and unknown fields/metrics are rejected. New configs
persist the full default object; old configs without it use defaults without
a migration rewrite.
Evaluation, sustain, retry and maximum age must be 1–86400 seconds; sustain and
retry must be at least the evaluation interval. Cooldown permits 0–604800
seconds, warm-up 0–86400 seconds. Runtime edits apply on the next worker tick
and reset detection baselines; they do not enable HAL hardware.

Only fresh, enabled, ready readings with advancing timestamps qualify. The
HAL `stale` flag and OS maximum age both apply per metric and source. For
composite snapshots, `sources`, `components`, and `metric_timestamps` prevent
a healthy sensor from making another sensor's old values appear current. A
faulty SEN55 resets its own metrics without suppressing healthy SCD41 CO₂.
Legacy single-sensor snapshots still use the top-level timestamp/status.
After continuous valid readings for each metric's warm-up, the first value
establishes its change-detection baseline. Each HAL component optionally reports
`continuous_data_s`: elapsed successful acquisition time from its first to its
latest sample in the uninterrupted acquisition period. It is null while invalid,
stale or errored and resets after interrupted acquisition. OS can use this
continuity plus locally observed valid duration to satisfy warm-up after an
OS-only restart; legacy HAL without it uses local observation. Freshness and
per-metric validity still apply; component uptime alone is not usable data.
Changes must meet `delta` in the same direction for `sustain_s`; falling below
that difference or reversing direction resets the pending duration. Null
metrics reset their own warm-up/baseline; an unavailable snapshot or read failure
resets readings for all metrics. Large gaps also reset continuity. Warm-up is an
OS gating period, not a certificate of sensor calibration.

`environment.initial_report` defaults to `true`. The system greeting never
waits for a sensor or fetches HAL: it may attach a fresh, warmed-up snapshot
already cached by the OS worker as `[environment:initial]` JSON. The agent adds
at most one factual sentence with one or two readings to the normal greeting.
Cold boot normally greets without environmental data. After greeting completion,
OS sends the first eligible snapshot once through `environment.update`, with
`reason: "initial"` and `changes: {}`, even if no significant change occurred.
Only eligible metrics are included; metrics still warming up do not each trigger
another initial report. A successful greeting with context consumes the report;
otherwise it remains pending. Rejected dispatch retries at `retry_interval_s`;
accepted or queued dispatch consumes it and starts the shared cooldown. Queued
acceptance is best-effort, so later expiry does not generate another initial
report. Capability, enabled policy, sleep, busy and conversation-floor rules
still apply to the separate event. This state lasts for the OS process; sensor
reconnections and configuration edits do not rearm a consumed report. Setting
`initial_report: false` disables both greeting inclusion and the separate
initial update, preserving sustained-change detection. No initial snapshot
establishes a trend, health diagnosis, or assurance of safe air.

A qualifying change event contains `[environment:update]` followed by JSON with
`observed_at` (Unix seconds), `sample`, `changes` keyed by metric with
`previous`, `previous_at` (baseline Unix seconds), `current`, `current_at`
(current metric Unix seconds), `source`, signed `delta`, and `sustained_s`.
Composite events also include `sources` and `metric_timestamps`; legacy
single-sensor readings may omit provenance. The POST body carries the event as JSON text in `message`;
the sensing formatter adds `[environment:update]` when forwarding to the agent.
`previous` is the
baseline established initially or after the last acknowledged event for that
metric; `current` is the latest sample, not a rolling average. Both increases
and decreases can qualify. Acknowledged dispatch advances included baselines
and starts the shared cooldown; rejected dispatch retains them and waits at
least the retry interval before another attempt. The event duration is the
configured sustained-change requirement, not a health exposure measurement.

Dispatch respects the explicit capability, sleep and conversation floor even
in guard mode. Busy-agent queues keep only the latest environment event,
expire it after 60 seconds, and recheck capability/sleep/policy enabled before
replay. Setting `environment.enabled: false` also makes the event handler return
`dropped_disabled`; it stops automatic interpretation, not HAL acquisition or
diagnostic status reads.
A queued acknowledgement is best-effort: later expiry or dropping is possible;
it does not guarantee the user hears a notification. Automatic environmental
events do not require a camera reaction, emotion marker or physical action.

Local agent tools can read `GET http://127.0.0.1:5000/api/environment/status`.
This loopback-only, capability-gated OS route returns the usual
`{"status":1,"data":{...HAL snapshot...},"message":null}` envelope. It reads
HAL's cached diagnostic snapshot, not a forced hardware measurement; callers
must check readiness, freshness and individual null values. Missing capability
returns HTTP 403; HAL read/format failures return HTTP 502. Disabled/error/stale
snapshots can still be successful diagnostic responses. Browser and MQTT
clients continue using their existing authenticated routes.

## Environment skill and well-being use cases

The capability-gated `skills/environment/SKILL.md` owns data interpretation.
It is excluded when capabilities are missing or empty, even where legacy skills
retain their fallback availability.
It consults only the environmental-care section of `skills/wellbeing/SKILL.md`
for proactive timing and phrasing, avoiding a routing loop. Room questions do
not require camera observations, identity, activity logs or hydration counters.

- **Ask about the room:** read status once, report useful measurements; missing
  or stale data is unknown, not zero pollution or proof of safe air.
- **Startup:** greet immediately; optionally use cached eligible readings in one
  short sentence. If unavailable, the first eligible snapshot can produce a
  separate observation after the greeting, without greeting again or fetching
  data for this report. Stale initial data is omitted.
- **Sustained change:** explain supported changes and offer at most one useful
  action when appropriate. Respect quiet/sleep preferences; output `NO_REPLY`
  when no useful notification is warranted.
- **After an action:** compare with a real earlier timestamped reading from the
  event/conversation. Report improvement or worsening without claiming causation.
  Without a baseline, explain that a comparison is unavailable. Do not promise
  a timed recheck, start polling, or invent persistent history.

VOC/NOx indices are relative, not ppm or chemical identification. SEN55 cannot
support CO₂/O₂ claims. SCD41 supports CO₂ statements only when its measured
`co2_ppm` is fresh; VOC/NOx must never be used to infer CO₂. Neither component
measures O₂ or CO, or establishes a smoke/fire alarm. Instantaneous PM does not establish compliance with WHO
24-hour or annual exposure guidelines. Consider outside conditions before
suggesting ventilation and enclosure heat before interpreting temperature.
The skills do not diagnose health, invent thresholds, resume unrelated tasks,
or operate a purifier/fan/HVAC without an available authorized integration.
No environment-specific well-being log action is introduced.

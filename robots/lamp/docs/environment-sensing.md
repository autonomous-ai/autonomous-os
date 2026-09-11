# Environmental sensing — SEN55

SEN55 is an environmental sensor: particulate matter, humidity, temperature,
and VOC/NOx indexes. HAL exposes it as an optional `environment` capability,
with its own driver, lifecycle, and HTTP snapshots alongside camera and audio.
The integration provides HAL acquisition, read-only web/MQTT snapshots, and
configurable OS detection of sustained environmental changes for the agent.
SEN55 does not measure touch, pressure, CO₂, CO or O₂.

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
pin 3 (SDA), and OrangePi pin 5 to SEN55 pin 4 (SCL). The Linux `/dev/i2c-N`
bus number and pin-mux configuration still require confirmation; this note
has not been verified on the device and does not enable acquisition.

`sen55.json` records these physical host header positions as `sda_pin: 3` and
`scl_pin: 5`. They are wiring metadata; the driver uses `bus` and does not
configure GPIO pin-mux from these fields.

SDA/SCL support 3.3 V logic. Use pull-ups to 3.3 V with a 3.3 V host;
5 V powers the sensor, not the host GPIO. The I2C address is `0x69`, with
bus speed at most 100 kHz. Confirm the actual board, header pin mapping,
pin multiplexing, available bus, and power budget before wiring. HAL does
not select board header pins or configure bus speed. Keep the air inlet and
outlet clear and avoid heat from the host when mounting.

Physical wiring, board I2C configuration, and readings on a real SEN55 have
not yet been verified in this integration.

## Enable in HAL

Lamp's `ROBOT.md` keeps the optional `environment` declaration commented out.
HAL does not mount its endpoints or load its wiring until that line is uncommented.
The prepared declaration uses driver `sen55`, `routes: [environment]`, and
`required: false`.

Configuration belongs to the device in `robots/<device>/sen55.json`, using a
`boards` map like `mpr121.json`. The target board is OrangePi (`orangepi_sun60`); Lamp ships its entry disabled
(`{"enabled": false}`), without an assumed bus. A missing file
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
board; OrangePi is the selected board, but the physical wiring and bus still need confirmation.
HAL accesses `/dev/i2c-N` through Python's standard library; the process needs
permission to open that device. No additional Python I2C package is needed.
Simulation never accesses the hardware, even with an enabled entry.

With default timing, the worker polls every 1 second and retries hardware failures after 5 seconds.
No new data for more than 30 seconds triggers recovery through the same retry
path. Shutdown stops the worker and releases the bus. Acquisition runs
separately from the camera/microphone sensing loop. Camera/microphone privacy
controls and sleep do not stop environmental acquisition. This capability
adds no actuator policy or new `SAFETY.md` bounds; ambient temperature is not
the SoC thermal reading.

## HAL API

These are HAL endpoints on port 5001, subject to the existing HAL access
controls. They are available when the robot loads the `environment` route.

| Endpoint | Behavior |
|---|---|
| `GET /environment/status` | Reports state, `last_error`, latest `sample`, `age_s`, `stale`, and configured `timing`, including when disabled or unavailable |
| `GET /environment/sample` | Returns a fresh snapshot; HTTP 503 when unavailable or stale |
| `GET /health` | The `environment` boolean reports whether a fresh sample is available |

States are `disabled`, `starting`, `ready`, `error`, and `stopped`. A sample
older than `stale_after_s` (default 5 seconds) is stale; any state other than `ready` is also stale,
including `error`, `disabled`, and `stopped`. Without a sample, `sample` and `age_s` are
`null` and `stale` is true. A retained sample in status is diagnostic;
callers must check freshness. `ready` means data is available, not that gas
sensor warm-up or adaptation has completed.

A sample contains:

| Field | Meaning |
|---|---|
| `timestamp` | Unix time in seconds |
| `pm1_0_ug_m3`, `pm2_5_ug_m3`, `pm4_0_ug_m3`, `pm10_ug_m3` | Particle mass concentration in µg/m³ |
| `humidity_pct` | Relative humidity in % |
| `temperature_c` | Temperature in °C |
| `voc_index`, `nox_index` | Unitless gas indexes, not ppm measurements |
| `device_status` | Sensor status register bitmask |

Unavailable measurement values are JSON `null`; callers must not treat them
as zero. OS change detection and agent interpretation are described below.

## Local web view

[Device → Sensing](../../../docs/web-ui.md#58-device--sensing) shows an
**Environment · SEN55** card only when the device explicitly declares the
`environment` capability. Loading or missing capabilities do not trigger
sensor requests. The Sensing menu is available without debug mode and
supports devices with `vision`, `environment`, or both, and camera cards
remain gated by `vision`.

The browser polls `GET /api/hardware/environment/status` every 3 seconds via
the existing authenticated OS hardware proxy, which forwards to HAL
`GET /environment/status`. This refresh interval is separate from the HAL
polling configuration below. The card shows state, eight measurements, sample
time, stale status, and errors. Missing or stale values appear as `—` and
request failures are shown explicitly so old values are not presented as live
readings. Bus, device status register, and timing configuration are in a
collapsed technical section; timings come from `status.timing`. This is a read-only display with no good/bad
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

Each board entry accepts these optional timing fields (seconds). Omitted fields use these defaults. Values must be finite positive numbers; `stale_after_s` and `no_data_timeout_s` must exceed `poll_interval_s`. Restart HAL after editing. These control HAL polling, not the sensor’s internal sampling rate.

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
This is separate from HAL timing in `sen55.json`. Defaults are:

```json
{
  "environment": {
    "enabled": true,
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
      "nox_index": {"delta": 20, "warmup_s": 21600}
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

Only fresh, enabled, ready snapshots with advancing timestamps qualify. The
HAL `stale` flag and OS maximum age both apply. A nonzero `sample.device_status`
suppresses detection and resets readings until the status is clean. After continuous valid readings
for each metric's warm-up, the first value establishes a silent baseline.
Changes must meet `delta` in the same direction for `sustain_s`; falling below
that difference or reversing direction resets the pending duration. Null
metrics reset their own warm-up/baseline; an unavailable snapshot or read failure
resets readings for all metrics. Large gaps also reset continuity. Warm-up is an
OS gating period, not a certificate of sensor calibration.

A qualifying event contains `[environment:update]` followed by JSON with
`observed_at` (Unix seconds), `sample`, `changes` keyed by metric with
`previous`, `previous_at` (baseline Unix seconds), `current`, signed `delta`,
and `sustained_s`. The POST body carries the event as JSON text in `message`;
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
- **Sustained change:** explain supported changes and offer at most one useful
  action when appropriate. Respect quiet/sleep preferences; output `NO_REPLY`
  when no useful notification is warranted.
- **After an action:** compare with a real earlier timestamped reading from the
  event/conversation. Report improvement or worsening without claiming causation.
  Without a baseline, explain that a comparison is unavailable. Do not promise
  a timed recheck, start polling, or invent persistent history.

VOC/NOx indices are relative, not ppm or chemical identification. SEN55 cannot
support CO₂/O₂ claims. Instantaneous PM does not establish compliance with WHO
24-hour or annual exposure guidelines. Consider outside conditions before
suggesting ventilation and enclosure heat before interpreting temperature.
The skills do not diagnose health, invent thresholds, resume unrelated tasks,
or operate a purifier/fan/HVAC without an available authorized integration.
No environment-specific well-being log action is introduced.

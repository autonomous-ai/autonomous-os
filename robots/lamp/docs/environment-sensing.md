# Environmental sensing — interchangeable components

HAL exposes environmental sensors through one optional `environment` capability,
with independent component drivers, lifecycles, and HTTP snapshots alongside camera and audio.
The integration provides HAL acquisition, read-only web/MQTT snapshots, and
configurable OS detection of sustained environmental changes for the agent.
SEN55 does not measure touch, pressure, CO₂, CO or O₂. Optional SCD41 adds
measured `co2_ppm` to the same capability; its temperature/humidity wire values
are not published. SEN63C provides PM, temperature, humidity and measured CO₂
in one component; it does not provide VOC/NOx indices. No supported component
measures CO or O₂. Software consumes metric keys and provenance, not sensor names.

## Current scope: readings and sustained-change events

HAL keeps its latest snapshot in RAM. The OS environment worker independently
polls that snapshot and sends qualifying `environment.update` events through
`POST /api/sensing/event`; it does not invoke the agent for every sensor sample.
The `environment` skill interprets measurements and consults `wellbeing` for
considerate advice. Web and MQTT reads remain read-only and do not trigger turns.
There is no environmental history store, scheduled follow-up service, automatic
actuator control, or medical alarm. Only Lamp hardware profiles `pro`, `pro-respeaker-lite` and
`pro-xvf3800` declare the optional `environment` capability and enable SEN63C
on OrangePi `orangepi_sun60`, bus `0`. Standard leaves both disabled; SEN55/SCD41
and boards without a matching entry remain disabled in all four profiles.
The worker requires the declared capability.

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
not select board header pins; SEN55 requires board-level bus-speed configuration
(the SEN63C-specific Sunxi handling is described below). Keep the air inlet and
outlet clear and avoid heat from the host when mounting.

Device inspection on 2026-09-11 confirmed the host bus mapping, but the
SEN55 product-name command at `0x69` received no address ACK (the Sunxi
driver returned `EINVAL`). The live bus clock was 400 kHz, above the SEN55
limit. No sensor identity or readings have been verified. Configure the bus
for at most 100 kHz and check power, common ground, SEL and wiring before
enabling acquisition. This inspection did not change device configuration.

## Enable in HAL

The base Lamp `ROBOT.md` keeps `environment` commented out and the base
`sen63c.json` keeps `orangepi_sun60` disabled on bus `0`. Standard therefore
does not acquire environmental readings, write the SEN63C bus clock, or qualify
for the capability-gated `environment` skill; its UI does not poll the sensor.

The `profile.json` files in `overrides/pro/`, `overrides/pro-respeaker-lite/`
and `overrides/pro-xvf3800/` set
`capabilities: {"environment": true}`. The shared renderer activates the
existing capability declaration (driver `composite`, `routes: [environment]`,
`required: false`) and copies that override's `device/sen63c.json` to the
package root. HAL then mounts the endpoints and loads enabled components.
A missing SEN63C on any Pro profile reports component `error` and retries;
this optional sensor is not a startup requirement.

HAL discovers registered component drivers and reads their per-device, per-board
JSON configuration: `sen55.json`, `scd41.json`, and `sen63c.json`. Each
`enabled` flag controls that component; a disabled or missing board entry does
not access hardware. There is no separate component selection list; an old
`environment.json` is ignored. Each enabled component runs its own worker.

SEN63C is the default component for OrangePi in all three Pro profiles. To use SEN55 + SCD41 instead, first
set SEN63C to `"enabled": false`, then enable the replacement entries with
confirmed wiring and restart HAL. Two enabled components cannot own the same metric: SEN55 + SEN63C
or SCD41 + SEN63C is rejected as a configuration error instead of silently
overwriting readings. Disabled components do not participate in this check.
Adding future hardware requires its driver, registered metric ownership and
board config; the environment API and downstream flow remain shared.
The binding lives in `hal/drivers/environment/registry.py`: config loader,
driver (`start`, `read`, `close`), timing defaults and supported metric keys.
Setup and `build-orangepi` extract the full device-profile archive, so new
sensor JSON files need no sensor-specific installation branch.
When upgrading older versions, install both the updated HAL package (including
the clock fix) and device-profile package. Setup, image building and OTA apply
the selected hardware override to a fresh base package. The device update
replaces the extracted profile and restarts HAL and os-server.

A missing, empty or `standard` `/etc/autonomous/hardware-profile` selects
Standard, including machines previously enabled by the global SEN63C default.
There is no automatic Pro migration. For Pro hardware, explicitly select `pro`,
`pro-respeaker-lite` or `pro-xvf3800` and reinstall the device package; a HAL-only update cannot
change capability or sensor JSON. See [hardware overrides](../../../docs/bootstrap-ota.md#optional-hardware-overrides).

SEN55 configuration belongs to `robots/<device>/sen55.json`, using a
`boards` map like `mpr121.json`. The target board is OrangePi (`orangepi_sun60`); Lamp ships its entry disabled
(`{"enabled": false}`), with the verified host-header bus `0`. A missing file
or selected-board entry disables the sensor. Disabled entries may omit `bus` or set it to `null` while the bus is unknown.
Enabling acquisition requires a nonnegative integer `bus`.
Invalid configuration, including unknown fields, is rejected at startup.

To enable SEN55 after wiring is confirmed, first disable SEN63C to avoid
overlapping metrics, edit the actual board's SEN55 entry, and restart HAL. This template
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

## SEN63C combined component

`robots/lamp/sen63c.json` uses the same `boards` map with SEN63C disabled for
`orangepi_sun60` on bus `0`. All three Pro overrides supply a replacement JSON with
that entry enabled. `sda_pin` and `scl_pin` remain null; other boards without
an entry (including Raspberry Pi) remain disabled even in Pro. Confirm wiring for each installation; the
SEN55 pin note above does not establish SEN63C wiring. The driver uses I2C
address `0x6B`, verifies the SEN63C product type, checks word CRCs, and reads
PM1/PM2.5/PM4/PM10, temperature, humidity and measured `co2_ppm`. VOC/NOx remain
null in the shared sample. CO₂ can be unavailable during the first 22–24 seconds
while other readings are already usable; OS warm-up applies independently.

Defaults are `poll_interval_s: 1`, `retry_interval_s: 5`, `stale_after_s: 5`,
and `no_data_timeout_s: 30`. `automatic_self_calibration: null` preserves the
sensor setting; explicit true/false configures CO₂ ASC. This is separate from
OS warm-up and change thresholds. No forced recalibration or persistence command
is sent. See [Sensirion's SEN63C driver reference](https://sensirion.github.io/python-i2c-sen63c/api.html).
The host I2C bus must run at **100 kHz or less**, as specified in
[Sensirion's SEN6x datasheet, section 4.4](https://sensirion.com/resource/datasheet/SEN6x).
The OrangePi Sun60 bus can default to 400 kHz: on the tested device this
caused product-type CRC failures; switching bus 0 to 100 kHz restored valid
identity and measurement responses. Do not bypass CRC checks to accept these
corrupted packets. This verifies that device's bus, not other installations' wiring.

Before opening the sensor, every SEN63C driver construction calls the shared
I2C clock helper for its configured bus. For adapters whose `name` starts with
`SUNXI TWI`, HAL reads `/sys/class/i2c-adapter/i2c-N/device/info` and its
`twi->freqency` field (the kernel's spelling). A rate above `100000` Hz is
lowered to `100000` through the controller's `device/freq` attribute, then read
back to verify that it is at most `100000`. An already compliant rate is left
unchanged. HAL needs permission to read these attributes and write `freq` when
lowering the clock. Missing/malformed controller data, a failed write, or a
readback above the limit prevents sensor access and appears in the component's
`last_error`; the normal worker retry reconstructs the driver and tries again.

This preparation runs on each driver construction, including after HAL starts
at boot or recovers from an error. Installing updated HAL through setup or OTA
therefore includes the fix without a separately installed systemd drop-in.
Disabled components and simulation do not construct the hardware driver and
never write the clock. Other adapter types are left unchanged: configure their
bus to at most 100 kHz through the board/kernel's supported configuration.
The sensor JSON's `bus` field selects the adapter, not its speed. Wiring,
pin-mux, power and the correct bus still require per-board confirmation.
Lowering the controller clock affects every peripheral sharing that physical bus.

After installing updated HAL, the earlier device-local workaround
`/etc/systemd/system/hal.service.d/20-sen63c-i2c.conf` can be removed; reload
systemd after removing it. Restart HAL and check the selected controller's
`device/info` and `/environment/status` for a clock at most `100000`, fresh
samples and no errors. On the tested OrangePi Sun60, the automatic path was
verified by removing that drop-in, restoring 400 kHz with HAL stopped, and
starting updated HAL: the driver logged the change to 100 kHz and resumed
valid measurements. MPR121 on the same bus completed 100/100 status reads while
SEN63C was running (mean 0.823 ms, maximum 5.040 ms). This checks communication;
physical touch gestures and a full board reboot were not exercised in that test.

HAL lifecycle logs use component keys `[sen55]`, `[scd41]`, and `[sen63c]`: disabled/start,
measurement start, retry failures and stop are visible at INFO (failures may
be WARNING or ERROR). Every valid sample is logged at INFO with timestamp and
measured values; polls without a new sample log waiting state and last-data
age. `[environment]` records registered components/simulation or missing capability.

To follow logs on the device, use either its log file or systemd journal:

```sh
tail -F /var/log/hal/server.log | grep --line-buffered -E '\[(environment|sen55|scd41|sen63c)\]'
journalctl -u hal -f | grep --line-buffered -E '\[(environment|sen55|scd41|sen63c)\]'
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
and timing. Non-ready or expired component readings are unavailable; a component
with a nonzero status register is also excluded from the combined sample.

The group is `ready` and not stale when at least one component contributes a
fresh value. `partial: true` means another enabled component is unavailable;
a disabled component alone does not make the group partial. Healthy values
remain usable when another sensor fails. The group `sample` is always an object
with all nine metric keys below plus `timestamp`. Unsupported, disabled, not-yet-ready
and stale values are `null`. With no usable values, all metrics, `sample.timestamp`
and `age_s` are `null`, and `stale` is true. Raw per-component samples may retain
old values for diagnostics; they are not current aggregate readings.

`sources` maps metrics owned by enabled components to their component; `metric_timestamps` records each
usable metric's Unix timestamp. Group `sample.timestamp` and `age_s` describe
the newest contributing data, not every metric. Consumers must check source
status and metric freshness independently. Bus/timing are also exposed at the
top level only for a single-component group. `ready` does not certify gas
sensor warm-up or calibration.

A sample contains:

| Field | Meaning |
|---|---|
| `timestamp` | Unix time in seconds, or `null` without a usable reading |
| `pm1_0_ug_m3`, `pm2_5_ug_m3`, `pm4_0_ug_m3`, `pm10_ug_m3` | Particle mass concentration in µg/m³ |
| `humidity_pct` | Relative humidity in % |
| `temperature_c` | Temperature in °C |
| `voc_index`, `nox_index` | Unitless gas indexes, not ppm measurements |
| `co2_ppm` | Measured carbon dioxide concentration in ppm, from an enabled CO₂ component |

Sensor-specific `device_status` remains inside each component sample for
diagnostics. Consumers use the shared metric schema and component state rather
than treating a device register as a combined sensor status.

Unavailable measurement values are JSON `null`; callers must not treat them
as zero. OS change detection and agent interpretation are described below.

## Local web view

[Device → Sensing](../../../docs/web-ui.md#58-device--sensing) and its
**Environment** card always remain visible without debug mode. Missing or
loading capabilities show `N/A` measurements without sensor requests. Camera
cards remain gated by `vision`.

When `environment` is declared, the browser polls
`GET /api/hardware/environment/status` every 3 seconds via
the existing authenticated OS hardware proxy, which forwards to HAL
`GET /environment/status`. This refresh interval is separate from the HAL
polling configuration below. The card shows state, the nine shared metric fields
(including CO₂ in ppm), source labels, sample
time, stale status, and errors. Missing or stale values appear as `N/A` and
request failures are shown explicitly so old values are not presented as live
readings. Component failures do not hide healthy readings. Bus, device status
register, and timing configuration are in a collapsed technical section per
component under `status.components`; legacy single-sensor snapshots remain supported. This is a read-only display with no good/bad
thresholds or historical storage. OS → agent events come from the independent
worker below, not browser refreshes.

Only the Pro profiles declare the capability, so Standard shows `N/A` without
polling. On Pro, fresh OrangePi SEN63C readings appear; missing hardware shows
errors and `N/A` while the worker retries. Disabling all components, or using a board without matching
entries, shows the disabled state and `N/A`. UI visibility does not enable
acquisition or agent events.

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
HTTP responses, and invalid status JSON also return failure. Only Pro profiles
declare the capability, so missing hardware on Pro is reported in the snapshot;
Standard returns the missing-capability failure. This is
request/reply, with no continuous stream, automatic events, or agent invocation.
See the [MQTT protocol](../../../docs/mqtt.md) for payloads and response rules.

## Timing configuration

Each board entry accepts these optional timing fields (seconds). The following defaults are for SEN55; other component defaults are listed above. Values must be finite positive numbers; `stale_after_s` and `no_data_timeout_s` must exceed `poll_interval_s`. Restart HAL after editing. These control HAL polling, not the sensor’s internal sampling rate.

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
This is separate from HAL timing in each component JSON. Defaults are:

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
It consults the environmental-care section of `skills/wellbeing/SKILL.md`
for proactive timing and phrasing. User-reported discomfort uses
`skills/wellbeing/reference/discomfort.md`; reuse already-loaded instructions
and data rather than passing the turn repeatedly between skills. Room questions
and discomfort support do not require camera observations, identity, activity
logs or hydration counters.

For fatigue, headache, dizziness, stuffiness or difficulty concentrating,
wellbeing responds to the user first. Environment is optional: absent/unknown
capability means no environment tool call; failed, all-null or stale readings
mean silently omit environmental advice. Do not mention sensor errors or ask
for hardware setup in response to a wellbeing concern. Only explicit questions
about room readings need an unavailable-data explanation. With declared
capability, one bounded status read (or a supplied current snapshot) can add a
relevant observation and one conditional comfort/ventilation suggestion. No
reading establishes the cause of a symptom or dismisses a user's concern.

The discomfort reference covers when a reported symptom/exposure should take
priority over sensor checks, and includes sourced guidance and example cases.
This is skill-level response guidance, not an OS medical/alarm engine. The
CO₂ interpretation reference distinguishes a rise from a sustained ventilation
concern; it adds no automatic concentration classification or OS threshold.
A later real reading may support comparison, but no scheduled follow-up,
new wellbeing log action or appliance permission is implied.

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

VOC/NOx indices are relative, not ppm or chemical identification. Use CO₂
statements only when measured `co2_ppm` is fresh, regardless of component;
VOC/NOx must never be used to infer CO₂. Missing indices on SEN63C are expected,
not a sensor fault. No supported component measures O₂ or CO, or establishes a smoke/fire alarm. Instantaneous PM does not establish compliance with WHO
24-hour or annual exposure guidelines. Consider outside conditions before
suggesting ventilation and enclosure heat before interpreting temperature.
The skills do not diagnose health, invent thresholds, resume unrelated tasks,
or operate a purifier/fan/HVAC without an available authorized integration.
No environment-specific well-being log action is introduced.

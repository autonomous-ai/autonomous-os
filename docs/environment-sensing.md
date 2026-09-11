# Environmental sensing — SEN55

SEN55 is an environmental sensor: particulate matter, humidity, temperature,
and VOC/NOx indexes. HAL exposes it as an optional `environment` capability,
with its own driver, lifecycle, and HTTP snapshots alongside camera and audio.
This first integration provides acquisition only. It defines no thresholds,
notifications, OS sensing events, or agent behavior. It does not measure touch
or pressure; tactile sensing would require a different sensor.

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
or selected-board entry disables the sensor. Disabled entries need no `bus`.
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

The worker polls every 1 second and retries hardware failures after 5 seconds.
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
| `GET /environment/status` | Reports state, `last_error`, latest `sample`, `age_s`, and `stale`, including when disabled or unavailable |
| `GET /environment/sample` | Returns a fresh snapshot; HTTP 503 when unavailable or stale |
| `GET /health` | The `environment` boolean reports whether a fresh sample is available |

States are `disabled`, `starting`, `ready`, `error`, and `stopped`. A sample
older than 5 seconds is stale; any state other than `ready` is also stale,
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
as zero. The raw readings and status leave future interpretation to a later
OS/agent integration.

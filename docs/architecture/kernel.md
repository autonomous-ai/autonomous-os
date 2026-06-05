# Kernel

**Autonomous's kernel is the Linux kernel** — the vendor kernel shipped with Raspberry Pi
OS (Pi 4/5) and OrangePi. We run on it; we don't ship our own.

> **Is the runtime (OpenClaw / Hermes) the kernel? No.**
> A kernel is the *foundation* — it manages the hardware, and everything runs on top of it
> (the bottom of the stack). The agentic runtime is the *brain* — it reasons and decides
> (the top of the stack). Opposite ends. The thing that feels "core," our `os/core` daemon,
> is the **System Services** layer — not the kernel.

## Why the vendor kernel

The target boards ship mature kernels with working drivers, device trees, and power
management for their exact silicon. Autonomous's value is the layers *above* the kernel —
HAL, services, runtime, the device contract — not maintaining a kernel.

## Interfaces the drivers use

Autonomous drivers are **userspace** programs. They never touch kernel internals; they use
the kernel's stable userspace interfaces:

| Interface | Node | Used by |
|---|---|---|
| GPIO (character device, via lgpio) | `/dev/gpiochipN` | button, touch |
| SPI | `spidev` | ws2812 LED, gc9a01 display |
| PWM | `rpi_ws281x` | ws2812 LED (Pi 4) |
| Serial / USB-CDC | `/dev/ttyACM0` | feetech servo |
| Audio | ALSA | mic, speaker |
| Camera | V4L2 | vision |

Because these interfaces are stable, a kernel point-release doesn't break a driver. What
varies per board (which GPIO, PWM vs SPI LED) is isolated in **Board Support**
(`platform/board.py`), detected once from `/proc/device-tree/model` — not here.

## Bringing up a new board

Its kernel must expose: character-device GPIO; `spidev`/PWM for LED and display; a serial
node for the servo bus (if it moves); ALSA for audio; V4L2 for the camera (if it sees); and
a readable `/proc/device-tree/model`. A board lacking a subsystem simply doesn't declare
that capability in its `DEVICE.md`.

## See also

[overview.md](overview.md) · [hal.md](hal.md) · `imager/` (board images + device-tree overlays)

# Kernel

## What is Autonomous's kernel?

**The Linux kernel** — specifically the vendor kernel shipped with **Raspberry Pi OS**
(Pi 4/5) and **OrangePi**'s distribution. Exactly like Android, Autonomous runs *on* a
Linux kernel; it does not ship its own.

> ### Is the gateway (OpenClaw / Hermes) the kernel?
>
> **No.** This is a common and understandable mix-up, so it's worth stating plainly.
>
> A *kernel* is the **foundation** of an OS: it manages the hardware, processes, memory,
> and scheduling, and everything else runs on top of it. It is the **bottom** of the
> stack, closest to the metal.
>
> The agentic gateway is the **brain** — the reasoning engine at the **top** of the
> stack that decides what to do. It is the opposite end from the kernel. In Android
> terms the gateway is nearer the apps + runtime than the kernel.
>
> So: **kernel = Linux (bottom). Gateway = OpenClaw/Hermes (top).** The thing in the
> middle that feels "core" — our system runtime (`os/core`) — maps to Android's
> *framework / system services*, not to the kernel.

## Why we use the vendor kernel (and don't fork it)

The boards we target ship mature, board-specific Linux kernels with working drivers,
device trees, and power management for their exact silicon. Autonomous's value is the
**layers above** the kernel (HAL, framework, gateway, the device contract), not in
maintaining a kernel. This mirrors Android's split between the **Generic Kernel Image**
(hardware-agnostic core) and **vendor modules** (board-specific): we simply let the board
vendor own the entire kernel + vendor-module side, and we build on the stable interfaces
it exposes to userspace.

## Kernel interfaces the HAL relies on

Autonomous drivers are **userspace** programs. They never touch kernel internals; they
use the stable kernel→userspace interfaces (the equivalent of Linux's "don't break
userspace" syscall ABI):

| Interface | Kernel surface | Used by |
|-----------|----------------|---------|
| GPIO (lines, edges) | `/dev/gpiochipN` via **lgpio** (character-device GPIO) | button, touch (`gpio_button`, `ttp223`) |
| SPI | `spidev` (`/dev/spidevB.D`) | ws2812 LED, gc9a01 display |
| PWM | `rpi_ws281x` (DMA/PWM) | ws2812 LED on Pi 4 |
| Serial / USB-CDC | `/dev/ttyACM0` | feetech servo bus |
| Audio | **ALSA** (`plughw:X,Y`) | mic capture, speaker output |
| Camera | **V4L2** (`/dev/videoN`) | camera / vision |
| Networking | standard sockets, wpa_supplicant | provisioning, OTA, gateway WS |

Because these are the kernel's stable userspace interfaces, the HAL is insulated from
kernel-version churn — a kernel point-release does not break a driver.

## Board kernels

| Board | Kernel | Notes |
|-------|--------|-------|
| Raspberry Pi 4 | Raspberry Pi OS (Debian-based) | LED via `rpi_ws281x` PWM on GPIO 12 |
| Raspberry Pi 5 | Raspberry Pi OS | LED via `spidev` (SPI0.0); RP1 I/O |
| OrangePi (sun60iw2 / A733) | OrangePi distribution | LED via SPI3.0; button on gpiochip1 |

What differs per board (which GPIO chip/line, PWM vs SPI LED transport, touch lines) is
**not** in the kernel layer for us — it's isolated in the **Platform** layer
([`platform/board.py`](../../os/hal/lelamp/platform/board.py)), detected once from
`/proc/device-tree/model`. See [overview.md](overview.md).

## Device tree

Board hardware topology (which peripherals exist, on which buses) is described by the
**device tree** the vendor kernel ships, plus overlays enabled at boot (e.g. `dtparam=spi=on`
to expose `spidev` for the LED). Enabling the right overlays is part of image building —
see `imager/`. Autonomous reads the resulting model string at runtime to pick the board
profile; it does not modify the device tree at runtime.

## Kernel requirements (for a new board)

To bring Autonomous up on a new Linux SBC, its kernel must expose:

1. **Character-device GPIO** (`/dev/gpiochipN`, usable via lgpio) for buttons/touch.
2. **`spidev`** (and/or PWM) for the LED and display transports the device uses.
3. A **serial/USB-CDC** node for the servo bus (if the device has motion).
4. **ALSA** capture + playback for mic/speaker.
5. **V4L2** for the camera (if the device has vision).
6. A readable **`/proc/device-tree/model`** so the platform layer can identify the board.

A device that lacks a subsystem simply doesn't declare that capability in its `DEVICE.md`
— the HAL won't bring it up. See [hal.md](hal.md).

## What's next

- [overview.md](overview.md) — the full stack
- [hal.md](hal.md) — how capabilities map onto these kernel interfaces
- `imager/` — building flashable board images with the right device-tree overlays

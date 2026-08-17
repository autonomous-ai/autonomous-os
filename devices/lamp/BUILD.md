# Building a Lamp from parts

The builder's path. A bought Lamp ships with Autonomous OS installed and skips all of this — open the Autonomous app, tap **Add robot**. This page is for a Lamp you assemble yourself: what to buy, how to flash and install, and how to get the servos and audio right before you talk to it. It ends where the README's [Get started](../../README.md#autonomous-lamp) begins.

## Parts

- **Compute:** Raspberry Pi 5 or OrangePi 4 Pro (our board). Pi 4 is declared in `ROBOT.md` but untested.
- **Motion:** five Feetech STS3215 bus servos on a Waveshare USB bus servo adapter.
- **Senses:** any UVC webcam, a USB mic, a speaker on a USB DAC, a 64-LED WS2812 ring on SPI. That is the minimum for a lamp that sees, talks and moves; everything else in the [BOM](hardware/components.md) is optional or decorative.
- **Power:** a single 12 V / 5 A adaptor into the chassis, one buck to 5 V for the board and ring — see [`hardware/power.md`](hardware/power.md). The servo bus rail is the one line the hardware docs still disagree on (`wiring.md` says external 5 V, the README steps assume the 12 V adaptor); check the STS3215 datasheet against your adapter before you power the bus, and open an issue with what you measured.
- **Body:** 17 parts, each as STEP and STL, in [`hardware/cad/`](hardware/cad/) (Git LFS — GitHub's Download button gives the real file; after a clone, install git-lfs, then `git lfs install && git lfs pull`). The servo carriers are CNC aluminium with no printed version yet; [`hardware/assembly.md`](hardware/assembly.md) is a skeleton without print times or torques. PRs welcome on both.
- **Wiring:** [`hardware/wiring.md`](hardware/wiring.md) — Pi 5 and OrangePi pinouts, taken from the code.

## 1. Flash the board's own Linux

Raspberry Pi OS Lite 64-bit, or OrangePi's Debian, with SSH on. Plug in Ethernet — it is your way back in if the hotspot never appears: the installer replaces the Wi-Fi stack near the end and reboots into a hotspot, so an SSH-over-Wi-Fi session drops. That is expected.

Which Pi OS: the SD-card imager targets Trixie on Pi 5 and Bookworm on Pi 4 and handles NetworkManager itself; the curl installer below has not been verified on stock Trixie (which ships NetworkManager). If the hotspot doesn't come up after reboot, `sudo systemctl disable --now NetworkManager && sudo reboot` — and tell us in an issue.

## 2. Install Autonomous OS

Needs internet and ~4 GB free; budget 15 min, most of it HAL's Python environment. `install.sh` fetches [`scripts/provision/setup.sh`](../../scripts/provision/setup.sh) from our CDN — a published snapshot of the file in this tree — and runs it as root; it pulls every component from the release feed and puts each behind a systemd unit. Over Wi-Fi run it under `nohup` so the session drop doesn't kill it:

```bash
curl -fsSL https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/scripts/provision/install.sh -o install.sh
sudo -v   # cache the password first (OrangePi asks, Pi OS doesn't)
sudo DEVICE_TYPE=lamp nohup bash install.sh > install.log 2>&1 &
tail -f install.log
```

Over Wi-Fi your session drops in the last stage — that is the sign it is nearly done. When `lamp-xxxx` shows up in your Wi-Fi list (xxxx = last 4 characters of the board serial, lowercase) it has rebooted into setup mode; `install.log` stays on the board and ends with `AP SSID: lamp-xxxx` and any stage that failed — re-run the same command to retry. No Ethernet and no `lamp-xxxx` after 5 min? The board's old Wi-Fi is masked too, so SSH is gone: pull the SD card, read `install.log` from your home directory on it, reflash, and start again over a cable.

Prefer to run the file you can read? `git clone https://github.com/autonomous-ai/autonomous-os && cd autonomous-os && sudo DEVICE_TYPE=lamp OTA_METADATA_URL=https://cdn.autonomous.ai/os/ota/metadata.json bash scripts/provision/setup.sh` — `install.sh` does nothing but fetch that file and run it. The feed is our CDN — prebuilt `os-server`, HAL, web UI and OpenClaw, built from this tree by `scripts/release/`; to run your own build, publish your own feed and pass `OTA_METADATA_URL=https://…/metadata.json`. Prefer an SD-card image? [`scripts/imager/`](../../scripts/imager/) builds one in 25–40 min (Docker on Linux; no prebuilt image yet).

## 3. Give the servos IDs, then calibrate

One time, ~15 min. The board is now in hotspot mode: SSH back in over Ethernet (same IP) or join `lamp-xxxx` and `ssh <user>@192.168.100.1`. New STS3215s all ship as ID 1, so assign IDs first — with the arm still open and the servo supply on — one servo on the bus at a time (the tool tells you which to plug in):

```bash
sudo systemctl stop hal            # it holds /dev/ttyACM0
cd /opt/hal
sudo ./.venv/bin/python3 -m hal.setup_motors --id lamp-xxxx --port /dev/ttyACM0
sudo ./.venv/bin/python3 -m hal.calibrate   --id lamp-xxxx --port /dev/ttyACM0 --follower-only
sudo systemctl start hal
```

Homing lives in the servos' own EEPROM, so it survives reflashes. Full notes: [`hal/calibration/calibration.md`](../../hal/calibration/calibration.md).

## 4. Check the body before you talk to it

Run the runtime CTS: HAL and the daemon listen on the board's loopback only, so tunnel first — `ssh -N -L 5001:127.0.0.1:5001 -L 5000:127.0.0.1:5000 <user>@lamp-xxxx.local`, then `make cts-runtime TARGET=127.0.0.1` from a clone. It proves the live body matches its own `ROBOT.md` (every declared route mounted and answering, nothing undeclared, `/servo/track/stop` replying) and names the first thing that fails.

Silent or deaf? A self-built board has no `/etc/asound.conf`, so HAL takes the first mic and speaker it finds. If it guessed wrong: `arecord -l` / `aplay -l`, then in `/opt/hal/.env` set `HAL_AUDIO_INPUT_ALSA=plughw:<card>,0` and `HAL_AUDIO_OUTPUT_ALSA=plughw:<card>,0` (servo adapter not on `/dev/ttyACM0`? `HAL_SERVO_PORT=/dev/ttyUSB0`), then `sudo systemctl restart hal`. Logs: `journalctl -u hal -f`; the body's API: `http://lamp-xxxx.local/api/hardware/docs`.

## 5. Set it up

Now continue at the README's [Get started → Autonomous Lamp](../../README.md#autonomous-lamp), step 1: open the Autonomous app, tap **Add robot** → Lamp, and it finds the `lamp-xxxx` hotspot your board is broadcasting. The app claims a device in your account, and the AI key comes with it — self-built bodies too.

Without the app or an account: gather an AI key and a chat channel first (the hotspot has no internet — a Telegram bot token from [@BotFather](https://t.me/BotFather) → `/newbot` and your numeric Telegram user ID from [@userinfobot](https://t.me/userinfobot); Slack and Discord work too), join `lamp-xxxx`, and open `http://192.168.100.1/setup?debug=true&device_id=lamp-xxxx` (`device_id` is required — it becomes the robot's name in your account, any unique string). The default OpenClaw brain takes its model list from the Autonomous AI gateway (our setup wires only that provider today), so with your own Anthropic key (`https://api.anthropic.com`) or OpenAI key (`https://api.openai.com/v1`): finish setup, then swap the brain to Claude Code or Codex at `http://lamp-xxxx.local/setting?debug=true#runtime` before you talk to it — they use your key directly. Voice, face and mood also ride on the gateway (or your own Deepgram + OpenAI speech keys), so the robot is text-first until you run **Add robot** in the app once, which issues the gateway key. Bring-your-own endpoint for OpenClaw is on the README's [Not built yet](../../README.md#contribute) list.

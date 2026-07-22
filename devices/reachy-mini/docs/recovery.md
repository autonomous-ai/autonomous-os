# Reachy Mini Recovery & Troubleshooting

This document covers Pollen OS recovery methods, SSH access, and the impact of
running Autonomous `setup.sh` on the stock Pollen OS network configuration.

## SSH Access

Default credentials on stock Pollen OS:

```bash
ssh pollen@reachy-mini.local   # password: root
```

If mDNS is not available, use the IP directly: `ssh pollen@<IP>`.

After Autonomous `setup.sh` has run, the hostname changes to
`reachy-mini-<suffix>` (suffix derived from the Pi serial number), so:

```bash
ssh pollen@reachy-mini-abcd.local   # substitute actual suffix
```

Ref: [Pollen troubleshooting](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/troubleshooting)

## Recovery Methods

Ordered from least to most invasive. Try each level before escalating.

### Level A: Power Cycle

Press OFF, wait 5 seconds, press ON. Fixes transient daemon hangs.

### Level B: Bluetooth Software Reset (No SSH / No WiFi Required)

Reachy Mini exposes a BLE GATT service for out-of-band recovery. Three client
options:

1. **Reachy Mini Control App** (desktop) — "First time WiFi setup" → "Try the
   Bluetooth Console"
2. **Web Bluetooth Dashboard** (Chrome/Edge/Opera) — no install needed
3. **nRF Connect** (mobile) — generic BLE client, advanced users

**PIN**: last 5 digits of the robot's serial number, sent before any command.

| BLE Command | Effect |
|-------------|--------|
| `STATUS` | Check robot status |
| `CMD_HOTSPOT` | Reset WiFi hotspot to default (`reachy-mini-ap` / `reachy-mini`) |
| `CMD_RESTART_DAEMON` | Restart the Pollen daemon service |
| `CMD_SOFTWARE_RESET` | Full software reset (~5 minutes to come back online) |

Ref: [Seeed Studio BLE reset guide](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/)

### Level C: SSH venv Recovery (Daemon Restart Loop)

If the Pollen daemon enters an infinite restart loop (common after power loss
during an app install), the Python virtual environments may be corrupted. The
robot ships with a clean backup at `/restore/venvs`:

```bash
ssh pollen@reachy-mini.local   # password: root
sudo systemctl stop reachy-mini-daemon   # or equivalent service name
sudo mv /venvs /venvs.broken
sudo cp -a /restore/venvs /venvs
sudo reboot
```

Ref: [pollen-robotics/reachy_mini#599](https://github.com/pollen-robotics/reachy_mini/issues/599)

### Level D: Full eMMC Reflash (Factory Reset)

Nuclear option — wipes everything (all user data, WiFi config, installed apps)
and restores Pollen OS to factory state. Only use when all other methods fail.

**Hardware note**: Reachy Mini Wireless uses a Raspberry Pi CM4 with 16 GB
onboard eMMC (no SD card slot). Flashing requires USB boot mode.

#### Prerequisites

| Item | Source |
|------|--------|
| OS image (`.img.xz`) + `.bmap` file | [reachy-mini-os releases](https://github.com/pollen-robotics/reachy-mini-os/releases) |
| `rpiboot` tool | [raspberrypi/usbboot](https://github.com/raspberrypi/usbboot) |
| `bmaptool` (Linux/macOS) or Raspberry Pi Imager (Windows) | Package manager or [rpi-imager](https://www.raspberrypi.com/software/) |
| USB cable | To the USB2 port on the head PCB |

#### Steps

1. **Shut down** the robot completely.
2. **Set the hardware switch** on the head PCB to **DOWNLOAD (SW1)** position.
3. **Start rpiboot** on your computer:
   - Linux/macOS: `sudo ./rpiboot -d mass-storage-gadget64`
   - Windows: RPiBoot GUI → select `rpiboot-CM4-CM5 - Mass storage Gadget`
4. **Connect** USB cable to the USB2 port on the head PCB.
5. **Power on** the robot. rpiboot exposes the internal eMMC as USB storage.
6. **Unmount** auto-mounted partitions:
   - macOS: `diskutil unmountDisk /dev/diskX`
   - Linux: `sudo umount /media/$USER/bootfs /media/$USER/rootfs`
7. **Flash** the image:
   - macOS: `sudo bmaptool copy <image>.xz --bmap <image>.bmap /dev/rdiskX`
   - Linux: `sudo bmaptool copy <image>.xz --bmap <image>.bmap /dev/sdX`
   - Windows: Raspberry Pi Imager → device "Raspberry Pi 4" → "Use custom" → select image
8. **Restore normal boot**: power off, switch back to DEBUG, disconnect USB, power on.
9. **Verify**: connect to WiFi `reachy-mini-ap` (password `reachy-mini`), then:
   ```bash
   ssh pollen@reachy-mini.local   # password: root
   reachyminios_check             # should output "Image validation PASSED"
   ```

**macOS Apple Silicon note**: there are known issues with rpiboot on M-series
Macs — see [pollen-robotics/reachy_mini#734](https://github.com/pollen-robotics/reachy_mini/issues/734)
for workarounds.

Ref: [Official reflash guide](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)

## Impact of Autonomous setup.sh on Pollen OS Network

Running `DEVICE_TYPE=reachy-mini setup.sh` modifies the network stack to enable
Autonomous's AP-mode captive portal and STA-mode switching. Below is what it
touches:

| Config File | Action | Reversible? |
|-------------|--------|-------------|
| `/etc/wpa_supplicant/wpa_supplicant-wlan0.conf` | **Replace** (old config backed up as `.conf.bak`) | Yes — restore from `.bak` |
| `/etc/hostapd/hostapd.conf` | **Replace** | Reflash to restore original |
| `/etc/dnsmasq.d/99-reachy-mini.conf` | **Add** (drop-in file) | Yes — just delete the file |
| `/etc/dnsmasq.conf` | **Edit** (comments out conflicting `interface=wlan0` lines) | Yes — uncomment |
| `/etc/dhcpcd.conf` | **Edit** (removes old `interface wlan0` block, appends new AP block) | Partial — original block is not backed up |
| `wpa_supplicant.service` (global) | **Masked** (only `wpa_supplicant@wlan0` instance is used) | Yes — `systemctl unmask wpa_supplicant` |

### Risk Assessment

- **If Pollen OS uses `dhcpcd` + `wpa_supplicant`** (classic Raspberry Pi OS
  stack): setup.sh is designed for this stack. WiFi will work, AP mode will
  work, and `device-sta-mode` / `device-ap-mode` scripts handle switching. Low
  risk.

- **If Pollen OS uses `NetworkManager`** (newer Bookworm default): setup.sh
  stops and disables NetworkManager. This breaks Pollen's own WiFi management.
  The robot may lose network connectivity until you either complete the
  Autonomous setup flow or manually re-enable NetworkManager. **Check which
  stack Pollen uses before running setup.sh on the real device.**

### How to Check (Before Running setup.sh)

```bash
ssh pollen@reachy-mini.local
# Check if NetworkManager is active
systemctl is-active NetworkManager
# Check if dhcpcd is active
systemctl is-active dhcpcd
# Check which manages wlan0
nmcli device status 2>/dev/null || echo "No NetworkManager"
```

### Recovery After WiFi Breakage

1. **BLE hotspot reset**: Send `CMD_HOTSPOT` via Bluetooth (see Level B above).
   This resets WiFi to factory AP mode (`reachy-mini-ap` / `reachy-mini`).
2. **Ethernet**: Plug in a USB-to-Ethernet adapter, SSH via wired connection,
   and fix configs manually.
3. **Reflash**: Level D above restores everything to factory state.

## References

- [Reachy Mini OS repo (pi-gen based)](https://github.com/pollen-robotics/reachy-mini-os)
- [Reachy Mini OS releases](https://github.com/pollen-robotics/reachy-mini-os/releases)
- [Reachy Mini SDK & docs](https://github.com/pollen-robotics/reachy_mini)
- [Hardware datasheet (Hugging Face)](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/hardware)
- [Reflash guide (Hugging Face)](https://huggingface.co/docs/reachy_mini/platforms/reachy_mini/reflash_the_rpi_ISO)
- [BLE reset guide (Seeed Studio)](https://wiki.seeedstudio.com/reachymini_platforms_reachy_mini_reset/)
- [Daemon restart-loop fix #599](https://github.com/pollen-robotics/reachy_mini/issues/599)
- [Apple Silicon rpiboot issue #734](https://github.com/pollen-robotics/reachy_mini/issues/734)
- [raspberrypi/usbboot](https://github.com/raspberrypi/usbboot)

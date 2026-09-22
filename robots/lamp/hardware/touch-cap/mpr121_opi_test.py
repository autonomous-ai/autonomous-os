#!/usr/bin/env python3
"""MPR121 capacitive touch test for OrangePi (12 electrodes, I2C).

Linux/OrangePi counterpart of src/main.cpp (which targets an ESP32-C3).
Pure stdlib - talks to /dev/i2c-* through the I2C_RDWR ioctl, so it needs
no smbus/smbus2/i2c-tools package.

Wiring on the 40-pin header:
    pin 1  -> VCC (3.3V, NOT 5V)
    pin 3  -> SDA
    pin 5  -> SCL
    pin 6  -> GND

Usage:
    sudo ./mpr121_opi_test.py check         # bus health: pin levels, stuck-low, clock
    sudo ./mpr121_opi_test.py scan          # list every I2C device on every bus
    sudo ./mpr121_opi_test.py calibrate     # measure idle noise, recommend thresholds
    sudo ./mpr121_opi_test.py test          # stream touch/release events
    sudo ./mpr121_opi_test.py raw           # live per-electrode delta from baseline
    sudo ./mpr121_opi_test.py trace --ignore 0,1,2,3,4,5,6,7   # filt/base per sample: why a touch ends
    sudo ./mpr121_opi_test.py test --touch 20 --release 10 --debounce 3 --ignore 0,1,2

Phantom touches: run `calibrate` with hands off the pads. It reports how far
each electrode wanders at idle and prints thresholds that clear that with
margin. Then re-run `test` with those values and confirm real touches still
register. Unconnected electrodes read a flat 0 and should be --ignore'd.

Nothing found: run `check` first. It probes the MPR121 addresses and then
asks the kernel whether the TWI controller found SDA stuck low while doing so
("twi bus barrier failed, sda is still low!" in dmesg). The SoC keeps a
pull-up on PB2/PB3 (header 5/3), so an idle bus is HIGH; a barrier failure
means something external is sinking the line - an unpowered or dead breakout,
VCC/GND swapped, or SDA/SCL landing on the breakout's ADDR/IRQ pins. Do not
trust `gpio readall` for this: on sun60iw2 the data register reads 0 while a
pin is muxed to the TWI controller, stuck or not.
"""

import argparse
import ctypes
import glob
import os
import subprocess
import sys
import time

# --- Linux i2c-dev ioctl plumbing -------------------------------------------

I2C_SLAVE = 0x0703
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001


class I2CMsg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.POINTER(ctypes.c_uint8)),
    ]


class I2CRdwrIoctlData(ctypes.Structure):
    _fields_ = [
        ("msgs", ctypes.POINTER(I2CMsg)),
        ("nmsgs", ctypes.c_uint32),
    ]


libc = ctypes.CDLL("libc.so.6", use_errno=True)
libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]


class I2CBus:
    """Minimal I2C master using repeated-start transfers."""

    def __init__(self, bus):
        self.bus = bus
        self.path = "/dev/i2c-%d" % bus
        self.fd = os.open(self.path, os.O_RDWR)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _rdwr(self, msgs):
        arr = (I2CMsg * len(msgs))(*msgs)
        data = I2CRdwrIoctlData(msgs=arr, nmsgs=len(msgs))
        if libc.ioctl(self.fd, I2C_RDWR, ctypes.byref(data)) < 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err))

    def read_regs(self, addr, reg, length):
        """Write the register pointer, then read with a repeated start."""
        wbuf = (ctypes.c_uint8 * 1)(reg)
        rbuf = (ctypes.c_uint8 * length)()
        self._rdwr([
            I2CMsg(addr=addr, flags=0, len=1, buf=wbuf),
            I2CMsg(addr=addr, flags=I2C_M_RD, len=length, buf=rbuf),
        ])
        return bytes(rbuf)

    def write_reg(self, addr, reg, value):
        wbuf = (ctypes.c_uint8 * 2)(reg, value)
        self._rdwr([I2CMsg(addr=addr, flags=0, len=2, buf=wbuf)])

    def probe(self, addr):
        """True if a device ACKs at this address."""
        try:
            rbuf = (ctypes.c_uint8 * 1)()
            self._rdwr([I2CMsg(addr=addr, flags=I2C_M_RD, len=1, buf=rbuf)])
            return True
        except OSError:
            return False


# --- MPR121 ------------------------------------------------------------------

TOUCHSTATUS_L = 0x00
OORSTATUS_L = 0x02      # out-of-range: autoconfig failed for that electrode
FILTDATA_0L = 0x04
BASELINE_0 = 0x1E
MHDR = 0x2B
TOUCHTH_0 = 0x41
RELEASETH_0 = 0x42
DEBOUNCE = 0x5B
CONFIG1 = 0x5C
CONFIG2 = 0x5D
ECR = 0x5E
AUTOCONFIG0 = 0x7B
UPLIMIT = 0x7D
LOWLIMIT = 0x7E
TARGETLIMIT = 0x7F
SOFTRESET = 0x80

NUM_ELECTRODES = 12


class MPR121:
    def __init__(self, bus, addr=0x5A):
        self.bus = bus
        self.addr = addr

    def _w(self, reg, val):
        self.bus.write_reg(self.addr, reg, val)

    def begin(self, touch_th=12, release_th=6, autoconfig=True,
              debounce=2, sfi=2, esi=0, overrides=()):
        """Mirror Adafruit_MPR121::begin() + setAutoconfig(), with the three
        knobs that decide how noise-tolerant the chip is exposed:

        debounce  0-7  consecutive samples a change must persist before the
                       touch/release bit flips (reg 0x5B, same value applied
                       to both). 0 = a single noisy sample fires an event.
        sfi       0-3  second-level filter: 4 / 6 / 10 / 18 samples averaged
                       per reading (CONFIG2 bits 4:3). More = less noise.
        esi       0-7  sample interval 1ms << esi (CONFIG2 bits 2:0). Larger
                       = slower response but even more averaging.
        """
        self._w(SOFTRESET, 0x63)
        time.sleep(0.001)

        # All config writes must happen in stop mode (ECR = 0).
        self._w(ECR, 0x00)

        cfg2 = self.bus.read_regs(self.addr, CONFIG2, 1)[0]
        if cfg2 != 0x24:
            raise RuntimeError(
                "CONFIG2 reads 0x%02X, expected 0x24 - not an MPR121 at 0x%02X"
                % (cfg2, self.addr)
            )

        self.set_thresholds(touch_th, release_th)

        # Baseline filter tuning (Adafruit defaults).
        for reg, val in (
            (MHDR, 0x01), (0x2C, 0x01), (0x2D, 0x0E), (0x2E, 0x00),
            (0x2F, 0x01), (0x30, 0x05), (0x31, 0x01), (0x32, 0x00),
            (0x33, 0x00), (0x34, 0x00), (0x35, 0x00),
        ):
            self._w(reg, val)

        debounce = max(0, min(7, debounce))
        self._w(DEBOUNCE, (debounce << 4) | debounce)
        self._w(CONFIG1, 0x10)  # 16uA charge current
        # CDT=001 (0.5us charge time) | SFI | ESI
        self._w(CONFIG2, 0x20 | ((sfi & 3) << 3) | (esi & 7))

        if autoconfig:
            # Limits for a 3.3V supply: up = ((3.3-0.7)/3.3)*256
            self._w(UPLIMIT, 200)
            self._w(TARGETLIMIT, 180)
            self._w(LOWLIMIT, 130)
            self._w(AUTOCONFIG0, 0x0B)

        # Experiment hook: raw register writes on top of the defaults, e.g.
        # --reg 0x32=0x40 to slow the falling baseline filter.
        for reg, val in overrides:
            self._w(reg, val)

        # Start: baseline tracking enabled, all 12 electrodes on.
        self._w(ECR, 0x8F)

    def charge_regs(self):
        """(CDC, CDT) per electrode; a change while running = auto-reconfig."""
        cdc = self.bus.read_regs(self.addr, 0x5F, NUM_ELECTRODES)
        cdt = self.bus.read_regs(self.addr, 0x6C, 6)
        return [(cdc[i], (cdt[i // 2] >> (4 * (i % 2))) & 0x07)
                for i in range(NUM_ELECTRODES)]

    def set_thresholds(self, touch, release):
        for i in range(NUM_ELECTRODES):
            self._w(TOUCHTH_0 + 2 * i, touch)
            self._w(RELEASETH_0 + 2 * i, release)

    def touched(self):
        data = self.bus.read_regs(self.addr, TOUCHSTATUS_L, 2)
        return ((data[1] << 8) | data[0]) & 0x0FFF

    def out_of_range(self):
        data = self.bus.read_regs(self.addr, OORSTATUS_L, 2)
        return ((data[1] << 8) | data[0]) & 0x0FFF

    def all_filtered(self):
        """All 12 filtered values in one transaction (24 bytes from 0x04)."""
        data = self.bus.read_regs(self.addr, FILTDATA_0L, 24)
        return [data[2 * i] | (data[2 * i + 1] << 8) for i in range(NUM_ELECTRODES)]

    def all_baseline(self):
        data = self.bus.read_regs(self.addr, BASELINE_0, NUM_ELECTRODES)
        return [b << 2 for b in data]

    def filtered_data(self, i):
        data = self.bus.read_regs(self.addr, FILTDATA_0L + 2 * i, 2)
        return (data[1] << 8) | data[0]

    def baseline_data(self, i):
        return self.bus.read_regs(self.addr, BASELINE_0 + i, 1)[0] << 2


# --- helpers -----------------------------------------------------------------

def bus_name(bus_num):
    try:
        with open("/sys/class/i2c-adapter/i2c-%d/name" % bus_num) as fh:
            return fh.read().strip()
    except OSError:
        return ""


def list_buses(include_hdmi=False):
    """All /dev/i2c-* bus numbers, sorted.

    The HDMI DDC adapter is skipped unless asked for: it is never where a
    sensor lives, and with no monitor attached every probe on it has to wait
    out the controller timeout, which makes a full scan look hung.
    """
    buses = []
    for path in glob.glob("/dev/i2c-*"):
        try:
            n = int(path.rsplit("-", 1)[1])
        except ValueError:
            continue
        if not include_hdmi and "HDMI" in bus_name(n).upper():
            continue
        buses.append(n)
    return sorted(buses)


# --- bus health --------------------------------------------------------------

MPR121_ADDRS = (0x5A, 0x5B, 0x5C, 0x5D)


def adapter_device(bus_num):
    """Platform device behind /dev/i2c-N, e.g. '2510000.twi'."""
    try:
        return os.path.basename(os.path.realpath(
            "/sys/class/i2c-adapter/i2c-%d/device" % bus_num))
    except OSError:
        return ""


def adapter_clock_hz(bus_num):
    try:
        with open("/sys/class/i2c-adapter/i2c-%d/device/of_node/clock-frequency"
                  % bus_num, "rb") as fh:
            return int.from_bytes(fh.read(4), "big")
    except (OSError, ValueError):
        return None


def stuck_low_errors(bus_num):
    """How many times the sunxi TWI driver gave up on this bus with SDA low.

    dmesg is usually root-only on this kernel; None means we could not read it.
    """
    dev = adapter_device(bus_num)
    if not dev:
        return None
    try:
        res = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return sum(1 for l in res.stdout.splitlines()
               if dev in l and "sda is still low" in l)


def report_bus_health(bus_num, addrs=MPR121_ADDRS):
    """Print a verdict on whether the bus can carry an MPR121 conversation.

    Returns the ACKing address, or None. The stuck-low test is the kernel's
    own: the sunxi TWI driver checks SDA before every transfer and logs a
    barrier failure when it is held low, so probing and diffing the dmesg
    count tells us whether the bus is free without touching the pinmux.
    """
    print("Bus /dev/i2c-%d  (%s%s)" % (
        bus_num, adapter_device(bus_num) or bus_name(bus_num),
        ", %d kHz" % (adapter_clock_hz(bus_num) // 1000) if adapter_clock_hz(bus_num) else ""))

    errs_before = stuck_low_errors(bus_num)
    found = None
    try:
        bus = I2CBus(bus_num)
    except OSError as exc:
        print("  open failed: %s" % exc)
        return None
    try:
        for a in addrs:
            if bus.probe(a):
                found = a
                break
    finally:
        bus.close()
    print("  MPR121 probe 0x%02x-0x%02x: %s" % (
        addrs[0], addrs[-1], ("ACK at 0x%02x" % found) if found else "no ACK"))

    errs_after = stuck_low_errors(bus_num)
    stuck = None
    if errs_before is not None and errs_after is not None:
        stuck = errs_after - errs_before
        print("  kernel: %d new 'twi bus barrier failed, sda is still low' during probe"
              " (%d since boot)" % (stuck, errs_after))
    else:
        print("  kernel: dmesg not readable - run with sudo for the stuck-low test")

    print()
    if found is not None:
        print("VERDICT: bus healthy, MPR121 at 0x%02x." % found)
    elif stuck:
        print("VERDICT: SDA held LOW - the SoC pull-up is on, so something external\n"
              "is sinking the line. Check, in this order:\n"
              "  1. breakout VCC on header pin 1 (3.3V) and GND on pin 6 - an\n"
              "     unpowered breakout pulls the bus down through its own\n"
              "     pull-ups; VCC/GND swapped does the same\n"
              "  2. the cable at the BREAKOUT end, by silkscreen label: SDA/SCL\n"
              "     landing on ADDR (strapped to GND) or IRQ (open-drain, asserted\n"
              "     low) reads as a stuck bus\n"
              "  3. no solder bridge / stray strand shorting SDA or SCL to GND\n"
              "  4. a breakout that was ever fed 5V (MPR121 max VDD is 3.6V) is\n"
              "     dead and clamps the bus even when 3.3V measures fine now\n"
              "Unplug the sensor and re-run `check`: the new-error count must be 0.")
    elif stuck == 0:
        print("VERDICT: bus idle and healthy (SDA released) but nothing answers at\n"
              "0x5a-0x5d - the MPR121 is not powered, not on this bus, or (if ADDR\n"
              "is strapped) run `scan` to find where it landed.")
    else:
        print("VERDICT: nothing answers at 0x5a-0x5d; run `scan` to see the bus.")
    return found


def cmd_check(args):
    found = report_bus_health(args.bus if args.bus is not None else 0)
    return 0 if found is not None else 1


def scan_bus(bus_num):
    """Return the list of addresses that ACK on this bus."""
    try:
        bus = I2CBus(bus_num)
    except OSError as exc:
        return None, str(exc)
    found = []
    try:
        for addr in range(0x08, 0x78):
            if bus.probe(addr):
                found.append(addr)
    finally:
        bus.close()
    return found, None


def find_mpr121(preferred_addr=None):
    """Locate an MPR121 across every bus. Returns (bus_num, addr) or (None, None).

    Probes only the four MPR121 addresses per bus rather than the whole 0x08-0x77
    range - a full scan is ~780 ioctls, each of which waits out a bus timeout on
    an idle bus, which takes long enough to look like a hang.
    """
    candidates = [preferred_addr] if preferred_addr else [0x5A, 0x5B, 0x5C, 0x5D]
    buses = list_buses()
    print("Searching for MPR121 on %d bus(es): %s"
          % (len(buses), ", ".join(str(b) for b in buses)), flush=True)
    for bus_num in buses:
        try:
            bus = I2CBus(bus_num)
        except OSError:
            continue
        try:
            for addr in candidates:
                if bus.probe(addr):
                    return bus_num, addr
        finally:
            bus.close()
    return None, None


def cmd_scan(args):
    buses = [args.bus] if args.bus is not None else list_buses(include_hdmi=args.all)
    if not buses:
        print("No /dev/i2c-* devices found.")
        return 1
    print("Scanning %d bus(es): %s%s\n" % (
        len(buses), ", ".join(str(b) for b in buses),
        "" if (args.all or args.bus is not None)
        else "  (HDMI DDC bus skipped; --all to include)"))
    any_found = False
    for bus_num in buses:
        print("/dev/i2c-%-2d  scanning..." % bus_num, end="\r", flush=True)
        found, err = scan_bus(bus_num)
        if err:
            print("/dev/i2c-%-2d  open failed: %s" % (bus_num, err))
            continue
        if found:
            any_found = True
            labels = []
            for a in found:
                tag = " <-- MPR121" if 0x5A <= a <= 0x5D else ""
                labels.append("0x%02x%s" % (a, tag))
            print("/dev/i2c-%-2d  %s" % (bus_num, ", ".join(labels)))
        else:
            print("/dev/i2c-%-2d  -" % bus_num)
    if not any_found:
        print("\nNothing responded on any bus. Check:")
        print("  * VCC on pin 1 (3.3V) and GND on pin 6")
        print("  * SDA on pin 3, SCL on pin 5 (not swapped)")
        print("  * pull-ups present (most MPR121 breakouts have them)")
    return 0


def resolve_target(args):
    if args.bus is not None:
        return args.bus, args.addr or 0x5A
    bus_num, addr = find_mpr121(args.addr)
    if bus_num is None:
        print("\nNo MPR121 found on any bus.", file=sys.stderr)
        if not os.path.exists("/dev/i2c-0"):
            print(
                "/dev/i2c-0 does not exist, so header pins 3 (SDA) and 5 (SCL) have\n"
                "no I2C controller behind them. Enable TWI0 by adding the i2c0 overlay\n"
                "to /boot/orangepiEnv.txt and rebooting:\n"
                "    overlays=<existing entries> i2c0\n"
                "Then re-run this script.", file=sys.stderr)
        else:
            print(file=sys.stderr)
            report_bus_health(0)
        sys.exit(1)
    print("Auto-detected MPR121 on /dev/i2c-%d at 0x%02x" % (bus_num, addr))
    return bus_num, addr


def parse_electrodes(spec):
    if not spec:
        return set()
    out = set()
    for tok in spec.replace(",", " ").split():
        n = int(tok)
        if not 0 <= n < NUM_ELECTRODES:
            raise SystemExit("electrode %d out of range 0-%d" % (n, NUM_ELECTRODES - 1))
        out.add(n)
    return out


def open_and_begin(args):
    bus_num, addr = resolve_target(args)
    bus = I2CBus(bus_num)
    cap = MPR121(bus, addr)
    cap.begin(touch_th=args.touch, release_th=args.release,
              autoconfig=not args.no_autoconfig,
              debounce=args.debounce, sfi=args.sfi, esi=args.esi,
              overrides=args.overrides)
    ignore = parse_electrodes(args.ignore)
    print("MPR121 ready: touch=%d release=%d debounce=%d sfi=%d esi=%d%s%s"
          % (args.touch, args.release, args.debounce, args.sfi, args.esi,
             ("  ignoring %s" % sorted(ignore)) if ignore else "",
             ("  overrides %s" % ", ".join("0x%02X=0x%02X" % o for o in args.overrides))
             if args.overrides else ""))
    # Give autoconfig + the baseline filter a moment, then report any electrode
    # the chip itself gave up on.
    time.sleep(0.1)
    oor = cap.out_of_range()
    if oor:
        bad = [i for i in range(NUM_ELECTRODES) if oor & (1 << i)]
        print("WARNING: autoconfig failed (out of range) on electrodes %s -\n"
              "         they are unconnected, shorted, or outside the charge\n"
              "         limits. Expect junk from them; add them to --ignore."
              % bad)
    return bus, cap, ignore


def cmd_test(args):
    """Stream touch/release events with timing.

    Each release prints how long the chip held the touch asserted. That is
    the number the HAL debounce is compared against: a press shorter than
    mpr121.json debounce_ms never becomes a button event.
    """
    bus, cap, ignore = open_and_begin(args)
    mask = 0x0FFF
    for i in ignore:
        mask &= ~(1 << i)
    print("Touch the pads; Ctrl-C to stop.  [t+s] electrode event (hold ms)\n")

    last = 0
    t0 = time.time()
    down = {}
    try:
        while True:
            now = time.time()
            curr = cap.touched() & mask
            if curr != last and args.verbose:
                filt = cap.all_filtered()
                base = cap.all_baseline()
            for i in range(NUM_ELECTRODES):
                bit = 1 << i
                if (curr & bit) and not (last & bit):
                    down[i] = now
                    extra = ("  (delta %d)" % (base[i] - filt[i])) if args.verbose else ""
                    print("[%7.3f] %2d touched%s" % (now - t0, i, extra), flush=True)
                elif not (curr & bit) and (last & bit):
                    held = (now - down.pop(i, now)) * 1000
                    print("[%7.3f] %2d released  (held %.0f ms)" % (now - t0, i, held), flush=True)
            last = curr
            time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        bus.close()
    return 0


def cmd_raw(args):
    bus, cap, ignore = open_and_begin(args)
    print("Per-electrode  baseline-filtered delta  (touch = delta rises above the\n"
          "touch threshold; * = chip reports touched). Ctrl-C to stop.\n")
    try:
        while True:
            touched = cap.touched()
            filt = cap.all_filtered()
            base = cap.all_baseline()
            cells = []
            for i in range(NUM_ELECTRODES):
                if i in ignore:
                    cells.append("%2d:  --- " % i)
                    continue
                mark = "*" if touched & (1 << i) else " "
                cells.append("%2d:%5d%s" % (i, base[i] - filt[i], mark))
            print(" ".join(cells), flush=True)
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        bus.close()
    return 0


def cmd_trace(args):
    """Per-sample filtered/baseline trace for the selected electrodes.

    Answers *why* a touch ends: the baseline walking down to meet the
    filtered value is the baseline filter (regs 0x2F-0x35); the filtered
    value jumping back up together with a CDC/CDT change is auto-reconfig
    (ACCR0 ARE bit) re-centring the electrode mid-touch.
    """
    bus, cap, ignore = open_and_begin(args)
    elec = [i for i in range(NUM_ELECTRODES) if i not in ignore]
    print("Tracing electrodes %s at %d ms; lines print while any is touched or\n"
          "|delta| >= 3.  Columns per electrode: filt/base/delta[T]\n"
          % (elec, args.interval_ms))
    t0 = time.time()
    last_charge = cap.charge_regs()
    print("charge (CDC uA, CDT): %s" % " ".join(
        "e%d=%d/%d" % (i, last_charge[i][0], last_charge[i][1]) for i in elec))
    try:
        while True:
            now = time.time()
            touched = cap.touched()
            filt = cap.all_filtered()
            base = cap.all_baseline()
            oor = cap.out_of_range()
            charge = cap.charge_regs()
            if charge != last_charge:
                changed = [i for i in elec if charge[i] != last_charge[i]]
                print("[%7.3f] AUTO-RECONFIG on %s -> %s" % (
                    now - t0, changed,
                    " ".join("e%d=%d/%d" % (i, charge[i][0], charge[i][1]) for i in changed)))
                last_charge = charge
            active = any((touched >> i) & 1 or abs(base[i] - filt[i]) >= 3 for i in elec)
            if active:
                cells = []
                for i in elec:
                    cells.append("e%d %4d/%4d/%+4d%s" % (
                        i, filt[i], base[i], base[i] - filt[i],
                        "T" if (touched >> i) & 1 else " "))
                print("[%7.3f] %s%s" % (now - t0, "  ".join(cells),
                                        "  OOR=0x%03x" % oor if oor else ""), flush=True)
            time.sleep(args.interval_ms / 1000.0)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        bus.close()
    return 0


def cmd_calibrate(args):
    """Measure the idle noise per electrode and recommend thresholds.

    The chip flags a touch when (baseline - filtered) exceeds the touch
    threshold. So the question is simply: with nobody touching, how far does
    that delta wander? Thresholds must sit well clear of that.
    """
    bus, cap, ignore = open_and_begin(args)
    secs = args.seconds
    print("\nRecording %d s of idle noise. DO NOT TOUCH the pads.\n" % secs)
    # let the baseline filter settle before we start judging it
    time.sleep(1.0)

    samples = [[] for _ in range(NUM_ELECTRODES)]
    events = [0] * NUM_ELECTRODES
    last = 0
    t_end = time.time() + secs
    n = 0
    try:
        while time.time() < t_end:
            filt = cap.all_filtered()
            base = cap.all_baseline()
            curr = cap.touched()
            for i in range(NUM_ELECTRODES):
                samples[i].append(base[i] - filt[i])
                if (curr & (1 << i)) and not (last & (1 << i)):
                    events[i] += 1
            last = curr
            n += 1
            if n % 20 == 0:
                print("  %3d s left, %d samples" % (t_end - time.time(), n),
                      end="\r", flush=True)
            time.sleep(0.02)
    except KeyboardInterrupt:
        print("\ninterrupted - report is on partial data")
    finally:
        bus.close()

    print("\n%-4s %6s %6s %6s %6s %7s  %s" %
          ("ele", "min", "max", "mean", "p2p", "spurious", "verdict"))
    print("-" * 60)
    worst_noise = 0
    dead, noisy = [], []
    for i in range(NUM_ELECTRODES):
        d = samples[i]
        if not d:
            continue
        lo, hi = min(d), max(d)
        mean = sum(d) / len(d)
        p2p = hi - lo
        if i in ignore:
            verdict = "ignored"
        elif all(v == 0 for v in d):
            # delta stuck at exactly 0 the whole time = no signal at all
            verdict = "DEAD - no electrode connected"
            dead.append(i)
        elif events[i]:
            verdict = "NOISY - fired %d phantom touch(es)" % events[i]
            noisy.append(i)
            worst_noise = max(worst_noise, hi)
        else:
            verdict = "ok"
            worst_noise = max(worst_noise, hi)
        print("%-4d %6d %6d %6.1f %6d %7d  %s" % (i, lo, hi, mean, p2p, events[i], verdict))

    # Recommendation: touch clear of the worst idle excursion by a margin,
    # release at roughly half so a held touch does not chatter.
    rec_touch = max(6, int(worst_noise * 2.5) + 3)
    rec_release = max(3, rec_touch // 2)
    print()
    print("worst idle excursion on a live electrode: %d counts" % worst_noise)
    print("current thresholds: touch=%d release=%d" % (args.touch, args.release))
    print("recommended:        touch=%d release=%d debounce=%d"
          % (rec_touch, rec_release, max(args.debounce, 2)))
    if dead:
        print("dead electrodes (add to --ignore): %s" % ",".join(str(i) for i in dead))
    cmd = "%s test --touch %d --release %d --debounce %d" % (
        os.path.basename(sys.argv[0]), rec_touch, rec_release, max(args.debounce, 2))
    if dead or ignore:
        cmd += " --ignore %s" % ",".join(str(i) for i in sorted(set(dead) | ignore))
    print("\nnext:  sudo ./%s" % cmd)
    print("then touch a pad and check it still registers; if a real touch\n"
          "produces a delta near the threshold, lower --touch a little.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["check", "scan", "test", "raw", "trace", "calibrate"])
    parser.add_argument("--bus", type=int, default=None,
                        help="I2C bus number (default: auto-detect)")
    parser.add_argument("--addr", type=lambda s: int(s, 0), default=None,
                        help="I2C address (default: probe 0x5a-0x5d)")
    parser.add_argument("--touch", type=int, default=12,
                        help="touch threshold in counts (default 12; the ESP32 "
                             "sample's 2 sits inside the noise floor)")
    parser.add_argument("--release", type=int, default=6,
                        help="release threshold (default 6)")
    parser.add_argument("--debounce", type=int, default=2,
                        help="consecutive samples before a state change counts, "
                             "0-7 (default 2)")
    parser.add_argument("--sfi", type=int, default=2, choices=[0, 1, 2, 3],
                        help="samples averaged per reading: 0=4 1=6 2=10 3=18 "
                             "(default 2)")
    parser.add_argument("--esi", type=int, default=0, choices=range(0, 8),
                        help="sample interval 1ms<<esi (default 0 = 1ms)")
    parser.add_argument("--ignore", default="",
                        help="electrodes to mask out, e.g. 0,1,2")
    parser.add_argument("--seconds", type=int, default=10,
                        help="calibrate: idle recording length (default 10)")
    parser.add_argument("--verbose", action="store_true",
                        help="test: print the delta that triggered each touch")
    parser.add_argument("--no-autoconfig", action="store_true",
                        help="skip MPR121 autoconfiguration")
    parser.add_argument("--all", action="store_true",
                        help="scan: include the HDMI DDC bus (slow)")
    parser.add_argument("--reg", action="append", default=[], metavar="ADDR=VAL",
                        help="raw register override applied after the defaults, "
                             "repeatable, e.g. --reg 0x32=0x40 (FDLF)")
    parser.add_argument("--interval-ms", type=int, default=20,
                        help="trace: sample period (default 20)")
    args = parser.parse_args()
    try:
        args.overrides = [tuple(int(x, 0) for x in item.split("=", 1)) for item in args.reg]
    except ValueError:
        parser.error("--reg expects ADDR=VAL, e.g. 0x32=0x40")

    try:
        if args.command == "check":
            return cmd_check(args)
        if args.command == "scan":
            return cmd_scan(args)
        if args.command == "test":
            return cmd_test(args)
        if args.command == "trace":
            return cmd_trace(args)
        if args.command == "calibrate":
            return cmd_calibrate(args)
        return cmd_raw(args)
    except PermissionError:
        print("Permission denied on /dev/i2c-* - run with sudo.", file=sys.stderr)
        return 1
    except OSError as exc:
        print("I2C error: %s" % exc, file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print("Error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

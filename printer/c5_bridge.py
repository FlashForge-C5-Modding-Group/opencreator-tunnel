#!/usr/bin/env python3
# Creator 5 MCU tunnel: forward the printer's four MCU UARTs to an SBC over
# USB. The SBC runs a five-port "gser" serial gadget (two endpoints per
# port; five ports still fit the Pi's dwc2 controller -- tested on real
# hardware): four for the MCUs, one (interface 1.4) for the beep control
# channel (see BeepServer below). This side binds it to a built-in
# usb-serial driver and sees one ttyUSB per gadget interface.
#
# Runs on the printer SoC with its bundled Python 3.8, stdlib only.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import errno
import re
import os
import select
import signal
import stat
import subprocess
import sys
import termios
import threading
import time
import traceback

GADGET_VID, GADGET_PID = "1d6b", "0104"
PORTS = [  # name, uart, baud, needs_wake, wake parity order, gadget interface
    ("mainboardgd", "/dev/ttyS2", 230400, False, (), "1.0"),
    ("heaterboard", "/dev/ttyS4", 230400, True, ("N", "E"), "1.1"),
    ("eboard", "/dev/ttyS5", 460800, True, ("E", "N"), "1.2"),
    ("levelboard", "/dev/ttyS7", 230400, True, ("N", "E"), "1.3"),
]
# Built-in one-port bulk usb-serial drivers that accept a dynamic id
USB_SERIAL_DRIVERS = ("flashloader", "vivopay", "zio", "carelink", "funsoft")
WAKE_BANNER_TIMEOUT = 4.0   # s per parity waiting for b"Ready"
WAKE_ACK_TIMEOUT = 0.3      # s waiting for 0x06 after each b"A"
WAKE_ACK_TRIES = 3
IDENTIFY_UNANSWERED = 0.4   # s after a host identify with no valid MCU frame
WAKE_RETRY_MIN = 5.0        # s between reactive wake attempts per port
PROBE_TIMEOUT = 0.3         # s waiting for any frame after a probe identify
LINK_POLL = 0.5             # s between gadget tty scans
UART_RETRY = 5.0            # s between attempts to open a missing UART
MAX_PENDING = 65536         # bytes buffered per direction before backpressure
LOG = "/usr/data/logs/c5-tunnel.log"
SYSFS_TTY = "/sys/class/tty"
SYSFS_USB_SERIAL = "/sys/bus/usb-serial/drivers"
DEV_DIR = "/dev"
# Host PWM buzzer, forwarded over the gadget's 5th serial port
# (interface 1.4) instead of the network, so it keeps working with no
# Wi-Fi/LAN on the printer. Klipper on the SBC has no local cmd_pwm, so
# creator5_remote_beeper.py (klippy/extras) sends one line here instead
# of the no-op the SBC would otherwise need; this mirrors
# creator5_beeper.py's own PWM programming sequence for printers that
# still run Klipper on the SoC directly.
BEEP_IFACE = "1.4"
BEEP_BAUD = 115200
BEEP_COMMAND = "cmd_pwm"
BEEP_CHANNEL = "pc12"
BEEP_ACCEPT_POLL = 1.0       # s, so the listener notices `stop` promptly

######################################################################
# Logging
######################################################################

_log_lock = threading.Lock()
_log_file = None


def open_log(path=LOG):
    global _log_file
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.replace(path, path + ".1")
        _log_file = open(path, "a", buffering=1)
    except OSError as e:
        sys.stderr.write("log unavailable: %s\n" % (e,))


def log(msg):
    line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    with _log_lock:
        for f in (_log_file, sys.stderr):
            if f is None:
                continue
            try:
                f.write(line)
                f.flush()
            except (OSError, ValueError):
                pass


_kernel_logged = [0.0]


def log_kernel_usb(lines=20):
    """Record recent kernel USB messages once per link-loss event; the
    printer's kernel ring buffer is lost on power-cycle."""
    with _log_lock:
        now = time.monotonic()
        if now - _kernel_logged[0] < 5.0:
            return
        _kernel_logged[0] = now
    try:
        out = subprocess.run(["dmesg"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log("kernel: dmesg failed: %s" % (e,))
        return
    keep = [line for line in out.decode("utf-8", "replace").splitlines()
            if "RTW:" not in line][-lines:]
    log("kernel:\n" + "\n".join(keep))

######################################################################
# Klipper block framing (klippy/msgproto.py)
######################################################################

MESSAGE_MIN = 5
MESSAGE_MAX = 64
MESSAGE_DEST = 0x10
MESSAGE_SYNC = 0x7e
IDENTIFY_MSGID = 0x01


def crc16_ccitt(buf):
    crc = 0xffff
    for data in buf:
        data ^= crc & 0xff
        data ^= (data & 0x0f) << 4
        crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
    return crc & 0xffff


def build_block(seq, payload):
    head = bytes([len(payload) + MESSAGE_MIN, MESSAGE_DEST | (seq & 0x0f)])
    body = head + bytes(payload)
    crc = crc16_ccitt(body)
    return body + bytes([crc >> 8, crc & 0xff, MESSAGE_SYNC])


# identify offset=0 count=40 (bootstrap msgid 1)
IDENTIFY_BLOCK = build_block(0, [IDENTIFY_MSGID, 0x00, 0x28])


def check_packet(s):
    if len(s) < MESSAGE_MIN:
        return 0
    msglen = s[0]
    if msglen < MESSAGE_MIN or msglen > MESSAGE_MAX:
        return -1
    if (s[1] & ~0x0f) != MESSAGE_DEST:
        return -1
    if len(s) < msglen:
        return 0
    if s[msglen - 1] != MESSAGE_SYNC:
        return -1
    crc = crc16_ccitt(s[:msglen - 3])
    if s[msglen - 3] != crc >> 8 or s[msglen - 2] != crc & 0xff:
        return -1
    return msglen


class FrameDetector:
    """Observe a byte stream and return the valid Klipper blocks in it."""
    def __init__(self):
        self.buf = bytearray()

    def reset(self):
        del self.buf[:]

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            n = check_packet(self.buf)
            if n == 0:
                break
            if n < 0:
                i = self.buf.find(MESSAGE_SYNC)
                del self.buf[:len(self.buf) if i < 0 else i + 1]
                continue
            frames.append(bytes(self.buf[:n]))
            del self.buf[:n]
        return frames


def is_identify(frame):
    return len(frame) > MESSAGE_MIN and frame[2] == IDENTIFY_MSGID


class WakeMonitor:
    """Decide when an unanswered host identify warrants a boot-stage wake."""
    def __init__(self):
        self.last_host_identify = None
        self.last_mcu_frame = None
        self.last_wake_attempt = None

    def host_frame(self, frame, now):
        if is_identify(frame):
            self.last_host_identify = now

    def mcu_frame(self, now):
        self.last_mcu_frame = now

    def should_wake(self, now):
        ident = self.last_host_identify
        if ident is None or now - ident <= IDENTIFY_UNANSWERED:
            return False
        if self.last_mcu_frame is not None and self.last_mcu_frame >= ident:
            return False
        if (self.last_wake_attempt is not None
                and now - self.last_wake_attempt <= WAKE_RETRY_MIN):
            return False
        return True

    def wake_started(self, now):
        self.last_wake_attempt = now
        self.last_host_identify = None

######################################################################
# tty helpers
######################################################################

BAUDS = {115200: termios.B115200, 230400: termios.B230400,
         460800: termios.B460800}


def configure_tty(fd, baud, parity=None):
    iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
    iflag &= ~(termios.IGNBRK | termios.BRKINT | termios.PARMRK
               | termios.ISTRIP | termios.INLCR | termios.IGNCR
               | termios.ICRNL | termios.IXON | termios.IXOFF
               | termios.IXANY | termios.INPCK)
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON
               | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.PARODD
               | termios.CRTSCTS)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    if parity == "E":
        cflag |= termios.PARENB
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    speed = BAUDS[baud]
    termios.tcsetattr(fd, termios.TCSANOW,
                      [iflag, oflag, cflag, lflag, speed, speed, cc])
    termios.tcflush(fd, termios.TCIOFLUSH)


def open_tty(path, baud):
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    try:
        configure_tty(fd, baud)
    except Exception:
        os.close(fd)
        raise
    return fd


def read_ready(fd, timeout):
    """Read what is available within timeout; b"" when nothing arrived."""
    r, _, _ = select.select([fd], [], [], max(0.0, timeout))
    if not r:
        return b""
    try:
        return os.read(fd, 4096)
    except BlockingIOError:
        return b""


def write_all(fd, data, timeout=1.0):
    deadline = time.monotonic() + timeout
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
            view = view[n:]
            continue
        except BlockingIOError:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("write timeout")
        select.select([], [fd], [], remaining)


def write_some(fd, pending):
    """Write as much of pending as the fd accepts; drop what was written."""
    try:
        n = os.write(fd, pending)
    except BlockingIOError:
        return
    del pending[:n]

######################################################################
# MCU probing and boot-stage wake
######################################################################


def probe_alive(fd, timeout=PROBE_TIMEOUT):
    termios.tcflush(fd, termios.TCIFLUSH)
    # Leading sync ends any garbage-discard in the MCU parser.
    write_all(fd, bytes([MESSAGE_SYNC]) + IDENTIFY_BLOCK)
    detector = FrameDetector()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if detector.feed(read_ready(fd, remaining)):
            return True


def _wake_attempt(fd):
    """Return (got_bytes, saw_banner, acked, rx_after_a) for this line setup."""
    got_bytes = saw_banner = False
    tail = bytearray()
    after_a = bytearray()
    deadline = time.monotonic() + WAKE_BANNER_TIMEOUT
    while time.monotonic() < deadline:
        data = read_ready(fd, deadline - time.monotonic())
        if not data:
            continue
        got_bytes = True
        tail += data
        if b"Ready" not in tail:
            del tail[:-8]
            continue
        saw_banner = True
        for _ in range(WAKE_ACK_TRIES):
            write_all(fd, b"A")
            ack_deadline = time.monotonic() + WAKE_ACK_TIMEOUT
            while time.monotonic() < ack_deadline:
                data = read_ready(fd, ack_deadline - time.monotonic())
                after_a += data[:32 - len(after_a)]
                if 0x06 in data:
                    return got_bytes, saw_banner, True, bytes(after_a)
        return got_bytes, saw_banner, False, bytes(after_a)
    return got_bytes, saw_banner, False, bytes(after_a)


def wake(fd, baud, parity_order):
    """Release a board from its resident boot stage.

    Returns (True, detail) once the board acknowledged or, with the ack
    lost, answers an identify at its Klipper baud; else (False, detail).
    The fd is always left at baud 8N1 with flushed queues.
    """
    saw_banner = False
    rx = []
    try:
        for parity in parity_order:
            configure_tty(fd, 115200, parity)
            got_bytes, banner, acked, after_a = _wake_attempt(fd)
            if acked:
                return True, "parity %s" % (parity,)
            if not got_bytes:
                break
            saw_banner = saw_banner or banner
            if banner:
                configure_tty(fd, baud)
                if probe_alive(fd):
                    return True, "parity %s, ack lost" % (parity,)
                rx.append("%s: %s" % (parity, after_a.hex() or "-"))
    finally:
        configure_tty(fd, baud)
    if not saw_banner:
        return False, "no banner"
    return False, "no ack (after A %s)" % ("; ".join(rx),)

######################################################################
# Gadget link discovery
######################################################################


def _read_text(path):
    with open(path) as f:
        return f.read().strip()


INTERFACE_DIR = re.compile(r"^\d+-[\d.]+:(\d+\.\d+)$")


def bind_usb_serial(sysfs=SYSFS_USB_SERIAL):
    """Give the gadget id to a usb-serial driver; return the driver name.

    gser interfaces are vendor-specific, so no driver claims them until
    one is given their id; usb-serial then attaches every interface.
    """
    gadget_id = "%s %s" % (GADGET_VID, GADGET_PID)
    for name in USB_SERIAL_DRIVERS:
        new_id = os.path.join(sysfs, name, "new_id")
        if not os.path.exists(new_id):
            continue
        try:
            with open(new_id) as f:
                if any(l.split()[:2] == gadget_id.split() for l in f):
                    return name
            with open(new_id, "w") as f:
                f.write(gadget_id + " ff\n")
            return name
        except OSError as e:
            log("usb-serial: %s new_id failed: %s" % (name, e))
    return None


def find_link(iface, sysfs=SYSFS_TTY, devdir=DEV_DIR):
    """Return the /dev path of the gadget ttyUSB on interface iface."""
    try:
        names = sorted(n for n in os.listdir(sysfs) if n.startswith("ttyUSB"))
    except OSError:
        return None
    for name in names:
        try:
            # device -> .../<bus>-<port>:<cfg>.<iface>/ttyUSBn
            path = os.path.realpath(os.path.join(sysfs, name, "device"))
            while path not in ("/", ""):
                match = INTERFACE_DIR.match(os.path.basename(path))
                if match:
                    break
                path = os.path.dirname(path)
            else:
                continue
            if match.group(1) != iface:
                continue
            usbdev = os.path.dirname(path)
            if (_read_text(os.path.join(usbdev, "idVendor")) != GADGET_VID
                    or _read_text(os.path.join(usbdev, "idProduct"))
                    != GADGET_PID):
                continue
            node = os.path.join(devdir, name)
            if not os.path.exists(node):
                major, minor = _read_text(
                    os.path.join(sysfs, name, "dev")).split(":")
                os.mknod(node, stat.S_IFCHR | 0o600,
                         os.makedev(int(major), int(minor)))
            return node
        except (OSError, ValueError):
            continue
    return None

######################################################################
# Per-port bridge
######################################################################


class LinkDown(Exception):
    pass


class PortBridge(threading.Thread):
    def __init__(self, stop, name, uart, baud, needs_wake, parity_order,
                 iface):
        threading.Thread.__init__(self, name=name, daemon=True)
        self.stop = stop
        self.port = name
        self.uart_path = uart
        self.baud = baud
        self.needs_wake = needs_wake
        self.parity_order = parity_order
        self.iface = iface
        self.uart = None
        self.probed = False
        self.monitor = WakeMonitor()

    def log(self, msg):
        log("%s: %s" % (self.port, msg))

    def run(self):
        while not self.stop.is_set():
            try:
                if self.uart is None:
                    self.open_uart()
                    continue
                if not self.probed:
                    self.initial_probe()
                    self.probed = True
                link = self.wait_link()
                if link is not None:
                    self.forward(link)
            except Exception:
                self.log("error\n" + traceback.format_exc().rstrip())
                self.stop.wait(2.0)

    def open_uart(self):
        try:
            self.uart = open_tty(self.uart_path, self.baud)
        except OSError as e:
            self.log("uart %s unavailable: %s" % (self.uart_path, e))
            self.stop.wait(UART_RETRY)
            return
        self.log("uart open %s %d" % (self.uart_path, self.baud))

    def initial_probe(self):
        if probe_alive(self.uart):
            self.log("alive")
            return
        self.log("not alive")
        if not self.needs_wake:
            return
        self.run_wake()
        self.log("alive" if probe_alive(self.uart) else "not alive")

    def run_wake(self):
        ok, detail = wake(self.uart, self.baud, self.parity_order)
        if ok:
            self.log("wake ok (%s)" % (detail,))
        else:
            self.log("wake failed: %s" % (detail,))

    def wait_link(self):
        """Poll for the gadget tty, discarding UART bytes meanwhile."""
        self.log("waiting for gadget")
        while not self.stop.is_set():
            path = find_link(self.iface)
            if path is not None:
                try:
                    fd = open_tty(path, 115200)
                except OSError as e:
                    self.log("open %s failed: %s" % (path, e))
                else:
                    self.log("link up %s (%s)" % (path, self.iface))
                    return fd
            deadline = time.monotonic() + LINK_POLL
            while time.monotonic() < deadline:
                read_ready(self.uart, deadline - time.monotonic())
        return None

    def forward(self, link):
        try:
            self._forward(link)
        except LinkDown:
            self.log("link down")
            log_kernel_usb()
        finally:
            os.close(link)

    def _read_link(self, link):
        try:
            data = os.read(link, 4096)
        except BlockingIOError:
            return None
        except OSError as e:
            raise LinkDown(e)
        if not data:
            raise LinkDown("eof")
        return data

    def _drain_link(self, link):
        while read_ready(link, 0.0):
            pass

    def _forward(self, link):
        uart = self.uart
        to_uart = bytearray()
        to_link = bytearray()
        host_frames = FrameDetector()
        mcu_frames = FrameDetector()
        monitor = self.monitor
        while not self.stop.is_set():
            rlist = []
            if len(to_link) < MAX_PENDING:
                rlist.append(uart)
            if len(to_uart) < MAX_PENDING:
                rlist.append(link)
            wlist = []
            if to_uart:
                wlist.append(uart)
            if to_link:
                wlist.append(link)
            r, w, _ = select.select(rlist, wlist, [], 0.1)
            now = time.monotonic()
            if link in r:
                data = self._read_link(link)
                if data:
                    to_uart += data
                    for frame in host_frames.feed(data):
                        monitor.host_frame(frame, now)
            if uart in r:
                try:
                    data = os.read(uart, 4096)
                except BlockingIOError:
                    data = b""
                if data:
                    to_link += data
                    if mcu_frames.feed(data):
                        monitor.mcu_frame(now)
            if to_uart:
                write_some(uart, to_uart)
            if to_link:
                try:
                    write_some(link, to_link)
                except OSError as e:
                    raise LinkDown(e)
            if self.needs_wake and monitor.should_wake(now):
                self.log("identify unanswered")
                monitor.wake_started(now)
                del to_uart[:]
                self.run_wake()
                self._drain_link(link)
                host_frames.reset()
                mcu_frames.reset()

######################################################################
# Main
######################################################################


######################################################################
# Host PWM buzzer (remote C5_BUZZER)
######################################################################

def _run_pwm(*args, tolerate_already_working=False):
    try:
        result = subprocess.run([BEEP_COMMAND] + list(args),
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                check=False, timeout=3)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("%s failed: %s" % (BEEP_COMMAND, exc))
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        # The kernel PWM driver rejects config/set_level/set_prescale
        # with this exact error once the channel is already in its
        # "working" state ("pwm ch N Cannot configure at working" in
        # dmesg) -- including right after *this process* restarts,
        # since that state lives in the kernel, not here. There is no
        # way to query or release it (disable_channels does not), so
        # tolerate it on those three calls: it means the one-time setup
        # already happened, not that something is actually wrong.
        if tolerate_already_working and "operation not permitted" in detail.lower():
            return
        raise RuntimeError("%s failed (%d): %s"
                           % (BEEP_COMMAND, result.returncode, detail))


_pwm_configured = set()


def play_tone(duration_ms, frequency, level, channel=BEEP_CHANNEL):
    # Same waveform math and sequencing as creator5_beeper.py's
    # _play_tone, just run synchronously in this thread instead of via
    # Klipper's reactor/aio_executor.
    # set_level/set_prescale are only ever called here with fixed
    # constants (100, 6), never derived from the tone's own parameters,
    # so they are one-time setup alongside config; only set_wc (the
    # actual waveform) and enable/disable_channels vary per beep. Call
    # them at most once per channel per process lifetime, not on every
    # beep: the kernel soc_pwm driver serializes config/set_level/
    # set_prescale/enable/disable on one internal mutex, and real
    # hardware testing showed that calling config repeatedly (even
    # just to hit its expected "already configured" failure) risks a
    # cmd_pwm process getting stuck forever in kernel D state waiting
    # on that mutex ("task cmd_pwm ... blocked for more than N
    # seconds" in dmesg) -- a genuine kernel deadlock only a reboot
    # clears, not something retryable in userspace.
    period = max(2, int(round(50000000. / frequency)))
    high = period * level // 200
    if channel not in _pwm_configured:
        _run_pwm("config", channel, "freq=50000000", "max_level=300",
                 "active_level=1", "accuracy_priority=freq",
                 tolerate_already_working=True)
        _run_pwm("set_level", channel, "100", tolerate_already_working=True)
        _run_pwm("set_prescale", channel, "6", tolerate_already_working=True)
        _pwm_configured.add(channel)
    try:
        _run_pwm("set_wc", channel, str(period), str(high))
        _run_pwm("enable_channels", channel)
        time.sleep(duration_ms / 1000.)
    finally:
        try:
            _run_pwm("set_wc", channel, "1", "0")
        finally:
            _run_pwm("disable_channels", channel)


class BeepServer(threading.Thread):
    # One line in, one line out, over the gadget's 5th serial port
    # (ttyUSB4 here / ttyGS4 on the SBC, USB interface 1.4) instead of
    # the network, so C5_BUZZER works even with no Wi-Fi/LAN on the
    # printer: "BEEP DURATION=5000 FREQUENCY=2500 LEVEL=100" -> "OK" or
    # "ERROR <message>". Needs a usb-serial-bound gadget (see
    # bind_usb_serial); on a kernel with no usb-serial driver this
    # link just never comes up, same as everything else over the
    # tunnel being unreachable without Wi-Fi today -- not a new
    # failure mode.
    def __init__(self, stop):
        threading.Thread.__init__(self, name="beep", daemon=True)
        self.stop = stop
        self.fd = None

    def run(self):
        buf = b""
        while not self.stop.is_set():
            if self.fd is None:
                node = find_link(BEEP_IFACE)
                if node is None:
                    self.stop.wait(BEEP_ACCEPT_POLL)
                    continue
                try:
                    self.fd = open_tty(node, BEEP_BAUD)
                    log("beep: link up %s" % (node,))
                except OSError as exc:
                    log("beep: cannot open %s: %s" % (node, exc))
                    self.stop.wait(BEEP_ACCEPT_POLL)
                    continue
                buf = b""
            try:
                chunk = read_ready(self.fd, BEEP_ACCEPT_POLL)
            except OSError as exc:
                log("beep: link error, reopening: %s" % (exc,))
                os.close(self.fd)
                self.fd = None
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    reply = self._handle(line)
                except Exception:
                    log("beep: handler error:\n" + traceback.format_exc())
                    reply = b"ERROR internal error\n"
                try:
                    write_all(self.fd, reply)
                except (OSError, TimeoutError) as exc:
                    log("beep: reply write failed: %s" % (exc,))
        if self.fd is not None:
            os.close(self.fd)

    def _handle(self, line):
        text = line.decode("utf-8", "replace").strip()
        parts = text.split()
        if not parts or parts[0] != "BEEP":
            return b"ERROR unknown command\n"
        params = {}
        for part in parts[1:]:
            if "=" in part:
                key, _, value = part.partition("=")
                params[key.upper()] = value
        try:
            duration_ms = int(params.get("DURATION", "5000"))
            frequency = int(params.get("FREQUENCY", "2500"))
            level = int(params.get("LEVEL", "100"))
        except ValueError:
            return b"ERROR invalid parameter\n"
        try:
            play_tone(duration_ms, frequency, level)
        except RuntimeError as exc:
            return ("ERROR %s\n" % (exc,)).encode("utf-8", "replace")
        return b"OK\n"


def set_priority():
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
        log("sched: SCHED_FIFO 50")
    except (AttributeError, OSError):
        try:
            os.nice(-20)
        except OSError:
            pass
        log("sched: nice -20")


def main():
    open_log()
    log("c5-tunnel bridge starting (pid %d)" % (os.getpid(),))
    # Threads created after this call inherit the scheduling policy.
    set_priority()
    driver = bind_usb_serial()
    if driver is None:
        log("usb-serial: no driver accepted the gadget id")
    else:
        log("usb-serial: gadget bound to %s" % (driver,))
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    bridges = [PortBridge(stop, *p) for p in PORTS]
    beep_server = BeepServer(stop)
    for b in bridges:
        b.start()
    beep_server.start()
    while not stop.wait(1.0):
        pass
    for b in bridges:
        b.join(5.0)
    beep_server.join(5.0)
    log("c5-tunnel bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

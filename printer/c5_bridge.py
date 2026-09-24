#!/usr/bin/env python3
# Creator 5 MCU tunnel: forward the printer's four MCU UARTs to an SBC over
# USB. The SBC runs a four-port "gser" serial gadget (two endpoints per port,
# so four ports fit the Pi's dwc2 controller); this side binds it to a
# built-in usb-serial driver and sees one ttyUSB per gadget interface.
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
    ("levelboard", "/dev/ttyS7", 230400, True, ("E", "N"), "1.3"),
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

CAMERA_DIR = "/usr/prog/mjpg-streamer"
CAMERA_DEVICE = "/dev/video0"
CAMERA_LOG = "/usr/data/logs/c5-camera.log"
CAMERA_ARGS = ["./mjpg_streamer",
               "-i", "input_uvc.so -d /dev/video0 -r 1280x720 -f 30",
               "-o", "output_http.so -p 8080 -w www"]

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
    write_all(fd, IDENTIFY_BLOCK)
    detector = FrameDetector()
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if detector.feed(read_ready(fd, remaining)):
            return True


def _wake_attempt(fd):
    """Return (got_bytes, saw_banner, acked) for the current line setup."""
    got_bytes = saw_banner = False
    tail = bytearray()
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
                if 0x06 in read_ready(fd, ack_deadline - time.monotonic()):
                    return got_bytes, saw_banner, True
        return got_bytes, saw_banner, False
    return got_bytes, saw_banner, False


def wake(fd, baud, parity_order):
    """Release a board from its resident boot stage.

    Returns (True, parity) on ACK, else (False, "no banner"|"no ack").
    The fd is always left at baud 8N1 with flushed queues.
    """
    saw_banner = False
    try:
        for parity in parity_order:
            configure_tty(fd, 115200, parity)
            got_bytes, banner, acked = _wake_attempt(fd)
            if acked:
                return True, parity
            saw_banner = saw_banner or banner
            if not got_bytes:
                break
    finally:
        configure_tty(fd, baud)
    return False, "no ack" if saw_banner else "no banner"

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
            self.log("wake ok (parity %s)" % (detail,))
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
# Camera
######################################################################


def _camera_preexec():
    # Do not let the camera inherit the bridge's real-time priority.
    try:
        os.sched_setscheduler(0, os.SCHED_OTHER, os.sched_param(0))
    except (AttributeError, OSError):
        pass


def run_camera(stop):
    binary = os.path.join(CAMERA_DIR, "mjpg_streamer")
    if not os.path.exists(binary):
        log("camera missing: %s" % (binary,))
        return
    deadline = time.monotonic() + 30.0
    while not os.path.exists(CAMERA_DEVICE) and time.monotonic() < deadline:
        if stop.wait(1.0):
            return
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = ":".join(
        p for p in (CAMERA_DIR, env.get("LD_LIBRARY_PATH")) if p)
    while not stop.is_set():
        try:
            with open(CAMERA_LOG, "ab") as out:
                child = subprocess.Popen(CAMERA_ARGS, cwd=CAMERA_DIR, env=env,
                                         stdout=out, stderr=out,
                                         preexec_fn=_camera_preexec)
        except OSError as e:
            log("camera missing: %s" % (e,))
            return
        log("camera started (pid %d)" % (child.pid,))
        while child.poll() is None:
            if stop.wait(1.0):
                child.terminate()
                try:
                    child.wait(5.0)
                except subprocess.TimeoutExpired:
                    child.kill()
                return
        log("camera exited (%d)" % (child.returncode,))
        stop.wait(3.0)

######################################################################
# Main
######################################################################


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
    for b in bridges:
        b.start()
    run_camera(stop)
    while not stop.wait(1.0):
        pass
    for b in bridges:
        b.join(5.0)
    log("c5-tunnel bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# Creator 5 MCU tunnel: forward the printer's four MCU UARTs to an SBC over
# USB. The SBC runs a four-port "gser" serial gadget (two bulk endpoints per
# port). This printer's kernel has no USB-serial/CDC-ACM support at all (no
# cdc_acm, no usb-serial core -- CONFIG_USB_SERIAL was never enabled), so no
# /dev/ttyUSB* ever appears for it. Instead this talks to the gadget's bulk
# endpoints directly through usbfs (/dev/bus/usb/BBB/DDD) via raw ioctls
# (USBDEVFS_CLAIMINTERFACE / USBDEVFS_BULK), bypassing the missing driver
# entirely. The exact ioctl numbers and struct layout below were obtained by
# compiling a tiny probe against this printer's own kernel headers
# (/opt/include/linux/usbdevice_fs.h via Entware's gcc) rather than hand
# computed, since MIPS encodes ioctl numbers differently from x86/ARM.
#
# This is a direct, wired USB link only -- no network/Wi-Fi hop is involved
# anywhere in this path.
#
# Runs on the printer SoC with its bundled Python 3.8, stdlib only.
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import ctypes
import errno
import fcntl
import glob
import os
import queue
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback

GADGET_VID, GADGET_PID = "1d6b", "0104"
PORTS = [  # name, uart, baud, needs_wake, wake parity order, gadget interface#
    ("mainboardgd", "/dev/ttyS2", 230400, False, (), 0),
    ("heaterboard", "/dev/ttyS4", 230400, True, ("N", "E"), 1),
    ("eboard", "/dev/ttyS5", 460800, True, ("E", "N"), 2),
    ("levelboard", "/dev/ttyS7", 230400, True, ("N", "E"), 3),
]
# gadget interface# -> (bulk-out endpoint, bulk-in endpoint), from this
# printer's own /sys/bus/usb/devices/<bus>-<port>:1.<iface>/ep_*/bEndpointAddress
IFACE_ENDPOINTS = {0: (0x01, 0x81), 1: (0x02, 0x82),
                   2: (0x03, 0x83), 3: (0x04, 0x84)}
WAKE_BANNER_TIMEOUT = 4.0   # s per parity waiting for b"Ready"
WAKE_ACK_TIMEOUT = 0.3      # s waiting for 0x06 after each b"A"
WAKE_ACK_TRIES = 3
IDENTIFY_UNANSWERED = 0.4   # s after a host identify with no valid MCU frame
WAKE_RETRY_MIN = 5.0        # s between reactive wake attempts per port
PROBE_TIMEOUT = 0.3         # s waiting for any frame after a probe identify
LINK_POLL = 0.5             # s between gadget device scans
UART_RETRY = 5.0            # s between attempts to open a missing UART
MAX_PENDING = 65536         # bytes buffered per direction before backpressure
LOG = "/usr/data/logs/c5-tunnel.log"
SYSFS_USB_DEVICES = "/sys/bus/usb/devices"
USB_BULK_TIMEOUT_MS = 20    # per usbfs bulk-transfer ioctl

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
# Raw USB gadget link (usbfs, no kernel usb-serial driver needed)
######################################################################

# From compiling against this printer's own kernel headers
# (/opt/include/linux/usbdevice_fs.h); MIPS encodes ioctl numbers
# differently from x86/ARM, so these are taken from the running kernel's
# headers rather than hand computed.
USBDEVFS_CLAIMINTERFACE = 1074025743 & 0xffffffff
USBDEVFS_RELEASEINTERFACE = 1074025744 & 0xffffffff
USBDEVFS_BULK = (-1072671486) & 0xffffffff
USBDEVFS_RESET = 536892692 & 0xffffffff
USBDEVFS_CLEAR_HALT = 1074025749 & 0xffffffff
# Only these mean the physical device is actually gone (unplugged,
# descriptor read failed, etc.) -- worth tearing the shared device handle
# down for. Everything else (EBUSY from a transient claim race right after
# a reopen, EPIPE from one endpoint stalling, EBADF from a benign close
# race with another port's thread) is recoverable and should just be
# retried; treating them as fatal previously caused one port's transient
# error to cascade into tearing down and re-claiming all four interfaces
# at once (visible as all ports cycling link up/down together in the log),
# which is exactly the kind of multi-second gap that trips Klipper's
# homing/probe communication timeout.
FATAL_USB_ERRNOS = (errno.ENODEV, errno.ENOENT, errno.ESHUTDOWN)
RETRY_BACKOFF = 0.05  # s, pause before retrying a recoverable USB error
# struct usbdevfs_bulktransfer { unsigned ep, len, timeout; void *data; }
# all four fields are 32-bit on this platform (confirmed via sizeof/offsetof
# probe): ep@0 len@4 timeout@8 data@12, size 16.
BULKTRANSFER_FMT = "<IIII"


def _read_text(path):
    with open(path) as f:
        return f.read().strip()


def find_gadget_device():
    """Return /dev/bus/usb/BBB/DDD for the gadget, or None if not present.

    Only matches device-level sysfs entries (no ':' in the name); the
    busnum/devnum change on every reconnect, so this is re-resolved each
    time a link is needed rather than cached."""
    for path in glob.glob(os.path.join(SYSFS_USB_DEVICES, "*")):
        name = os.path.basename(path)
        if ":" in name:
            continue
        try:
            if (_read_text(os.path.join(path, "idVendor")) != GADGET_VID
                    or _read_text(os.path.join(path, "idProduct"))
                    != GADGET_PID):
                continue
            bus = int(_read_text(os.path.join(path, "busnum")))
            dev = int(_read_text(os.path.join(path, "devnum")))
        except (OSError, ValueError):
            continue
        node = "/dev/bus/usb/%03d/%03d" % (bus, dev)
        if os.path.exists(node):
            return node
    return None


class LinkDown(Exception):
    pass


URB_READER_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "urb_reader.c")
URB_READER_BIN = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "urb_reader")


def ensure_urb_reader_binary():
    """Compile urb_reader.c with Entware's gcc if the binary isn't already
    there. See urb_reader.c's own header comment for why the async read
    path lives there instead of in this file: a side-by-side C vs Python
    ctypes/fcntl.ioctl test against the same device showed the C version
    correctly round-tripping a submitted/reaped URB address and real data,
    while the Python version did not, for reasons not pinned down despite
    several attempts -- rather than keep fighting that layer, this one
    small, focused piece is C, reached over a pipe from the rest of this
    (otherwise unchanged, already-working) Python bridge."""
    if os.path.exists(URB_READER_BIN):
        return True
    gcc = "/opt/bin/gcc"
    if not os.path.exists(gcc):
        log("usb: urb_reader.c present but no gcc at %s to build it" % gcc)
        return False
    env = dict(os.environ)
    env["PATH"] = "/opt/bin:" + env.get("PATH", "")
    try:
        subprocess.run([gcc, "-O2", "-I", "/opt/include", "-o",
                        URB_READER_BIN, URB_READER_SRC],
                      env=env, check=True,
                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except (OSError, subprocess.CalledProcessError) as e:
        out = getattr(e, "output", b"")
        log("usb: failed to build urb_reader: %s %s"
           % (e, out.decode(errors="replace") if out else ""))
        return False
    log("usb: built urb_reader")
    return True


def _read_exact(f, n):
    """Read exactly n bytes from a file object, or None on EOF/short read."""
    if n == 0:
        return b""
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


class UsbGadgetDevice:
    """One claimed usbfs handle shared by all four PortBridge links.

    Opened lazily and shared because claiming an interface requires an
    open file descriptor on the *device* node, and all four gadget
    interfaces live on the same physical device."""
    def __init__(self):
        self._lock = threading.Lock()
        self.fd = None
        self.node = None
        self.claimed = set()
        self.links = {}  # iface -> UsbSerialLink, for urb_reader dispatch
        self._reader_proc = None
        self._reader_thread = None

    def get_cached(self):
        """Return (fd, node) without touching sysfs, or (None, None) if not
        currently open. This is the hot path used by every read/write --
        scanning sysfs on every single bulk transfer added enough latency
        to trip Klipper's "Timer too close" check during MCU config."""
        with self._lock:
            return self.fd, self.node

    def get(self):
        """Return (fd, node), (re)opening and re-claiming all interfaces
        if the device has disappeared or changed since last time. Scans
        sysfs, so only call this after get_cached() comes back empty (not
        yet opened, or invalidated after an I/O error)."""
        with self._lock:
            node = find_gadget_device()
            if node is None:
                self._close_locked()
                return None, None
            if self.fd is not None and node == self.node:
                return self.fd, self.node
            self._close_locked()
            try:
                fd = os.open(node, os.O_RDWR)
            except OSError as e:
                log("usb: open %s failed: %s" % (node, e))
                return None, None
            self.fd = fd
            self.node = node
            return fd, node

    def claim(self, iface):
        with self._lock:
            if self.fd is None or iface in self.claimed:
                return self.fd is not None
            try:
                fcntl.ioctl(self.fd, USBDEVFS_CLAIMINTERFACE,
                           struct.pack("<I", iface))
            except OSError as e:
                log("usb: claim interface %d failed: %s" % (iface, e))
                return False
            self.claimed.add(iface)
            return True

    def register_link(self, iface, link):
        """Called once by each UsbSerialLink as it's constructed. Starts
        the shared urb_reader subprocess once every interface has both
        been claimed and has a link registered for it (whichever
        PortBridge is last to connect triggers the actual launch; the
        others just sit with an empty inbound queue until it's up,
        exactly as if their own read simply hadn't produced data yet)."""
        with self._lock:
            self.links[iface] = link
            self._maybe_start_reader_locked()

    def unregister_link(self, iface, link):
        with self._lock:
            if self.links.get(iface) is link:
                del self.links[iface]

    def _maybe_start_reader_locked(self):
        if (self._reader_proc is not None or self.fd is None
                or not all(i in self.claimed for i in IFACE_ENDPOINTS)
                or not all(i in self.links for i in IFACE_ENDPOINTS)):
            return
        if not ensure_urb_reader_binary():
            return
        args = [URB_READER_BIN, str(self.fd)]
        for iface in sorted(IFACE_ENDPOINTS):
            _, in_ep = IFACE_ENDPOINTS[iface]
            args += [str(iface), str(in_ep)]
        try:
            proc = subprocess.Popen(
                args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                pass_fds=(self.fd,))
        except OSError as e:
            log("usb: failed to start urb_reader: %s" % e)
            return
        self._reader_proc = proc
        self._reader_thread = threading.Thread(
            target=self._reader_loop, args=(proc,), daemon=True)
        self._reader_thread.start()
        log("usb: urb_reader started (pid %d)" % proc.pid)

    def _reader_loop(self, proc):
        """Runs on its own thread for the lifetime of one urb_reader
        subprocess: parses its framed stdout and dispatches each
        completed read to the matching UsbSerialLink's inbound queue. See
        urb_reader.c for the frame format."""
        stdout = proc.stdout
        try:
            while True:
                header = _read_exact(stdout, 5)
                if header is None:
                    break
                iface = header[0]
                (length,) = struct.unpack("<I", header[1:])
                if length == 0xFFFFFFFF:
                    link = self.links.get(iface)
                    if link is not None:
                        link._mark_down()
                    continue
                data = _read_exact(stdout, length) if length else b""
                if data is None:
                    break
                link = self.links.get(iface)
                if link is not None and data:
                    link.inbound.put(data)
        finally:
            with self._lock:
                if self._reader_proc is proc:
                    self._reader_proc = None
            # The helper exiting (for any reason -- device gone, killed on
            # a reconnect, crashed) means every port it was serving is
            # down; each PortBridge's own reconnect loop handles the rest.
            for link in list(self.links.values()):
                link._mark_down()

    def _close_locked(self):
        if self._reader_proc is not None:
            proc = self._reader_proc
            self._reader_proc = None
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                proc.terminate()
            except OSError:
                pass
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.node = None
        self.claimed.clear()
        self.links.clear()

    def invalidate(self):
        with self._lock:
            self._close_locked()

    def heartbeat(self):
        """Heartbeat signal, checks if it's died due to unforseen issues, and automatically kills if so."""
        with self._lock:
            if self.fd is None:
                return  # nothing open yet; wait_link() handles this
            stuck = (self._reader_proc is None
                     or self._reader_proc.poll() is not None)
            if not stuck:
                return
            log("usb: heartbeat found urb_reader missing/dead with the"
               " device still open -- forcing a full reconnect")
            self._close_locked()


GADGET_DEVICE = UsbGadgetDevice()


def usb_bulk_transfer(fd, ep, buf, timeout_ms=USB_BULK_TIMEOUT_MS):
    """One USBDEVFS_BULK ioctl. buf is a ctypes buffer (in or out); returns
    the actual transfer length, or raises OSError (including ETIMEDOUT,
    which is the normal "nothing arrived" case for IN endpoints)."""
    # fcntl.ioctl() only returns the ioctl()'s integer return value (the
    # actual transfer length here) when given a *mutable* buffer; with
    # immutable bytes it instead hands back the (unchanged) buffer content.
    req = bytearray(struct.pack(BULKTRANSFER_FMT, ep, len(buf), timeout_ms,
                                ctypes.addressof(buf)))
    return fcntl.ioctl(fd, USBDEVFS_BULK, req)


class UsbSerialLink:
    """Gadget-side link for one PortBridge.

    Reads are handled by the shared urb_reader subprocess (see
    UsbGadgetDevice.register_link/_reader_loop and urb_reader.c) -- async
    URBs kept outstanding per endpoint instead of each port continuously
    re-submitting a new blocking read the instant the last one times out,
    which was proven (via A/B/C testing) to starve the camera's USB
    traffic down to ~2fps regardless of the camera's own settings, simply
    by keeping the bus constantly busy with bulk-IN polling even at
    complete idle. write() is unchanged from the original synchronous
    USBDEVFS_BULK approach -- it only runs when there's actually data to
    send, so it was never part of that problem.
    """
    IN_SIZE = 4096

    def __init__(self, iface):
        self.iface = iface
        self.out_ep, self.in_ep = IFACE_ENDPOINTS[iface]
        self.inbound = queue.Queue()
        self._stop = threading.Event()
        self._down = threading.Event()
        GADGET_DEVICE.register_link(iface, self)

    def _mark_down(self):
        self._down.set()

    def down(self):
        return self._down.is_set()

    def read_nowait(self):
        try:
            return self.inbound.get_nowait()
        except queue.Empty:
            return None

    MAX_WRITE_RETRIES = 40  # ~2s of RETRY_BACKOFF before giving up

    def write(self, data):
        view = memoryview(data)
        retries = 0
        while view:
            if self._stop.is_set():
                raise LinkDown("stopping")
            fd, _ = GADGET_DEVICE.get_cached()
            if fd is None:
                raise LinkDown("device gone")
            chunk = bytes(view[:self.IN_SIZE])
            buf = ctypes.create_string_buffer(chunk, len(chunk))
            try:
                n = usb_bulk_transfer(fd, self.out_ep, buf, timeout_ms=1000)
            except OSError as e:
                if e.errno in FATAL_USB_ERRNOS:
                    GADGET_DEVICE.invalidate()
                    raise LinkDown(e)
                if e.errno == errno.EPIPE:
                    try:
                        fcntl.ioctl(fd, USBDEVFS_CLEAR_HALT,
                                   struct.pack("<I", self.out_ep))
                    except OSError:
                        pass
                    continue
                # EBUSY/EBADF/etc: transient, retry rather than tearing
                # the shared device down (see _read_loop for why) -- but
                # only up to a point, so a genuinely stuck device doesn't
                # hang this thread forever.
                retries += 1
                if retries > self.MAX_WRITE_RETRIES:
                    GADGET_DEVICE.invalidate()
                    raise LinkDown(e)
                self._stop.wait(RETRY_BACKOFF)
                continue
            if n <= 0:
                continue
            retries = 0
            view = view[n:]

    def close(self):
        self._stop.set()
        GADGET_DEVICE.unregister_link(self.iface, self)

######################################################################
# Per-port bridge
######################################################################


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
        """Poll for the gadget device, discarding UART bytes meanwhile."""
        self.log("waiting for gadget")
        while not self.stop.is_set():
            fd, node = GADGET_DEVICE.get()
            if fd is not None and GADGET_DEVICE.claim(self.iface):
                link = UsbSerialLink(self.iface)
                self.log("link up %s iface %d" % (node, self.iface))
                return link
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
            link.close()

    def _forward(self, link):
        uart = self.uart
        to_uart = bytearray()
        to_link = bytearray()
        host_frames = FrameDetector()
        mcu_frames = FrameDetector()
        monitor = self.monitor
        while not self.stop.is_set():
            if link.down():
                raise LinkDown("read thread stopped")
            # usbfs bulk I/O has no select()-able fd; poll the reader
            # thread's queue instead of blocking on it.
            data = link.read_nowait()
            if data:
                now = time.monotonic()
                to_uart += data
                for frame in host_frames.feed(data):
                    monitor.host_frame(frame, now)

            rlist = [uart] if len(to_link) < MAX_PENDING else []
            wlist = [uart] if to_uart else []
            r, w, _ = select.select(rlist, wlist, [], 0.005)
            now = time.monotonic()
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
                link.write(bytes(to_link))
                del to_link[:]
            if self.needs_wake and monitor.should_wake(now):
                self.log("identify unanswered")
                monitor.wake_started(now)
                del to_uart[:]
                self.run_wake()
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


HEARTBEAT_INTERVAL_S = 60


def heartbeat_loop(stop):
    while not stop.wait(HEARTBEAT_INTERVAL_S):
        GADGET_DEVICE.heartbeat()


def main():
    open_log()
    log("c5-tunnel bridge starting (pid %d)" % (os.getpid(),))
    # Threads created after this call inherit the scheduling policy.
    set_priority()
    stop = threading.Event()

    def on_signal(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    bridges = [PortBridge(stop, *p) for p in PORTS]
    for b in bridges:
        b.start()
    threading.Thread(target=heartbeat_loop, args=(stop,), daemon=True).start()
    # Camera disabled for now (believed broken; also conflicts with
    # whatever's already serving /dev/video0 while install.sh hasn't been
    # run yet). Re-enable by restoring the run_camera(stop) call below.
    while not stop.wait(1.0):
        pass
    for b in bridges:
        b.join(5.0)
    log("c5-tunnel bridge stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

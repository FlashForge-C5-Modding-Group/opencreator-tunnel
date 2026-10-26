#!/usr/bin/env python3
"""Low-idle-CPU supervisor for four socat MCU tunnels.

Only socat owns live serial devices. Its binary trace FIFOs let a small C
process spot unanswered Klipper identify packets; the supervisor stops socat
before changing UART parity or sending the stock boot-stage wake sequence.
GPL-3.0-or-later.
"""
import ctypes
import os
import errno
import re
import select
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time
import traceback

PORTS = (
    ("mainboardgd", "/dev/ttyS2", 230400, (), "1.0"),
    ("heaterboard", "/dev/ttyS4", 230400, ("N", "E"), "1.1"),
    ("eboard", "/dev/ttyS5", 460800, ("E", "N"), "1.2"),
    ("levelboard", "/dev/ttyS7", 230400, ("N", "E"), "1.3"),
)
DRIVERS = ("flashloader", "vivopay", "zio", "carelink", "funsoft")
BAUDS = {115200: termios.B115200, 230400: termios.B230400,
         460800: termios.B460800}
LOG_PATH = "/usr/data/logs/c5-socat.log"
MONITOR = os.path.join(os.path.dirname(__file__), "identify_monitor")
SOCAT = ("/opt/bin/socat" if os.path.isfile("/opt/bin/socat")
         else "/usr/bin/socat")
STOP = threading.Event()
LOG_LOCK = threading.Lock()
LOG_FILE = None
INTERFACE_DIR = re.compile(r"^\d+-[\d.]+:(\d+\.\d+)$")
# Host PWM buzzer, forwarded over the gadget's 5th serial port (USB
# interface 1.4, ttyUSB4 here / ttyGS4 on the SBC) instead of the
# network, so it keeps working with no Wi-Fi/LAN on the printer. See
# ../printer/c5_bridge.py's run_beep_server for the same thing on that
# tunnel implementation, and klippy/extras/creator5_remote_beeper.py
# on the SBC side for the other end of the wire protocol.
BEEP_IFACE = "1.4"
BEEP_BAUD = 115200
BEEP_COMMAND = "cmd_pwm"
BEEP_CHANNEL = "pc12"
BEEP_ACCEPT_POLL = 1.0
# Wi-Fi/LAN fallback for C5_BUZZER when the gadget's 5th serial port isn't
# present (board pinned to 4 tunnel ports -- see pi/c5-tunnel-gadget.sh).
# Same one-line-in/one-line-out protocol as run_beep_server, just over TCP;
# the SBC side only uses this when network_host is set in
# [creator5_remote_beeper] (klippy/extras/creator5_remote_beeper.py).
BEEP_NET_PORT = 8411


def log(port, message):
    line = "%s %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                              port, message)
    with LOG_LOCK:
        if LOG_FILE is not None:
            LOG_FILE.write(line)
            LOG_FILE.flush()
        # firmwareExe may inherit /dev/console as stderr. On the printer that
        # device can return EIO after boot; file logging must still continue.
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except OSError:
            pass


def bind_usb_serial():
    gadget_id = "1d6b 0104"
    # new_id is a registration endpoint, not a reliable list of IDs already
    # registered. A launcher restart must reuse the driver that owns the
    # gadget instead of attempting to register the same ID again.
    existing = find_link("1.0")
    if existing is not None:
        driver_path = os.path.realpath(
            "/sys/class/tty/%s/device/driver" % os.path.basename(existing))
        driver = os.path.basename(driver_path)
        if driver in DRIVERS:
            return driver
    for name in DRIVERS:
        new_id = "/sys/bus/usb-serial/drivers/%s/new_id" % name
        if not os.path.exists(new_id):
            continue
        try:
            with open(new_id) as f:
                if any(line.split()[:2] == gadget_id.split() for line in f):
                    return name
            with open(new_id, "w") as f:
                f.write(gadget_id + " ff\n")
            return name
        except OSError as e:
            if e.errno == errno.EEXIST:
                return name
            log("usb", "%s binding failed: %s" % (name, e))
    return None


def find_link(iface):
    if USBFS:
        return USBFS_LINKS.get(iface)
    try:
        names = sorted(n for n in os.listdir("/sys/class/tty")
                       if n.startswith("ttyUSB"))
    except OSError:
        return None
    for name in names:
        try:
            path = os.path.realpath("/sys/class/tty/%s/device" % name)
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
            with open(os.path.join(usbdev, "idVendor")) as f:
                if f.read().strip() != "1d6b":
                    continue
            with open(os.path.join(usbdev, "idProduct")) as f:
                if f.read().strip() != "0104":
                    continue
            node = "/dev/%s" % name
            if not os.path.exists(node):
                with open("/sys/class/tty/%s/dev" % name) as f:
                    major, minor = (int(n) for n in f.read().split(":"))
                os.mknod(node, stat.S_IFCHR | 0o600,
                         os.makedev(major, minor))
            return node
        except (OSError, ValueError):
            continue
    return None


# ---- usbfs: kernels without usb-serial ------------------------------------
# FlashForge's kernel 2.0.8 (firmware 2.0.0) has no usb-serial drivers, so
# nothing can be bound to the gadget. usbfs is part of the USB core: each
# gadget interface is claimed and its bulk endpoints are moved to and from a
# pty, whose slave stands in for the ttyUSB (socat and the beep server open
# it). See README.md.
USB_DEVICES = "/sys/bus/usb/devices"
GADGET_VID, GADGET_PID = "1d6b", "0104"
USBFS = False
USBFS_LINKS = {}   # gadget interface ("1.0") -> pty slave path
LIBC = None


class _BulkTransfer(ctypes.Structure):
    _fields_ = [("ep", ctypes.c_uint), ("len", ctypes.c_uint),
                ("timeout", ctypes.c_uint), ("data", ctypes.c_void_p)]


class _UsbIoctl(ctypes.Structure):
    _fields_ = [("ifno", ctypes.c_int), ("ioctl_code", ctypes.c_int),
                ("data", ctypes.c_void_p)]


def _ioc(direction, nr, size, mips=None):
    # asm/ioctl.h, type 'U': MIPS has three direction bits (none 1, read 2,
    # write 4) and 13 size bits; the generic layout two (none 0, write 1,
    # read 2) and 14.
    if mips is None:
        mips = os.uname().machine.startswith("mips")
    if mips:
        bits = {"": 1, "r": 2, "w": 4, "rw": 6}[direction]
        return bits << 29 | size << 16 | ord("U") << 8 | nr
    bits = {"": 0, "w": 1, "r": 2, "rw": 3}[direction]
    return bits << 30 | size << 16 | ord("U") << 8 | nr


USBDEVFS_BULK = _ioc("rw", 2, ctypes.sizeof(_BulkTransfer))
USBDEVFS_CLAIMINTERFACE = _ioc("r", 15, 4)
USBDEVFS_RELEASEINTERFACE = _ioc("r", 16, 4)
USBDEVFS_IOCTL = _ioc("rw", 18, ctypes.sizeof(_UsbIoctl))
USBDEVFS_CLEAR_HALT = _ioc("r", 21, 4)
USBDEVFS_DISCONNECT = _ioc("", 22, 0)


def usb_ioctl(fd, request, arg):
    # Through ctypes, which releases the GIL while a bulk transfer waits.
    global LIBC
    if LIBC is None:
        LIBC = ctypes.CDLL(None, use_errno=True)
        LIBC.ioctl.restype = ctypes.c_int
    result = LIBC.ioctl(fd, ctypes.c_ulong(request), arg)
    if result < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
    return result


def _sysfs(*parts):
    with open(os.path.join(*parts)) as f:
        return f.read().strip()


def find_gadget(root=USB_DEVICES):
    """The gadget's USB device: (sysfs name, bus, devnum), or None."""
    try:
        names = sorted(n for n in os.listdir(root) if ":" not in n)
    except OSError:
        return None
    for name in names:
        try:
            if (_sysfs(root, name, "idVendor") == GADGET_VID
                    and _sysfs(root, name, "idProduct") == GADGET_PID):
                return (name, int(_sysfs(root, name, "busnum")),
                        int(_sysfs(root, name, "devnum")))
        except (OSError, ValueError):
            continue
    return None


def gadget_interfaces(name, root=USB_DEVICES):
    """{"1.N": (ifno, ep_in, ep_out, max_packet)} for each interface with a
    bulk endpoint each way (the gser ports)."""
    result = {}
    for entry in os.listdir(root):
        if not entry.startswith(name + ":"):
            continue
        path = os.path.join(root, entry)
        ep_in = ep_out = None
        packet = 0
        for ep in os.listdir(path):
            if not ep.startswith("ep_"):
                continue
            if _sysfs(path, ep, "type") != "Bulk":
                continue
            address = int(_sysfs(path, ep, "bEndpointAddress"), 16)
            if address & 0x80:
                ep_in = address
                packet = int(_sysfs(path, ep, "wMaxPacketSize"), 16) & 0x7ff
            else:
                ep_out = address
        if ep_in is not None and ep_out is not None:
            result[entry.split(":", 1)[1]] = (
                int(_sysfs(path, "bInterfaceNumber"), 16), ep_in, ep_out, packet)
    return result


def gadget_node(name, bus, dev, root=USB_DEVICES):
    node = "/dev/bus/usb/%03d/%03d" % (bus, dev)
    if os.path.exists(node):
        return node
    major, minor = (int(n) for n in _sysfs(root, name, "dev").split(":"))
    node = "/tmp/c5-usbfs-%03d-%03d" % (bus, dev)
    if os.path.exists(node):
        os.unlink(node)
    os.mknod(node, stat.S_IFCHR | 0o600, os.makedev(major, minor))
    return node


def _pty_write(master, data, down):
    # Nobody reading the pty for a second (socat restarting): drop it, as a
    # tty with no reader would.
    view = memoryview(data)
    deadline = time.monotonic() + 1.0
    while view and not down.is_set():
        try:
            view = view[os.write(master, view):]
        except BlockingIOError:
            if time.monotonic() > deadline:
                return
            select.select([], [master], [], 0.1)


def usbfs_in(fd, ep, packet, master, down, iface):
    # One packet per transfer: any packet completes it, so a timeout never
    # cancels a transfer that has data in it.
    buf = ctypes.create_string_buffer(packet)
    xfer = _BulkTransfer(ep, packet, 1000, ctypes.addressof(buf))
    while not down.is_set():
        try:
            n = usb_ioctl(fd, USBDEVFS_BULK, ctypes.byref(xfer))
        except OSError as e:
            if e.errno == errno.ETIMEDOUT:
                continue
            if e.errno == errno.EPIPE:
                try:
                    usb_ioctl(fd, USBDEVFS_CLEAR_HALT, ctypes.byref(ctypes.c_uint(ep)))
                    continue
                except OSError:
                    pass
            log("usb", "%s in: %s" % (iface, e))
            down.set()
            return
        if n:
            _pty_write(master, buf.raw[:n], down)


def usbfs_out(fd, ep, master, down, iface):
    buf = ctypes.create_string_buffer(4096)
    while not down.is_set():
        if not select.select([master], [], [], 0.5)[0]:
            continue
        try:
            data = os.read(master, len(buf))
        except BlockingIOError:
            continue
        except OSError as e:
            log("usb", "%s pty: %s" % (iface, e))
            down.set()
            return
        ctypes.memmove(buf, data, len(data))
        xfer = _BulkTransfer(ep, len(data), 1000, ctypes.addressof(buf))
        try:
            usb_ioctl(fd, USBDEVFS_BULK, ctypes.byref(xfer))
        except OSError as e:
            if e.errno == errno.ETIMEDOUT:
                continue   # the SBC isn't reading this port
            log("usb", "%s out: %s" % (iface, e))
            down.set()
            return


class UsbfsLink:
    def __init__(self, gadget):
        self.gadget = gadget
        self.down = threading.Event()
        self.fd = None
        self.claimed = []
        self.ptys = []
        self.threads = []
        self.ifaces = []

    def open(self):
        name, bus, dev = self.gadget
        self.fd = os.open(gadget_node(name, bus, dev), os.O_RDWR)
        interfaces = gadget_interfaces(name)
        if not interfaces:
            raise OSError(errno.ENODEV, "gadget has no bulk interfaces")
        for iface, (ifno, ep_in, ep_out, packet) in sorted(interfaces.items()):
            # A usb-serial driver (kernel 2.0.7, forced usbfs) lets go first
            request = _UsbIoctl(ifno, USBDEVFS_DISCONNECT, None)
            try:
                usb_ioctl(self.fd, USBDEVFS_IOCTL, ctypes.byref(request))
            except OSError as e:
                if e.errno != errno.ENODATA:
                    raise
            usb_ioctl(self.fd, USBDEVFS_CLAIMINTERFACE,
                      ctypes.byref(ctypes.c_uint(ifno)))
            self.claimed.append(ifno)
            master, slave = os.openpty()
            self.ptys += [master, slave]
            # Raw before any data: an echoing pty would send it straight back
            configure_tty(slave, 115200)
            os.set_blocking(master, False)
            for target, args in ((usbfs_in, (self.fd, ep_in, packet, master)),
                                 (usbfs_out, (self.fd, ep_out, master))):
                thread = threading.Thread(target=target,
                                          args=args + (self.down, iface),
                                          name="usb-%s" % iface, daemon=True)
                thread.start()
                self.threads.append(thread)
            USBFS_LINKS[iface] = os.ttyname(slave)
            self.ifaces.append(iface)

    def close(self):
        self.down.set()
        for iface in self.ifaces:
            USBFS_LINKS.pop(iface, None)
        for thread in self.threads:
            thread.join(2.0)
        for fd in self.ptys:
            os.close(fd)
        for ifno in self.claimed:
            try:
                usb_ioctl(self.fd, USBDEVFS_RELEASEINTERFACE,
                          ctypes.byref(ctypes.c_uint(ifno)))
            except OSError:
                pass
        if self.fd is not None:
            os.close(self.fd)


def run_usbfs():
    link = None
    try:
        while not STOP.is_set():
            gadget = find_gadget()
            if link is not None and (link.down.is_set() or gadget != link.gadget):
                link.close()
                link = None
                log("usb", "usbfs link down")
            if link is None and gadget is not None:
                link = UsbfsLink(gadget)
                try:
                    link.open()
                    log("usb", "usbfs link up: %s" % ", ".join(
                        "%s=%s" % (i, USBFS_LINKS[i]) for i in link.ifaces))
                except (OSError, ValueError) as e:
                    log("usb", "usbfs open failed: %s" % e)
                    link.close()
                    link = None
                    STOP.wait(2.0)
                    continue
            STOP.wait(0.5)
    except Exception:
        log("usb", "usbfs thread crashed:\n" + traceback.format_exc())
    finally:
        if link is not None:
            link.close()


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
    if not select.select([fd], [], [], max(0.0, timeout))[0]:
        return b""
    try:
        return os.read(fd, 4096)
    except BlockingIOError:
        return b""


def write_all(fd, data):
    view = memoryview(data)
    deadline = time.monotonic() + 1.0
    while view:
        try:
            n = os.write(fd, view)
            if n == 0:
                raise OSError("UART write returned zero")
            view = view[n:]
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("UART write timeout")
            select.select([], [fd], [], remaining)


def crc16_ccitt(data):
    crc = 0xffff
    for byte in data:
        byte ^= crc & 0xff
        byte ^= (byte & 0x0f) << 4
        crc = ((byte << 8) | (crc >> 8)) ^ (byte >> 4) ^ (byte << 3)
    return crc & 0xffff


def identify_block():
    data = bytes((8, 0x10, 1, 0, 40))
    crc = crc16_ccitt(data)
    return data + bytes((crc >> 8, crc & 0xff, 0x7e))


def has_valid_frame(data):
    """Accept only a CRC-valid MCU response, not a bootloader banner."""
    for start in range(max(0, len(data) - 1024), len(data) - 4):
        length = data[start]
        end = start + length
        if length < 5 or length > 64 or end > len(data):
            continue
        frame = data[start:end]
        if (frame[1] & 0xf0) != 0x10 or frame[-1] != 0x7e:
            continue
        crc = crc16_ccitt(frame[:-3])
        if frame[-3] == crc >> 8 and frame[-2] == crc & 0xff:
            return True
    return False


def probe_alive(fd):
    termios.tcflush(fd, termios.TCIFLUSH)
    write_all(fd, b"\x7e" + identify_block())
    deadline = time.monotonic() + 0.3
    received = bytearray()
    while time.monotonic() < deadline:
        received += read_ready(fd, deadline - time.monotonic())
        if has_valid_frame(received):
            return True
    return False


def wake(fd, baud, parity_order):
    try:
        for parity in parity_order:
            configure_tty(fd, 115200, parity)
            deadline = time.monotonic() + 4.0
            tail = bytearray()
            got_bytes = False
            while time.monotonic() < deadline:
                data = read_ready(fd, deadline - time.monotonic())
                if not data:
                    continue
                got_bytes = True
                tail += data
                if b"Ready" in tail:
                    for _ in range(3):
                        write_all(fd, b"A")
                        ack_deadline = time.monotonic() + 0.3
                        while time.monotonic() < ack_deadline:
                            if 6 in read_ready(fd, ack_deadline - time.monotonic()):
                                return "parity %s" % parity
                    configure_tty(fd, baud)
                    if probe_alive(fd):
                        return "parity %s, ack lost" % parity
                    break
                del tail[:-8]
            if not got_bytes:
                break
    finally:
        configure_tty(fd, baud)
    return None


def prepare_uart(path, baud, parity_order, port):
    fd = open_tty(path, baud)
    try:
        if probe_alive(fd):
            log(port, "alive")
        elif parity_order:
            log(port, "not alive; trying boot-stage wake")
            result = wake(fd, baud, parity_order)
            log(port, "wake %s" % (result or "failed"))
        else:
            log(port, "not alive")
    finally:
        os.close(fd)


def terminate(proc):
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def run_port(port, uart, baud, parity_order, iface):
    last_link = None
    last_wake = 0.0
    while not STOP.is_set():
        link = find_link(iface)
        if link is None:
            if last_link is not None:
                log(port, "gadget disconnected")
                last_link = None
            STOP.wait(0.5)
            continue
        if link != last_link:
            log(port, "gadget link %s" % link)
            last_link = link
        try:
            # Never have the supervisor and socat open a UART together.
            prepare_uart(uart, baud, parity_order, port)
            with tempfile.TemporaryDirectory(prefix="c5-socat-%s-" % port,
                                             dir="/tmp") as directory:
                host_fifo = os.path.join(directory, "host")
                mcu_fifo = os.path.join(directory, "mcu")
                os.mkfifo(host_fifo, 0o600)
                os.mkfifo(mcu_fifo, 0o600)
                monitor = subprocess.Popen([MONITOR, host_fifo, mcu_fifo],
                                           stdout=subprocess.PIPE,
                                           stderr=LOG_FILE,
                                           bufsize=0)
                socat = None
                try:
                    if not select.select([monitor.stdout], [], [], 2.0)[0] \
                            or monitor.stdout.readline() != b"READY\n":
                        raise RuntimeError("identify monitor did not start")
                    socat = subprocess.Popen([
                        SOCAT, "-b4096", "-r", host_fifo, "-R", mcu_fifo,
                        # Match the working Python bridge's CLOCAL/CREAD and
                        # disabled hardware flow control on both serial fds.
                        "FILE:%s,rawer,echo=0,clocal=1,cread=1,crtscts=0,b115200" % link,
                        "FILE:%s,rawer,echo=0,clocal=1,cread=1,crtscts=0,b%d" % (uart, baud)],
                        stdout=subprocess.DEVNULL,
                        stderr=LOG_FILE)
                    log(port, "socat started pid %d" % socat.pid)
                    while not STOP.is_set() and socat.poll() is None:
                        if not select.select([monitor.stdout], [], [], 0.5)[0]:
                            continue
                        event = monitor.stdout.readline()
                        if event == b"WAKE\n":
                            # The trace reader has exited. Stop socat now so
                            # its FIFO writes cannot block during backoff.
                            terminate(socat)
                            now = time.monotonic()
                            if parity_order:
                                delay = max(0.0, 5.0 - (now - last_wake))
                                if delay:
                                    log(port, "identify unanswered; wake delayed %.1fs" % delay)
                                    STOP.wait(delay)
                                last_wake = time.monotonic()
                                log(port, "identify unanswered; restarting link for wake")
                            else:
                                log(port, "identify unanswered; restarting link")
                            break
                        raise RuntimeError("identify monitor exited: %r" % event)
                    if socat.poll() is not None:
                        log(port, "socat exited %s" % socat.returncode)
                finally:
                    terminate(socat)
                    terminate(monitor)
                    monitor.stdout.close()
        except (OSError, RuntimeError, subprocess.SubprocessError) as e:
            log(port, "error: %s" % e)
        STOP.wait(0.5)


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
    # _play_tone, run synchronously in the beep listener's own thread.
    # set_level/set_prescale are only ever called here with fixed
    # constants (100, 6), never derived from the tone's own parameters,
    # so they are one-time setup alongside config; only set_wc (the
    # actual waveform) and enable/disable_channels vary per beep.
    # Call them at most once per channel per process lifetime, not on
    # every beep: the kernel soc_pwm driver serializes config/
    # set_level/set_prescale/enable/disable on one internal mutex, and
    # calling config repeatedly when it is only ever going to fail
    # (already configured) just adds extra contention on that mutex for
    # no benefit. Real hardware testing hit the driver hang this risks:
    # cmd_pwm processes stuck forever in kernel D state waiting on that
    # mutex (visible as "task cmd_pwm ... blocked for more than N
    # seconds" in dmesg), which only a reboot clears.
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


def _open_beep_tty(path):
    # Plain blocking reads with a VTIME timeout instead of
    # select()+O_NONBLOCK: a long-lived select() loop on this gadget
    # tty never woke up for newly arrived data in testing (a fresh
    # blocking reader, e.g. plain `cat`, saw it immediately), so avoid
    # select() for this port entirely rather than rely on it.
    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    try:
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
        cc[termios.VMIN] = 0
        cc[termios.VTIME] = 10  # deciseconds: 1s read timeout
        speed = BAUDS[BEEP_BAUD]
        termios.tcsetattr(fd, termios.TCSANOW,
                          [iflag, oflag, cflag, lflag, speed, speed, cc])
        termios.tcflush(fd, termios.TCIOFLUSH)
    except Exception:
        os.close(fd)
        raise
    return fd


def run_beep_server():
    # One line in, one line out, over the gadget's 5th serial port
    # (ttyUSB4/ttyGS4, USB interface 1.4) instead of the network, so
    # C5_BUZZER works even with no Wi-Fi/LAN on the printer: "BEEP
    # DURATION=5000 FREQUENCY=2500 LEVEL=100" -> "OK" or
    # "ERROR <message>".
    buf = b""
    fd = None
    try:
        while not STOP.is_set():
            if fd is None:
                node = find_link(BEEP_IFACE)
                if node is None:
                    STOP.wait(BEEP_ACCEPT_POLL)
                    continue
                try:
                    fd = _open_beep_tty(node)
                    log("beep", "link up %s" % (node,))
                except OSError as exc:
                    log("beep", "cannot open %s: %s" % (node, exc))
                    STOP.wait(BEEP_ACCEPT_POLL)
                    continue
                buf = b""
            try:
                chunk = os.read(fd, 256)
            except OSError as exc:
                log("beep", "link error, reopening: %s" % (exc,))
                os.close(fd)
                fd = None
                continue
            if not chunk:
                continue
            log("beep", "rx %r" % (chunk,))
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    reply = _handle_beep_line(line)
                except Exception:
                    log("beep", "handler error:\n" + traceback.format_exc())
                    reply = b"ERROR internal error\n"
                try:
                    write_all(fd, reply)
                except (OSError, TimeoutError) as exc:
                    log("beep", "reply write failed: %s" % (exc,))
    except Exception:
        log("beep", "thread crashed:\n" + traceback.format_exc())
    finally:
        if fd is not None:
            os.close(fd)


def _handle_beep_line(line):
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


def run_beep_network_server(port=BEEP_NET_PORT):
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", port))
        sock.listen(4)
        sock.settimeout(1.0)
    except OSError as exc:
        log("beep-net", "cannot bind port %d: %s" % (port, exc))
        return
    log("beep-net", "listening on %d" % (port,))
    try:
        while not STOP.is_set():
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError as exc:
                log("beep-net", "accept error: %s" % (exc,))
                continue
            try:
                conn.settimeout(4.0)
                buf = b""
                while b"\n" not in buf and len(buf) < 256:
                    chunk = conn.recv(256)
                    if not chunk:
                        break
                    buf += chunk
                line = buf.split(b"\n", 1)[0]
                log("beep-net", "rx %r from %s" % (line, addr))
                try:
                    reply = _handle_beep_line(line)
                except Exception:
                    log("beep-net", "handler error:\n" + traceback.format_exc())
                    reply = b"ERROR internal error\n"
                try:
                    conn.sendall(reply)
                except OSError as exc:
                    log("beep-net", "reply send failed: %s" % (exc,))
            finally:
                conn.close()
    except Exception:
        log("beep-net", "thread crashed:\n" + traceback.format_exc())
    finally:
        sock.close()


def set_priority():
    try:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
        log("supervisor", "SCHED_FIFO 50")
    except (AttributeError, OSError):
        try:
            os.nice(-20)
            log("supervisor", "nice -20")
        except OSError:
            log("supervisor", "running at normal priority")


def main():
    global LOG_FILE, USBFS
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    LOG_FILE = open(LOG_PATH, "a", buffering=1)
    log("supervisor", "starting")
    if not os.path.isfile(MONITOR) or not os.access(MONITOR, os.X_OK):
        log("supervisor", "identify_monitor binary missing; run install.sh")
        return 1
    if not os.access(SOCAT, os.X_OK):
        log("supervisor", "%s missing or not executable" % SOCAT)
        return 1
    # C5_TUNNEL_USBFS=1 forces usbfs (testing it on a kernel that has the
    # drivers)
    driver = None if os.environ.get("C5_TUNNEL_USBFS") == "1" else bind_usb_serial()
    usbfs_thread = None
    if driver is None:
        log("supervisor", "no usb-serial driver; reaching the gadget through usbfs")
        USBFS = True
        usbfs_thread = threading.Thread(target=run_usbfs, name="usbfs", daemon=True)
    else:
        log("supervisor", "gadget bound to %s" % driver)
    set_priority()
    def on_signal(signum, frame):
        STOP.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threads = [threading.Thread(target=run_port, args=port, daemon=True)
               for port in PORTS]
    beep_thread = threading.Thread(target=run_beep_server, name="beep",
                                   daemon=True)
    beep_net_thread = threading.Thread(target=run_beep_network_server,
                                       name="beep-net", daemon=True)
    if usbfs_thread is not None:
        usbfs_thread.start()
    for thread in threads:
        thread.start()
    beep_thread.start()
    beep_net_thread.start()
    while not STOP.wait(1.0):
        pass
    for thread in threads:
        thread.join(4.0)
    beep_thread.join(4.0)
    beep_net_thread.join(4.0)
    if usbfs_thread is not None:
        usbfs_thread.join(4.0)
    log("supervisor", "stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # The stock launcher has no persistent stderr capture. Keep startup
        # failures in the same file as the normal supervisor diagnostics.
        with open(LOG_PATH, "a") as failure_log:
            traceback.print_exc(file=failure_log)
        raise

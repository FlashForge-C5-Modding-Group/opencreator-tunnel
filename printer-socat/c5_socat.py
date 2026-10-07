#!/usr/bin/env python3
"""Low-idle-CPU supervisor for four socat MCU tunnels.

Only socat owns live serial devices. Its binary trace FIFOs let a small C
process spot unanswered Klipper identify packets; the supervisor stops socat
before changing UART parity or sending the stock boot-stage wake sequence.
GPL-3.0-or-later.
"""
import os
import re
import select
import signal
import stat
import subprocess
import sys
import tempfile
import termios
import threading
import time

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


def log(port, message):
    line = "%s %s: %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"),
                              port, message)
    with LOG_LOCK:
        if LOG_FILE is not None:
            LOG_FILE.write(line)
            LOG_FILE.flush()
        sys.stderr.write(line)
        sys.stderr.flush()


def bind_usb_serial():
    gadget_id = "1d6b 0104"
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
            log("usb", "%s binding failed: %s" % (name, e))
    return None


def find_link(iface):
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
                        "FILE:%s,rawer,echo=0,b115200" % link,
                        "FILE:%s,rawer,echo=0,b%d" % (uart, baud)],
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
    global LOG_FILE
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    LOG_FILE = open(LOG_PATH, "a", buffering=1)
    log("supervisor", "starting")
    if not os.path.isfile(MONITOR) or not os.access(MONITOR, os.X_OK):
        log("supervisor", "identify_monitor binary missing; run install.sh")
        return 1
    if not os.access(SOCAT, os.X_OK):
        log("supervisor", "%s missing or not executable" % SOCAT)
        return 1
    driver = bind_usb_serial()
    if driver is None:
        log("supervisor", "no usable usb-serial driver; refusing socat mode")
        return 1
    log("supervisor", "gadget bound to %s" % driver)
    set_priority()
    def on_signal(signum, frame):
        STOP.set()
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    threads = [threading.Thread(target=run_port, args=port, daemon=True)
               for port in PORTS]
    for thread in threads:
        thread.start()
    while not STOP.wait(1.0):
        pass
    for thread in threads:
        thread.join(4.0)
    log("supervisor", "stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

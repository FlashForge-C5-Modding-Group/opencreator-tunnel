"""Host-side tests. Run on Linux: python3 -m unittest -v test_socat."""
import os
import io
import pty
import select
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import c5_socat


class GadgetBindingTest(unittest.TestCase):
    def test_reuses_already_bound_gadget_driver(self):
        with mock.patch.object(c5_socat, "find_link", return_value="/dev/ttyUSB0"), \
                mock.patch.object(c5_socat.os.path, "realpath",
                           return_value="/sys/bus/usb-serial/drivers/flashloader"), \
                mock.patch.object(c5_socat.os.path, "exists",
                           side_effect=AssertionError("unexpected registration")):
            self.assertEqual(c5_socat.bind_usb_serial(), "flashloader")

    def test_log_survives_broken_console_stderr(self):
        original_log_file = c5_socat.LOG_FILE
        destination = io.StringIO()
        try:
            c5_socat.LOG_FILE = destination
            with mock.patch.object(c5_socat.sys.stderr, "write",
                                   side_effect=OSError(5, "Input/output error")):
                c5_socat.log("supervisor", "still running")
            self.assertIn("supervisor: still running", destination.getvalue())
        finally:
            c5_socat.LOG_FILE = original_log_file


class LoadUsbSerialTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.drivers = os.path.join(self.tmp.name, "drivers")
        self.modules = os.path.join(self.tmp.name, "modules")
        os.makedirs(self.drivers)
        os.makedirs(self.modules)
        self.log = io.StringIO()
        self.original_log_file = c5_socat.LOG_FILE
        c5_socat.LOG_FILE = self.log

    def tearDown(self):
        c5_socat.LOG_FILE = self.original_log_file
        self.tmp.cleanup()

    def add_modules(self):
        for name in c5_socat.USB_SERIAL_MODULES:
            open(os.path.join(self.modules, name), "w").close()

    def insmod(self, *results):
        """Fake insmod: each call returns the next (code, stderr); a code of
        0 for usb-serial-simple registers the flashloader driver."""
        calls = []
        def run(cmd, **kwargs):
            code, err = results[len(calls)]
            calls.append(os.path.basename(cmd[1]))
            if code == 0 and cmd[1].endswith("usb-serial-simple.ko"):
                os.makedirs(os.path.join(self.drivers, "flashloader"))
            return subprocess.CompletedProcess(cmd, code, b"", err.encode())
        return calls, mock.patch.object(c5_socat.subprocess, "run", side_effect=run)

    def load(self):
        return c5_socat.load_usb_serial(self.drivers, self.modules)

    def test_built_in_driver_needs_no_modules(self):
        os.makedirs(os.path.join(self.drivers, "flashloader"))
        calls, patch = self.insmod()
        with patch:
            self.assertIsNone(self.load())
        self.assertEqual(calls, [])

    def test_loads_core_before_simple(self):
        self.add_modules()
        calls, patch = self.insmod((0, ""), (0, ""))
        with patch:
            self.assertIsNone(self.load())
        self.assertEqual(calls, ["usbserial.ko", "usb-serial-simple.ko"])

    def test_module_left_loaded_by_earlier_run_is_fine(self):
        self.add_modules()
        calls, patch = self.insmod(
            (1, "insmod: can't insert 'usbserial.ko': File exists"), (0, ""))
        with patch:
            self.assertIsNone(self.load())
        self.assertIn("already loaded", self.log.getvalue())

    def test_missing_modules_give_a_reason(self):
        calls, patch = self.insmod()
        with patch:
            reason = self.load()
        self.assertIn("usbserial.ko", reason)
        self.assertEqual(calls, [])

    def test_insmod_failure_stops_and_gives_a_reason(self):
        self.add_modules()
        calls, patch = self.insmod(
            (1, "insmod: can't insert 'usbserial.ko': invalid module format"))
        with patch:
            reason = self.load()
        self.assertIn("invalid module format", reason)
        self.assertEqual(calls, ["usbserial.ko"])


class UsbfsTest(unittest.TestCase):
    def test_ioctl_numbers(self):
        # Values from linux/usbdevice_fs.h as built on each architecture
        generic = {("r", 15, 4): 0x8004550F, ("", 22, 0): 0x5516,
                   ("rw", 2, 16): 0xC0105502, ("rw", 18, 12): 0xC00C5512}
        mips = {("r", 15, 4): 0x4004550F, ("", 22, 0): 0x20005516,
                ("rw", 2, 16): 0xC0105502, ("rw", 18, 12): 0xC00C5512}
        for args, value in generic.items():
            self.assertEqual(c5_socat._ioc(*args, mips=False), value, args)
        for args, value in mips.items():
            self.assertEqual(c5_socat._ioc(*args, mips=True), value, args)

    def make_sysfs(self, root, ports=5):
        def put(path, **files):
            os.makedirs(path, exist_ok=True)
            for name, value in files.items():
                with open(os.path.join(path, name), "w") as f:
                    f.write(value + "\n")
        put(os.path.join(root, "usb1"), idVendor="1d6b", idProduct="0002",
            busnum="1", devnum="1")
        put(os.path.join(root, "1-1.2"), idVendor="05a3", idProduct="9230",
            busnum="1", devnum="3")
        put(os.path.join(root, "1-1.3"), idVendor="1d6b", idProduct="0104",
            busnum="1", devnum="7", dev="189:6")
        for n in range(ports):
            iface = os.path.join(root, "1-1.3:1.%d" % n)
            put(iface, bInterfaceNumber="%02x" % n)
            put(os.path.join(iface, "ep_%02x" % (0x81 + n)), type="Bulk",
                bEndpointAddress="%02x" % (0x81 + n), wMaxPacketSize="0200")
            put(os.path.join(iface, "ep_%02x" % (0x01 + n)), type="Bulk",
                bEndpointAddress="%02x" % (0x01 + n), wMaxPacketSize="0200")

    def test_finds_gadget_and_its_five_ports(self):
        with tempfile.TemporaryDirectory() as root:
            self.make_sysfs(root)
            self.assertEqual(c5_socat.find_gadget(root), ("1-1.3", 1, 7))
            ifaces = c5_socat.gadget_interfaces("1-1.3", root)
            self.assertEqual(sorted(ifaces), ["1.0", "1.1", "1.2", "1.3", "1.4"])
            self.assertEqual(ifaces["1.4"], (4, 0x85, 0x05, 512))

    def test_link_moves_data_both_ways_on_every_port(self):
        """UsbfsLink against a simulated device: bytes the SBC sends on each
        bulk IN endpoint come out of that port's pty, and bytes written to
        the pty go out of its bulk OUT endpoint."""
        import ctypes, errno, queue, threading
        inbound = {0x81 + n: queue.Queue() for n in range(5)}
        sent = {0x01 + n: bytearray() for n in range(5)}
        claimed, lock = set(), threading.Lock()

        def fake_ioctl(fd, request, arg):
            obj = arg._obj if hasattr(arg, "_obj") else None
            if request == c5_socat.USBDEVFS_CLAIMINTERFACE:
                claimed.add(obj.value)
                return 0
            if request == c5_socat.USBDEVFS_RELEASEINTERFACE:
                claimed.discard(obj.value)
                return 0
            if request == c5_socat.USBDEVFS_IOCTL:
                raise OSError(errno.ENODATA, "no driver")
            if request == c5_socat.USBDEVFS_BULK:
                if obj.ep & 0x80:
                    try:
                        data = inbound[obj.ep].get(timeout=0.05)
                    except queue.Empty:
                        raise OSError(errno.ETIMEDOUT, "timeout")
                    ctypes.memmove(obj.data, data, len(data))
                    return len(data)
                with lock:
                    sent[obj.ep] += ctypes.string_at(obj.data, obj.len)
                return obj.len
            raise AssertionError(hex(request))

        with tempfile.TemporaryDirectory() as root:
            self.make_sysfs(root)
            real_interfaces = c5_socat.gadget_interfaces
            with mock.patch.object(c5_socat, "usb_ioctl", fake_ioctl), \
                    mock.patch.object(c5_socat, "gadget_node", return_value=os.devnull), \
                    mock.patch.object(c5_socat, "gadget_interfaces",
                                      lambda name: real_interfaces(name, root)), \
                    mock.patch.object(c5_socat, "USBFS", True), \
                    mock.patch.dict(c5_socat.USBFS_LINKS, clear=True):
                link = c5_socat.UsbfsLink(("1-1.3", 1, 7))
                link.open()
                try:
                    self.assertEqual(claimed, {0, 1, 2, 3, 4})
                    for n in range(5):
                        path = c5_socat.find_link("1.%d" % n)
                        self.assertTrue(path.startswith("/dev/pts/"), path)
                        fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
                        try:
                            payload = c5_socat.identify_block() + bytes([n])
                            inbound[0x81 + n].put(payload)
                            self.assertTrue(select.select([fd], [], [], 2)[0])
                            self.assertEqual(os.read(fd, 64), payload)
                            os.write(fd, payload[::-1])
                            deadline = time.monotonic() + 2
                            while time.monotonic() < deadline and \
                                    bytes(sent[0x01 + n]) != payload[::-1]:
                                time.sleep(0.01)
                            self.assertEqual(bytes(sent[0x01 + n]), payload[::-1])
                        finally:
                            os.close(fd)
                finally:
                    link.close()
                self.assertEqual(claimed, set())
                self.assertEqual(c5_socat.USBFS_LINKS, {})


class IdentifyMonitorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("cc"):
            raise unittest.SkipTest("C compiler unavailable")
        cls.build = tempfile.TemporaryDirectory()
        cls.binary = os.path.join(cls.build.name, "identify_monitor")
        subprocess.run(["cc", "-std=c99", "-O2", "-Wall", "-Wextra",
                        "-Werror", "-o", cls.binary,
                        os.path.join(os.path.dirname(__file__),
                                     "identify_monitor.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.host = os.path.join(self.tmp.name, "host")
        self.mcu = os.path.join(self.tmp.name, "mcu")
        os.mkfifo(self.host)
        os.mkfifo(self.mcu)
        self.monitor = subprocess.Popen([self.binary, self.host, self.mcu],
                                        stdout=subprocess.PIPE)
        self.assertEqual(self.monitor.stdout.readline(), b"READY\n")
        self.host_writer = os.open(self.host, os.O_WRONLY | os.O_NONBLOCK)
        self.mcu_writer = os.open(self.mcu, os.O_WRONLY | os.O_NONBLOCK)

    def tearDown(self):
        os.close(self.host_writer)
        os.close(self.mcu_writer)
        if self.monitor.poll() is None:
            self.monitor.terminate()
        self.monitor.wait(timeout=2)
        self.monitor.stdout.close()
        self.tmp.cleanup()

    def test_unanswered_identify_requests_wake(self):
        os.write(self.host_writer, c5_socat.identify_block())
        self.assertTrue(select.select([self.monitor.stdout], [], [], 1)[0])
        self.assertEqual(self.monitor.stdout.readline(), b"WAKE\n")

    def test_response_clears_pending_identify(self):
        os.write(self.host_writer, c5_socat.identify_block())
        time.sleep(0.05)
        os.write(self.mcu_writer, c5_socat.identify_block())
        self.assertFalse(select.select([self.monitor.stdout], [], [], 0.6)[0])
        os.write(self.host_writer, c5_socat.identify_block())
        self.assertTrue(select.select([self.monitor.stdout], [], [], 1)[0])
        self.assertEqual(self.monitor.stdout.readline(), b"WAKE\n")

    def test_corrupt_identify_does_not_wake(self):
        frame = bytearray(c5_socat.identify_block())
        frame[-2] ^= 1
        os.write(self.host_writer, frame)
        self.assertFalse(select.select([self.monitor.stdout], [], [], 0.6)[0])

    def test_socat_forwards_both_directions_without_false_wake(self):
        socat_binary = os.environ.get("C5_TEST_SOCAT") or shutil.which("socat")
        if not socat_binary:
            self.skipTest("socat unavailable")
        host_master, host_slave = pty.openpty()
        mcu_master, mcu_slave = pty.openpty()
        proc = subprocess.Popen([
            socat_binary, "-b4096", "-r", self.host, "-R", self.mcu,
            "FILE:%s,rawer,echo=0,b115200" % os.ttyname(host_slave),
            "FILE:%s,rawer,echo=0,b230400" % os.ttyname(mcu_slave)],
            stderr=subprocess.PIPE)
        try:
            time.sleep(0.15)
            self.assertIsNone(proc.poll(), proc.stderr.read() if proc.poll() is not None else "")
            frame = c5_socat.identify_block()
            os.write(host_master, frame)
            self.assertTrue(select.select([mcu_master], [], [], 1)[0])
            self.assertEqual(os.read(mcu_master, 64), frame)
            os.write(mcu_master, frame)
            self.assertTrue(select.select([host_master], [], [], 1)[0])
            self.assertEqual(os.read(host_master, 64), frame)
            self.assertFalse(select.select([self.monitor.stdout], [], [], 0.6)[0])
        finally:
            proc.terminate()
            proc.wait(timeout=2)
            proc.stderr.close()
            for fd in (host_master, host_slave, mcu_master, mcu_slave):
                os.close(fd)


if __name__ == "__main__":
    unittest.main()

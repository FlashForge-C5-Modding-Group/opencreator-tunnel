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

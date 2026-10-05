"""Dependency-free regression tests for the usbfs bridge hot path.

Run on Linux with: python3 -m unittest -v test_bridge_io
"""
import errno
import io
import os
import select
import threading
import time
import struct
import unittest
from unittest import mock

import c5_bridge as bridge


class UsbBridgeIOTest(unittest.TestCase):
    def make_link(self):
        link = bridge.UsbSerialLink.__new__(bridge.UsbSerialLink)
        link.iface = 0
        link.out_ep = 1
        link._stop = threading.Event()
        return link

    def test_write_stall_is_bounded(self):
        device = mock.Mock()
        device.get_cached.return_value = (9, "usb-node")
        with mock.patch.object(bridge, "GADGET_DEVICE", device), \
             mock.patch.object(bridge, "RETRY_BACKOFF", 0), \
             mock.patch.object(bridge.UsbSerialLink, "MAX_WRITE_RETRIES", 2), \
             mock.patch.object(bridge.fcntl, "ioctl", return_value=0), \
             mock.patch.object(bridge, "usb_bulk_transfer",
                               side_effect=OSError(errno.EPIPE, "stall")) as tx:
            with self.assertRaises(bridge.LinkDown):
                self.make_link().write(b"abc")
        self.assertEqual(tx.call_count, 3)
        device.invalidate.assert_called_once()

    def test_zero_write_is_bounded(self):
        device = mock.Mock()
        device.get_cached.return_value = (9, "usb-node")
        with mock.patch.object(bridge, "GADGET_DEVICE", device), \
             mock.patch.object(bridge, "RETRY_BACKOFF", 0), \
             mock.patch.object(bridge.UsbSerialLink, "MAX_WRITE_RETRIES", 2), \
             mock.patch.object(bridge, "usb_bulk_transfer", return_value=0) as tx:
            with self.assertRaises(bridge.LinkDown):
                self.make_link().write(b"abc")
        self.assertEqual(tx.call_count, 3)
        device.invalidate.assert_called_once()

    def test_reader_generation_cannot_mark_new_link_down(self):
        device = bridge.UsbGadgetDevice()
        old = mock.Mock()
        new = mock.Mock()
        device.links[0] = new
        data = b"abc"
        proc = mock.Mock(stdout=io.BytesIO(bytes([0])
                                           + struct.pack("<I", 3) + data))
        device._reader_proc = proc
        device._reader_loop(proc, {0: old})
        old._queue_data.assert_called_once_with(data)
        old._mark_down.assert_called_once()
        new._queue_data.assert_not_called()
        new._mark_down.assert_not_called()

    def test_reader_rejects_oversized_frame(self):
        device = bridge.UsbGadgetDevice()
        link = mock.Mock()
        proc = mock.Mock(stdout=io.BytesIO(bytes([0])
                                           + struct.pack("<I", 4097)))
        with mock.patch.object(bridge, "log"):
            device._reader_loop(proc, {0: link})
        link._queue_data.assert_not_called()
        link._mark_down.assert_called_once()

    def test_single_interface_failure_restarts_reader_generation(self):
        device = bridge.UsbGadgetDevice()
        links = {0: mock.Mock(), 1: mock.Mock()}
        proc = mock.Mock(stdout=io.BytesIO(b"\x00\xff\xff\xff\xff"))
        device._reader_proc = proc
        with mock.patch.object(bridge, "log"):
            device._reader_loop(proc, links)
        self.assertIsNone(device._reader_proc)
        for link in links.values():
            link._mark_down.assert_called_once()

    def test_usb_data_wakes_bridge_immediately(self):
        with mock.patch.object(bridge, "GADGET_DEVICE"):
            link = bridge.UsbSerialLink(0)
            try:
                link._queue_data(b"klipper")
                ready, _, _ = select.select([link.wake_fd], [], [], 0)
                self.assertEqual(ready, [link.wake_fd])
                self.assertEqual(os.read(link.wake_fd, 1), b"\x01")
                self.assertEqual(link.read_nowait(), b"klipper")
            finally:
                link.close()

    def test_forwarder_preserves_both_directions(self):
        master, slave = os.openpty()
        bridge.configure_tty(slave, 230400)
        stop = threading.Event()
        port = bridge.PortBridge(stop, "test", "pty", 230400, False, (), 0)
        port.uart = slave
        with mock.patch.object(bridge, "GADGET_DEVICE"):
            link = bridge.UsbSerialLink(0)
            link.write = mock.Mock()
            worker = threading.Thread(target=port._forward, args=(link,))
            worker.start()
            try:
                link._queue_data(b"to-mcu")
                ready, _, _ = select.select([master], [], [], 0.5)
                self.assertTrue(ready)
                self.assertEqual(os.read(master, 4096), b"to-mcu")
                os.write(master, b"from-mcu")
                deadline = time.monotonic() + 0.5
                while not link.write.called and time.monotonic() < deadline:
                    time.sleep(0.001)
                link.write.assert_called()
                self.assertEqual(link.write.call_args.args[0], b"from-mcu")
            finally:
                stop.set()
                worker.join(1)
                link.close()
                os.close(master)
                os.close(slave)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()

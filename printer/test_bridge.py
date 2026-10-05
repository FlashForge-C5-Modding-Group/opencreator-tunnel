# Tests for c5_bridge.py; Linux only (uses ptys).  Run: python3 -m pytest
import errno
import io
import os
import select
import struct
import termios
import threading
import time
from unittest import mock

import pytest

import c5_bridge as bridge


def mcu_block(seq=1, payload=b"\x00\x01\x02"):
    return bridge.build_block(seq, list(payload))


# -- (a) frame detection ---------------------------------------------------

def test_identify_block_matches_klipper_crc():
    # identify offset=0 count=40; bytes computed with klippy's crc16_ccitt
    block = bridge.IDENTIFY_BLOCK
    assert block == bytes.fromhex("08100100285e9f7e")
    assert bridge.check_packet(block) == 8
    assert bridge.is_identify(block)


def test_detector_accepts_valid_blocks_and_rejects_bad_crc():
    det = bridge.FrameDetector()
    good = mcu_block()
    bad = bytearray(good)
    bad[-2] ^= 0xff
    frames = det.feed(bridge.IDENTIFY_BLOCK + bytes(bad) + good)
    assert frames == [bridge.IDENTIFY_BLOCK, good]


def test_detector_resyncs_after_garbage_and_split_input():
    det = bridge.FrameDetector()
    good = mcu_block(seq=5)
    stream = b"Ready.\r\n\x00\xff" + b"\x7e" + good
    frames = []
    for i in range(len(stream)):
        frames += det.feed(stream[i:i + 1])
    assert frames == [good]


def test_ack_frame_is_not_identify():
    assert not bridge.is_identify(bridge.build_block(1, []))


# -- (b) boot-stage wake ---------------------------------------------------

@pytest.fixture
def pty_port(monkeypatch):
    master, slave = os.openpty()
    bridge.configure_tty(slave, 230400)
    # Some kernels (e.g. WSL2) reject PARENB on ptys; record the requested
    # parity and apply the rest of the line setup unchanged.
    parities = []
    real_configure = bridge.configure_tty

    def configure(fd, baud, parity=None):
        parities.append(parity)
        real_configure(fd, baud)
    monkeypatch.setattr(bridge, "configure_tty", configure)
    yield master, slave, parities
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def fake_boot_stage(master, stop, ack=True):
    """Print Ready every 100 ms until an 'A' arrives, then answer 0x06."""
    while not stop.is_set():
        os.write(master, b"Ready.\r\n")
        data = bridge.read_ready(master, 0.1)
        if b"A" in data:
            if ack:
                os.write(master, b"\x06")
            return


def assert_klipper_line(fd, baud):
    iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
    assert ospeed == bridge.BAUDS[baud]
    assert not cflag & termios.PARENB


def test_wake_succeeds_on_first_parity(pty_port):
    master, slave, parities = pty_port
    stop = threading.Event()
    t = threading.Thread(target=fake_boot_stage, args=(master, stop))
    t.start()
    try:
        assert bridge.wake(slave, 230400, ("E", "N")) == (True, "parity E")
    finally:
        stop.set()
        t.join()
    assert parities == ["E", None]
    assert_klipper_line(slave, 230400)


def test_wake_without_banner_gives_up_after_one_parity(pty_port, monkeypatch):
    monkeypatch.setattr(bridge, "WAKE_BANNER_TIMEOUT", 0.5)
    master, slave, parities = pty_port
    start = time.monotonic()
    assert bridge.wake(slave, 230400, ("E", "N")) == (False, "no banner")
    assert time.monotonic() - start < 0.5 + 0.3
    assert parities == ["E", None]
    assert_klipper_line(slave, 230400)


def test_wake_banner_without_ack_tries_every_parity(pty_port, monkeypatch):
    monkeypatch.setattr(bridge, "WAKE_BANNER_TIMEOUT", 0.5)
    monkeypatch.setattr(bridge, "WAKE_ACK_TIMEOUT", 0.05)
    master, slave, parities = pty_port
    stop = threading.Event()

    def banner_forever():
        while not stop.is_set():
            os.write(master, b"Ready.\r\n")
            bridge.read_ready(master, 0.05)
    t = threading.Thread(target=banner_forever)
    t.start()
    try:
        ok, detail = bridge.wake(slave, 460800, ("N", "E"))
    finally:
        stop.set()
        t.join()
    assert not ok and detail.startswith("no ack (after A N: ")
    # each banner-without-ack is checked at the Klipper baud
    assert parities == ["N", None, "E", None, None]
    assert_klipper_line(slave, 460800)


def test_wake_accepts_release_with_lost_ack(pty_port, monkeypatch):
    monkeypatch.setattr(bridge, "WAKE_ACK_TIMEOUT", 0.05)
    master, slave, parities = pty_port
    stop = threading.Event()

    def release_without_ack():
        # Boot stage that jumps to the application on 'A' but whose 0x06
        # never arrives; the application then answers identify.
        while not stop.is_set():
            os.write(master, b"Ready.\r\n")
            if b"A" in bridge.read_ready(master, 0.1):
                break
        det = bridge.FrameDetector()
        while not stop.is_set():
            for frame in det.feed(bridge.read_ready(master, 0.05)):
                os.write(master, mcu_block())
    t = threading.Thread(target=release_without_ack)
    t.start()
    try:
        assert bridge.wake(slave, 230400, ("E", "N")) == (
            True, "parity E, ack lost")
    finally:
        stop.set()
        t.join()
    assert parities == ["E", None, None]
    assert_klipper_line(slave, 230400)


# -- (c) reactive wake rule ------------------------------------------------

IDENT = bridge.IDENTIFY_BLOCK


def test_reactive_wake_fires_once_per_unanswered_identify():
    m = bridge.WakeMonitor()
    m.host_frame(IDENT, 10.0)
    assert not m.should_wake(10.3)
    assert m.should_wake(10.5)
    m.wake_started(10.5)
    assert not m.should_wake(10.6)
    assert not m.should_wake(20.0)
    # A new unanswered identify inside the retry window stays quiet...
    m.host_frame(IDENT, 11.0)
    assert not m.should_wake(12.0)
    # ...and fires again once the window has passed.
    assert m.should_wake(10.5 + bridge.WAKE_RETRY_MIN + 0.01)


def test_reactive_wake_quiet_while_mcu_answers():
    m = bridge.WakeMonitor()
    for i in range(20):
        now = 1.0 + i
        m.host_frame(IDENT, now)
        m.mcu_frame(now + 0.01)
        assert not m.should_wake(now + 0.9)


def test_reactive_wake_ignores_non_identify_host_frames():
    m = bridge.WakeMonitor()
    m.host_frame(bridge.build_block(2, [0x05, 0x01]), 1.0)
    assert not m.should_wake(5.0)


# -- usbfs gadget and failure handling ------------------------------------

def test_find_gadget_device_matches_device_not_interface(tmp_path,
                                                          monkeypatch):
    gadget = tmp_path / "1-1"
    gadget.mkdir()
    (gadget / "idVendor").write_text("1d6b\n")
    (gadget / "idProduct").write_text("0104\n")
    (gadget / "busnum").write_text("1\n")
    (gadget / "devnum").write_text("7\n")
    (tmp_path / "1-1:1.0").mkdir()
    monkeypatch.setattr(bridge, "SYSFS_USB_DEVICES", str(tmp_path))
    real_exists = os.path.exists
    monkeypatch.setattr(bridge.os.path, "exists",
                        lambda path: path == "/dev/bus/usb/001/007"
                        or real_exists(path))
    assert bridge.find_gadget_device() == "/dev/bus/usb/001/007"
    (gadget / "idVendor").write_text("1a86\n")
    assert bridge.find_gadget_device() is None


def make_link():
    link = bridge.UsbSerialLink.__new__(bridge.UsbSerialLink)
    link.iface = 0
    link.out_ep = 1
    link._stop = threading.Event()
    return link


@pytest.mark.parametrize("failure", ["stall", "zero"])
def test_usb_write_cannot_spin_forever(failure, monkeypatch):
    device = mock.Mock()
    device.get_cached.return_value = (9, "usb-node")
    monkeypatch.setattr(bridge, "GADGET_DEVICE", device)
    monkeypatch.setattr(bridge, "RETRY_BACKOFF", 0)
    monkeypatch.setattr(bridge.UsbSerialLink, "MAX_WRITE_RETRIES", 2)
    monkeypatch.setattr(bridge.fcntl, "ioctl", lambda *args: 0)
    calls = []

    def transfer(*args, **kwargs):
        calls.append(1)
        if failure == "stall":
            raise OSError(errno.EPIPE, "stalled")
        return 0

    monkeypatch.setattr(bridge, "usb_bulk_transfer", transfer)
    with pytest.raises(bridge.LinkDown):
        make_link().write(b"abc")
    assert len(calls) == 3
    device.invalidate.assert_called_once()


def test_old_reader_cannot_mark_new_links_down():
    device = bridge.UsbGadgetDevice()
    old = mock.Mock()
    new = mock.Mock()
    device.links[0] = new
    data = b"abc"
    proc = mock.Mock(stdout=io.BytesIO(bytes([0])
                                       + struct.pack("<I", len(data)) + data))
    device._reader_proc = proc
    device._reader_loop(proc, {0: old})
    old._queue_data.assert_called_once_with(data)
    old._mark_down.assert_called_once()
    new._mark_down.assert_not_called()
    new._queue_data.assert_not_called()


def test_usb_reader_notifies_forwarder_without_poll_delay(monkeypatch):
    device = mock.Mock()
    monkeypatch.setattr(bridge, "GADGET_DEVICE", device)
    link = bridge.UsbSerialLink(0)
    try:
        link._queue_data(b"klipper")
        ready, _, _ = select.select([link.wake_fd], [], [], 0)
        assert ready == [link.wake_fd]
        assert os.read(link.wake_fd, 1) == b"\x01"
        assert link.read_nowait() == b"klipper"
    finally:
        link.close()

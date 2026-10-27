# Experimental socat MCU tunnel

This is a separate, opt-in alternative to `../printer/c5_bridge.py`. It does
not change that bridge, the Pi gadget setup, or printer configuration.

Each of the four `/opt/bin/socat` processes forwards one gadget serial port
to one MCU UART. A small C observer reads socat's binary trace FIFOs. It
only checks Klipper framing during connection: if a valid host identify is
unanswered for 400 ms, the Python supervisor stops that socat process,
reopens the UART, performs the stock boot-stage wake handshake, and restarts
the relay. The supervisor never opens a UART while socat owns it. It also
performs an initial MCU probe/wake and remaps gadget interfaces after a USB
disconnect. Nothing periodically sends wake bytes during a print.

This design reduces Python's data-path work, but socat must copy each stream
to the observer FIFO, so **lower CPU use is not yet measured or guaranteed**.
## Reaching the gadget: usb-serial or usbfs

On FlashForge kernel 2.0.7 (firmware 1.9.9) and older, the supervisor gives
the gadget's id (`1d6b:0104`) to a built-in `usb-serial-simple` driver
(`flashloader` and so on), and each gadget port appears as a `/dev/ttyUSB*`.

Kernel 2.0.8 (firmware 2.0.0) has no usb-serial drivers at all: the
usb-serial core, `usb-serial-simple`, `ch341` and `cdc_acm` were all left out
of FlashForge's build. There the supervisor first loads the bundled
`modules/usbserial.ko` and `modules/usb-serial-simple.ko` (see
[modules/README.md](modules/README.md)) and then binds as above.

If usb-serial still isn't usable (modules missing or rejected, or no driver
takes the gadget id), the supervisor logs
`falling back to usbfs instead of usb-serial: <reason>` and reaches the
gadget through usbfs instead (part of the USB core, so present on every
kernel):

- it finds the gadget in `/sys/bus/usb/devices` and opens its
  `/dev/bus/usb/BBB/DDD` node;
- for each interface with a bulk endpoint each way (all five gser ports,
  `1.0`–`1.4`), it detaches any kernel driver, claims the interface, and
  creates a pty;
- two threads per port move the data: bulk IN → pty, and pty → bulk OUT,
  with `USBDEVFS_BULK` through ctypes (the GIL is released while a transfer
  waits). An IN transfer is exactly one packet long, so it completes as soon
  as any packet arrives and a timeout never cancels one with data in it;
- the pty slave stands in for the `ttyUSB`: socat, the wake handshake and
  the beep server use it unchanged.

A USB disconnect (the SBC rebooting) closes the ptys; the link comes back,
with new ptys, when the gadget reappears. `C5_TUNNEL_USBFS=1` forces usbfs on
a kernel that does have the drivers, for testing.

## Safe installation

Do this only while idle, with no print active. Copy this folder to
`/usr/prog/c5-tunnel-socat` on the printer, then run:

```sh
sh /usr/prog/c5-tunnel-socat/install.sh
reboot
```

The installer checks for socat, a compiler, and Python syntax before it
replaces `/usr/prog/PROGRAM/software/firmwareExe`. It saves that exact
previous launcher as `firmwareExe.previous`. The new launcher retains the
existing coprocessor and Wi-Fi initialization. Logs go to
`/usr/data/logs/c5-socat.log`.

The installer uses the printer Python library path for its syntax check. A
custom launcher must not put a foreground touchscreen or other long-running
program before the supervisor loop. If it starts GrumpyScreen there, run it
in the background so the MCU relays can start. Keep a copy of the customized
launcher before changing it.

To roll back to the exact previous launcher:

```sh
sh /usr/prog/c5-tunnel-socat/uninstall.sh
reboot
```

Do not run this variant and the original bridge simultaneously; both need
exclusive ownership of the same UARTs and gadget ports. The existing
`firmwareExe` startup slot chooses one or the other.

## Validation before printing

1. Confirm four `socat started` lines in the log and all four MCUs `Ready`
   in Klipper.
2. While idle, issue `FIRMWARE_RESTART`. Verify the levelboard and other
   boards reconnect. If any fail, roll back before printing.
3. Compare CPU load with the old bridge using the same print file, noting
   peak `socat`, `identify_monitor`, and supervisor usage on the printer SoC.
   Compare Klipper `srtt`, retransmits, and print stalls as well. Do not infer
   improvement from idle CPU alone.

An installed relay can be running while the Pi still reports MCU timeouts.
The gadget serial ports need local-carrier mode (`clocal`) and disabled
hardware flow control on both ends. Check the Pi's Klipper state after a
`FIRMWARE_RESTART`; four socat processes alone do not prove bidirectional
communication. The supervisor logs to the file even if its inherited console
stderr is unavailable.

Host-side tests (Linux, no printer required):

```sh
cd printer-socat
python3 -m unittest -v test_socat
sh -n install.sh uninstall.sh firmwareExe
```

The tests compile the C observer in a temporary directory. If socat is
available, they also relay between pseudo-terminals through the trace FIFOs.
The installer builds the observer for the printer's MIPS architecture.

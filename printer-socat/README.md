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
The current printer has `/opt/bin/socat` and a working usb-serial driver.
This variant requires `/dev/ttyUSB*` on the printer. It will not run on a
kernel that only supports the original bridge's usbfs transport.

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

Host-side tests (Linux, no printer required):

```sh
cd printer-socat
python3 -m unittest -v test_socat
sh -n install.sh uninstall.sh firmwareExe
```

The tests compile the C observer in a temporary directory. If socat is
available, they also relay between pseudo-terminals through the trace FIFOs.
The installer builds the observer for the printer's MIPS architecture.

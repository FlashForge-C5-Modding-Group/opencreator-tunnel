# c5-tunnel

Run Klipper, Moonraker and Mainsail for a FlashForge Creator 5 Pro on a
separate single-board computer (SBC). The printer's own Linux board keeps
only a serial bridge: it forwards its four MCU UARTs to the SBC over one USB
cable and serves the built-in camera. Stock `firmwareExe`, stock Klipper,
Moonraker, nginx and the touchscreen UI no longer run on the printer.

All four MCUs (mainBoardGD, heaterBoard, eBoard, levelBoard) must run
[klipper-c5](https://github.com/wondercrash/klipper-c5) firmware. The SBC
runs Klipper from the same repository (branch `c5`).

```
SBC (Klipper, Moonraker, Mainsail)
  /dev/ttyGS0..3  --USB gadget, 4x gser serial-->  printer /dev/ttyUSB*
                                                   c5_bridge.py
                                                     ttyS2  mainBoardGD  230400
                                                     ttyS4  heaterBoard  230400
                                                     ttyS5  eBoard       460800
                                                     ttyS7  levelBoard   230400
```

Status: phase 1 (tunnel and upstream configuration). Tool changes, extruder
offset and vibration calibrations, and touchscreen streaming are later
phases.

## Hardware

- An SBC with a USB device (OTG) port. Tested target: Raspberry Pi Zero 2 W
  (use the port labelled `USB`, not `PWR`). Any board exposing an entry in
  `/sys/class/udc` in peripheral mode works.
- A USB cable from the SBC's device port to the USB-A port on the side of
  the printer. A Pi Zero 2 W can be powered from that port. Other SBCs must
  be powered separately and connected with a data-only (power-blocked)
  cable so two supplies are not tied together.

## Printer

Install (printer reachable over SSH as root):

```sh
scp -r printer root@<printer-ip>:/usr/prog/c5-tunnel
ssh root@<printer-ip> sh /usr/prog/c5-tunnel/install.sh
ssh root@<printer-ip> reboot
```

`install.sh` backs up the stock `firmwareExe` to
`/usr/prog/c5-tunnel/firmwareExe.stock` (and prints its SHA-256 next to the
versioned copy it came from), then puts a launcher script in its place. The
stock startup script starts the launcher instead, which runs the bridge
forever. After reboot the touchscreen stays blank.

- Bridge log: `/usr/data/logs/c5-tunnel.log` (previous boot:
  `c5-tunnel.log.1`). Each port logs `alive` or `wake ok (parity E|N)`,
  then `waiting for gadget` until the SBC is connected, then
  `link up /dev/ttyUSBn (1.x)`.
- Wi-Fi: the launcher joins the networks saved from the stock touchscreen
  (`/usr/prog/wifi/wpa_supplicant.conf`), as stock `firmwareExe` did. To
  change networks, return to stock or edit that file.
- A stock software update replaces the launcher with a new stock
  `firmwareExe`. Run `install.sh` again afterwards.

Return to stock:

```sh
ssh root@<printer-ip> sh /usr/prog/c5-tunnel/uninstall.sh   # --purge also deletes /usr/prog/c5-tunnel
ssh root@<printer-ip> reboot
```

## SBC

1. Install Raspberry Pi OS Lite (or Armbian) with network access.
2. Copy `pi/` to the SBC and run `sudo sh pi/setup-gadget.sh`, then reboot.
   `ls -l /dev/ttyGS0 /dev/ttyGS1 /dev/ttyGS2 /dev/ttyGS3` must list four
   devices.
3. Install Klipper, Moonraker and Mainsail with
   [KIAUH](https://github.com/dw-0/kiauh). For Klipper, use the custom
   repository `https://github.com/wondercrash/klipper-c5`, branch `c5`. It
   provides the `[mclib]` module that configures the mainBoardGD motors
   (currents, microstepping, resonance damping), which upstream Klipper
   lacks.
   Moonraker's update manager may report the fork as unofficial; that is
   cosmetic.
4. Copy `pi/config/*.cfg` to `~/printer_data/config/` and restart Klipper.

`setup-gadget.sh` installs `c5-tunnel-gadget.service` (creates the gadget
at boot) and a `klipper.service` drop-in so Klipper starts after it.
Connect the USB cable; the printer log then shows `link up` for all four
ports and Mainsail reports `Ready`.

## Configuration

- `c5_hardware.cfg`: MCUs, steppers, probe, heaters, fans, sensors, all
  transcribed from the stock 1.9.9 configuration. Sections that need
  FlashForge-only Klipper modules are left out.
- `c5_macros.cfg`: `M106`/`M107`/`M900`, gear-stepper building blocks, and
  `M104`/`M109`/`SET_HEATER_TEMPERATURE` wrappers that switch on the 24 V
  heater rail (`DC24V_CTL`) as stock Klipper does. The idle timeout switches
  it off again.
- `printer.cfg`: includes the two files above and holds your calibrated
  values; `SAVE_CONFIG` writes here.

The four extruders share one extruder stepper; select the docked tool with
`ACTIVATE_EXTRUDER EXTRUDER=extruderN`. Tool-change macros are not part of
phase 1.

First run:

1. `PROBE_CALIBRATE`, then `SAVE_CONFIG`.
2. `BED_MESH_CALIBRATE`, then `SAVE_CONFIG`.

## Camera

The bridge runs the printer's stock mjpg-streamer on port 8080. In Mainsail
add a webcam with stream URL `http://<printer-ip>:8080/?action=stream` and
snapshot URL `http://<printer-ip>:8080/?action=snapshot`. Its output goes to
`/usr/data/logs/c5-camera.log`.

## Disconnects and restarts

- `FIRMWARE_RESTART` resets every MCU. The levelBoard reset lands in its
  boot stage. The bridge notices the unanswered identify, releases the
  board (`identify unanswered`, `wake ok`) and Klipper connects normally.
- Unplugging the USB cable disconnects Klipper; the heaters shut down
  because their MCU commands time out. After plugging it back in, run
  `FIRMWARE_RESTART`.

## Development

`printer/test_bridge.py` covers framing, boot-stage wake and the wake rule
(Linux, uses ptys): `cd printer && python3 -m pytest test_bridge.py`.

## License

GPL-3.0-or-later; see `LICENSE`. The framing code follows Klipper's
`klippy/msgproto.py`.

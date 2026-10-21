# c5-tunnel

Run Klipper, Moonraker and Mainsail for a FlashForge Creator 5 Pro on a
separate single-board computer (SBC). The printer's own Linux board keeps
only a serial bridge: it forwards its four MCU UARTs to the SBC over one USB
cable and serves the built-in camera. Stock `firmwareExe`, stock Klipper,
Moonraker, nginx and the touchscreen UI no longer run on the printer.

All four MCUs (mainBoardGD, heaterBoard, eBoard, levelBoard) must run
[Kalico](https://github.com/FlashForge-C5-Modding-Group/kalico) firmware. The
SBC runs Kalico from the same repository (branch `something`). klipper-c5 is
archived.

```
SBC (Klipper, Moonraker, Mainsail)
  /dev/ttyGS0..3  --USB gadget, 4x gser serial-->  printer usbfs bulk endpoints
                                                   c5_bridge.py + urb_reader
                                                     ttyS2  mainBoardGD  230400
                                                     ttyS4  heaterBoard  230400
                                                     ttyS5  eBoard       460800
                                                     ttyS7  levelBoard   230400
```

Status: phase 2 (tool changes and tool calibrations). Vibration
calibrations and touchscreen streaming are later phases.

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

### Automatic install

As your normal user (not root):

```sh
curl -fsSL https://raw.githubusercontent.com/FlashForge-C5-Modding-Group/opencreator-tunnel/main/pi/install.sh | sh
```

It first asks which printer the Pi is connected to, Creator 5 or Creator 5 Pro. To skip the question, add
`sh -s -- --model creator5` or `--model creator5pro`; `pi/install-existing-kiauh.sh` takes the same option. The
answer goes in `printer.model.cfg`: only a Creator 5 Pro gets the chamber heater and the chamber and aux fans
(`printer.pro.cfg`). Run the installer again to change it. If the file is missing, the updater writes the Pro version,
so installs from before the question keep the hardware they had.

`pi/install.sh` downloads and installs, in order:

1. **This repo** into `~/opencreator-tunnel`.
2. **Fluidd**: the latest release goes into `~/fluidd`, served by nginx on port 80. `fluidd-config` goes into `~/fluidd-config`.
3. **Moonraker** into `~/moonraker`. If there is no `moonraker.conf`, the script writes one with update-manager entries for Fluidd, fluidd-config and the OpenCreator updater. It also links the `opencreator_3mf` component into Moonraker and adds `[opencreator_3mf]` to `moonraker.conf` (see [3MF uploads](#3mf-uploads)).
4. **Kalico** from `FlashForge-C5-Modding-Group/kalico`, branch `something`, into `~/klipper`, with `klipper.service` using the `~/printer_data` layout.

Then it sets up the printer:

5. **Config**: if `printer.cfg` doesn't exist yet, it copies in `pi/config` plus the Kalico overlay (`printer.motor.cfg`, `printer.kalico_mpc.cfg`). An existing config is left alone.
6. **Updater**: installs the OpenCreator updater service.
7. **Tunnel**: runs `setup-gadget.sh` and starts the services. On a first install, reboot so the USB gadget comes up; Klipper starts after it.

Running it again updates the git checkouts and Fluidd, and leaves your config alone.

For detailed pip output, including NumPy build steps during "Preparing metadata",
use `--verbose` (or `-v`). Add `--skip-cython` to skip compiling Cython itself:

```sh
curl -fsSL https://raw.githubusercontent.com/FlashForge-C5-Modding-Group/opencreator-tunnel/main/pi/install.sh | sh -s -- --skip-cython --verbose
```

`--skip-cython` sets `NO_CYTHON_COMPILE=true`: Cython's Python implementation
remains available for dependencies such as NumPy. NumPy's own compilation still
runs. These options can also be enabled with `SKIP_CYTHON=1` and `VERBOSE=1`.
Verbosity must be selected when starting the installer.

Build temporary files default to `~/.cache/opencreator/build-tmp` on the SD card,
avoiding the small RAM-backed `/tmp` on many SBC images. Set `TMPDIR` to choose
another directory. NumPy source builds default to two workers; set
`NUMPY_BUILD_JOBS` to a positive integer to change the limit.

The installer installs `libopenblas-dev` and links NumPy against OpenBLAS,
avoiding its large bundled LAPACK fallback sources. On 32-bit ARM, it also omits
optional CPU dispatch variants to avoid the NEON code that crashed GCC 14;
baseline kernels are still built, with fewer CPU-specific optimizations.

Environment overrides: `TUNNEL_REPO`, `TUNNEL_BRANCH`, `KALICO_REPO`, `KALICO_BRANCH`, `MOONRAKER_REPO`, `FLUIDD_ZIP`, `FLUIDD_CONFIG_REPO`, `SKIP_CYTHON`, `VERBOSE`, `NUMPY_BUILD_JOBS`, `TMPDIR`.

### Manual install

2. Copy `pi/` to the SBC and run `sudo sh pi/setup-gadget.sh`, then reboot.
   `ls -l /dev/ttyGS0 /dev/ttyGS1 /dev/ttyGS2 /dev/ttyGS3` must list four
   devices.
3. Install Klipper, Moonraker and Mainsail with
   [KIAUH](https://github.com/dw-0/kiauh). For Klipper, use the custom
   repository `https://github.com/FlashForge-C5-Modding-Group/kalico`, branch
   `something`. It provides the `[mclib]` module that configures the
   mainBoardGD motors (currents, microstepping, resonance damping) and the
   other Creator 5 modules; upstream Klipper lacks them.
   Moonraker's update manager may report the fork as unofficial; that is
   cosmetic.
4. Copy `pi/config/*.cfg` to `~/printer_data/config/` (keep the
   `mainsail.cfg` that KIAUH installed) and restart Klipper.

`setup-gadget.sh` installs `c5-tunnel-gadget.service` (creates the gadget
at boot) and a `klipper.service` drop-in so Klipper starts after it.
Connect the USB cable; the printer log then shows `link up` for all four
ports and Mainsail reports `Ready`.

## Configuration

- `c5_hardware.cfg`: MCUs, steppers, probe, heaters, fans, sensors, all
  transcribed from the stock 1.9.9 configuration. Sections that need
  FlashForge-only Klipper modules are left out.
- `c5_macros.cfg`: `M106`/`M107`/`M900`, gear-stepper building blocks,
  `M104`/`M109`/`SET_HEATER_TEMPERATURE` wrappers that switch on the 24 V
  heater rail (`DC24V_CTL`) as stock Klipper does (the idle timeout switches
  it off again), and the Mainsail park position, kept clear of the docks.
- `c5_toolchanger.cfg`: `T0`-`T3`, `TOOL_DROP` and the stock tool
  calibrations. Results are stored in `c5_variables.cfg`.
- `printer.cfg`: includes `mainsail.cfg` and the files above and holds your
  calibrated values; `SAVE_CONFIG` writes here.

Tool changes follow the stock grab and release sequences. `T<n>` lifts Z by
1 mm, puts the held tool back in its dock, picks up tool `n`, applies its
offsets, activates `extruder<n>` (so `M104`/`M109` without `T` heat the
active tool) and stays by the docks, 1 mm above the old height; like stock,
the slicer's next moves take the new tool back to the print. A failed grab
or release stops with an error; there are no automatic retries.

### Filament loading

Insert filament manually until AFC reports the toolhead sensor loaded.
`C5_PREPARE_LOAD_T0` through `C5_PREPARE_LOAD_T3` select and heat a tool for
insertion (optional `TEMP=...`, default 220 °C).

`C5_LOAD_FILAMENT LENGTH=20` heats and loads the physically attached tool,
including when run without parameters from a macro button. The mount sensors
must report a valid attached tool matching the active extruder; the command
never selects a tool implicitly. `TEMP=...` sets the loading temperature.
Without it, a heater target within 150–300 °C is reused; otherwise the loader
uses 220 °C, never the measured room temperature. It sets the target and waits
for at least target minus 2 °C before feeding, even if cold extrusion is
enabled. The screen's “Load Selected Tool” button uses this command;
“Load Attached Filament” calls the same heating path directly.

For explicit selection, use `C5_LOAD_FILAMENT TOOL=2 TEMP=255 LENGTH=20`,
or `C5_LOAD_FILAMENT TOOLS=0,2 TEMP0=220 TEMP2=255 LENGTH=20`.
`TEMP` supplies a common temperature (default 220 °C); `TEMP0` through
`TEMP3` override it per tool. Explicit loading temperatures must be finite
and within 150–300 °C. Insert filament into every requested tool first.
The mounted requested tool is loaded first; a mounted tool not in the list
must be safely cooled and docked before loading other tools.

An idle load applies the calibrated automatic nozzle Z offset, purges over
the rear-right bucket, cools at the wiper, turns the heater off, and docks.
The chosen loading temperature is passed to the existing `C5_COOL_FOR_DOCK`
command; its `max(0, HOTEND - 100)` cooldown calculation is unchanged.
During a paused print, loading without `TOOL`/`TOOLS` heats and waits, then
feeds in place while preserving the paused G-code state. It does not select
a tool, travel, cool, or dock. Explicit tool selection requires an idle print.
Existing length limits are unchanged: use `LENGTH=20` for the final purge;
the attached-tool helper accepts at most 80 mm.

### First-run calibration

First run:

1. Dock positions, for each tool `n` (0-3): `M84`, push the carriage onto
   the docked tool until it latches, then `CALIBRATE_TOOL_DOCK TOOL=n`. The
   tool is pulled out, the X/Y endstops are probed, and the tool is docked
   again.
2. Nozzle offsets: remove the build plate, `G28`, `CALIBRATE_TOOL_OFFSETS`.
   It first checks that the plate is off (stock's probe comparison), then
   heats the bed to 65 °C and waits 60 s; each docked tool is then
   picked up, held at 120 °C and centred on the bed's eddy-current station.
   Defaults are in `_TC_SETTINGS` (`c5_toolchanger.cfg`); `BED=`, `SOAK=`,
   `NOZZLE=` override them per run, and `CLEAN=1` adds stock's nozzle clean
   (220 °C, purge, wipe, cool to 120 °C). Refit the plate.
3. `G28`, `T0`, then `PROBE_CALIBRATE` and `SAVE_CONFIG`. Tool offsets are
   relative to T0, so this sets the Z height of every tool. Per-tool Z fine
   tune: `SAVE_VARIABLE VARIABLE=t1_z_fine VALUE=0.02` (mm, T1-T3).
4. `BED_MESH_CALIBRATE`, then `SAVE_CONFIG`.

## Camera

The bridge runs the printer's stock mjpg-streamer on port 8080. In Mainsail
add a webcam with stream URL `http://<printer-ip>:8080/?action=stream` and
snapshot URL `http://<printer-ip>:8080/?action=snapshot`. Its output goes to
`/usr/data/logs/c5-camera.log`.

## 3MF uploads

Moonraker accepts sliced 3MF files (OrcaSlicer or Bambu Studio "Export plate
sliced file", `.gcode.3mf`) uploaded to the gcodes folder, including uploads
with "print" set. The `opencreator_3mf` component in `pi/moonraker` repacks the
upload as a UFP, so Moonraker's own UFP handling extracts it: `part.gcode.3mf`
is stored as `part.gcode`, with the plate preview as its thumbnail, and the
3MF itself is not kept.

- Only the lowest-numbered sliced plate (`Metadata/plate_N.gcode`) is used.
  Export one plate per file.
- A 3MF with no sliced G-code (a project or model file) is rejected.
- The G-code must be sliced for this printer; nothing in it is converted.
- Only uploads are handled. A 3MF copied into `gcodes` by other means is
  ignored.

The component is a symlink in `~/moonraker/moonraker/components`, so
Moonraker's checkout stays unmodified and its updates keep working. It needs
`[opencreator_3mf]` in `moonraker.conf`; `install.sh` adds both. To set it up
by hand:

```sh
ln -sf ~/opencreator-tunnel/pi/moonraker/opencreator_3mf.py \
    ~/moonraker/moonraker/components/opencreator_3mf.py
printf '\n[opencreator_3mf]\n' >> ~/printer_data/config/moonraker.conf
sudo systemctl restart moonraker
```

## Slicer time estimates

OrcaSlicer's FlashForge Creator 5 profiles set **File start G-code** to a
FlashForge-style estimate line (`{print_time_day}d {print_time_hour}h ...`).
Orca also writes that setting, unfilled, into the config block at the end of
the file. Moonraker takes the first `; estimated printing time` it finds in the
last 1 MiB, so it reads the template, finds no numbers and stores
`estimated_time: 0`.

The fix is to clear **Printer settings → Machine G-code → File start G-code**
in Orca. For files sliced with the setting still in place, `opencreator_3mf`
also changes `estimated printing time` to `estimated_printing_time` inside any
`; *_gcode = ` setting line in the footer of an uploaded G-code (or of the
G-code inside a 3MF). The change is in place and the same length, and Moonraker
then reads the real `; estimated printing time (normal mode) = ...` line.
Files already on the printer keep `0` until uploaded again.

## Fluidd theme

`pi/config/.fluidd-theme/custom.css` is a plain dark OpenCreator theme for
Fluidd: flat dark-grey surfaces with thin borders (no gradients, glows or
blur), the white OpenCreator logo in the top bar and as the background
watermark, and the primary color picked in Fluidd's theme settings (orange
by default) only for the active menu item and selections. Fluidd loads it from
`~/printer_data/config/.fluidd-theme/`. New installs get it with the config,
and the updater adds it to existing ones. `install.sh` also sets Fluidd's
accent color to `#FF6A2B` unless you already chose a color other than the
default. It also gives Fluidd the OpenCreator macro layout from
`pi/fluidd/macros.json` (On / Off, Calibrations, Toolhead and Misc) if Fluidd
has no macro layout yet.

- The theme is for dark mode (Settings → Theme → Dark). Light mode only gets
  the logo.
- The logos are embedded in the CSS, generated from the docs logo
  (`OpenCreator/docs/assets/logo.svg`).
- To go back to stock Fluidd, delete or rename `.fluidd-theme/custom.css` and
  reload the page.

## OpenCreator Updater

`pi/opencreator-updater/install.sh` sets up the updater for the checkout it
is in, wherever that is and whichever user runs it: `opencreator-updater.service`,
the `moonraker.asvc` entry and the `[update_manager opencreator-updater]`
section in `moonraker.conf`. The section is added if missing, or corrected in
place (path, origin, branch, duplicate options; backup
`moonraker.conf.bak-updater`). The updater always follows `main` of
`FlashForge-C5-Modding-Group/opencreator-tunnel`. Overrides: `OC_REPO_DIR`,
`OC_DATA` (default `~/printer_data`), `TUNNEL_REPO`, `TUNNEL_BRANCH`.

### Updater repair

If Fluidd or Mainsail shows "No git repo detected at configured path",
"Failed to detect git branch" or "Failed to detect repo url" for
`opencreator-updater`, run as your normal user:

```sh
sh <path-to-opencreator-tunnel>/pi/opencreator-updater/repair.sh
```

It works on the tree it is in, git or not:

- A copied tree without git gets a repository started in place; a broken
  `.git` is moved to `.git.broken-<date>`.
- Files owned by another user are given back (needs `sudo`).
- The origin is set to the official repository and the checkout is moved onto
  `main`, tracking `origin/main`. Local edits to tracked files are stashed,
  not discarded (`git stash pop` brings them back).
- Then it runs `install.sh` and restarts Moonraker.

If Moonraker does not start because of a duplicate option in
`[update_manager opencreator-updater]`, running `repair.sh` also fixes that;
restart Moonraker afterwards with `sudo systemctl restart moonraker`.

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

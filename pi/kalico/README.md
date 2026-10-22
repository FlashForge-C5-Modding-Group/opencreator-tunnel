# Kalico notes for the tunneled Pi

Every install runs Kalico, so `pi/config` (including `printer.motor.cfg`) is
written for it; this folder only holds `printer.kalico_mpc.cfg`.

The tunneled printer.cfg sets `pressure_advance_smooth_time: 0.06` on all
four logical extruders. The shared eboard step pin has about 1086 steps/mm
(16 microsteps, 6.5:1 gearing, 19.15 mm rotation distance). With PA 0.4, the
old 0.02 second smoothing generated bursts near 240,000 steps/s and caused
`Stepper too far in past`. The longer smoothing window spreads the PA motion
without lowering XY velocity or acceleration. It may change corner extrusion,
so inspect print quality and retune PA if needed. A slicer command that
explicitly sets `SMOOTH_TIME` will override this setting.

The Kalico `MANUAL_STEPPER` parser requires an integer for
`STOP_ON_ENDSTOP`. The unlock macro in `printer.motor.cfg` therefore uses `STOP_ON_ENDSTOP=1`,
which stops on the gear-stepper endstop and requires it to trigger.
The old `SYNC=0` was removed from that command because Kalico's homing
branch does not use `SYNC`; it waits for the endstop move to finish.

G28 intentionally homes X and Y one at a time, with a retract and slower
second pass on each axis. `safe_z_home` also makes a 10 mm clearance hop at
3 mm/s before an unhomed move, which takes several seconds. The homing
start delay in Kalico is only 1 ms. No homing speed or clearance was changed
here because those movements protect the attached tool and nozzle.

## MPC hotend control

MPC is opt-in and Kalico-only. In the copied `printer.cfg`, uncomment
`[include printer.mpc.cfg]` and `[include printer.kalico_mpc*.cfg]` (both
are commented out in `pi/config/printer.cfg`). Keep them after
`printer.base.cfg`, which includes `AFC/AFC.cfg`, to wrap AFC's `M109`.

1. Copy `pi/kalico/printer.kalico_mpc.cfg` into `~/printer_data/config/`;
   it enables MPC on all four hotends. Never install it on stock Klipper.
2. Remove each extruder's `control = pid` from `printer.cfg`'s `SAVE_CONFIG`
   block, since saved values override includes.
3. Restart and run `C5_MPC_CALIBRATE` (all tools) or
   `C5_MPC_CALIBRATE TOOL=n`. `TARGET=` defaults to 250 °C; calibration saves
   once at the end unless `SAVE=0`.

Updated shared Kalico passes Orca material metadata for the initial tool
on automatic virtual-SD start, without Orca edits. MPC must be enabled and
calibrated first; missing/unknown materials use configured defaults, not
filament sensing. Explicit `C5_PRINT_START` in the header bypasses automatic
start/material injection; multi-tool preparation still needs `TOOLS=` and
materials. See the canonical
[`MPC guide`](../../../opencreator-fs/config-docs/CREATOR5_MPC.md)
for bounded header/trailer handling, manual material parameters, calibration,
temperature waits and PID rollback.

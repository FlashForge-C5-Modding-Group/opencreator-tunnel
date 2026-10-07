# Kalico config overlay for the tunneled Pi

Copy the normal `pi/config` tree to the Pi's `~/printer_data/config/`, then
copy `pi/kalico/printer.motor.cfg` over its `printer.motor.cfg`. Restart
Klipper after replacing the file. This overlay is for Kalico; it does not
change the stock Klipper-C5 configuration.

The tunneled printer.cfg sets `pressure_advance_smooth_time: 0.06` on all
four logical extruders. The shared eboard step pin has about 1086 steps/mm
(16 microsteps, 6.5:1 gearing, 19.15 mm rotation distance). With PA 0.4, the
old 0.02 second smoothing generated bursts near 240,000 steps/s and caused
`Stepper too far in past`. The longer smoothing window spreads the PA motion
without lowering XY velocity or acceleration. It may change corner extrusion,
so inspect print quality and retune PA if needed. A slicer command that
explicitly sets `SMOOTH_TIME` will override this setting.

The Kalico `MANUAL_STEPPER` parser requires an integer for
`STOP_ON_ENDSTOP`. The unlock macro therefore uses `STOP_ON_ENDSTOP=1`,
which stops on the gear-stepper endstop and requires it to trigger.
The old `SYNC=0` was removed from that command because Kalico's homing
branch does not use `SYNC`; it waits for the endstop move to finish.

G28 intentionally homes X and Y one at a time, with a retract and slower
second pass on each axis. `safe_z_home` also makes a 10 mm clearance hop at
3 mm/s before an unhomed move, which takes several seconds. The homing
start delay in Kalico is only 1 ms. No homing speed or clearance was changed
here because those movements protect the attached tool and nozzle.

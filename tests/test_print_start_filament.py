import pathlib
import textwrap
import unittest

import jinja2


CONFIG = (pathlib.Path(__file__).resolve().parents[1] / "pi" /
          "config" / "printer.creator5.cfg")
FILAMENT_CONFIG = CONFIG.with_name("printer.filament.cfg")


def macro_template(config, name):
    text = config.read_text(encoding="utf-8")
    section = text.split("[gcode_macro %s]\n" % name, 1)[1]
    section = section.split("\n[", 1)[0]
    gcode = section.split("gcode:\n", 1)[1]
    return jinja2.Environment(
        "{%", "%}", "{", "}", undefined=jinja2.StrictUndefined
    ).from_string(textwrap.dedent(gcode))


def render_start(tool, tools, present=(), absent_sensors=(), sensors=None):
    printer = {
        "output_pin flow_calibration": {"value": 0.},
        "output_pin purge": {"value": 0.},
        "output_pin bed_leveling": {"value": 1.},
    }
    for index in range(4):
        if index not in absent_sensors:
            printer["filament_switch_sensor extruder%d_tool_start" % index] = {
                "filament_detected": index in present}
    if sensors is not None:
        for name in tuple(printer):
            if name.startswith("filament_switch_sensor "):
                del printer[name]
        printer.update(sensors)
    messages = []
    def raise_error(message):
        raise ValueError(message)
    output = macro_template(CONFIG, "_C5_PRINT_START_AFTER_BOOT").render({
        "printer": printer,
        "params": {"TOOL": str(tool), "TOOLS": tools, "HOTEND": "220",
                   "FLOW_CALIBRATION": "0", "PURGE": "0"},
        "rawparams": "",
        "action_raise_error": raise_error,
        "action_respond_info": lambda message: messages.append(message) or "",
    })
    return output, messages


ALIAS_SENSOR = "filament_switch_sensor extruder0_tool_start"
LEGACY_SENSOR = "filament_switch_sensor extruder_tool_start"
T0_SENSOR_CASES = (
    ("alias detected", {ALIAS_SENSOR: {"filament_detected": True}}, True),
    ("alias empty", {ALIAS_SENSOR: {"filament_detected": False}}, False),
    ("legacy detected", {LEGACY_SENSOR: {"filament_detected": True}}, True),
    ("legacy empty", {LEGACY_SENSOR: {"filament_detected": False}}, False),
    ("missing", {}, False),
    ("alias empty overrides legacy detected", {
        ALIAS_SENSOR: {"filament_detected": False},
        LEGACY_SENSOR: {"filament_detected": True},
    }, False),
    ("alias detected overrides legacy empty", {
        ALIAS_SENSOR: {"filament_detected": True},
        LEGACY_SENSOR: {"filament_detected": False},
    }, True),
)


class PrintStartFilamentTests(unittest.TestCase):
    def test_t0_sensor_selection_fails_closed_and_prefers_alias(self):
        for name, sensors, allowed in T0_SENSOR_CASES:
            with self.subTest(name=name):
                output, messages = render_start(0, "0", sensors=sensors)
                if allowed:
                    self.assertIn("C5_HOME_FOR_PRINT", output)
                    self.assertNotIn("CANCEL_PRINT", output)
                    self.assertEqual(messages, [])
                else:
                    self.assertIn("CANCEL_PRINT", output)
                    self.assertNotIn("C5_HOME_FOR_PRINT", output)
                    self.assertNotIn("SET_HEATER_TEMPERATURE", output)
                    self.assertIn("T0", messages[0])

    def test_missing_selected_tool_cancels_before_motion_and_heat(self):
        output, messages = render_start(3, "3", set())
        self.assertIn("CANCEL_PRINT", output)
        self.assertNotIn("C5_HOME_FOR_PRINT", output)
        self.assertNotIn("SET_HEATER_TEMPERATURE", output)
        self.assertIn("T3", messages[0])

    def test_unused_empty_tools_do_not_block_print(self):
        output, messages = render_start(3, "3", {3})
        self.assertIn("C5_HOME_FOR_PRINT", output)
        self.assertNotIn("CANCEL_PRINT", output)
        self.assertEqual(messages, [])
        # The mapping reset lives in C5_PRINT_STOP so a SET_MAP survives.
        self.assertNotIn("RESET_AFC_MAPPING", output)

    def test_cancel_does_not_change_afc_mapping(self):
        output, _ = render_start(3, "3", set())
        self.assertNotIn("RESET_AFC_MAPPING", output)

    def test_all_selected_tools_must_have_filament(self):
        output, messages = render_start(3, "0,3", {3})
        self.assertIn("CANCEL_PRINT", output)
        self.assertIn("T0", messages[0])
        self.assertNotIn("T3", messages[0])

    def test_missing_sensor_fails_closed(self):
        output, messages = render_start(3, "3", {3}, absent_sensors={3})
        self.assertIn("CANCEL_PRINT", output)
        self.assertNotIn("C5_HOME_FOR_PRINT", output)
        self.assertIn("T3", messages[0])


def render_runout(name, tool, sensors, temp=220):
    printer = {
        "gcode_macro C5_FILAMENT_RUNOUT": {
            "tool": tool, "temp": temp, "load_length": 80,
        },
        "extruder": {"target": 215.},
        "extruder1": {"target": 216.},
        "extruder2": {"target": 217.},
        "extruder3": {"target": 218.},
    }
    printer.update(sensors)
    return macro_template(FILAMENT_CONFIG, name).render(
        printer=printer, params={})


class RunoutFilamentTests(unittest.TestCase):
    def test_prompt_offers_t0_only_when_selected_sensor_detects_filament(self):
        for name, sensors, detected in T0_SENSOR_CASES:
            with self.subTest(name=name):
                output = render_runout("_OC_RUNOUT_PROMPT", 3, sensors)
                self.assertEqual(
                    "_OC_RUNOUT_SWITCH TO=0" in output, detected)
                self.assertNotIn("_OC_RUNOUT_SWITCH TO=3", output)
                self.assertNotIn("_OC_RUNOUT_SWITCH TO=1", output)
                self.assertNotIn("_OC_RUNOUT_SWITCH TO=2", output)

    def test_prompt_keeps_nonzero_detection_and_excludes_loaded_current_tool(self):
        for detected in (False, True):
            with self.subTest(detected=detected):
                sensors = {
                    "filament_switch_sensor extruder1_tool_start": {
                        "filament_detected": detected},
                }
                output = render_runout("_OC_RUNOUT_PROMPT", 0, sensors)
                self.assertEqual("_OC_RUNOUT_SWITCH TO=1" in output, detected)
        output = render_runout("_OC_RUNOUT_PROMPT", 1, {
            "filament_switch_sensor extruder1_tool_start": {
                "filament_detected": True},
        })
        self.assertNotIn("_OC_RUNOUT_SWITCH TO=1", output)

    def test_t0_load_continuation_uses_sensor_guard_and_physical_heater(self):
        for name, sensors, allowed in T0_SENSOR_CASES:
            with self.subTest(name=name):
                output = render_runout("_OC_RUNOUT_LOAD_CONTINUE", 0, sensors)
                if allowed:
                    self.assertIn(
                        "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=220",
                        output)
                    self.assertIn(
                        "TEMPERATURE_WAIT SENSOR=extruder MINIMUM=218", output)
                    self.assertIn("C5_TOOL_PURGE LENGTH=80", output)
                    self.assertIn("RESUME", output)
                    self.assertNotIn("HEATER=extruder0", output)
                    self.assertNotIn("SENSOR=extruder0", output)
                else:
                    self.assertIn("No filament detected in T0", output)
                    self.assertNotIn("SET_HEATER_TEMPERATURE", output)
                    self.assertNotIn("C5_TOOL_PURGE", output)
                    self.assertNotIn("G1 ", output)
                    self.assertNotIn("RESUME", output)

    def test_load_continuation_uses_physical_heater_target_when_temp_unset(self):
        output = render_runout("_OC_RUNOUT_LOAD_CONTINUE", 0, {
            ALIAS_SENSOR: {"filament_detected": True},
        }, temp=0)
        self.assertIn(
            "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=215.0", output)
        self.assertIn(
            "TEMPERATURE_WAIT SENSOR=extruder MINIMUM=213.0", output)

    def test_load_continuation_keeps_cold_tool_rejection(self):
        output = render_runout("_OC_RUNOUT_LOAD_CONTINUE", 0, {
            ALIAS_SENSOR: {"filament_detected": True},
        }, temp=140)
        self.assertIn("T0 has no print temperature to load at", output)
        self.assertNotIn("SET_HEATER_TEMPERATURE", output)
        self.assertNotIn("C5_TOOL_PURGE", output)
        self.assertNotIn("RESUME", output)

    def test_nonzero_load_continuation_keeps_detection_and_physical_heater(self):
        for detected in (False, True):
            with self.subTest(detected=detected):
                output = render_runout("_OC_RUNOUT_LOAD_CONTINUE", 1, {
                    "filament_switch_sensor extruder1_tool_start": {
                        "filament_detected": detected},
                })
                if detected:
                    self.assertIn(
                        "SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=220",
                        output)
                    self.assertIn("RESUME", output)
                else:
                    self.assertIn("No filament detected in T1", output)
                    self.assertNotIn("SET_HEATER_TEMPERATURE", output)
                    self.assertNotIn("RESUME", output)


class StopMacroMachine:
    """Execute real stop/cancel templates against the Creator 5/AFC boundary.

    Provider contracts: AFC_extruder.on_shuttle reads fresh pins and reports
    false on invalid sensors; Creator 5 UI status may be cached for a second.
    AFC_UNSELECT_TOOL resolves the selected physical extruder, not current_load,
    and falls back to the unavailable legacy command when it is not mounted.
    C5_TOOL_DOCK freshly validates sensors and is a no-op on an empty shuttle.
    """

    MACROS = {"C5_PRINT_STOP", "CANCEL_PRINT", "_OC_PRINT_START_REST"}

    def __init__(self, attached=None, selected="extruder", current_load=None,
                 homed="xyz", lower_bed=True, sensor_error=None,
                 cached_tool=None, state="printing", z=245.):
        self.attached = attached
        self.sensor_error = sensor_error
        self.cached_tool = cached_tool
        self.current_load = current_load
        self.status = {
            "toolhead": {
                "homed_axes": homed, "extruder": selected,
                "axis_maximum": {"z": 250.}, "position": {"z": z},
            },
            "print_stats": {"state": state},
            "output_pin lower_bed_on_end": {"value": float(lower_bed)},
        }
        self.heaters = [220., 215., 210., 205., 60.]
        self.fans = {0: 1., 2: 1., 3: 1.}
        self.messages = []
        self.commands = []
        self.docked = []
        self.z_moves = []
        self.mapping_reset = False
        self.motors_enabled = True
        self.end_tune_restored = False
        self.afc_unselect_count = 0
        self.afc_state = "printing"
        self.active_spool = "print-spool"
        self.relative = False
        self.saved_relative = False

    def on_shuttle(self, tool):
        # _sensor_state invalidates the UI cache even when validation fails.
        self.cached_tool = None
        return self.sensor_error is None and self.attached == tool

    def __getitem__(self, name):
        if name == "creator5_toolchanger":
            return {
                "active_tool": self.cached_tool,
                "sensor_error": self.sensor_error,
                "build_plate_detected": False,
            }
        if name == "AFC":
            return {"current_load": self.current_load}
        if name.startswith("AFC_extruder "):
            alias = name.split()[1]
            tool = int(alias.removeprefix("extruder") or "0")
            return {
                "on_shuttle": self.on_shuttle(tool),
                "mount_sensor_error": self.sensor_error,
            }
        return self.status[name]

    def __contains__(self, name):
        return (name in self.status or name in ("AFC", "creator5_toolchanger")
                or name in ("AFC_extruder extruder0", "AFC_extruder extruder1",
                            "AFC_extruder extruder2", "AFC_extruder extruder3"))

    def dock(self):
        if self.sensor_error:
            raise ValueError(self.sensor_error)
        if self.attached is not None:
            self.docked.append(self.attached)
            self.attached = None

    def execute(self, name):
        def raise_error(message):
            raise ValueError(message)

        if name in self.MACROS:
            # Klipper renders each nested macro when its command executes,
            # but completes that render before executing any of its G-code.
            rendered = macro_template(CONFIG, name).render(
                printer=self, params={}, rawparams="",
                action_raise_error=raise_error,
                action_respond_info=lambda text: self.messages.append(text) or "")
            for line in rendered.splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    self.execute(line)
            return
        words = name.split()
        command = words[0]
        if command in self.MACROS:
            self.execute(command)
            return
        self.commands.append(name)
        if command == "CANCEL_PRINT_BASE":
            self.status["print_stats"]["state"] = "cancelled"
        elif command == "TURN_OFF_HEATERS":
            self.heaters = [0.] * len(self.heaters)
        elif command == "M107":
            self.fans[int(words[1][1:]) if len(words) > 1 else 0] = 0.
        elif command == "AFC_UNSELECT_TOOL":
            self.afc_unselect_count += 1
            selected = self.status["toolhead"]["extruder"]
            tool = int(selected.removeprefix("extruder") or "0")
            if not self.on_shuttle(tool):
                raise ValueError('Unknown command: "UNSELECT_TOOL"')
            self.afc_state = "tool_dock"
            self.dock()
            self.active_spool = None
            self.afc_state = "idle"
        elif command == "C5_TOOL_DOCK":
            self.dock()
        elif command == "SAVE_GCODE_STATE":
            self.saved_relative = self.relative
        elif command == "G91":
            self.relative = True
        elif command == "RESTORE_GCODE_STATE":
            self.relative = self.saved_relative
        elif command == "G1":
            z = float(next(word[1:] for word in words if word.startswith("Z")))
            position = self.status["toolhead"]["position"]
            position["z"] = position["z"] + z if self.relative else z
            self.z_moves.append(position["z"])
        elif command == "C5_LOWER_BED":
            self.status["toolhead"]["position"]["z"] = 250.
            self.z_moves.append(250.)
        elif command == "RESET_AFC_MAPPING":
            self.mapping_reset = True
        elif command == "M84":
            self.motors_enabled = False
        elif command == "C5_PRINT_END_TUNE":
            self.end_tune_restored = True
        elif command not in ("M400", "STEPPER_RESONANCE_DAMP_DISABLE",
                             "SET_PAUSE_NEXT_LAYER", "SET_PAUSE_AT_LAYER"):
            raise AssertionError("Unmodelled command: %s" % name)


class PrintStopTests(unittest.TestCase):
    def assert_shutdown(self, machine):
        self.assertEqual(machine.heaters, [0., 0., 0., 0., 0.])
        self.assertEqual(machine.fans, {0: 0., 2: 0., 3: 0.})

    def assert_cleanup(self, machine):
        self.assert_shutdown(machine)
        self.assertTrue(machine.mapping_reset)
        self.assertFalse(machine.motors_enabled)
        self.assertTrue(machine.end_tune_restored)

    def test_missing_plate_cancel_with_empty_shuttle_finishes_cleanup(self):
        machine = StopMacroMachine()
        machine.execute("_OC_PRINT_START_REST")
        self.assertEqual(machine.status["print_stats"]["state"], "cancelled")
        self.assertIn("build plate not detected", machine.messages[0])
        self.assert_cleanup(machine)
        self.assertEqual(machine.docked, [])
        self.assertEqual(machine.afc_unselect_count, 0)
        self.assertEqual(machine.z_moves, [250., 250.])

    def test_loaded_lane_is_not_evidence_of_an_attached_head(self):
        machine = StopMacroMachine(current_load="extruder0")
        machine.execute("C5_PRINT_STOP")
        self.assert_cleanup(machine)
        self.assertEqual(machine.afc_unselect_count, 0)
        self.assertEqual(machine.docked, [])

    def test_matched_heads_keep_afc_bookkeeping_even_without_a_loaded_lane(self):
        for tool, selected in enumerate(
                ("extruder", "extruder1", "extruder2", "extruder3")):
            for current_load in (None, "extruder%d" % tool):
                with self.subTest(tool=tool, current_load=current_load):
                    machine = StopMacroMachine(
                        attached=tool, selected=selected,
                        current_load=current_load)
                    machine.execute("C5_PRINT_STOP")
                    self.assert_cleanup(machine)
                    self.assertEqual(machine.docked, [tool])
                    self.assertEqual(machine.afc_unselect_count, 1)
                    self.assertEqual(machine.afc_state, "idle")
                    self.assertIsNone(machine.active_spool)

    def test_mismatched_selected_extruder_docks_the_physical_head(self):
        machine = StopMacroMachine(
            attached=3, selected="extruder", current_load="extruder0")
        machine.execute("C5_PRINT_STOP")
        self.assert_cleanup(machine)
        self.assertEqual(machine.docked, [3])
        self.assertEqual(machine.afc_unselect_count, 0)

    def test_stale_empty_ui_status_does_not_skip_attached_head(self):
        machine = StopMacroMachine(attached=0, cached_tool=None)
        machine.execute("C5_PRINT_STOP")
        self.assert_cleanup(machine)
        self.assertEqual(machine.docked, [0])
        self.assertEqual(machine.afc_unselect_count, 1)

    def test_stale_attached_ui_status_does_not_trigger_legacy_unselect(self):
        machine = StopMacroMachine(attached=None, cached_tool=0)
        machine.execute("C5_PRINT_STOP")
        self.assert_cleanup(machine)
        self.assertEqual(machine.docked, [])
        self.assertEqual(machine.afc_unselect_count, 0)

    def test_invalid_sensors_fail_closed_after_heaters_and_fans_stop(self):
        machine = StopMacroMachine(
            sensor_error="Conflicting dock/grab sensors; motion blocked")
        with self.assertRaisesRegex(ValueError, "Conflicting dock/grab"):
            machine.execute("C5_PRINT_STOP")
        self.assert_shutdown(machine)
        self.assertEqual(machine.docked, [])
        self.assertEqual(machine.afc_unselect_count, 0)
        self.assertFalse(machine.mapping_reset)
        self.assertTrue(machine.motors_enabled)

    def test_partial_homing_never_docks_and_only_lowers_a_homed_z(self):
        for homed, moves in (("", []), ("xy", []), ("z", [250.])):
            with self.subTest(homed=homed):
                machine = StopMacroMachine(attached=2, homed=homed)
                machine.execute("C5_PRINT_STOP")
                self.assert_cleanup(machine)
                self.assertEqual(machine.docked, [])
                self.assertEqual(machine.afc_unselect_count, 0)
                self.assertEqual(machine.z_moves, moves)

    def test_lower_bed_disabled_preserves_bounded_end_hop(self):
        machine = StopMacroMachine(attached=1, selected="extruder1",
                                   lower_bed=False, z=249.)
        machine.execute("C5_PRINT_STOP")
        self.assert_cleanup(machine)
        self.assertEqual(machine.z_moves, [250.])
        self.assertEqual(machine.docked, [1])

    def test_idle_cancel_does_not_run_stop_motion(self):
        machine = StopMacroMachine(attached=1, state="standby")
        machine.execute("CANCEL_PRINT")
        self.assertEqual(machine.status["print_stats"]["state"], "cancelled")
        self.assertEqual(machine.fans, {0: 0., 2: 0., 3: 0.})
        self.assertEqual(machine.z_moves, [])
        self.assertEqual(machine.docked, [])
        self.assertFalse(machine.mapping_reset)


if __name__ == "__main__":
    unittest.main()

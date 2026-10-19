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


if __name__ == "__main__":
    unittest.main()

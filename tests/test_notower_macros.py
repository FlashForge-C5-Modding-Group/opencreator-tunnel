import ast
import pathlib
import re
import textwrap
import unittest

import jinja2

CONFIG = (pathlib.Path(__file__).resolve().parents[1] / "pi" / "config" /
          "printer.creator5.cfg")
NOTOWER = "SET_GCODE_VARIABLE MACRO=_OC_NOTOWER "


class RaisedError(Exception):
    pass


def section(name):
    text = CONFIG.read_text(encoding="utf-8")
    body = text.split("[%s]\n" % name, 1)[1]
    return body.split("\n[", 1)[0]


def variables(body):
    found = {}
    for match in re.finditer(r"^variable_(\w+):(.*?)(?=^\S)", body + "\nx",
                             re.M | re.S):
        lines = [re.sub(r"\s+#.*$", "", line)
                 for line in match.group(2).splitlines()]
        lines = [line for line in lines if not line.strip().startswith("#")]
        found[match.group(1)] = ast.literal_eval("\n".join(lines).strip())
    return found


def render(name, printer, params):
    body = section("gcode_macro " + name)
    gcode = textwrap.dedent(body.split("gcode:\n", 1)[1])
    template = jinja2.Environment("{%", "%}", "{", "}").from_string(gcode)

    def fail(message):
        raise RaisedError(message)

    out = template.render(printer=printer, params=params, rawparams="",
                          action_raise_error=fail,
                          action_respond_info=lambda m: "",
                          **variables(body))
    return [line.strip() for line in out.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def start(params):
    printer = {
        "output_pin flow_calibration": {"value": 0.},
        "output_pin purge": {"value": 0.},
        "output_pin bed_leveling": {"value": 0.},
        "bed_mesh": {"profiles": {}},
        "creator5_toolchanger": {"build_plate_detected": True},
    }
    for heater in ("extruder", "extruder1", "extruder2", "extruder3"):
        printer["filament_switch_sensor %s_tool_start" % heater] = {
            "filament_detected": True}
    output = []
    for line in render("_C5_PRINT_START_AFTER_BOOT", printer, params):
        if line.split()[0] == "_OC_PRINT_START_REST":
            output.extend(render("_OC_PRINT_START_REST", printer, params))
        else:
            output.append(line)
    return output


class NotowerArmingTests(unittest.TestCase):
    def test_multi_tool_print_with_poop_arms_after_the_last_tool_selection(self):
        out = start({"TOOL": "0", "TOOLS": "0", "USED_TOOLS": "0,2",
                     "HOTEND": "220", "HOTEND2": "240", "POOP": "1.5",
                     "DENSITY2": "1.27"})
        arm = [line for line in out if line.startswith(NOTOWER)]
        self.assertEqual(arm, [
            NOTOWER + "VARIABLE=grams VALUE=1.5",
            NOTOWER + "VARIABLE=temps VALUE=[220.0,220.0,240.0,220.0]",
            NOTOWER + "VARIABLE=densities VALUE=[0.0,0.0,1.27,0.0]",
            NOTOWER + "VARIABLE=armed VALUE=1",
        ])
        armed = out.index(NOTOWER + "VARIABLE=armed VALUE=1")
        selections = [i for i, line in enumerate(out)
                      if line.startswith(("AFC_SELECT_TOOL", "LINE_PURGE"))]
        self.assertGreater(armed, max(selections))

    def test_single_tool_or_no_poop_never_arms(self):
        for params in ({"TOOL": "1", "TOOLS": "1", "HOTEND": "220", "POOP": "2"},
                       {"TOOL": "0", "TOOLS": "0", "USED_TOOLS": "0,1",
                        "HOTEND": "220"}):
            with self.subTest(params=params):
                self.assertFalse([line for line in start(params)
                                  if line.startswith(NOTOWER)])

    def test_poop_must_be_positive_grams(self):
        for poop in ("0", "-1", "lots"):
            with self.subTest(poop=poop):
                with self.assertRaises(RaisedError):
                    start({"TOOL": "0", "TOOLS": "0,1", "HOTEND": "220",
                           "POOP": poop})


class NotowerSwapTests(unittest.TestCase):
    def swap(self, armed, state):
        printer = {"gcode_macro _OC_NOTOWER": {"armed": armed},
                   "print_stats": {"state": state}}
        return render("_OC_TOOL_SWAP", printer, {"T": "2"})

    def test_purges_only_when_armed_and_printing(self):
        self.assertEqual(self.swap(1, "printing"),
                         ["C5_TOOL_SELECT T=2", "_OC_NOTOWER_PURGE T=2"])
        for armed, state in ((0, "printing"), (1, "paused"), (1, "standby")):
            with self.subTest(armed=armed, state=state):
                self.assertEqual(self.swap(armed, state), ["C5_TOOL_SELECT T=2"])


class NotowerPurgeTests(unittest.TestCase):
    def purge(self, target, **state):
        notower = variables(section("gcode_macro _OC_NOTOWER"))
        notower.update(state)
        printer = {
            "gcode_macro _OC_NOTOWER": notower,
            "configfile": {"settings": {"extruder1": {"filament_diameter": 1.75}}},
            "extruder1": {"target": target},
        }
        return render("_OC_NOTOWER_PURGE", printer, {"T": "1"})

    def extruded(self, out):
        return [float(line.split()[1][1:]) for line in out
                if line.startswith("G1 E") and not line.startswith("G1 E-")]

    def test_two_grams_of_pla_at_ten_cubic_mm_per_second(self):
        out = self.purge(150.0, grams=2.0, temps=[0.0, 230.0, 0.0, 0.0])
        # Fast trip to the bucket, heats there so ooze lands in it, then purges
        self.assertEqual(out[:3], [
            "C5_MOVE_TO_PURGE_BUCKET SPEED=300 ACCEL=20000",
            "SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=230.0",
            "TEMPERATURE_WAIT SENSOR=extruder1 MINIMUM=228.0"])
        moves = self.extruded(out)
        self.assertAlmostEqual(sum(moves), 670.6, delta=0.1)
        self.assertTrue(all(move <= 100.0 for move in moves))
        self.assertTrue(all(line.endswith(" F249") for line in out
                            if line.startswith("G1 E") and "E-" not in line))
        self.assertEqual(out[-3:], ["G1 E-1.0 F1200", "M400",
                                    "RESTORE_GCODE_STATE NAME=OC_NOTOWER"])

    def test_slicer_density_and_hotter_target_win(self):
        out = self.purge(245.0, grams=1.0, temps=[0.0, 230.0, 0.0, 0.0],
                         densities=[0.0, 1.27, 0.0, 0.0])
        self.assertIn("SET_HEATER_TEMPERATURE HEATER=extruder1 TARGET=245.0", out)
        self.assertAlmostEqual(sum(self.extruded(out)),
                               1000.0 / 1.27 / 2.40528, delta=0.1)


if __name__ == "__main__":
    unittest.main()

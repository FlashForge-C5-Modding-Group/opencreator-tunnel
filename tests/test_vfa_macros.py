import pathlib
import textwrap
import unittest

import jinja2

CONFIG = pathlib.Path(__file__).resolve().parents[1] / "pi" / "config" / "printer.vibration.cfg"


class Raised(Exception):
    pass


def raise_error(msg):
    raise Raised(msg)


def run(name, state="standby", **params):
    text = CONFIG.read_text(encoding="utf-8")
    gcode = text.split("[gcode_macro %s]\n" % name, 1)[1].split("\n[", 1)[0].split("gcode:\n", 1)[1]
    printer = {"print_stats": {"state": state}}
    for tool in range(1, 5):
        printer["gcode_button extruder_grab%d" % tool] = {"state": "RELEASED"}
        printer["gcode_button extruder_pos%d" % tool] = {"state": "PRESSED"}
    out = jinja2.Environment("{%", "%}", "{", "}").from_string(textwrap.dedent(gcode)).render(
        params=params, printer=printer, action_raise_error=raise_error)
    return [l.strip() for l in out.splitlines() if l.strip() and not l.strip().startswith("#")]


def steppers(lines, command):
    return [l.split("STEPPER=")[1].split()[0] for l in lines if l.startswith(command)]


class VfaMacroTest(unittest.TestCase):
    def test_both_motors_by_default(self):
        lines = run("STEPPER_RESONANCE_FACTORY_CALIBRATE")
        self.assertEqual(steppers(lines, "STEPPER_RESONANCE_AUTO_CALIBRATE"), ["stepper_x", "stepper_y"])

    def test_one_axis_leaves_the_other_alone(self):
        lines = run("STEPPER_RESONANCE_FACTORY_CALIBRATE", AXIS="Y")
        self.assertEqual(steppers(lines, "STEPPER_RESONANCE_AUTO_CALIBRATE"), ["stepper_y"])
        # X's damping isn't switched off either
        self.assertEqual(set(steppers(lines, "MCLIB_SET_RESONANCE_DAMP")), {"stepper_y"})

    def test_compensation_passes_the_axis_and_graphs_only_it(self):
        lines = run("VFA_COMPENSATION", AXIS="y", GRAPH="1")
        self.assertIn("STEPPER_RESONANCE_FACTORY_CALIBRATE AXIS=Y GRAPH=1", lines)
        # the "after" sweep is part of the calibration now, not a separate one
        self.assertEqual(steppers(lines, "STEPPER_RESONANCE_CURVE_TEST"), [])
        self.assertIn('RUN_SHELL_COMMAND CMD=oc_vfa_graph PARAMS="stepper_y"', lines)
        self.assertNotIn('RUN_SHELL_COMMAND CMD=oc_vfa_graph PARAMS="stepper_x"', lines)
        self.assertEqual(lines[-1], "SAVE_CONFIG")

    def test_both_axes_graph_both(self):
        lines = run("VFA_COMPENSATION", GRAPH="1")
        self.assertIn("STEPPER_RESONANCE_FACTORY_CALIBRATE AXIS=XY GRAPH=1", lines)
        self.assertIn('RUN_SHELL_COMMAND CMD=oc_vfa_graph PARAMS="stepper_x"', lines)
        self.assertIn('RUN_SHELL_COMMAND CMD=oc_vfa_graph PARAMS="stepper_y"', lines)

    def test_graph_is_passed_to_each_motors_calibration(self):
        lines = run("STEPPER_RESONANCE_FACTORY_CALIBRATE", AXIS="XY", GRAPH="1")
        self.assertIn("STEPPER_RESONANCE_AUTO_CALIBRATE STEPPER=stepper_x GRAPH=1", lines)
        self.assertIn("STEPPER_RESONANCE_AUTO_CALIBRATE STEPPER=stepper_y GRAPH=1", lines)
        lines = run("STEPPER_RESONANCE_FACTORY_CALIBRATE")
        self.assertIn("STEPPER_RESONANCE_AUTO_CALIBRATE STEPPER=stepper_x GRAPH=0", lines)

    def test_bad_axis_and_printing_are_refused(self):
        with self.assertRaises(Raised):
            run("VFA_COMPENSATION", AXIS="Z")
        with self.assertRaises(Raised):
            run("VFA_COMPENSATION", state="printing")

    def test_graph_wrapper_passes_axis_and_save(self):
        lines = run("VFA_COMPENSATION_GRAPH", AXIS="X", SAVE="0")
        self.assertEqual(lines, ["VFA_COMPENSATION GRAPH=1 AXIS=X SAVE=0"])


if __name__ == "__main__":
    unittest.main()

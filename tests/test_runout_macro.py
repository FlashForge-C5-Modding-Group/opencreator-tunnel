import pathlib
import textwrap
import unittest

import jinja2

CONFIG = pathlib.Path(__file__).resolve().parents[1] / "pi" / "config" / "printer.filament.cfg"


def render(**params):
    text = CONFIG.read_text(encoding="utf-8")
    gcode = text.split("[gcode_macro C5_FILAMENT_RUNOUT]\n", 1)[1].split("\n[", 1)[0].split("gcode:\n", 1)[1]
    template = jinja2.Environment("{%", "%}", "{", "}").from_string(textwrap.dedent(gcode))
    printer = {"extruder": {"target": 220., "can_extrude": True}}
    out = template.render(params=params, printer=printer, retract_length=100, retract_speed=10)
    return [l.strip() for l in out.splitlines() if l.strip()]


class RunoutMacroTest(unittest.TestCase):
    def test_backup_tool_switches_without_prompt(self):
        lines = render(TOOL="0", TO="2")
        self.assertIn("G1 E-100 F600", lines)
        self.assertIn("_OC_RUNOUT_SWITCH TO=2", lines)
        self.assertNotIn("_OC_RUNOUT_PROMPT", lines)
        # retract before the switch
        self.assertLess(lines.index("G1 E-100 F600"), lines.index("_OC_RUNOUT_SWITCH TO=2"))

    def test_no_backup_asks(self):
        lines = render(TOOL="0")
        self.assertIn("_OC_RUNOUT_PROMPT", lines)
        self.assertFalse(any(l.startswith("_OC_RUNOUT_SWITCH") for l in lines))


if __name__ == "__main__":
    unittest.main()

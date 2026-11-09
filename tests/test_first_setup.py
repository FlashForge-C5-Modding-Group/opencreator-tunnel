import pathlib
import re
import textwrap
import unittest

import jinja2

CONFIG = pathlib.Path(__file__).resolve().parents[1] / "pi" / "config" / "printer.misc.cfg"


def render(**params):
    text = CONFIG.read_text(encoding="utf-8")
    gcode = text.split("[gcode_macro _OC_FIRST_SETUP]\n", 1)[1].split("\n[", 1)[0].split("gcode:\n", 1)[1]
    template = jinja2.Environment("{%", "%}", "{", "}").from_string(textwrap.dedent(gcode))
    printer = {"save_variables": {"variables": {"c5_first_setup_step": 0}}}
    out = template.render(params=params, printer=printer)
    return [l.strip() for l in out.splitlines() if l.strip()]


def buttons(lines):
    return re.findall(r"prompt_footer_button ([^|]+)\|([^|\"]+)", "\n".join(lines))


class FirstSetupTest(unittest.TestCase):
    def test_welcome_says_it_starts_from_template_values(self):
        text = " ".join(render(STEP="0"))
        self.assertIn("starts from template values", text)
        self.assertNotIn("imported", text)

    def test_calibration_page_recommends_everything_in_order(self):
        lines = render(STEP="5")
        text = " ".join(lines)
        self.assertIn("action:prompt_begin Calibrate your printer", text)
        order = ["C5_CALIBRATE_OFFSETS", "CALIBRATE_INPUT_SHAPER", "VFA_COMPENSATION"]
        self.assertEqual([c for c in order if c in text], order)
        self.assertEqual(sorted(text.index(c) for c in order), [text.index(c) for c in order])
        # Dock positions only when docking gives trouble, after the rest
        docks = next(l for l in lines if "C5_CALIBRATE_TOOL_POSITION" in l)
        self.assertIn("Only if a tool has trouble docking", docks)
        self.assertGreater(text.index("C5_CALIBRATE_TOOL_POSITION"), text.index("VFA_COMPENSATION"))
        self.assertEqual(dict(buttons(lines)), {"Next": "_OC_FIRST_SETUP STEP=6"})

    def test_pages_and_their_switches(self):
        pages = {1: ("FLOW", "flow_calibration"), 2: ("PURGE", "purge"), 3: ("CLOG", "clog_detection"), 4: ("SOAK", "bed_soak")}
        for step, (param, pin) in pages.items():
            with self.subTest(step=step):
                b = dict(buttons(render(STEP=str(step))))
                self.assertEqual(b["Yes"], "_OC_FIRST_SETUP STEP=%d %s=1" % (step + 1, param))
                self.assertEqual(b["No"], "_OC_FIRST_SETUP STEP=%d %s=0" % (step + 1, param))
                self.assertIn("SET_PIN PIN=%s VALUE=1" % pin, render(STEP=str(step + 1), **{param: "1"}))

    def test_pressure_advance_page(self):
        self.assertIn("action:prompt_begin Pressure Advance Calibration", " ".join(render(STEP="1")))

    def test_last_page_thanks_and_finishes(self):
        lines = render(STEP="6")
        self.assertIn("SAVE_VARIABLE VARIABLE=c5_first_setup_done VALUE=1", lines)
        self.assertIn("first version of OpenCreator", " ".join(lines))
        self.assertIn("action:prompt_end", " ".join(render(STEP="7")))


if __name__ == "__main__":
    unittest.main()

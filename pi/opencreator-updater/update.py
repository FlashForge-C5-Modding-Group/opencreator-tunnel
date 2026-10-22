#!/usr/bin/env python3
"""OpenCreator Updater for a tunnel SBC: merges pi/config template updates into
printer_data/config, then pulls Klipper. The work is in opencreator_updater.py,
shared with the printer SoC (opencreator-fs keeps the original); see README.md
("OpenCreator Updater")."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import opencreator_updater  # noqa: E402

if __name__ == "__main__":
    sys.exit(opencreator_updater.main(["pi"] + sys.argv[1:]))

import pathlib
import unittest

HERE = pathlib.Path(__file__).resolve().parents[1] / "pi" / "opencreator-updater" / "opencreator_updater.py"
FS = pathlib.Path(__file__).resolve().parents[2] / "opencreator-fs" / "fs" / "usr" / "data" / "opencreator" / "startup" / "opencreator_updater.py"


class UpdaterCopyTest(unittest.TestCase):
    def test_same_as_opencreator_fs(self):
        if not FS.is_file():
            self.skipTest("opencreator-fs is not checked out next to opencreator-tunnel")
        self.assertEqual(HERE.read_bytes(), FS.read_bytes(),
                         "pi/opencreator-updater/opencreator_updater.py differs from opencreator-fs's copy")


if __name__ == "__main__":
    unittest.main()

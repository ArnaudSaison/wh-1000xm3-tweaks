"""The findings shown on the firmware map must line up with the firmware."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tests")]
FIRMWARE = ROOT / "firmware" / "HDX-2916_V0452_SPLC_with_stack.bin"

import double_press as dp  # noqa: E402
import firmware_viz as viz  # noqa: E402


@unittest.skipUnless(FIRMWARE.exists(), "firmware not downloaded")
class Findings(unittest.TestCase):
    def test_every_finding_resolves_inside_the_firmware(self):
        with tempfile.TemporaryDirectory() as folder:
            app = dp.app_from_image(FIRMWARE.read_bytes(), Path(folder) / "vm.app")
        findings = viz.build_findings(app)
        self.assertEqual(len(findings), len(viz.FINDINGS))
        for finding in findings:
            self.assertGreater(finding["len"], 0, finding["name"])
            self.assertLessEqual(finding["start"] + finding["len"], len(app.words), finding["name"])

    def test_known_tables_hold_what_their_names_say(self):
        with tempfile.TemporaryDirectory() as folder:
            app = dp.app_from_image(FIRMWARE.read_bytes(), Path(folder) / "vm.app")
        by_name = {f["name"]: f for f in viz.build_findings(app)}
        hold = by_name["Hold timing table"]["start"]
        self.assertEqual(app.words[hold + 6:hold + 9], (1500, 0, 0x13))
        pattern = by_name["Pattern list"]["start"] + 23 * 5
        steps = app.words[pattern] + app.delta
        self.assertEqual(app.words[steps:steps + 4], (0, 650, 1, 650))


if __name__ == "__main__":
    unittest.main()

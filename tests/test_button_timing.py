"""Rules and emulator rehearsal for button timings. Nothing here touches a device."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools"), str(ROOT / "tests")]
FIRMWARE = ROOT / "firmware" / "HDX-2916_V0452_SPLC_with_stack.bin"

import button_timing as bt  # noqa: E402
import dfu_sign  # noqa: E402
import double_press as dp  # noqa: E402
import test_double_press as flows  # noqa: E402


def levels(**values):
    return {row["level"] for row in bt.check(values)}


class Rules(unittest.TestCase):
    def test_tried_values_have_no_errors(self):
        self.assertNotIn("error", levels(long_ms=4000))

    def test_stock_is_refused_as_nothing_to_do(self):
        self.assertIn("error", levels())

    def test_range_limits(self):
        for values in ({"short_ms": 400}, {"short_ms": 5100, "long_ms": 9000}, {"long_ms": 1900}, {"long_ms": 15100},
                       {"input2_ms": 100}, {"inputs56_ms": 950}):
            self.assertIn("error", levels(**values), values)

    def test_holds_need_a_gap(self):
        self.assertIn("error", levels(short_ms=1500, long_ms=2400))
        self.assertIn("warn", levels(short_ms=1500, long_ms=3000))

    def test_short_values_warn(self):
        self.assertIn("warn", levels(short_ms=700, long_ms=4000))

    def test_double_press_alone_is_something_to_build_and_carries_a_warning(self):
        rows = bt.check({}, double_press=True)
        self.assertNotIn("error", {row["level"] for row in rows})
        self.assertTrue(any("has not run on one" in row["text"] for row in rows))
        self.assertIn("error", {row["level"] for row in bt.check({"long_ms": 1000}, double_press=True)})

    def test_bad_input_is_refused(self):
        for values in ({"nope": 1}, {"long_ms": "4"}, {"long_ms": 4000.5}, {"long_ms": True}):
            with self.assertRaises(dfu_sign.SignError):
                bt.check(values)


@unittest.skipUnless(FIRMWARE.exists(), "firmware not downloaded")
class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = FIRMWARE.read_bytes()

    def test_all_values_verify_and_rehearse(self):
        values = {"short_ms": 1000, "long_ms": 4000, "input2_ms": 200, "inputs56_ms": 600}
        output = bt.build(self.image, values)
        self.assertTrue(all(dfu_sign.verify(output).values()))
        self.assertTrue(all(row["ok"] for row in bt.rehearse(output, values)))

    def test_long_only_matches_the_build_tried_on_hardware(self):
        self.assertEqual(bt.build(self.image, {"long_ms": 4000}),
                         dfu_sign.patch(self.image, dfu_sign.pairing_edits(self.image, 4000)))

    def test_rehearsal_catches_a_file_with_other_values(self):
        output = bt.build(self.image, {"long_ms": 4000})
        self.assertFalse(all(row["ok"] for row in bt.rehearse(output, {"short_ms": 1000, "long_ms": 4000})))

    def test_double_press_with_a_4_s_hold_is_the_audited_file(self):
        """The same bytes as tools/double_press.py build --hold-ms 4000, whose result was reviewed."""
        output = bt.build(self.image, {"long_ms": 4000}, double_press=True)
        with tempfile.TemporaryDirectory() as folder:
            app = dp.app_from_image(self.image, Path(folder) / "vm.app")
        code, _ = dp.build_edits(app, dp._layout(self.image).covered)
        self.assertEqual(output, dfu_sign.patch(self.image, {**code, **dfu_sign.pairing_edits(self.image, 4000)}))
        self.assertTrue(all(dfu_sign.verify(output).values()))

    def test_double_press_builds_with_the_owners_timings_and_rehearses(self):
        values = {"short_ms": 1000, "long_ms": 3000}
        output = bt.build(self.image, values, double_press=True)
        self.assertTrue(all(dfu_sign.verify(output).values()))
        rows = bt.rehearse(output, values, double_press=True)
        self.assertEqual(sum(row["label"].startswith("Double press") for row in rows), 4)
        self.assertTrue(all(row["ok"] for row in rows), [row for row in rows if not row["ok"]])
        plain = bt.build(self.image, values)
        changed = {i for i, (a, b) in enumerate(zip(dp._vm_words(output), dp._vm_words(plain))) if a != b}
        self.assertTrue({0x92D0, 0x92D1} <= changed)
        self.assertFalse([w for w in changed if dp._layout(self.image).covered(w)])

    def test_double_press_alone_builds_on_sonys_timings(self):
        output = bt.build(self.image, {}, double_press=True)
        self.assertTrue(all(row["ok"] for row in bt.rehearse(output, {}, double_press=True)))

    def test_rehearsal_tells_a_file_with_the_patch_from_one_without(self):
        values = {"short_ms": 1000, "long_ms": 3000}
        plain, patched = bt.build(self.image, values), bt.build(self.image, values, double_press=True)
        self.assertTrue(all(row["ok"] for row in bt.rehearse(plain, values)))
        self.assertFalse(all(row["ok"] for row in bt.rehearse(plain, values, double_press=True)))
        self.assertFalse(all(row["ok"] for row in bt.rehearse(patched, values)))

    def test_error_values_do_not_build(self):
        with self.assertRaises(dfu_sign.SignError):
            bt.build(self.image, {"short_ms": 1500, "long_ms": 2000})

    def test_the_shorter_hold_keeps_the_real_handlers_and_double_click_working(self):
        edits = bt.edits_for(self.image, {"short_ms": 1000, "long_ms": 4000})
        with tempfile.TemporaryDirectory() as folder:
            app = dp.app_from_image(self.image, Path(folder) / "vm.app")
        patch, _ = dp.build_edits(app, dp._layout(self.image).covered)
        words = dp.apply_to_words(list(app.words), {**edits, **patch})
        m = flows.machine(words)
        flows.press(m, 0, 100)
        flows.press(m, 300, 100)
        m.run_until(dp.PAIRING_DELAY_MS + 1000)
        self.assertEqual(flows.state(m), 2)
        m = flows.machine(words)
        flows.press(m, 0, 1100)
        m.run_until(1500)
        self.assertEqual([d for d in m.delivered if d[1] == flows.APP and d[2] == 0x13], [(1000, flows.APP, 0x13)])
        self.assertEqual(flows.count(m, dp.BATTERY), 0)


if __name__ == "__main__":
    unittest.main()

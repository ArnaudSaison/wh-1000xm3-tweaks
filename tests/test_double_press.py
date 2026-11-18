"""Runs button presses through the real engine and app handlers, before and after the patch.

Skips when the firmware isn't present. Nothing here touches a device.
"""
import struct
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools")]
FIRMWARE = ROOT / "firmware" / "HDX-2916_V0452_SPLC_with_stack.bin"
GLOBAL_FIRMWARE = ROOT / "firmware" / "HDX-2916_V0452_with_stack.bin"

import dfu_sign  # noqa: E402
import double_press as dp  # noqa: E402
from xap_machine import Machine, Unmodelled  # noqa: E402
from xapanalyze import VmApp  # noqa: E402

ENGINE, APP, POWER, CONN = dp.ENGINE_TASK, dp.APP_TASK, dp.POWER_TASK, dp.CONNECTION_TASK
HANDLERS = dp.FLOW_HANDLERS
PLAY, LED, DISCOVERABLE = dp.PLAY_SOUND, dp.LED_PATTERN, dp.DISCOVERABLE
# The harness lives in tools/double_press.py, so the web UI can rehearse a build without the test suite.
machine, press, count, state = dp.flow_machine, dp.flow_press, dp.flow_count, dp.flow_state


@unittest.skipUnless(FIRMWARE.exists(), "firmware not downloaded")
class Flows(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = FIRMWARE.read_bytes()
        cls.layout = dp._layout(cls.image)
        with tempfile.TemporaryDirectory() as folder:
            cls.app = dp.app_from_image(cls.image, Path(folder) / "vm.app")
        cls.original = list(cls.app.words)
        cls.edits, cls.info = dp.build_edits(cls.app, cls.layout.covered)
        cls.patched = dp.apply_to_words(cls.original, cls.edits)

    def run_presses(self, words, presses, until=dp.PAIRING_DELAY_MS + 1000):
        m = machine(words)
        last = 0
        for at, held in presses:
            press(m, at, held)
            last = at + held
        m.run_until(last + until)
        return m

    # --- what the unpatched firmware does: these anchor the model to known behaviour ---

    def test_stock_short_click_announces_battery(self):
        m = self.run_presses(self.original, [(0, 100)])
        self.assertEqual(count(m, dp.BATTERY), 1)
        self.assertEqual(state(m), 1)

    def test_stock_double_click_is_two_battery_announcements(self):
        m = self.run_presses(self.original, [(0, 100), (300, 100)])
        self.assertEqual(count(m, dp.BATTERY), 2)
        self.assertEqual(count(m, PLAY, 8), 0)
        self.assertEqual(state(m), 1)

    def test_stock_hold_cancels_the_click(self):
        m = self.run_presses(self.original, [(0, 2000)])
        self.assertEqual(count(m, dp.BATTERY), 0)
        self.assertIn((1500, ENGINE, 1), m.delivered)
        self.assertEqual([d for d in m.delivered if d[1] == APP and d[2] == 0x13], [(1500, APP, 0x13)])

    # --- the patch ---

    def test_single_click_is_unchanged_and_the_timer_expires(self):
        m = self.run_presses(self.patched, [(0, 100)])
        self.assertEqual(count(m, dp.BATTERY), 1)
        self.assertEqual(state(m), 1)
        self.assertFalse(m.pending(ENGINE, dp.PRIVATE_ID))
        self.assertIn((600, ENGINE, dp.PRIVATE_ID), m.delivered)

    def test_double_click_enters_pairing(self):
        m = self.run_presses(self.patched, [(0, 100), (300, 100)])
        self.assertEqual(count(m, dp.BATTERY), 1)
        self.assertEqual(count(m, PLAY, 8), 1)
        self.assertEqual(count(m, dp.LEAVE_CONNECTIONS), 1)
        self.assertEqual(state(m), 2)
        self.assertEqual(m.read(m.read(0x581)), 1)  # as in Sony's own on-to-pairing path, 0x10BDD

    def test_pairing_steps_run_in_the_order_sonys_code_uses(self):
        """Sound, stop reconnecting, leave connections, then discoverable: 0x11E01 and 0x14C32
        both stop reconnecting before they leave the connections."""
        calls = [x[1] for x in dp.program() if x[0] == "call_abs"]
        tail = calls[calls.index(dp.STOP_RECONNECT):]
        self.assertEqual(tail, [dp.STOP_RECONNECT, dp.LEAVE_CONNECTIONS, dp.SEND_LATER, dp.SEND_LATER,
                                dp.PLAY_SOUND, dp.START_CONNECTION_MODE])
        m = self.run_presses(self.patched, [(0, 100), (300, 100)])
        seen = [(when, f) for when, f, al in m.calls
                if (f, al) in ((PLAY, 8), (DISCOVERABLE, 1)) or f == dp.LEAVE_CONNECTIONS]
        self.assertEqual(seen, [(400, dp.LEAVE_CONNECTIONS), (3400, PLAY), (3400, DISCOVERABLE)])

    def test_the_pairing_sound_waits_for_the_disconnect_announcement(self):
        """On the headset, the first version asked for the pairing sound and dropped the link in one go.
        The firmware then said "Bluetooth disconnected" (0x12FC0, in states 1 and 2), which cut the
        pairing sound off. Now nothing asks for a sound until three seconds after the link is dropped."""
        m = self.run_presses(self.patched, [(0, 100), (300, 100)], until=dp.PAIRING_DELAY_MS - 1)
        self.assertEqual(count(m, dp.LEAVE_CONNECTIONS), 1)
        self.assertEqual(count(m, PLAY, 8), 0)
        self.assertEqual(state(m), 1)                      # still the normal state while the link drops
        self.assertTrue(m.pending(ENGINE, dp.MARKER_ID))
        self.assertTrue(m.pending(APP, dp.CLICK_EVENT))
        m.run_until(400 + dp.PAIRING_DELAY_MS)
        self.assertEqual(count(m, PLAY, 8), 1)
        self.assertEqual(count(m, dp.BATTERY), 1)          # the posted click is not a battery click
        self.assertEqual(state(m), 2)
        self.assertFalse(m.pending(ENGINE, dp.MARKER_ID))

    def test_a_third_click_while_waiting_starts_pairing_at_once(self):
        m = self.run_presses(self.patched, [(0, 100), (300, 100), (1500, 100)], until=100)
        self.assertEqual(state(m), 2)
        self.assertEqual(count(m, PLAY, 8), 1)
        m.run_until(12_000)                                # the posted click lands in state 2 and does nothing
        self.assertEqual(count(m, PLAY, 8), 1)
        self.assertEqual(count(m, dp.BATTERY), 1)

    def test_if_the_state_changes_while_waiting_pairing_doesnt_start(self):
        """Powering off in the three seconds moves to state 3. The posted click is then not a
        battery click, the marker runs out, and later clicks behave normally."""
        m = self.run_presses(self.patched, [(0, 100), (300, 100)], until=500)
        m.write(m.read(0x588) + 1, 3)
        m.run_until(dp.MARKER_MS + 1000)
        self.assertEqual(count(m, PLAY, 8), 0)
        self.assertEqual(state(m), 3)
        self.assertFalse(m.pending(ENGINE, dp.MARKER_ID))
        m.write(m.read(0x588) + 1, 1)
        press(m, 20_000, 100)
        m.run_until(21_000)
        self.assertEqual(count(m, dp.BATTERY), 2)
        self.assertEqual(state(m), 1)

    def test_the_posted_click_is_a_message_sonys_code_also_posts(self):
        """0x9863 posts event 0x12 to the application task through 0x10A81, the same call and task."""
        texts = [x[2] for x in self.app.ins if 0x9863 <= x[0] <= 0x989C and not x[2].startswith("prefix")]
        self.assertIn("ld AL, #0x7dca", texts)
        self.assertIn("ld AH, #0x12", texts)
        self.assertEqual(self.app.call_target(self.app.ins[self.app.index[0x989B]]), dp.SEND_LATER)

    def test_link_state_doesnt_change_the_outcome(self):
        """Words 3 and 4 of 0x588 hold the link states. The click reaches the hook whatever they are,
        which is what a connected phone needs. The real values on a live link are not known."""
        for link_a in range(7):
            for link_b in range(7):
                outcomes = []
                for words in (self.original, self.patched):
                    m = machine(words)
                    m.write(m.read(0x588) + 3, link_a)
                    m.write(m.read(0x588) + 4, link_b)
                    press(m, 0, 100)
                    press(m, 300, 100)
                    m.run_until(dp.PAIRING_DELAY_MS + 1500)
                    outcomes.append((count(m, dp.BATTERY), count(m, PLAY, 8), state(m)))
                self.assertEqual(outcomes, [(2, 0, 1), (1, 1, 2)], (link_a, link_b))

    def test_pairing_times_out_through_the_firmwares_own_timer(self):
        m = self.run_presses(self.patched, [(0, 100), (300, 100)])
        self.assertTrue(m.pending(POWER, 3))

    def test_pairing_ends_after_five_minutes_and_clicks_work_again(self):
        m = self.run_presses(self.patched, [(0, 100), (300, 100)])
        m.handlers[POWER] = 0x10831
        self.assertEqual(state(m), 2)
        m.run_until(303_000)                               # pairing started at 3.4 s
        self.assertEqual(state(m), 2)
        m.run_until(304_000)
        self.assertEqual(state(m), 1)
        before = count(m, dp.BATTERY)
        press(m, 310_000, 100)
        m.run_until(311_000)
        self.assertEqual(count(m, dp.BATTERY), before + 1)
        press(m, 320_000, 100)
        press(m, 320_300, 100)
        m.run_until(325_000)
        self.assertEqual(state(m), 2)

    def test_patch_only_uses_instruction_forms_found_in_sonys_code(self):
        """Far calls are common. Far bra doesn't occur, so the patch must not need one."""
        far = [x for x in self.app.ins if self.app.call_target(x) is not None
               and abs(self.app.call_target(x) - x[0]) > 0x8000]
        self.assertGreater(len(far), 100)
        code = [a for a in self.edits if a not in dp.HOOK_WORDS]
        self.assertLess(max(code) - min(code), 0x8000)
        self.assertEqual(dp.foreign_shapes(self.app, self.edits), [])

    def test_the_form_check_catches_a_form_sony_never_uses(self):
        known = {shape for _, shape in dp.instruction_shapes(self.app.words, 0, dp.CODE_LIMIT)}
        self.assertNotIn((2, 0xE0), known)  # a bra with two prefix words
        entry = self.info["entry"]
        self.assertEqual(dp.foreign_shapes(self.app, {entry: 0x0100, entry + 1: 0x0200, entry + 2: 0x00E0}),
                         [(entry, (2, 0xE0))])

    def test_the_hook_changes_only_the_two_words_of_the_original_call(self):
        self.assertEqual(sorted(a for a in self.edits if 0x9000 <= a < 0x9600), [0x92D0, 0x92D1])
        before = [x for x in self.app.ins if 0x92CE <= x[0] <= 0x92D3]
        self.assertEqual([x[2] for x in before], ["ld AL, #0x0", "ld AH, #0x0", "prefix 0xf7", "bsr rel -0x926",
                                                  "prefix 0x03", "bra 0x956a"])
        self.assertEqual(dp._encode_branch(0x92D0, self.info["entry"], 9, 3),
                         [self.patched[0x92D0], self.patched[0x92D1]])
        self.assertEqual(self.patched[0x92D0] & 0xFF, 0)       # still one prefix word
        self.assertEqual(self.patched[0x92D1] & 0xFF, 0x9C)    # still a bsr

    def test_replaced_functions_are_each_one_plain_function(self):
        """One frame entry at the start, one leave as the last word, and no rts inside: so no
        second function hides in the range, reachable by a pointer the scan can't see."""
        found = dp.unreferenced_functions(self.app)
        used = {f for f in dp.SPACE_FUNCTIONS
                if any(f <= a < found[f] for a in self.edits if a not in dp.HOOK_WORDS)}
        self.assertTrue(used)
        for start in used:
            body = [x for x in self.app.ins if start <= x[0] < found[start]]
            self.assertRegex(body[0][2], r"sys 0xf[0-9a-f]0b")
            self.assertRegex(body[-1][2], r"sys 0xf[0-9a-f]0f")
            self.assertEqual(body[0][1] >> 8, body[-1][1] >> 8, hex(start))  # same frame size in and out
            self.assertEqual([x[2] for x in body[1:] if x[2] == "rts" or x[2].endswith("0b")], [], hex(start))
            pointer = (start - dp.POINTER_OFFSET) & 0xFFFF  # how task tables and code refer to a function
            self.assertNotIn(pointer, self.original, hex(start))

    def test_the_emulator_treats_x_as_the_link_register(self):
        """A call overwrites X with the return address, and rts jumps through X. Sony's flows and
        the patch all run under that rule, so the patch doesn't lean on a stale X."""
        m = machine(self.original)
        m.stubs[0x107F0] = lambda s: None           # a leaf: ld AL,#task ; rts
        m.X = 0x4242
        m.call(0x10C1D)                             # enter, ld AL,#1, call 0x11D84, leave
        self.assertEqual(m.X, 0xFFFF)               # the host's return address, not 0x4242
        # a leaf that changes X can't return
        words = list(self.original)
        words[0x107F0:0x107F3] = [0x0018, 0x00E2, 0x0000]  # ld X,#0 ; rts
        m = machine(words)
        with self.assertRaises(Unmodelled):
            m.call(0x107F0)

    def test_slow_double_click_is_two_battery_announcements(self):
        m = self.run_presses(self.patched, [(0, 100), (800, 100)])
        self.assertEqual(count(m, dp.BATTERY), 2)
        self.assertEqual(count(m, PLAY, 8), 0)
        self.assertEqual(state(m), 1)

    def test_window_edges(self):
        inside = self.run_presses(self.patched, [(0, 100), (550, 49)])
        self.assertEqual(state(inside), 2)
        outside = self.run_presses(self.patched, [(0, 100), (600, 100)])
        self.assertEqual(state(outside), 1)

    def test_long_hold_is_not_a_click_and_doesnt_arm_the_timer(self):
        m = self.run_presses(self.patched, [(0, 2000), (2100, 100)])
        self.assertEqual(count(m, dp.BATTERY), 1)
        self.assertEqual(state(m), 1)

    def test_click_then_hold_does_not_pair(self):
        m = self.run_presses(self.patched, [(0, 100), (300, 2000)])
        self.assertEqual(count(m, PLAY, 8), 0)
        self.assertEqual(state(m), 1)

    def test_triple_click_pairs_once_then_a_third_click_is_not_handled_here(self):
        m = self.run_presses(self.patched, [(0, 100), (300, 100), (600, 100)])
        self.assertEqual(count(m, PLAY, 8), 1)
        self.assertEqual(state(m), 2)

    def test_other_states_keep_stock_behaviour(self):
        for other in (9,):
            for words in (self.original, self.patched):
                m = machine(words)
                m.write(m.read(0x588) + 1, other)
                press(m, 0, 100)
                press(m, 300, 100)
                m.run_until(1500)
                self.assertEqual(count(m, dp.BATTERY), 2, (other, words is self.patched))
                self.assertEqual(state(m), other)

    def test_rehearsal_passes_on_the_patch_and_fails_on_stock(self):
        rows = dp.rehearse(self.patched)
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(row["ok"] for row in rows), rows)
        stock = dp.rehearse(self.original)
        self.assertFalse(stock[0]["ok"])          # no pairing without the patch
        self.assertIn("battery x2", stock[0]["detail"])

    def test_all_stock_words_outside_the_edits_are_unchanged(self):
        changed = {i for i, (a, b) in enumerate(zip(self.original, self.patched)) if a != b}
        self.assertLessEqual(changed, set(self.edits))

    def test_no_edit_touches_a_signed_word(self):
        self.assertFalse([w for w in self.edits if self.layout.covered(w)])

    def test_code_stays_in_the_code_segment(self):
        from xap_machine import CODE_LIMIT
        self.assertTrue(all(w < CODE_LIMIT for w in self.edits))

    def test_space_functions_are_unreferenced(self):
        found = dp.unreferenced_functions(self.app)
        self.assertLessEqual(set(dp.SPACE_FUNCTIONS), set(found))
        ranges = [(f, found[f]) for f in dp.SPACE_FUNCTIONS]
        for address in self.edits:
            if address in dp.HOOK_WORDS:
                continue
            self.assertTrue(any(a <= address < b for a, b in ranges), hex(address))

    def test_the_patched_code_never_runs_without_a_click(self):
        m = machine(self.patched)
        m.run_until(5000)
        self.assertEqual(m.calls, [])

    def test_image_builds_and_signatures_still_verify(self):
        edits = {**self.edits, **dfu_sign.pairing_edits(self.image, 4000)}
        output = dfu_sign.patch(self.image, edits)
        self.assertEqual(len(output), len(self.image))
        self.assertTrue(all(dfu_sign.verify(output).values()))
        with tempfile.TemporaryDirectory() as folder:
            app = dp.app_from_image(output, Path(folder) / "vm.app")
        m = self.run_presses(list(app.words), [(0, 100), (300, 100)])
        self.assertEqual(state(m), 2)

    @unittest.skipUnless(GLOBAL_FIRMWARE.exists(), "global firmware not downloaded")
    def test_same_edits_build_on_the_global_file(self):
        image = GLOBAL_FIRMWARE.read_bytes()
        with tempfile.TemporaryDirectory() as folder:
            app = dp.app_from_image(image, Path(folder) / "vm.app")
        edits, _ = dp.build_edits(app, dp._layout(image).covered)
        self.assertTrue(all(dfu_sign.verify(dfu_sign.patch(image, edits)).values()))


if __name__ == "__main__":
    unittest.main()

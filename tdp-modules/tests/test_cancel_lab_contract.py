from __future__ import annotations

import pathlib
import unittest

from tvcgui.features.frame_data.cancel_lab import (
    elapsed_source_frame,
    frame_in_window,
    mailbox_value_for_action,
    normalize_slot_label,
    command_rows_for_target,
    normal_input_match,
    recognized_special_actions,
    manual_target_trigger,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]


class CancelLabContractTests(unittest.TestCase):
    def test_action_mailbox_matches_existing_trainer_encoding(self):
        self.assertEqual(mailbox_value_for_action(0x0100), 0xFFFFC100)
        self.assertEqual(mailbox_value_for_action(0x0130), 0xFFFFC130)
        self.assertEqual(mailbox_value_for_action(0x0160), 0xFFFFC160)

    def test_window_zero_latest_means_until_source_ends(self):
        self.assertFalse(frame_in_window(4, 5, 0))
        self.assertTrue(frame_in_window(5, 5, 0))
        self.assertTrue(frame_in_window(999, 5, 0))
        self.assertFalse(frame_in_window(11, 5, 10))

    def test_elapsed_source_frame_uses_local_source_age(self):
        self.assertEqual(elapsed_source_frame(10.0, 10.0), 1)
        self.assertEqual(elapsed_source_frame(10.0, 10.1), 6)
        self.assertEqual(elapsed_source_frame(0.0, 10.0), 0)

    def test_slot_labels_are_normalized(self):
        self.assertEqual(normalize_slot_label("P1C2"), "P1-C2")
        self.assertEqual(normalize_slot_label("p2-c1"), "P2-C1")
        self.assertEqual(normalize_slot_label("2"), "P2-C1")

    def test_recomp_command_rows_and_special_candidates(self):
        import tvcgui.features.frame_data.cancel_lab as lab

        base = 0x9246BA00
        table = 0x909719C4
        memory = {
            base + lab.OFF_COMMAND_TABLE: table,
            table + 0x00: 0x00060001,
            table + 0x04: 0x00000000,
            table + 0x08: 0x00000080,
            table + 0x0C: 0x00000000,
            table + 0x10: 0x00000000,
            table + 0x14: 0x00000100,
            table + 0x18: 0xFFFFFFFF,
            base + lab.OFF_INPUT_HELD: 0x00000800,
            base + lab.OFF_INPUT_PRESSED: 0x00000080,
            base + lab.OFF_SPECIAL_CANDIDATE: 0x00000030,
            base + lab.OFF_RAW_SPECIAL_CANDIDATE: 0x00000060,
            base + lab.OFF_SPECIAL_FLAGS: 0x12345678,
            base + lab.OFF_RAW_SPECIAL_FLAGS: 0xABCDEF00,
        }
        old_read = lab._read_u32
        try:
            lab._read_u32 = lambda addr, default=0: memory.get(addr, default)
            rows = command_rows_for_target(base, 0x0100)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["buttons"], 0x80)
            match = normal_input_match(base, 0x0100)
            self.assertIsNotNone(match)
            actions, evidence = recognized_special_actions(base)
            self.assertEqual(actions, {0x0130, 0x0160})
            self.assertEqual(evidence["cooked"], 0x30)
            self.assertIsNotNone(manual_target_trigger(base, 0x0100, "normal"))
            self.assertIsNotNone(manual_target_trigger(base, 0x0130, "special"))
        finally:
            lab._read_u32 = old_read

    def test_cancel_lab_is_separate_from_cancel_mapper(self):
        workbench = (ROOT / "tvcgui" / "features" / "frame_data" / "workbench.py").read_text(encoding="utf-8")
        mapper = (ROOT / "tvcgui" / "features" / "frame_data" / "cancel_mapper.py").read_text(encoding="utf-8")
        main = (ROOT / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("from . import cancel_lab as FCL", workbench)
        self.assertNotIn("Test this route in Live Cancel Lab", workbench)
        self.assertNotIn("Open Live Cancel Lab", workbench)
        self.assertNotIn("Open Live Cancel Lab", mapper)
        self.assertIn("elif dock.cancel_lab_btn_rect.collidepoint(mx, my):", main)
        lab = (ROOT / "tvcgui" / "features" / "frame_data" / "cancel_lab.py").read_text(encoding="utf-8")
        self.assertIn("DEFAULT_EARLIEST_FRAME = 8", lab)
        self.assertIn("self.armed_mode = mode", lab)
        self.assertIn("Manual target input", lab)
        self.assertIn("OFF_SPECIAL_CANDIDATE = 0x1994", lab)
        self.assertIn("OFF_RAW_SPECIAL_CANDIDATE = 0x210C", lab)
        self.assertIn("Save window to Frame Data", lab)
        self.assertIn("current != source_id", lab)


if __name__ == "__main__":
    unittest.main()

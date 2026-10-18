"""Portable settings, bounded replay and the shared fighter read."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from tvcgui.core.settings_bundle import (
    FORMAT as SETTINGS_FORMAT, PENDING_IMPORT, apply_pending_settings,
    export_settings, import_settings, schedule_settings_import,
)
from tvcgui.runtime.fighter_frames import read_fighter_frame, resolve_bases
from tvcgui.runtime.session_recording import SessionRecorder, load_session
from tvcgui.ui.settings_dock import SettingsDock
from tvcgui.ui.components import _command_dock_layout


class SettingsBundleTests(unittest.TestCase):
    def test_export_import_only_known_settings(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            (source / "hitboxes").mkdir(parents=True)
            (source / "runtime").mkdir()
            (source / "continuo" / "music").mkdir(parents=True)
            (source / "hitboxes" / "hitbox_filter.json").write_text('{"P1": true}')
            (source / "runtime" / "private_events.json").write_text('{"secret": 1}')
            (source / "continuo" / "music" / "song.ogg").write_bytes(b"music")
            archive = root / "settings.zip"
            self.assertEqual(export_settings(archive, data_root=source), 1)
            with zipfile.ZipFile(archive) as file:
                self.assertEqual(set(file.namelist()),
                                 {"manifest.json", "hitboxes/hitbox_filter.json"})
            self.assertEqual(import_settings(archive, data_root=target), 1)
            self.assertEqual(json.loads((target / "hitboxes" / "hitbox_filter.json").read_text()),
                             {"P1": True})
            self.assertFalse((target / "runtime").exists())

    def test_rejects_unlisted_archive_before_changing_settings(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "data"
            path = target / "hitboxes" / "hitbox_filter.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"P1": false}')
            payload = b'{"P1": true}'
            bad = b'{}'
            manifest = {"format": SETTINGS_FORMAT, "version": 1,
                        "files": {"hitboxes/hitbox_filter.json": hashlib.sha256(payload).hexdigest(),
                                  "../outside.json": hashlib.sha256(bad).hexdigest()}}
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as file:
                file.writestr("manifest.json", json.dumps(manifest))
                file.writestr("hitboxes/hitbox_filter.json", payload)
                file.writestr("../outside.json", bad)
            with self.assertRaises(ValueError):
                import_settings(archive, data_root=target)
            self.assertEqual(path.read_text(), '{"P1": false}')
            self.assertFalse((root / "outside.json").exists())

    def test_import_is_queued_until_next_startup(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            target = root / "target"
            source_file = source / "training" / "punish_trainer.json"
            source_file.parent.mkdir(parents=True)
            source_file.write_text('{"enabled": true}')
            current = target / "training" / "punish_trainer.json"
            current.parent.mkdir(parents=True)
            current.write_text('{"enabled": false}')
            archive = root / "settings.zip"
            export_settings(archive, data_root=source)
            self.assertEqual(schedule_settings_import(archive, data_root=target), 1)
            self.assertEqual(json.loads(current.read_text()), {"enabled": False})
            self.assertTrue((target / PENDING_IMPORT).is_file())
            self.assertEqual(apply_pending_settings(data_root=target), 1)
            self.assertEqual(json.loads(current.read_text()), {"enabled": True})
            self.assertFalse((target / PENDING_IMPORT).exists())


class SessionRecordingTests(unittest.TestCase):
    def test_mission_event_history_outlasts_old_four_thousand_event_limit(self):
        recorder = SessionRecorder(capacity=1)
        recorder.start()
        recorder.capture(1, 1.0, {"P1-C1": {"id": 1}}, context={})
        for sequence in range(5000):
            recorder.capture_event(float(sequence), "mission_event", {"seq": sequence})
        self.assertEqual(len(recorder.events()), 5000)

    def test_bounded_recording_round_trips_as_read_only_frames(self):
        with TemporaryDirectory() as temp:
            recorder = SessionRecorder(capacity=2)
            recorder.start()
            for i in range(3):
                recorder.capture(i, float(i), {"P1-C1": {"id": i, "cur": 100 - i}},
                                 context={"stage": 9})
            recorder.capture_event(1.0, "music_raw", {"scene": "character_select", "raw_stage": 17})
            recorder.capture_event(1.1, "music_raw", {"scene": "character_select", "raw_stage": 17})
            recorder.capture_event(2.0, "music_scene", {"from": "character_select", "to": "stage_select"})
            recorder.stop()
            self.assertEqual(recorder.status(), (False, 2))
            destination = Path(temp) / "session.json"
            self.assertEqual(recorder.save(destination), 2)
            frames = load_session(destination)
            self.assertEqual([frame["frame"] for frame in frames], [1, 2])
            self.assertEqual(frames[-1]["fighters"]["P1-C1"]["cur"], 98)
            self.assertEqual(frames[-1]["context"]["stage"], 9)
            events = json.loads(destination.read_text())["events"]
            self.assertEqual([event["kind"] for event in events], ["music_raw", "music_scene"])

    def test_fighter_block_is_shared_with_parser(self):
        block = bytes(range(32))
        with patch("tvcgui.runtime.fighter_frames.rbytes", return_value=block) as read, \
             patch("tvcgui.runtime.fighter_frames.read_fighter", return_value={"id": 1}) as parse:
            snapshot, raw = read_fighter_frame(0x80001000, 0xF4, 32)
        read.assert_called_once_with(0x80001000, 32)
        parse.assert_called_once_with(0x80001000, 0xF4, block=block)
        self.assertEqual((snapshot, raw), ({"id": 1}, block))

    def test_slot_resolution_refreshes_only_after_pointer_change(self):
        previous = {}
        offsets = {}
        with patch("tvcgui.runtime.fighter_frames.SLOTS", [("P1-C1", 0x10, "P1")]), \
             patch("tvcgui.runtime.fighter_frames.rd32", return_value=0x80001000), \
             patch("tvcgui.runtime.fighter_frames.addr_in_ram", return_value=True), \
             patch("tvcgui.runtime.fighter_frames.prime_mem2_latch"), \
             patch("tvcgui.runtime.fighter_frames.pick_posy_off_no_jump", return_value=0xF4) as pick, \
             patch("tvcgui.runtime.fighter_frames.METER_CACHE") as meter:
            first = resolve_bases(previous, offsets)
            second = resolve_bases(previous, offsets)
        self.assertEqual(first, [("P1-C1", "P1", 0x80001000)])
        self.assertEqual(second, first)
        self.assertEqual(offsets[0x80001000], 0xF4)
        pick.assert_called_once_with(0x80001000)
        meter.drop.assert_called_once_with(0x80001000)

    def test_settings_button_opens_only_after_inside_click_release(self):
        dock = SettingsDock()
        with patch("tvcgui.ui.settings_window.open_window") as open_window:
            dock.frame_mouse(inside=False, left_pressed=True)
            dock.frame_mouse(inside=True, left_pressed=False)
            open_window.assert_not_called()
            dock.frame_mouse(inside=True, left_pressed=True)
            dock.frame_mouse(inside=True, left_pressed=False)
            open_window.assert_called_once_with()

    def test_settings_button_has_a_responsive_dock_target(self):
        for width in (640, 1280):
            sections, height = _command_dock_layout(width, False)
            settings = sections["hud"][1]["settings_btn"]
            self.assertGreaterEqual(height, settings.bottom)
            self.assertLessEqual(settings.right, width)


if __name__ == "__main__":
    unittest.main()

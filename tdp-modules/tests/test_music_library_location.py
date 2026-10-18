"""Portable music locations and live playlist discovery."""
import json
import struct
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from tvcgui.features.audio import music_player
from tvcgui.platform import dolphin


class MusicLibraryLocationTests(unittest.TestCase):
    def test_native_selection_retries_restore_after_ax_pair_returns(self):
        restored = threading.Event()

        class Gate:
            def __init__(self):
                self.needs_restore = True
                self.calls = 0

            def request_native_restore(self):
                self.needs_restore = True

            def apply(self, _backend, *, mute, write_protected):
                self.calls += 1
                if self.calls == 1:
                    return SimpleNamespace(muted=False, ready=False, text='AX pair absent', found=0)
                self.needs_restore = False
                restored.set()
                return SimpleNamespace(muted=False, ready=True, text='Native routes restored', found=2)

        class Music:
            def stop(self):
                pass

        with TemporaryDirectory() as temp:
            controller = music_player.MusicController(
                root=Path(temp), mixer=type('Mixer', (), {'music': Music()})())
            controller._backend = lambda: object()
            controller._ax_gate = Gate()
            controller.enabled = True
            try:
                controller.set_output_mode('native')
                self.assertTrue(restored.wait(1.0))
                self.assertFalse(controller._ax_gate.needs_restore)
                controller._audio_service.join(timeout=1.0)
                self.assertFalse(controller._audio_service.is_alive())
            finally:
                controller._audio_service_stop.set()
                controller._audio_wakeup.set()
                if controller._audio_service is not None:
                    controller._audio_service.join(timeout=1.0)

    def test_tag_scope_defaults_to_both_but_preserves_saved_choice(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertEqual(music_player.MusicController(
                root=root, start_sampler=False).status()['tag_scope'], 'both')

            settings = root / 'settings.json'
            settings.write_text(json.dumps({'fighter_switch': 'tag'}),
                                encoding='utf-8')
            self.assertEqual(music_player.MusicController(
                root=root, start_sampler=False).status()['tag_scope'], 'both')

            settings.write_text(json.dumps({'tag_scope': 'selected'}),
                                encoding='utf-8')
            self.assertEqual(music_player.MusicController(
                root=root, start_sampler=False).status()['tag_scope'], 'selected')

    def make_controller(self, library: Path, *, clock=None):
        with patch.object(music_player, 'user_data_path', return_value=str(library)):
            return music_player.MusicController(clock=clock, start_sampler=False)

    def test_audio_service_plays_without_gui_ticks(self):
        played = threading.Event()

        class Music:
            def get_busy(self):
                return False

            def stop(self):
                pass

            def load(self, _path):
                played.set()

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                pass

        with TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'menu'
            folder.mkdir()
            (folder / 'song.ogg').touch()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': Music()})())
            controller.enabled = True
            controller._observed = {'scene': 'menu', 'stage': None,
                                    'fighters': {}, 'points': {}, 'evidence': {}}
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', True)
            try:
                controller.ensure_audio_service()
                self.assertTrue(played.wait(1.0))
            finally:
                controller._audio_service_stop.set()
                controller._audio_wakeup.set()
                controller._audio_service.join(timeout=1.0)

    def test_character_select_sequence_latches_with_live_code_pointer(self):
        seq = dolphin.MEM2_LO + 0x1000
        host = 0x100000000
        block = bytearray(0x80)
        block[:12] = dolphin.CHRSEL_SEQ_HEADER[:12]
        struct.pack_into('>I', block, 12, 0x80791C90)
        signature = dolphin.CHRSEL_SEQ_SIGNATURE
        block[0x40:0x40 + len(signature)] = signature

        def read_host(_handle, addr, size):
            offset = addr - host - 0x1000
            return bytes(block[offset:offset + size])

        regions = [(host, dolphin.MEM2_SIZE, next(iter(dolphin.READABLE_PROTECT)),
                    dolphin.MEM_COMMIT)]
        with (patch.object(dolphin, '_dme_rd32_be',
                           side_effect=lambda addr: seq if addr == dolphin.CHRSEL_SEQ_PTR_ADDRS[0] else None),
              patch.object(dolphin, '_iter_regions', return_value=regions),
              patch.object(dolphin, '_rpm', side_effect=read_host),
              patch.object(dolphin, '_mem2_host_base', None),
              patch.object(dolphin, '_mem2_host_size', None),
              patch.object(dolphin, '_mem2_latch_mode', None),
              patch.object(dolphin, '_mem2_latch_note', '')):
            self.assertTrue(dolphin._latch_mem2_from_chrsel_sequence())
            self.assertEqual(dolphin._mem2_latch_mode, 'chrsel_sequence')
            self.assertEqual(dolphin._mem2_host_base, host)

    def test_stage_menu_object_can_anchor_mem2_without_sequence(self):
        root = dolphin.MEM2_LO + 0x1000
        block = bytearray(0x70)
        struct.pack_into('>I', block, 4, 15)
        struct.pack_into('>I', block, 0x4C, root)
        struct.pack_into('>I', block, 0x6C, 17)
        pointers = {dolphin.MENU_OBJECT_PTR_ADDR: root,
                    dolphin.STAGE_CURSOR_ADDR: 17}
        regions = [(0x100000000, dolphin.MEM2_SIZE, next(iter(dolphin.READABLE_PROTECT)),
                    dolphin.MEM_COMMIT)]
        with (patch.object(dolphin, '_dme_rd32_be', side_effect=pointers.get),
              patch.object(dolphin, '_iter_regions', return_value=regions),
              patch.object(dolphin, '_rpm', side_effect=lambda *_args: bytes(block)),
              patch.object(dolphin, '_mem2_host_base', None),
              patch.object(dolphin, '_mem2_host_size', None),
              patch.object(dolphin, '_mem2_latch_mode', None),
              patch.object(dolphin, '_mem2_latch_note', '')):
            self.assertTrue(dolphin._latch_mem2_from_menu_object())
            self.assertEqual(dolphin._mem2_latch_mode, 'stage_menu_object')
            struct.pack_into('>I', block, 0x6C, 18)
            self.assertFalse(dolphin._latch_mem2_from_menu_object())

    def test_round_start_rechecks_ax_gate_before_scene_scan(self):
        with TemporaryDirectory() as temp:
            now = [1.0]
            controller = music_player.MusicController(
                root=Path(temp), clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._requested_mute = True
            controller._backend = lambda: object()
            controller._write_protected = lambda: False
            calls = []
            controller._ax_gate.apply = lambda *_args, **_kwargs: (
                calls.append(now[0]) or SimpleNamespace(
                    muted=True, ready=True, text='verified'))

            controller._observe_native_phase(2)
            controller._observe_native_phase(4)
            controller._maintain_bgm()
            self.assertEqual(calls, [1.0])
            now[0] += 0.03
            controller._maintain_bgm()
            self.assertEqual(calls, [1.0, 1.03])
            controller._observe_native_phase(4)
            self.assertEqual(controller._mute_urgent_until, 2.0)
            now[0] = 2.01
            controller._maintain_bgm()
            now[0] = 2.04
            controller._maintain_bgm()
            self.assertEqual(calls, [1.0, 1.03, 2.01])

    def test_verified_ax_rebuild_marks_restart_but_not_round_start(self):
        with TemporaryDirectory() as temp:
            controller = music_player.MusicController(root=Path(temp), start_sampler=False)
            controller.enabled = True
            controller._requested_mute = True
            controller._native_zero_confirmed = True
            controller._current_file = 'first.ogg'
            controller._current_owner = 'stages/17'
            controller._match_latched = True
            controller._fight_opened = True
            controller._last_native_phase = 4
            controller._backend = lambda: object()
            controller._write_protected = lambda: False
            controller._ax_gate.apply = lambda *_args, **_kwargs: SimpleNamespace(
                muted=True, ready=True, text='verified', rerouted=True)

            controller._maintain_bgm(force=True)
            self.assertEqual(controller._match_serial, 1)
            controller._observe_native_phase(5)
            controller._observe_native_phase(4)
            controller._maintain_bgm(force=True)
            self.assertEqual(controller._match_serial, 1)

    def test_restart_advances_after_ax_pair_disappears_and_returns(self):
        class Music:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

        with TemporaryDirectory() as temp:
            music = Music()
            controller = music_player.MusicController(
                root=Path(temp), mixer=SimpleNamespace(music=music),
                start_sampler=False)
            controller.enabled = True
            controller._requested_mute = True
            controller._native_zero_confirmed = True
            controller._current_file = 'first.ogg'
            controller._current_owner = 'stages/17'
            controller._match_latched = True
            controller._fight_opened = True
            controller._last_native_phase = 4
            controller._backend = lambda: object()
            controller._write_protected = lambda: False
            results = iter((SimpleNamespace(muted=False, ready=False,
                                            text='AX pair missing', rerouted=False),
                            SimpleNamespace(muted=True, ready=True,
                                            text='verified', rerouted=True)))
            controller._ax_gate.apply = lambda *_args, **_kwargs: next(results)

            controller._maintain_bgm(force=True)
            self.assertTrue(music.stopped)
            self.assertTrue(controller._ax_rebuild_match_candidate)
            self.assertEqual(controller._match_serial, 0)
            controller._maintain_bgm(force=True)
            self.assertEqual(controller._match_serial, 1)
            self.assertIsNone(controller._interrupted_track)

    def test_p2_lock_in_ignores_stale_loader_until_match_is_confirmed(self):
        with TemporaryDirectory() as temp:
            now = [1.0]
            controller = music_player.MusicController(
                root=Path(temp), clock=lambda: now[0], start_sampler=False)
            select = {'scene': 'character_select', 'stage': None,
                      'fighters': {}, 'points': {}, 'evidence': {'select_asset': True}}
            loading = {'scene': 'loading', 'stage': 17,
                       'fighters': {}, 'points': {},
                       'evidence': {'native_phase': 2, 'select_asset': False}}
            controller._accept_observation(select)
            controller._accept_observation(loading)
            self.assertEqual(controller._observed['scene'], 'character_select')
            controller._accept_observation(select)
            now[0] += music_player.CHAR_TO_MATCH_CONFIRM_SECONDS
            controller._accept_observation(loading)
            self.assertEqual(controller._observed['scene'], 'character_select')
            now[0] += music_player.CHAR_TO_MATCH_CONFIRM_SECONDS
            controller._accept_observation(loading)
            self.assertEqual(controller._observed['scene'], 'loading')

    def test_character_select_keeps_track_through_unknown_and_ax_recheck(self):
        class Music:
            def __init__(self):
                self.loaded = []
                self.busy = False
                self.loops = []

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _value):
                pass

            def play(self, **kwargs):
                self.loops.append(kwargs['loops'])
                self.busy = True

        with TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'character_select'
            folder.mkdir()
            for name in ('first.ogg', 'second.ogg'):
                (folder / name).touch()
            now = [1.0]
            music = Music()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            gate_ready = [True]
            controller._backend = lambda: object()
            controller._write_protected = lambda: False
            controller._ax_gate.apply = lambda *_args, **_kwargs: SimpleNamespace(
                muted=gate_ready[0], ready=gate_ready[0], text='verified')
            controller._accept_observation({'scene': 'character_select', 'stage': None,
                                            'fighters': {}, 'points': {}, 'evidence': {}})
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])
            self.assertEqual(music.loops, [-1])

            now[0] += 0.1
            controller._accept_observation({'scene': 'unknown', 'stage': None,
                                            'fighters': {}, 'points': {}, 'evidence': {}})
            controller.tick()
            self.assertEqual(controller._observed['scene'], 'character_select')
            self.assertEqual(music.loaded, ['first.ogg'])

            gate_ready[0] = False
            controller._maintain_bgm(force=True)
            self.assertFalse(music.busy)
            gate_ready[0] = True
            now[0] += 0.2
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg', 'first.ogg'])

    def test_save_state_to_menu_releases_stale_training_match(self):
        with TemporaryDirectory() as temp:
            controller = music_player.MusicController(root=Path(temp), start_sampler=False)
            battle = {'scene': 'battle', 'stage': 17, 'fighters': {}, 'points': {},
                      'evidence': {'native_phase': 4, 'both_points_idle': True}}
            controller._accept_observation(battle)
            self.assertEqual(controller._observed['stage'], 17)
            menu = {'scene': 'menu', 'stage': 17, 'fighters': {}, 'points': {},
                    'evidence': {'native_phase': None, 'menu_object': True,
                                 'menu_asset_verified': True}}
            for _ in range(music_player.MENU_RETURN_SAMPLES):
                controller._accept_observation(menu)
            self.assertEqual(controller._observed['scene'], 'menu')
            self.assertIsNone(controller._observed['stage'])
            self.assertFalse(controller._match_latched)

            controller._accept_observation(battle)
            unknown = {'scene': 'unknown', 'stage': 17, 'fighters': {}, 'points': {},
                       'evidence': {'native_phase': None, 'mem2_ready': False}}
            for _ in range(music_player.UNKNOWN_MATCH_RELEASE_SAMPLES):
                controller._accept_observation(unknown)
            self.assertEqual(controller._observed['scene'], 'unknown')
            self.assertIsNone(controller._observed['stage'])
            self.assertFalse(controller._match_latched)

    def test_training_stage_stops_during_menu_return_confirmation(self):
        class PlayingMusic:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                self.busy = True

        with TemporaryDirectory() as temp:
            root = Path(temp)
            for folder, names in (('stages/Training Stage', ('first.ogg', 'second.ogg')),
                                  ('menu', ('menu.ogg',))):
                directory = root / folder
                directory.mkdir(parents=True)
                for name in names:
                    (directory / name).touch()
            now = [1.0]
            music = PlayingMusic()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', True)
            battle = {'scene': 'battle', 'stage': 17, 'fighters': {}, 'points': {},
                      'evidence': {'native_phase': 4, 'both_points_idle': True}}
            menu = {'scene': 'menu', 'stage': 17, 'fighters': {}, 'points': {},
                    'evidence': {'native_phase': None, 'menu_object': True,
                                 'menu_asset_verified': True}}
            controller._accept_observation(battle)
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])

            controller._accept_observation(menu)
            now[0] += 0.2
            controller.tick()
            self.assertFalse(music.busy)
            self.assertEqual(music.loaded, ['first.ogg'])

            # If the cue was transient, resume the same stage track.
            controller._accept_observation(battle)
            now[0] += 0.2
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg', 'first.ogg'])

            controller._accept_observation(menu)
            now[0] += 0.2
            controller.tick()
            for _ in range(music_player.MENU_RETURN_SAMPLES - 1):
                controller._accept_observation(menu)
            now[0] += 0.2
            controller.tick()
            self.assertEqual(controller._observed['scene'], 'menu')
            self.assertEqual(music.loaded, ['first.ogg', 'first.ogg', 'menu.ogg'])

    def test_menu_root_and_asset_classify_menu_without_stage_result(self):
        class MenuIO:
            def __init__(self, asset=True, phase=None):
                self.asset = asset
                self.phase = phase
                self.root = 0x90001000
                self.seq = 0x90002000

            def mem2_latch_info(self):
                return {'latched': True}

            def rd32(self, addr):
                values = {music_player.STAGE_MENU_PTR: self.root,
                          self.root + 4: music_player.MENU_OBJECT_TYPE,
                          self.root + 0x4C: self.root,
                          music_player.CHRSEL_ROOTS[0]: self.seq,
                          music_player.STAGE_RESULT: 17}
                if self.phase is not None:
                    values[music_player.NATIVE_BATTLE_PTR] = 0x80001000
                    values[0x8000100C] = self.phase
                return values.get(addr)

            def rbytes(self, addr, size):
                data = bytearray(128)
                if self.asset == 'character':
                    path = music_player.CHRSEL_SIGNATURE
                    data[0x40:0x40 + len(path)] = path
                elif self.asset:
                    path = b'fpack/menu/011/0000.fpk\x00'
                    data[0x20:0x20 + len(path)] = path
                if self.seq <= addr < self.seq + len(data):
                    offset = addr - self.seq
                    return bytes(data[offset:offset + size])
                return b''

        reading = music_player._read_environment(MenuIO())
        self.assertEqual(reading['scene'], 'menu')
        self.assertEqual(reading['evidence']['menu_asset_id'], 11)
        self.assertEqual(reading['stage'], 17)  # raw RAM still holds Training
        self.assertEqual(music_player._read_environment(MenuIO(asset=False))['scene'], 'menu')
        self.assertEqual(music_player._read_environment(MenuIO(asset='character'))['scene'],
                         'character_select')
        self.assertEqual(music_player._read_environment(MenuIO(phase=4))['scene'], 'battle')
        self.assertEqual(music_player._read_environment(MenuIO(phase=9))['scene'], 'winner')

    def test_weak_menu_root_does_not_advance_p2_character_select(self):
        with TemporaryDirectory() as temp:
            controller = music_player.MusicController(root=Path(temp), start_sampler=False)
            select = {'scene': 'character_select', 'stage': None,
                      'fighters': {}, 'points': {}, 'evidence': {'character_ui_live': True}}
            weak_menu = {'scene': 'menu', 'stage': 17, 'fighters': {}, 'points': {},
                         'evidence': {'native_phase': None, 'menu_object': True,
                                      'menu_asset_verified': False}}
            controller._accept_observation(select)
            for _ in range(music_player.MENU_ROOT_RETURN_SAMPLES - 1):
                controller._accept_observation(weak_menu)
                self.assertEqual(controller._observed['scene'], 'character_select')
            controller._accept_observation(weak_menu)
            self.assertEqual(controller._observed['scene'], 'menu')

    def test_p2_lock_in_does_not_leave_character_select_on_brief_stage_cue(self):
        with TemporaryDirectory() as temp:
            now = [1.0]
            controller = music_player.MusicController(
                root=Path(temp), clock=lambda: now[0], start_sampler=False)
            select = {'scene': 'character_select', 'stage': None,
                      'fighters': {}, 'points': {},
                      'evidence': {'native_phase': None, 'character_ui_live': True}}
            stage = {'scene': 'stage_select', 'stage': 17,
                     'fighters': {}, 'points': {},
                     'evidence': {'native_phase': None, 'stage_cursor': True,
                                  'stage_ui_live': True}}
            controller._accept_observation(select)
            controller._accept_observation(stage)
            now[0] += 0.5
            controller._accept_observation(stage)
            self.assertEqual(controller._observed['scene'], 'character_select')
            controller._accept_observation(select)  # transient cue disappeared
            now[0] += 0.2
            controller._accept_observation(stage)
            self.assertEqual(controller._observed['scene'], 'character_select')
            now[0] += music_player.STAGE_RETURN_CONFIRM_SECONDS
            controller._accept_observation(stage)
            self.assertEqual(controller._observed['scene'], 'stage_select')

    def test_character_ui_crosses_stage_boundary_without_extra_delay(self):
        with TemporaryDirectory() as temp:
            controller = music_player.MusicController(root=Path(temp), start_sampler=False)
            controller._accept_observation({
                'scene': 'stage_select', 'stage': None, 'fighters': {}, 'points': {},
                'evidence': {'stage_cursor': True, 'select_asset': True},
            })
            controller._accept_observation({
                'scene': 'character_select', 'stage': 17, 'fighters': {}, 'points': {},
                'evidence': {'character_ui_live': True, 'select_asset': True},
            })
            self.assertEqual(controller._observed['scene'], 'character_select')

    def test_stage_character_loading_songs_follow_actual_screen_order(self):
        class Music:
            def __init__(self):
                self.loaded = []

            def get_busy(self):
                return True

            def stop(self):
                pass

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _value):
                pass

            def play(self, **_kwargs):
                pass

        with TemporaryDirectory() as temp:
            root = Path(temp)
            for folder, song in (('character_select', 'character.ogg'),
                                 ('stage_select', 'stage.ogg'),
                                 ('stages/Training Stage', 'match.ogg')):
                path = root / folder
                path.mkdir(parents=True)
                (path / song).touch()
            now = [1.0]
            music = Music()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', True)
            controller._accept_observation({
                'scene': 'stage_select', 'stage': None, 'fighters': {}, 'points': {},
                'evidence': {'stage_cursor': True, 'select_asset': True},
            })
            controller.tick()
            controller._accept_observation({
                'scene': 'character_select', 'stage': 17, 'fighters': {}, 'points': {},
                'evidence': {'character_ui_live': True, 'select_asset': True},
            })
            now[0] += 0.1
            controller.tick()
            self.assertEqual(music.loaded, ['stage.ogg', 'character.ogg'])
            controller._accept_observation({
                'scene': 'unknown', 'stage': 17, 'fighters': {}, 'points': {},
                'evidence': {'select_asset': False},
            })
            now[0] += 0.1
            controller.tick()
            self.assertEqual(controller._observed['scene'], 'character_select')
            self.assertEqual(music.loaded, ['stage.ogg', 'character.ogg'])
            controller._accept_observation({
                'scene': 'loading', 'stage': 17, 'fighters': {}, 'points': {},
                'evidence': {'native_phase': 0, 'select_asset': False},
            })
            now[0] += 0.1
            controller.tick()
            self.assertEqual(music.loaded, ['stage.ogg', 'character.ogg', 'match.ogg'])

    def test_return_from_stage_cue_resumes_same_character_select_song(self):
        class Music:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _value):
                pass

            def play(self, **_kwargs):
                self.busy = True

        with TemporaryDirectory() as temp:
            root = Path(temp)
            for folder_name, names in (('character_select', ('first.ogg', 'second.ogg')),
                                       ('stage_select', ('stage.ogg',))):
                folder = root / folder_name
                folder.mkdir()
                for name in names:
                    (folder / name).touch()
            now = [1.0]
            music = Music()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)
            select = {'scene': 'character_select', 'stage': None,
                      'fighters': {}, 'points': {}, 'evidence': {'character_ui_live': True}}
            stage = {'scene': 'stage_select', 'stage': None,
                     'fighters': {}, 'points': {},
                     'evidence': {'stage_cursor': True, 'stage_ui_live': True}}
            controller._accept_observation(select)
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])
            controller._accept_observation(stage)
            now[0] += music_player.STAGE_RETURN_CONFIRM_SECONDS
            controller._accept_observation(stage)
            controller.tick()
            now[0] += music_player.FADE_OUT_SECONDS + 0.1
            controller.tick()
            self.assertEqual(music.loaded[-1], 'stage.ogg')
            controller._accept_observation(select)
            now[0] += 0.1
            controller.tick()
            now[0] += music_player.FADE_OUT_SECONDS + 0.1
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg', 'stage.ogg', 'first.ogg'])

    def test_training_restart_and_versus_retry_advance_stage_album(self):
        class Music:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _value):
                pass

            def play(self, **_kwargs):
                self.busy = True

        with TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'stages' / 'Training Stage'
            folder.mkdir(parents=True)
            for name in ('first.ogg', 'second.ogg', 'third.ogg'):
                (folder / name).touch()
            now = [1.0]
            music = Music()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._source = 'stage'
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)
            battle = {'scene': 'battle', 'stage': 17, 'fighters': {}, 'points': {},
                      'evidence': {'native_phase': 4, 'both_points_idle': True}}

            def play_current(expected=None):
                controller._accept_observation(battle)
                now[0] += 0.4
                controller.tick()
                if expected is not None:
                    self.assertEqual(music.loaded, expected)
                now[0] += music_player.FADE_OUT_SECONDS + 0.1
                controller.tick()

            play_current()
            controller._observe_native_phase(4)
            self.assertEqual(music.loaded, ['first.ogg'])

            for _ in range(music_player.RESTART_LOADING_SAMPLES):
                controller._observe_native_phase(2)
            controller._observe_native_phase(4)
            self.assertTrue(controller._pending_match_boundary)
            play_current(['first.ogg', 'second.ogg'])
            self.assertEqual(music.loaded, ['first.ogg', 'second.ogg'])

            controller._observe_native_phase(5)  # normal KO/round transition
            for _ in range(music_player.RESTART_LOADING_SAMPLES):
                controller._observe_native_phase(2)
            controller._observe_native_phase(4)
            self.assertFalse(controller._pending_match_boundary)
            play_current()
            self.assertEqual(music.loaded, ['first.ogg', 'second.ogg'])

            controller._observe_native_phase(9)
            controller._observe_native_phase(9)
            controller._observe_native_phase(2)  # versus Retry after results
            controller._observe_native_phase(4)
            play_current(['first.ogg', 'second.ogg', 'third.ogg'])
            self.assertEqual(music.loaded, ['first.ogg', 'second.ogg', 'third.ogg'])

    def test_tagged_fighter_then_stage_then_native_fallback(self):
        class Music:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _value):
                pass

            def play(self, **_kwargs):
                self.busy = True

        with TemporaryDirectory() as temp:
            root = Path(temp)
            first, second, songless, opponent = list(music_player.CHAR_NAMES)[:4]
            for cid, name in ((first, 'first.ogg'), (second, 'second.ogg'),
                              (opponent, 'opponent.ogg')):
                char = music_player.CHAR_NAMES[cid]
                folder = root / 'characters' / f'{cid:02d}_{music_player._slug(char)}'
                folder.mkdir(parents=True)
                (folder / name).touch()
            stage = root / 'stages' / 'Wasteland'
            stage.mkdir(parents=True)
            (stage / 'wasteland.ogg').touch()
            now = [1.0]
            music = Music()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._source = 'fighter'
            controller._slot = 'P1-point'
            controller._fighter_switch = 'tag'
            controller._tag_scope = 'selected'
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)
            roster = {'P1-C1': first, 'P1-C2': second, 'P1-C3': songless,
                      'P2-C1': first, 'P2-C2': opponent}

            def step(point, stage_id=1, *, p2_slot='P2-C1', idle=True,
                     immediate_track=None):
                points = {'P1': point}
                if p2_slot:
                    points['P2'] = p2_slot
                controller._accept_observation({
                    'scene': 'battle', 'stage': stage_id, 'fighters': roster,
                    'points': points,
                    'evidence': {'native_phase': 4, 'both_points_idle': idle,
                                 'both_points_live': bool(p2_slot)}})
                now[0] += 0.4
                controller.tick()
                if immediate_track is not None:
                    self.assertEqual(music.loaded[-1], immediate_track)
                now[0] += music_player.FADE_OUT_SECONDS + 0.1
                controller.tick()

            step('P1-C1', idle=False)
            self.assertFalse(controller._fight_opened)
            step('P1-C1', idle=False)
            self.assertTrue(controller._fight_opened)
            self.assertTrue(controller._tag_armed)
            self.assertEqual(music.loaded, ['first.ogg'])
            step('P1-C1', p2_slot='P2-C2', idle=False)
            self.assertEqual(music.loaded, ['first.ogg'])
            controller.configure(tag_scope='both')
            self.assertEqual(json.loads((root / 'settings.json').read_text())['tag_scope'],
                             'both')
            self.assertEqual(music_player.MusicController(
                root=root, start_sampler=False).status()['tag_scope'], 'both')
            step('P1-C1', p2_slot='P2-C1', idle=False)
            step('P1-C1', p2_slot='P2-C2', idle=False,
                 immediate_track='opponent.ogg')
            self.assertEqual(controller._tag_selected_team, 'P2')
            step('P1-C2', p2_slot=None, idle=False,
                 immediate_track='second.ogg')
            self.assertEqual(music.loaded[-1], 'second.ogg')
            step('P1-C3', immediate_track='wasteland.ogg')
            self.assertEqual(music.loaded[-1], 'wasteland.ogg')
            step('P1-C3', stage_id=2)
            self.assertTrue(controller._native_fallback)
            self.assertEqual(music.loaded[-3:],
                             ['opponent.ogg', 'second.ogg', 'wasteland.ogg'])

            controller._source = 'stage'
            step('P1-C1')
            loaded_before_tag = len(music.loaded)
            step('P1-C2', p2_slot=None, idle=False)
            self.assertEqual(controller._current_owner, 'stages/1')
            self.assertEqual(len(music.loaded), loaded_before_tag)

    def test_legacy_default_path_follows_moved_exe_even_if_original_exists(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            old = base / 'old' / 'data' / 'continuo' / 'music'
            new = base / 'new' / 'data' / 'continuo' / 'music'
            old.mkdir(parents=True)
            new.mkdir(parents=True)
            song = new / 'menu' / 'theme.ogg'
            song.parent.mkdir()
            song.touch()
            (new.parent / 'music_location.json').write_text(
                json.dumps({'path': str(old)}), encoding='utf-8')
            controller = self.make_controller(new)
            self.assertEqual(controller.root, new)
            owner, tracks = controller._context({'scene': 'menu'})
            self.assertEqual((owner, tracks), ('menu', [song]))

    def test_local_selection_survives_directory_move(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            old_base = base / 'old' / 'data' / 'continuo'
            new_base = base / 'new' / 'data' / 'continuo'
            controller = self.make_controller(old_base / 'music')
            chosen = old_base / 'alternate'
            controller.set_music_root(chosen)
            self.assertEqual(json.loads((old_base / 'music_location.json').read_text()),
                             {'path': 'alternate', 'relative_to': 'config'})
            new_base.parent.mkdir(parents=True)
            old_base.rename(new_base)
            moved = self.make_controller(new_base / 'music')
            self.assertEqual(moved.root, new_base / 'alternate')

    def test_external_selection_stays_absolute_and_missing_drive_falls_back(self):
        with TemporaryDirectory() as temp:
            base = Path(temp)
            local = base / 'app' / 'data' / 'continuo' / 'music'
            external = base / 'external_songs'
            controller = self.make_controller(local)
            controller.set_music_root(external)
            self.assertEqual(controller.root, external)
            self.assertEqual(self.make_controller(local).root, external)
            external.rename(base / 'disconnected')
            self.assertEqual(self.make_controller(local).root, local)

    def test_playlist_cache_refreshes_after_one_second(self):
        with TemporaryDirectory() as temp:
            now = [0.0]
            controller = music_player.MusicController(
                root=Path(temp), clock=lambda: now[0], start_sampler=False)
            folder = Path(temp) / 'menu'
            folder.mkdir()
            first = folder / 'first.ogg'
            first.touch()
            self.assertEqual(controller._songs([folder]), [first])
            second = folder / 'second.mp3'
            second.touch()
            self.assertEqual(controller._songs([folder]), [first])
            now[0] = music_player.PLAYLIST_CACHE_SECONDS
            self.assertEqual(controller._songs([folder]), [first, second])

    def test_yami_result_id_reads_yami_folder_not_legacy_training(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            yami = root / 'stages' / 'Yami - First Form' / 'boss.mp3'
            training = root / 'stages' / '15' / 'training.mp3'
            yami.parent.mkdir(parents=True)
            training.parent.mkdir(parents=True)
            yami.touch()
            training.touch()
            controller = music_player.MusicController(root=root, start_sampler=False)

            self.assertEqual(music_player.stage_label(15), 'Yami - First Form')
            self.assertEqual(controller._stage_songs(15), [yami])
            self.assertIn(training, controller._stage_songs(17))
            self.assertEqual(controller._expected_ssd({'scene': 'battle', 'stage': 15}),
                             's_bgm_32')

    def test_yami_forms_share_stage_id_but_use_their_own_music(self):
        # The October 17 session records stage 15 for all three fights while
        # the loaded Yami character changes from 23 to 24 to 25.
        with TemporaryDirectory() as temp:
            root = Path(temp)
            tracks = {}
            for cid, label in ((23, 'First'), (24, 'Second'), (25, 'Third')):
                track = root / 'stages' / f'Yami - {label} Form' / f'{cid}.mp3'
                track.parent.mkdir(parents=True)
                track.touch()
                tracks[cid] = track
            controller = music_player.MusicController(root=root, start_sampler=False)
            controller._source = 'stage'
            for cid, key in ((23, 's_bgm_32'), (24, 's_bgm_33'), (25, 's_bgm_34')):
                observed = {'scene': 'loading', 'stage': 15,
                            'fighters': {'P2-C1': cid}}
                self.assertEqual(controller._context(observed)[1], [tracks[cid]])
                self.assertEqual(controller._expected_ssd(observed), key)
                observed['scene'] = 'battle'
                self.assertEqual(controller._context(observed)[1], [tracks[cid]])
                self.assertTrue(controller._match_has_custom_music(observed))

            # A fighter theme still takes priority in Follow fighter mode.
            fighter = root / 'characters' / '24_yami' / 'fighter.mp3'
            fighter.parent.mkdir(parents=True)
            fighter.touch()
            controller._source = 'fighter'
            controller._slot = 'P2-point'
            observed = {'scene': 'battle', 'stage': 15,
                        'fighters': {'P2-C1': 24}, 'points': {'P2': 'P2-C1'},
                        'evidence': {'both_points_idle': True}}
            self.assertEqual(controller._context(observed)[1], [fighter])
            controller._source = 'stage'
            self.assertEqual(controller._context({'scene': 'battle', 'stage': 7,
                                                  'fighters': {'P2-C1': 24},
                                                  'points': {}, 'evidence': {}})[0],
                             'stages/7')

    def test_numeric_stage_id_is_read_without_catalog_whitelist(self):
        class StageIO:
            def __init__(self, phase, stage):
                self.phase = phase
                self.stage = stage

            def rd32(self, address):
                values = {music_player.NATIVE_BATTLE_PTR: 0x80001000,
                          0x8000100C: self.phase, music_player.STAGE_RESULT: self.stage}
                return values.get(address)

            def mem2_latch_info(self):
                return {'latched': False}

        reading = music_player._read_environment(StageIO(4, 16))
        self.assertEqual(reading['scene'], 'battle')
        self.assertEqual(reading['stage'], 16)
        # Yami's resolved arena is available before fighter structs load.
        intro = music_player._read_environment(StageIO(2, 15))
        self.assertEqual((intro['scene'], intro['stage']), ('loading', 15))

    def test_training_cursor_remains_stage_select(self):
        class TrainingSelectIO:
            def __init__(self, character_asset=False, character_ui=False):
                self.character_asset = character_asset
                self.character_ui = character_ui

            def rd32(self, address):
                root = 0x90001000
                return {
                    music_player.STAGE_MENU_PTR: root,
                    root + 4: 15,
                    root + 0x4C: root,
                    root + 0x6C: 17,
                    root + music_player.CHARACTER_UI_STATE_OFFSET: int(self.character_ui),
                    music_player.SELECT_SCENE_GLOBAL: int(self.character_ui),
                    music_player.STAGE_GLOBAL_CURSOR: 17,
                    music_player.STAGE_RESULT: 17,
                    music_player.CHRSEL_ROOTS[0]: 0x90002000 if self.character_asset else None,
                }.get(address)

            def rbytes(self, address, size):
                if self.character_asset and address == 0x90002040:
                    return music_player.CHRSEL_SIGNATURE[:size]
                return b''

            def mem2_latch_info(self):
                return {'latched': True}

        reading = music_player._read_environment(TrainingSelectIO())
        self.assertEqual(reading['scene'], 'stage_select')
        self.assertTrue(reading['evidence']['stage_cursor'])
        early_stage = music_player._read_environment(TrainingSelectIO(character_asset=True))
        self.assertEqual(early_stage['scene'], 'stage_select')
        self.assertTrue(early_stage['evidence']['ambiguous'])
        self.assertFalse(early_stage['evidence']['character_ui_live'])
        self.assertTrue(early_stage['evidence']['stage_ui_live'])

        class P2SelectIO(TrainingSelectIO):
            def rd32(self, address):
                root = 0x90001000
                if address in (root + 0x6C, music_player.STAGE_GLOBAL_CURSOR):
                    return 9
                return super().rd32(address)

        p2_screen = music_player._read_environment(P2SelectIO(character_asset=True, character_ui=True))
        self.assertEqual(p2_screen['scene'], 'character_select')
        self.assertTrue(p2_screen['evidence']['character_ui_live'])
        self.assertFalse(p2_screen['evidence']['stage_ui_live'])
        self.assertEqual(p2_screen['evidence']['raw_stage'], 17)

        class MixedSelectIO(TrainingSelectIO):
            def rd32(self, address):
                if address == music_player.SELECT_SCENE_GLOBAL:
                    return 1
                return super().rd32(address)

        mixed = music_player._read_environment(MixedSelectIO(character_asset=True))
        self.assertEqual(mixed['scene'], 'unknown')
        self.assertFalse(mixed['evidence']['stage_ui_live'])

        class LoadedStageIO(TrainingSelectIO):
            def rd32(self, address):
                root = 0x90001000
                if address == root + 0x6C:
                    return 0
                if address in (music_player.STAGE_GLOBAL_CURSOR, music_player.STAGE_RESULT):
                    return 9
                return super().rd32(address)

        outgoing_select = music_player._read_environment(LoadedStageIO(character_asset=True))
        self.assertEqual(outgoing_select['scene'], 'unknown')
        self.assertEqual(outgoing_select['evidence']['stage_local_cursor_id'], 0)

    def test_fighter_without_tracks_uses_stage_and_keeps_prematch_song(self):
        class PlayingMusic:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                self.busy = True

        with TemporaryDirectory() as temp:
            root = Path(temp)
            stage = root / 'stages' / 'Wasteland'
            stage.mkdir(parents=True)
            (stage / 'first.ogg').touch()
            (stage / 'second.ogg').touch()
            now = [1.0]
            music = PlayingMusic()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._source = 'fighter'
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)
            fighter_id = next(iter(music_player.CHAR_NAMES))
            roster = {'P1-C1': fighter_id, 'P2-C1': fighter_id}

            def step(scene, phase, *, idle=False):
                controller._accept_observation({
                    'scene': scene, 'stage': 1, 'fighters': roster, 'points': {},
                    'evidence': {'native_phase': phase, 'both_points_idle': idle}})
                now[0] += 0.4
                controller.tick()
                now[0] += music_player.FADE_OUT_SECONDS + 0.1
                controller.tick()

            step('battle', 4, idle=True)
            self.assertEqual(music.loaded, ['first.ogg'])
            step('stage_select', 2)
            step('loading', 2)
            self.assertEqual(music.loaded[-1], 'second.ogg')
            step('battle', 4, idle=True)
            self.assertEqual(music.loaded, ['first.ogg', 'second.ogg'])
            # A tag between fighters with no playlists must not cycle the
            # fallback stage album.
            controller._tag_event_serial += 1
            now[0] += 0.4
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg', 'second.ogg'])

    def test_yami_stage_fallback_starts_during_loading_and_survives_fight(self):
        class Music:
            def __init__(self):
                self.loaded = []

            def get_busy(self):
                return bool(self.loaded)

            def stop(self):
                pass

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                pass

        with TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'stages' / 'Yami - First Form'
            folder.mkdir(parents=True)
            (folder / 'yami.mp3').touch()
            now = [1.0]
            music = Music()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._source = 'fighter'
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)
            controller._accept_observation({
                'scene': 'loading', 'stage': 15, 'fighters': {}, 'points': {},
                'evidence': {'native_phase': 2}})
            controller.tick()
            self.assertEqual(music.loaded, ['yami.mp3'])
            now[0] += 0.4
            controller._accept_observation({
                'scene': 'battle', 'stage': 15, 'fighters': {}, 'points': {},
                'evidence': {'native_phase': 4, 'both_points_idle': True}})
            controller.tick()
            self.assertEqual(music.loaded, ['yami.mp3'])

    def test_old_training_override_migrates_away_from_yami_id(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'settings.json').write_text('{"stage_override": 15}', encoding='utf-8')
            controller = music_player.MusicController(root=root, start_sampler=False)
            self.assertEqual(controller._stage_override, 17)
            controller._save_settings()
            self.assertEqual(json.loads((root / 'settings.json').read_text())['stage_ids_version'], 2)

    def test_large_menu_playlist_visits_every_song_before_repeat(self):
        class FinishedMusic:
            def __init__(self):
                self.loaded = []

            def get_busy(self):
                return False

            def stop(self):
                pass

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                pass

        with TemporaryDirectory() as temp:
            now = [1.0]
            music = FinishedMusic()
            controller = music_player.MusicController(
                root=Path(temp), mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            menu = Path(temp) / 'menu'
            menu.mkdir()
            for number in range(20):
                (menu / f'track_{number:02d}.mp3').touch()
            controller.enabled = True
            controller._mode = 'menu'
            controller._observed = {'scene': 'menu', 'stage': None, 'fighters': {}, 'points': {}}
            controller._maintain_bgm = lambda **_kwargs: setattr(controller, '_native_zero_confirmed', True)

            for _ in range(20):
                now[0] += 0.2
                controller.tick()
            self.assertEqual(len(set(music.loaded)), 20)

            controller.configure(stage_order='random')
            start = len(music.loaded)
            for _ in range(10):
                now[0] += 0.2
                controller.tick()
            controller.configure(volume=0.4)
            for _ in range(5):
                now[0] += 0.2
                controller.tick()

            # Opening settings and restarting the launcher must not reshuffle
            # tracks that are still waiting in this cycle.
            controller = music_player.MusicController(
                root=Path(temp), mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._mode = 'menu'
            controller._observed = {'scene': 'menu', 'stage': None,
                                    'fighters': {}, 'points': {}}
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', True)
            for _ in range(5):
                now[0] += 0.2
                controller.tick()
            random_cycle = music.loaded[start:]
            self.assertEqual(len(random_cycle), 20)
            self.assertEqual(len(set(random_cycle)), 20)
            now[0] += 0.2
            controller.tick()
            self.assertNotEqual(music.loaded[-1], random_cycle[-1])

    def test_unreadable_menu_frames_do_not_advance_playlist(self):
        class PlayingMusic:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                self.busy = True

        with TemporaryDirectory() as temp:
            root = Path(temp)
            menu = root / 'menu'
            menu.mkdir()
            for name in ('first.ogg', 'second.ogg'):
                (menu / name).touch()
            now = [1.0]
            music = PlayingMusic()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', True)
            menu_frame = {'scene': 'menu', 'stage': None, 'fighters': {},
                          'points': {}, 'evidence': {'menu_object': True}}
            unknown_frame = {'scene': 'unknown', 'stage': 17,
                             'fighters': {}, 'points': {}, 'evidence': {}}
            controller._accept_observation(menu_frame)
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])

            controller._accept_observation(unknown_frame)
            now[0] += music_player.FADE_OUT_SECONDS + 0.1
            controller.tick()
            self.assertEqual(controller._observed['scene'], 'menu')
            self.assertEqual(music.loaded, ['first.ogg'])

            controller._accept_observation(menu_frame)
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])

    def test_unreadable_playlist_keeps_native_audio_until_file_changes(self):
        class BrokenMusic:
            def __init__(self):
                self.loads = 0

            def get_busy(self):
                return False

            def stop(self):
                pass

            def load(self, _path):
                self.loads += 1
                raise RuntimeError('unsupported audio')

        with TemporaryDirectory() as temp:
            now = [1.0]
            music = BrokenMusic()
            mixer = type('Mixer', (), {'music': music})()
            controller = music_player.MusicController(
                root=Path(temp), mixer=mixer, clock=lambda: now[0], start_sampler=False)
            song = Path(temp) / 'menu' / 'broken.ogg'
            song.parent.mkdir()
            song.write_bytes(b'bad')
            controller.enabled = True
            controller._mode = 'menu'
            controller._observed = {'scene': 'menu'}
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)

            controller.tick()
            self.assertEqual(music.loads, 1)
            self.assertTrue(controller._native_fallback)
            now[0] += 0.2
            controller.tick()
            self.assertEqual(music.loads, 1)
            self.assertTrue(controller._native_fallback)

            song.write_bytes(b'replaced track')
            now[0] += 1.1
            controller.tick()
            self.assertEqual(music.loads, 2)

    def test_prematch_phase_changes_keep_song_until_ko_and_next_match(self):
        class PlayingMusic:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                self.busy = True

        def reading(scene, phase, *, idle=False):
            return {'scene': scene, 'stage': 1, 'fighters': {}, 'points': {},
                    'evidence': {'native_phase': phase, 'both_points_idle': idle}}

        with TemporaryDirectory() as temp:
            now = [1.0]
            music = PlayingMusic()
            mixer = type('Mixer', (), {'music': music})()
            controller = music_player.MusicController(
                root=Path(temp), mixer=mixer, clock=lambda: now[0], start_sampler=False)
            stage = Path(temp) / 'stages' / 'Wasteland'
            stage.mkdir(parents=True)
            (stage / 'first.ogg').touch()
            (stage / 'second.ogg').touch()
            controller.enabled = True
            controller._source = 'stage'
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)

            controller._accept_observation(reading('character_select', None))
            controller._accept_observation(reading('loading', 2))
            self.assertEqual(controller._observed['scene'], 'character_select')
            now[0] += music_player.CHAR_TO_MATCH_CONFIRM_SECONDS
            controller._accept_observation(reading('loading', 2))
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])

            for scene, phase, idle in [('battle', 4, False),
                                       ('post_battle', 5, False),
                                       ('loading', 2, False),
                                       ('character_select', 2, False),
                                       ('battle', 4, True)]:
                if phase == 5:
                    controller._observe_native_phase(5)
                    controller._observe_native_phase(5)
                now[0] += 0.2
                controller._accept_observation(reading(scene, phase, idle=idle))
                controller.tick()
                self.assertEqual(music.loaded, ['first.ogg'])
                self.assertEqual(controller._match_serial, 0)
                self.assertEqual(controller._observed['scene'],
                                 'battle' if idle else 'loading')

            now[0] += 0.2
            controller._accept_observation(reading('post_battle', 9))
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])
            now[0] += 0.2
            controller._accept_observation(reading('loading', 2))
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])
            now[0] += 0.2
            controller._accept_observation(reading('battle', 4, idle=True))
            controller.tick()
            now[0] += music_player.FADE_OUT_SECONDS + 0.1
            controller.tick()
            self.assertEqual(controller._match_serial, 1)
            self.assertEqual(music.loaded, ['first.ogg', 'second.ogg'])

    def test_one_bad_end_phase_read_does_not_arm_new_match(self):
        with TemporaryDirectory() as temp:
            controller = music_player.MusicController(root=Path(temp), start_sampler=False)
            controller._match_latched = True
            controller._fight_opened = True
            controller._observe_native_phase(9)
            controller._observe_native_phase(2)
            self.assertFalse(controller._match_end_seen)
            self.assertFalse(controller._pending_match_boundary)

            controller._observe_native_phase(9)
            controller._observe_native_phase(9)
            controller._observe_native_phase(2)
            self.assertTrue(controller._match_end_seen)
            self.assertTrue(controller._pending_match_boundary)

    def test_stage_ids_follow_current_reading_and_select_ends_old_match(self):
        class PlayingMusic:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                self.busy = True

        def reading(scene, phase, stage=None, idle=False):
            return {'scene': scene, 'stage': stage, 'fighters': {}, 'points': {},
                    'evidence': {'native_phase': phase, 'both_points_idle': idle,
                                 'character_ui_live': scene == 'character_select',
                                 'stage_cursor': scene == 'stage_select'}}

        with TemporaryDirectory() as temp:
            now = [1.0]
            music = PlayingMusic()
            controller = music_player.MusicController(
                root=Path(temp), mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            for stage_id, name in ((9, 'aensland.ogg'), (1, 'wasteland.ogg')):
                folder = Path(temp) / 'stages' / music_player.STAGE_NAMES[stage_id]
                folder.mkdir(parents=True)
                (folder / name).touch()
            controller.enabled = True
            controller._source = 'stage'
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)

            def step(scene, phase, stage=None, idle=False):
                now[0] += 0.4
                controller._accept_observation(reading(scene, phase, stage, idle))
                controller.tick()
                now[0] += music_player.FADE_OUT_SECONDS + 0.1
                controller.tick()

            step('battle', 4, 9, True)
            self.assertEqual(music.loaded, ['aensland.ogg'])
            # The loader can write the next stage while the win screen still
            # belongs to the previous match. That write is not a song cue.
            step('post_battle', 5, 1)
            self.assertEqual(controller._observed['stage'], 9)
            self.assertEqual(music.loaded, ['aensland.ogg'])
            step('loading', 2, 1)
            self.assertEqual(controller._observed['stage'], 9)
            self.assertEqual(music.loaded, ['aensland.ogg'])
            step('stage_select', 2)
            self.assertEqual(controller._observed['scene'], 'stage_select')
            self.assertIsNone(controller._observed['stage'])
            step('character_select', None)
            self.assertEqual(controller._observed['scene'], 'character_select')
            step('loading', 2, 1)
            self.assertEqual(controller._observed['scene'], 'character_select')
            now[0] += music_player.CHAR_TO_MATCH_CONFIRM_SECONDS
            step('loading', 2, 1)
            self.assertEqual(controller._observed['stage'], 1)
            step('battle', 4, 1, True)
            self.assertEqual(music.loaded[-1], 'wasteland.ogg')
            self.assertEqual(controller._match_serial, 1)

            # The live stage ID also wins when the short select/KO route is
            # missed entirely by the sampler.
            step('battle', 4, 9, True)
            self.assertEqual(controller._observed['stage'], 9)
            self.assertEqual(music.loaded[-1], 'aensland.ogg')

    def test_winner_ssd_waits_for_fighter_unload_and_accepts_next_stage(self):
        class PlayingMusic:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                self.busy = True

        with TemporaryDirectory() as temp:
            root = Path(temp)
            for folder, name in (('stages/Wasteland', 'wasteland.ogg'),
                                 ('menus/Winner Screen', 'winner.ogg'),
                                 ('stages/16', 'stage16.ogg')):
                path = root / folder
                path.mkdir(parents=True)
                (path / name).touch()
            now = [1.0]
            music = PlayingMusic()
            controller = music_player.MusicController(
                root=root, mixer=type('Mixer', (), {'music': music})(),
                clock=lambda: now[0], start_sampler=False)
            controller.enabled = True
            controller._source = 'stage'
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)

            def step(scene, phase, stage, fighters=None):
                reading = {'scene': scene, 'stage': stage,
                           'fighters': fighters or {}, 'points': {},
                           'evidence': {'native_phase': phase, 'both_points_idle': phase == 4}}
                controller._accept_observation(reading)
                now[0] += 0.4
                controller.tick()
                now[0] += music_player.FADE_OUT_SECONDS + 0.1
                controller.tick()

            roster = {'P1-C1': 0, 'P2-C1': 1}
            step('battle', 4, 1, roster)
            self.assertEqual(music.loaded, ['wasteland.ogg'])
            step('post_battle', 5, 16, roster)
            self.assertEqual(music.loaded, ['wasteland.ogg'])
            step('post_battle', 9, 16, roster)
            self.assertEqual(controller._observed['scene'], 'battle')
            self.assertEqual(music.loaded, ['wasteland.ogg'])
            step('post_battle', 9, 16)
            self.assertEqual(music.loaded, ['wasteland.ogg'])
            step('post_battle', 9, 16)
            self.assertEqual(controller._observed['scene'], 'winner')
            self.assertEqual(controller._observed['stage'], 1)
            self.assertEqual(controller._expected_ssd(controller._observed), 's_bgm_26')
            self.assertEqual(music.loaded, ['wasteland.ogg', 'winner.ogg'])
            step('loading', 2, 16)
            self.assertEqual(music.loaded, ['wasteland.ogg', 'winner.ogg', 'stage16.ogg'])
            step('battle', 4, 16)
            self.assertEqual(controller._match_serial, 1)
            self.assertEqual(music.loaded[-1], 'stage16.ogg')
            self.assertEqual(music.loaded.count('stage16.ogg'), 1)

    def test_battle_object_unload_releases_ko_with_cached_fighters(self):
        with TemporaryDirectory() as temp:
            controller = music_player.MusicController(root=Path(temp), start_sampler=False)
            roster = {'P1-C1': 0, 'P2-C1': 1}

            def frame(scene, phase):
                return {'scene': scene, 'stage': 1, 'fighters': roster,
                        'points': {}, 'evidence': {'native_phase': phase,
                                                 'both_points_idle': phase == 4}}

            controller._accept_observation(frame('battle', 4))
            controller._accept_observation(frame('winner', 9))
            self.assertEqual(controller._observed['scene'], 'battle')
            controller._accept_observation(frame('unknown', None))
            self.assertEqual(controller._observed['scene'], 'battle')
            controller._accept_observation(frame('unknown', None))
            self.assertEqual(controller._observed['scene'], 'winner')

    def test_followed_fighter_keeps_opening_track_when_fight_opens(self):
        class PlayingMusic:
            def __init__(self):
                self.loaded = []
                self.busy = False

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

            def load(self, path):
                self.loaded.append(Path(path).name)

            def set_volume(self, _volume):
                pass

            def play(self, **_kwargs):
                self.busy = True

        with TemporaryDirectory() as temp:
            now = [1.0]
            music = PlayingMusic()
            mixer = type('Mixer', (), {'music': music})()
            controller = music_player.MusicController(
                root=Path(temp), mixer=mixer, clock=lambda: now[0], start_sampler=False)
            fighter_id = next(iter(music_player.CHAR_NAMES))
            fighter_name = music_player.CHAR_NAMES[fighter_id]
            folder = Path(temp) / 'characters' / f'{fighter_id:02d}_{music_player._slug(fighter_name)}'
            folder.mkdir(parents=True)
            (folder / 'first.ogg').touch()
            (folder / 'second.ogg').touch()
            next_fighter = next(cid for cid in music_player.CHAR_NAMES if cid != fighter_id)
            next_name = music_player.CHAR_NAMES[next_fighter]
            next_folder = Path(temp) / 'characters' / f'{next_fighter:02d}_{music_player._slug(next_name)}'
            next_folder.mkdir(parents=True)
            (next_folder / 'new_opener.ogg').touch()
            controller.enabled = True
            controller._maintain_bgm = lambda **_kwargs: setattr(
                controller, '_native_zero_confirmed', not controller._native_fallback)

            def reading(phase, *, idle=False, fighter=None):
                selected = fighter_id if fighter is None else fighter
                return {'scene': 'battle' if phase == 4 else 'loading', 'stage': 1,
                        'fighters': {'P1-C1': selected, 'P2-C1': selected},
                        'points': {'P1': 'P1-C1', 'P2': 'P2-C1'} if idle else {},
                        'evidence': {'native_phase': phase, 'both_points_idle': idle}}

            controller._accept_observation(reading(2))
            controller.tick()
            self.assertEqual(music.loaded, ['first.ogg'])
            now[0] += 0.2
            controller._accept_observation(reading(4))
            controller.tick()
            self.assertEqual(controller._observed['scene'], 'loading')
            now[0] += 0.2
            controller._accept_observation(reading(4, idle=True))
            controller.tick()
            self.assertEqual(controller._observed['scene'], 'battle')
            self.assertEqual(music.loaded, ['first.ogg'])

            now[0] += 0.2
            controller._accept_observation(reading(9))
            now[0] += 0.2
            controller._accept_observation(reading(2))
            now[0] += 0.2
            controller._accept_observation(reading(4, fighter=next_fighter, idle=True))
            self.assertIsNone(controller._opening_character)
            controller.tick()
            now[0] += music_player.FADE_OUT_SECONDS + 0.1
            controller.tick()
            self.assertEqual(controller._opening_character, next_fighter)
            self.assertEqual(music.loaded, ['first.ogg', 'new_opener.ogg'])


if __name__ == '__main__':
    unittest.main()

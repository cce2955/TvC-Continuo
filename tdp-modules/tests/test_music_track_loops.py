"""Timed stage/fighter loops keep the intro separate from the repeated section."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import wave

from tvcgui.features.audio.music_player import MusicController
from tvcgui.features.audio.music_window import _loop_group, _parse_loop_time
from tvcgui.features.audio.loop_preview import song_duration


class Music:
    def __init__(self):
        self.calls = []
        self.position_ms = 0
        self.busy = False

    def stop(self):
        self.busy = False

    def load(self, path):
        self.calls.append(('load', path))

    def set_volume(self, _value):
        pass

    def play(self, **options):
        self.calls.append(('play', options))
        self.position_ms = 0
        self.busy = True

    def get_busy(self):
        return self.busy

    def get_pos(self):
        return self.position_ms


class MusicTrackLoopTests(unittest.TestCase):
    def make_controller(self, root, music, now):
        controller = MusicController(root=root, mixer=type('Mixer', (), {'music': music})(),
                                     clock=lambda: now[0], start_sampler=False)
        controller.enabled = True
        controller._source = 'stage'
        controller._observed = {'scene': 'battle', 'stage': 7, 'fighters': {},
                                'points': {}, 'evidence': {}}
        controller._maintain_bgm = lambda **_kwargs: setattr(
            controller, '_native_zero_confirmed', True)
        return controller

    def test_intro_then_loop_section_and_saved_settings(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            track = root / 'stages' / 'Daigo Temple (Cherry Blossoms)' / 'theme.ogg'
            track.parent.mkdir(parents=True)
            track.touch()
            now = [1.0]
            music = Music()
            controller = self.make_controller(root, music, now)
            name = controller._track_identity(track)
            controller.set_loop_setting(name, start=3.0, end=12.0, first='beginning')
            controller.tick()
            self.assertEqual(music.calls[-1], ('play', {'loops': 0, 'fade_ms': 160}))

            music.position_ms = 11_990
            now[0] += 0.2
            controller.tick()
            self.assertEqual(len([call for call in music.calls if call[0] == 'play']), 1)
            music.position_ms = 12_000
            now[0] += 0.2
            controller.tick()
            self.assertEqual(music.calls[-1],
                             ('play', {'loops': 0, 'fade_ms': 0, 'start': 3.0}))
            music.position_ms = 9_000
            now[0] += 0.2
            controller.tick()
            self.assertEqual(len([call for call in music.calls if call[0] == 'play']), 3)

            restored = MusicController(root=root, start_sampler=False)
            self.assertEqual(restored.loop_setting(name),
                             {'start': 3.0, 'end': 12.0, 'first': 'beginning'})

    def test_first_play_can_start_at_loop_point(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            track = root / 'stages' / 'Daigo Temple (Cherry Blossoms)' / 'theme.mp3'
            track.parent.mkdir(parents=True)
            track.touch()
            now = [1.0]
            music = Music()
            controller = self.make_controller(root, music, now)
            controller.set_loop_setting(controller._track_identity(track), start=5,
                                        end=20, first='loop')
            controller.tick()
            self.assertEqual(music.calls[-1],
                             ('play', {'loops': 0, 'fade_ms': 160, 'start': 5.0}))

    def test_fighter_song_loops_until_its_music_owner_changes(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            track = root / 'characters' / '24_yami' / 'theme.ogg'
            track.parent.mkdir(parents=True)
            track.touch()
            now = [1.0]
            music = Music()
            controller = self.make_controller(root, music, now)
            controller._source = 'fighter'
            controller._slot = 'P2-point'
            controller._fighter_switch = 'match'
            controller._observed['fighters'] = {'P2-C1': 24}
            controller._observed['points'] = {'P2': 'P2-C1'}
            controller.set_loop_setting(controller._track_identity(track), start=2, end=5)
            controller.tick()
            self.assertEqual(music.calls[-1], ('play', {'loops': 0, 'fade_ms': 160}))
            music.position_ms = 5_000
            now[0] += 0.2
            controller.tick()
            self.assertEqual(music.calls[-1],
                             ('play', {'loops': 0, 'fade_ms': 0, 'start': 2.0}))

    def test_stage_defaults_to_one_song_per_match(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'stages' / 'Daigo Temple (Cherry Blossoms)'
            folder.mkdir(parents=True)
            for name in ('a.ogg', 'b.ogg'):
                (folder / name).touch()
            now = [1.0]
            music = Music()
            controller = self.make_controller(root, music, now)
            controller.tick()
            self.assertEqual(Path(music.calls[0][1]).name, 'a.ogg')
            self.assertEqual(music.calls[-1], ('play', {'loops': -1, 'fade_ms': 160}))

            music.busy = False  # Even an unexpected mixer stop keeps this song.
            now[0] += 0.2
            controller.tick()
            self.assertEqual(len([c for c in music.calls if c[0] == 'load']), 1)
            self.assertEqual(music.calls[-1], ('play', {'loops': -1, 'fade_ms': 0}))

            controller._match_serial += 1
            now[0] += 0.2
            controller.tick()
            self.assertEqual(Path([c for c in music.calls if c[0] == 'load'][-1][1]).name,
                             'b.ogg')

    def test_stage_playlist_advances_and_ignores_loop_points(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'stages' / 'Daigo Temple (Cherry Blossoms)'
            folder.mkdir(parents=True)
            tracks = [folder / name for name in ('a.ogg', 'b.ogg')]
            for track in tracks:
                track.touch()
            now = [1.0]
            music = Music()
            controller = self.make_controller(root, music, now)
            controller.set_loop_setting(controller._track_identity(tracks[0]),
                                        start=2, end=5, first='loop')
            controller.configure(stage_end_mode='playlist')
            controller.tick()
            self.assertEqual(music.calls[-1], ('play', {'loops': 0, 'fade_ms': 160}))
            music.position_ms = 5_000
            now[0] += 0.2
            controller.tick()
            self.assertEqual(len([c for c in music.calls if c[0] == 'play']), 1)
            music.busy = False
            now[0] += 0.2
            controller.tick()
            self.assertEqual(Path([c for c in music.calls if c[0] == 'load'][-1][1]).name,
                             'b.ogg')
            restored = MusicController(root=root, start_sampler=False)
            self.assertEqual(restored.status()['stage_end_mode'], 'playlist')
            self.assertEqual(restored.status()['fighter_end_mode'], 'match')

    def test_fighter_playlist_can_advance(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'characters' / '24_yami'
            folder.mkdir(parents=True)
            for name in ('a.ogg', 'b.ogg'):
                (folder / name).touch()
            now = [1.0]
            music = Music()
            controller = self.make_controller(root, music, now)
            controller._source = 'fighter'
            controller._slot = 'P2-point'
            controller._fighter_switch = 'match'
            controller._observed['fighters'] = {'P2-C1': 24}
            controller._observed['points'] = {'P2': 'P2-C1'}
            controller.configure(fighter_end_mode='playlist')
            controller.tick()
            self.assertEqual(music.calls[-1], ('play', {'loops': 0, 'fade_ms': 160}))
            music.busy = False
            now[0] += 0.2
            controller.tick()
            self.assertEqual(Path([c for c in music.calls if c[0] == 'load'][-1][1]).name,
                             'b.ogg')

    def test_loop_config_rejects_bad_range_and_wav_seek(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            track = root / 'characters' / '24_yami' / 'theme.wav'
            track.parent.mkdir(parents=True)
            track.touch()
            controller = MusicController(root=root, start_sampler=False)
            name = controller._track_identity(track)
            with self.assertRaises(ValueError):
                controller.set_loop_setting(name, start=10, end=9)
            with self.assertRaises(ValueError):
                controller.set_loop_setting(name, start=1, end=10)
            controller.set_loop_setting(name, start=0, end=10)
            self.assertEqual(controller.loop_setting(name)['end'], 10)
            controller.set_loop_setting(name)
            self.assertIsNone(controller.loop_setting(name))
        self.assertEqual(_parse_loop_time('1:02.5'), 62.5)
        self.assertEqual(_loop_group('stages/Yami - Second Form/theme.ogg'),
                         ('Stages', 'Yami - Second Form', 'theme.ogg'))
        self.assertEqual(_loop_group('characters/24_yami/theme.mp3'),
                         ('Characters', '24_yami', 'theme.mp3'))
        self.assertEqual(_loop_group('menus/Winner Screen/theme.mp3'),
                         ('Menus', 'Winner Screen', 'theme.mp3'))
        self.assertEqual(_loop_group('extras/Ending 1/theme.ogg'),
                         ('Extras', 'Ending 1', 'theme.ogg'))
        self.assertEqual(_loop_group('character_select/theme.ogg'),
                         ('Legacy scenes', 'Character Select', 'theme.ogg'))

    def test_menu_and_extra_tracks_are_available_for_loops(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            tracks = [root / 'menus' / 'Main Menu' / 'menu.ogg',
                      root / 'extras' / 'Ending 1' / 'ending.mp3',
                      root / 'character_select' / 'legacy.ogg',
                      root / 'winner' / 'winner.ogg']
            for track in tracks:
                track.parent.mkdir(parents=True, exist_ok=True)
                track.touch()
            controller = MusicController(root=root, start_sampler=False)
            names = {controller._track_identity(track) for track in tracks}
            self.assertTrue(names.issubset(controller.loop_tracks()))
            for name in names:
                controller.set_loop_setting(name, start=2, end=8, first='loop')
                self.assertEqual(controller.loop_setting(name)['end'], 8.0)
            restored = MusicController(root=root, start_sampler=False)
            self.assertTrue(all(restored.loop_setting(name) for name in names))

    def test_menu_song_uses_saved_loop_points_during_playback(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            track = root / 'menus' / 'Main Menu' / 'menu.ogg'
            track.parent.mkdir(parents=True)
            track.touch()
            now = [1.0]
            music = Music()
            controller = self.make_controller(root, music, now)
            controller._observed['scene'] = 'menu'
            controller.set_loop_setting(controller._track_identity(track),
                                        start=2, end=5, first='beginning')
            controller.tick()
            self.assertEqual(music.calls[-1], ('play', {'loops': 0, 'fade_ms': 160}))
            music.position_ms = 5_000
            now[0] += 0.2
            controller.tick()
            self.assertEqual(music.calls[-1],
                             ('play', {'loops': 0, 'fade_ms': 0, 'start': 2.0}))

    def test_song_length_reads_wav_without_changing_playback(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            track = root / 'stages' / 'Daigo Temple (Cherry Blossoms)' / 'theme.wav'
            track.parent.mkdir(parents=True)
            with wave.open(str(track), 'wb') as audio:
                audio.setnchannels(1)
                audio.setsampwidth(2)
                audio.setframerate(8000)
                audio.writeframes(b'\0' * 24_000)
            music = Music()
            controller = self.make_controller(root, music, [1.0])
            self.assertEqual(controller.loop_duration(controller._track_identity(track)), 1.5)
            self.assertEqual(music.calls, [])

    def test_song_length_uses_decoder_for_compressed_audio(self):
        class Decoder:
            def __init__(self):
                self.initialized = False

            def get_init(self):
                return self.initialized

            def init(self, **_options):
                self.initialized = True

            def Sound(self, _path):
                return type('Sound', (), {'get_length': lambda _self: 93.25})()

        with TemporaryDirectory() as temp:
            track = Path(temp) / 'theme.ogg'
            track.touch()
            decoder = Decoder()
            self.assertEqual(song_duration(decoder, track), 93.25)
            self.assertTrue(decoder.initialized)


if __name__ == '__main__':
    unittest.main()

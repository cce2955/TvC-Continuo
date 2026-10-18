"""Preview audio straddles the entered loop seam without replacing music."""
import os
import struct
import wave
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tvcgui.features.audio.loop_preview import build_loop_preview
from tvcgui.features.audio.music_player import MusicController


class LoopPreviewTests(unittest.TestCase):
    def test_preview_plays_audio_before_end_then_after_start(self):
        os.environ['SDL_AUDIODRIVER'] = 'dummy'
        import pygame

        pygame.mixer.quit()
        pygame.mixer.init(frequency=8000, size=-16, channels=1)
        try:
            with TemporaryDirectory() as temp:
                song = Path(temp) / 'song.wav'
                with wave.open(str(song), 'wb') as output:
                    output.setnchannels(1)
                    output.setsampwidth(2)
                    output.setframerate(8000)
                    output.writeframes(b''.join(struct.pack('<h', second * 1000) * 8000
                                                for second in range(8)))
                preview, before, boundary, repeat, after = build_loop_preview(
                    pygame.mixer, song, start=2, end=6)
                self.assertEqual((before, boundary, repeat, after), (3, 6, 2, 5))
                samples = pygame.sndarray.array(preview)
                self.assertEqual(len(samples), 6 * 8000)
                self.assertEqual([int(samples[second * 8000]) for second in range(6)],
                                 [3000, 4000, 5000, 2000, 3000, 4000])
                _, before, boundary, repeat, after = build_loop_preview(
                    pygame.mixer, song, start=2, end=None)
                self.assertEqual((before, boundary, repeat, after), (5, 8, 2, 5))
                with self.assertRaises(ValueError):
                    build_loop_preview(pygame.mixer, song, start=2, end=9)
        finally:
            pygame.mixer.quit()

    def test_preview_uses_separate_channel_and_restores_live_volume(self):
        class Channel:
            def __init__(self):
                self.busy = True
                self.volume = None

            def set_volume(self, value):
                self.volume = value

            def get_busy(self):
                return self.busy

            def stop(self):
                self.busy = False

        class Sound:
            def __init__(self, channel):
                self.channel = channel

            def play(self):
                return self.channel

        class Music:
            def __init__(self):
                self.volumes = []

            def set_volume(self, value):
                self.volumes.append(value)

        with TemporaryDirectory() as temp:
            root = Path(temp)
            song = root / 'stages' / 'Yami - Second Form' / 'song.mp3'
            song.parent.mkdir(parents=True)
            song.touch()
            channel = Channel()
            music = Music()
            controller = MusicController(root=root,
                                         mixer=type('Mixer', (), {'music': music})(),
                                         start_sampler=False)
            controller._current_file = str(song)
            controller._current_owner = 'stages/s_bgm_33'
            with patch('tvcgui.features.audio.loop_preview.build_loop_preview',
                       return_value=(Sound(channel), 3, 6, 2, 5)):
                self.assertEqual(controller.preview_loop('stages/Yami - Second Form/song.mp3',
                                                         start=2, end=6), (3, 6, 2, 5))
            self.assertEqual(music.volumes[-1], controller._volume * 0.15)
            self.assertEqual(controller._current_file, str(song))
            controller.stop_loop_preview()
            self.assertFalse(channel.busy)
            self.assertEqual(music.volumes[-1], controller._volume)


if __name__ == '__main__':
    unittest.main()

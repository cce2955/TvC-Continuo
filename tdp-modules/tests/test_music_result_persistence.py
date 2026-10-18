"""Result screens keep their soundtrack across unclassified transition frames."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tvcgui.features.audio.music_player import MusicController, UNKNOWN_MATCH_RELEASE_SAMPLES


class MusicResultPersistenceTests(unittest.TestCase):
    def test_winner_survives_unknown_until_verified_menu(self):
        with TemporaryDirectory() as temp:
            controller = MusicController(root=Path(temp), start_sampler=False)
            battle = {'scene': 'battle', 'stage': 6, 'fighters': {'P1-C1': 29},
                      'points': {}, 'evidence': {'native_phase': 4}}
            result = {'scene': 'winner', 'stage': 6, 'fighters': {},
                      'points': {}, 'evidence': {'native_phase': 11}}
            unknown = {'scene': 'unknown', 'stage': 13, 'fighters': {},
                       'points': {}, 'evidence': {'native_phase': None,
                                                  'stage_root_type': 6,
                                                  'select_scene_state': 12}}
            menu = {'scene': 'menu', 'stage': None, 'fighters': {},
                    'points': {}, 'evidence': {'native_phase': None,
                                               'menu_object': True,
                                               'menu_asset_verified': True}}
            controller._accept_observation(battle)
            controller._accept_observation(result)
            controller._accept_observation(unknown)
            self.assertEqual(controller._observed['scene'], 'winner')
            for _ in range(UNKNOWN_MATCH_RELEASE_SAMPLES + 5):
                controller._accept_observation(unknown)
            self.assertEqual(controller._observed['scene'], 'winner')
            self.assertEqual(controller._expected_ssd(controller._observed), 's_bgm_26')
            for _ in range(3):
                controller._accept_observation(menu)
            self.assertEqual(controller._observed['scene'], 'menu')

    def test_missing_ax_pair_retries_quickly_outside_match(self):
        with TemporaryDirectory() as temp:
            now = [1.0]
            controller = MusicController(root=Path(temp), clock=lambda: now[0],
                                         start_sampler=False)
            controller.enabled = True
            controller._requested_mute = True
            controller._backend = lambda: object()
            controller._write_protected = lambda: False
            calls = []

            def apply(*_args, **_kwargs):
                calls.append(now[0])
                return SimpleNamespace(muted=False, ready=False, found=0,
                                       rerouted=False, text='AX BGM pair not verified')

            controller._ax_gate.apply = apply
            controller._maintain_bgm()
            self.assertEqual(calls, [1.0])
            self.assertGreater(controller._mute_urgent_until, now[0])
            now[0] += 0.03
            controller._maintain_bgm()
            self.assertEqual(calls, [1.0, 1.03])

    def test_winner_releases_music_when_next_round_loading_begins(self):
        with TemporaryDirectory() as temp:
            controller = MusicController(root=Path(temp), start_sampler=False)
            controller._accept_observation({
                'scene': 'battle', 'stage': 6, 'fighters': {'P1-C1': 1},
                'points': {}, 'evidence': {'native_phase': 4}})
            result = {'scene': 'winner', 'stage': 6, 'fighters': {},
                      'points': {}, 'evidence': {'native_phase': 11}}
            controller._accept_observation(result)
            controller._accept_observation({'scene': 'unknown', 'stage': 13,
                                            'fighters': {}, 'points': {},
                                            'evidence': {'native_phase': None}})
            self.assertEqual(controller._observed['scene'], 'winner')
            controller._accept_observation({'scene': 'loading', 'stage': 13,
                                            'fighters': {}, 'points': {},
                                            'evidence': {'native_phase': 2}})
            self.assertEqual(controller._observed['scene'], 'loading')
            self.assertEqual(controller._observed['stage'], 13)
            self.assertTrue(controller._pending_match_boundary)
            controller._accept_observation({'scene': 'unknown', 'stage': 13,
                                            'fighters': {}, 'points': {},
                                            'evidence': {'native_phase': None}})
            controller._accept_observation({'scene': 'unknown', 'stage': 13,
                                            'fighters': {}, 'points': {},
                                            'evidence': {'native_phase': None}})
            self.assertEqual(controller._observed['scene'], 'loading')
            self.assertEqual(controller._observed['stage'], 13)
            controller._accept_observation({'scene': 'battle', 'stage': 13,
                                            'fighters': {}, 'points': {},
                                            'evidence': {'native_phase': 4,
                                                         'both_points_idle': True}})
            self.assertEqual(controller._observed['scene'], 'battle')
            self.assertEqual(controller._match_serial, 1)


if __name__ == '__main__':
    unittest.main()

"""Result screens keep their soundtrack across unclassified transition frames."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tvcgui.features.audio.music_player import MusicController, UNKNOWN_MATCH_RELEASE_SAMPLES, _read_director, NATIVE_BATTLE_PTR


class MusicResultPersistenceTests(unittest.TestCase):
    def test_cached_select_asset_releases_on_director_load_after_menu_closes(self):
        with TemporaryDirectory() as temp:
            c = MusicController(root=Path(temp), start_sampler=False)
            def sample(scene, ptr, phase, ui):
                c._accept_observation(dict(scene=scene, stage=17, evidence=dict(
                    native_phase=phase, director_pointer=ptr, raw_stage=17,
                    menu_root_pointer=0x92460000 if ui else 0,
                    stage_root_type=15 if ui else None, character_ui_live=ui,
                    select_asset=True)))
            sample('character_select', 0, None, True)
            sample('loading', 0x90800000, 1, True)
            self.assertEqual(c._observed['scene'], 'character_select')
            sample('loading', 0x90800000, 2, False)
            self.assertEqual(c._observed['scene'], 'loading')
            self.assertFalse(c._fight_opened)
            event = c.research_operations_since(0)['events'][-1]
            self.assertEqual((event['operation'], event['scene'], event['phase']),
                             ('scene_boundary', 'loading', 2))

    def test_pointer_recycled_or_unreadable_is_distinct_from_clear(self):
        base = 0x90800000
        def read(values):
            words = iter(values)
            return _read_director(SimpleNamespace(rd32=lambda _: next(words)))
        self.assertEqual(read([base, 4, base]), (base, 4))
        self.assertEqual(read([base, 4, base + 0x400]), (None, None))
        self.assertEqual(read([None]), (None, None))
        self.assertEqual(read([0]), (0, None))

    def test_sampler_routes_on_confirmed_teardown_and_same_stage_restart(self):
        from unittest.mock import patch
        from tvcgui.features.audio import music_player
        # Replay a cached roster throughout, with no point/idle confirmation.
        phases = [4]*5 + [5, 6, 4] + [9, 9, 12, 11, None, None, 0, 1, 2, 4, 4, 4]
        phases += [0, 1, 2, 4, 4, 4]
        with TemporaryDirectory() as temp:
            index = -1
            def wait(_delay):
                nonlocal index
                index += 1
                return index >= len(phases)
            def read_word(address):
                if address == NATIVE_BATTLE_PTR:
                    return 0x90800000 if phases[index] is not None else 0
                return phases[index]
            io = SimpleNamespace(rd32=read_word)
            c = MusicController(root=Path(temp), io=io, clock=lambda: index * 0.08,
                                start_sampler=False)
            c.enabled = True
            def identity(_io):
                phase = phases[index]
                scene = ('battle' if phase == 4 else 'unknown' if phase is None else
                         'loading' if phase < 4 else 'winner' if phase >= 9 else 'post_battle')
                return dict(scene=scene, stage=15, fighters={'P1-C1': 20}, points={},
                            evidence={'native_phase': phase, 'director_pointer': read_word(NATIVE_BATTLE_PTR)})
            with patch.object(music_player, '_read_environment', identity):
                c._sample_loop(SimpleNamespace(wait=wait))
            events = c.research_operations_since(0)['events']
            self.assertEqual([(e['detail'], e['match_serial']) for e in events
                              if e['operation'] == 'match_boundary'],
                             [('live battle', 0), ('results', 0),
                              ('live battle', 1), ('live battle', 2)])

    def test_winner_survives_unknown_until_verified_menu(self):
        with TemporaryDirectory() as temp:
            controller = MusicController(root=Path(temp), start_sampler=False)
            battle = {'scene': 'battle', 'stage': 6, 'fighters': {'P1-C1': 29},
                      'points': {}, 'evidence': {'native_phase': 4}}
            result = {'scene': 'winner', 'stage': 6, 'fighters': {},
                      'points': {}, 'evidence': {'native_phase': 11}}
            unknown = {'scene': 'unknown', 'stage': 13, 'fighters': {},
                       'points': {}, 'evidence': {'director_pointer': 0, 'native_phase': None,
                                                  'stage_root_type': 6,
                                                  'select_scene_state': 12}}
            menu = {'scene': 'menu', 'stage': None, 'fighters': {},
                    'points': {}, 'evidence': {'director_pointer': 0, 'native_phase': None,
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

    def test_verified_select_music_rechecks_routes_between_scene_scans(self):
        from unittest.mock import Mock
        with TemporaryDirectory() as temp:
            now = [10.0]
            c = MusicController(root=Path(temp), clock=lambda: now[0], start_sampler=False)
            c.enabled = c._requested_mute = True
            c._observed = {'scene': 'stage_select'}
            c._backend = lambda: object()
            c._ax_gate.apply = Mock(return_value=SimpleNamespace(
                muted=True, ready=True, found=2, rerouted=False, text='verified'))
            c._maintain_bgm(force=True)
            now[0] += 0.005
            c._maintain_bgm()
            self.assertEqual(c._ax_gate.apply.call_count, 1)
            now[0] += 0.025
            c._maintain_bgm()
            self.assertEqual(c._ax_gate.apply.call_count, 2)

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
                                            'evidence': {'director_pointer': 0, 'native_phase': None}})
            self.assertEqual(controller._observed['scene'], 'winner')
            controller._accept_observation({'scene': 'loading', 'stage': 13,
                                            'fighters': {}, 'points': {},
                                            'evidence': {'native_phase': 2}})
            self.assertEqual(controller._observed['scene'], 'loading')
            self.assertEqual(controller._observed['stage'], 13)
            self.assertTrue(controller._pending_match_boundary)
            controller._accept_observation({'scene': 'unknown', 'stage': 13,
                                            'fighters': {}, 'points': {},
                                            'evidence': {'director_pointer': 0, 'native_phase': None}})
            controller._accept_observation({'scene': 'unknown', 'stage': 13,
                                            'fighters': {}, 'points': {},
                                            'evidence': {'director_pointer': 0, 'native_phase': None}})
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

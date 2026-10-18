"""Optional low-health and fighter action music rules."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tvcgui.features.audio.music_player import MusicController
from tvcgui.features.audio.music_window import _loop_group


def observed(*, remaining=30_000, stage=7):
    return {'scene': 'battle', 'stage': stage,
            'fighters': {'P1-C1': 1, 'P1-C2': 2, 'P2-C1': 3, 'P2-C2': 4},
            'points': {'P1': 'P1-C2', 'P2': 'P2-C1'},
            'health': {'P1-C1': {'cur': 0, 'max': 40_000},
                       'P1-C2': {'cur': remaining, 'max': 100_000},
                       'P2-C1': {'cur': 40_000, 'max': 40_000},
                       'P2-C2': {'cur': 40_000, 'max': 40_000}},
            'actions': {}, 'evidence': {'native_phase': 4}}


class MusicRulesTests(unittest.TestCase):
    def test_low_health_falls_back_then_uses_survivor_song(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            controller = MusicController(root=root, start_sampler=False)
            normal = controller._candidate_dirs('characters', 1)[0] / 'normal.ogg'
            special = (root / 'low_health' /
                       controller._candidate_dirs('characters', 2)[0].relative_to(root) /
                       'last_fighter.ogg')
            normal.parent.mkdir(parents=True)
            normal.touch()
            controller.configure(low_health_enabled=True, low_health_threshold=30)
            controller._source = 'fighter'
            controller._slot = 'P1-point'
            controller._fighter_switch = 'match'
            controller._opening_character = 1
            state = observed()
            owner, tracks = controller._context(state)
            self.assertEqual(owner, 'characters/1')
            self.assertEqual(tracks, [normal])
            special.parent.mkdir(parents=True)
            special.touch()
            controller._song_cache.clear()
            owner, tracks = controller._context(state)
            self.assertEqual(owner, 'low_health/characters/2')
            self.assertEqual(tracks, [special])
            self.assertEqual(controller._end_mode_for_owner(owner), 'match')
            self.assertIn(controller._track_identity(special), controller.loop_tracks())
            self.assertEqual(_loop_group(controller._track_identity(special)),
                             ('Low health', f'Characters / {special.parent.name}', special.name))
            controller.set_loop_setting(controller._track_identity(special), start=2, end=8)
            self.assertEqual(controller.loop_setting(controller._track_identity(special))['end'], 8)

            # The threshold and live-phase checks leave the normal theme alone.
            self.assertEqual(controller._context(observed(remaining=30_001))[0], 'characters/1')
            post_battle = observed()
            post_battle['evidence']['native_phase'] = 9
            self.assertEqual(controller._context(post_battle)[0], 'characters/1')

    def test_stage_low_health_uses_own_folder_and_saved_preferences(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)
            controller = MusicController(root=root, start_sampler=False)
            controller._source = 'stage'
            special = (root / 'low_health' /
                       controller._candidate_dirs('stages', 7)[0].relative_to(root) /
                       'danger.ogg')
            special.parent.mkdir(parents=True)
            special.touch()
            controller.configure(low_health_enabled=True, low_health_threshold=35,
                                 ignored_point_actions=['tag_out', 'dhc'],
                                 fighter_change_cooldown=2.5)
            owner, tracks = controller._context(observed(remaining=30_000))
            self.assertEqual(owner, 'low_health/stages/7')
            self.assertEqual(tracks, [special])
            restored = MusicController(root=root, start_sampler=False)
            status = restored.status()
            self.assertTrue(status['low_health_enabled'])
            self.assertEqual(status['low_health_threshold'], 35)
            self.assertEqual(status['ignored_point_actions'], ['dhc', 'tag_out'])
            self.assertEqual(status['fighter_change_cooldown'], 2.5)

    def test_ignored_actions_and_cooldown_consume_point_changes(self):
        with TemporaryDirectory() as temp:
            now = [1.0]
            controller = MusicController(root=Path(temp), clock=lambda: now[0],
                                         start_sampler=False)
            controller._fighter_switch = 'tag'
            controller._slot = 'P1-point'
            controller._opening_character = 1
            controller.configure(ignored_point_actions=['tag_out'],
                                 fighter_change_cooldown=2)
            state = observed()
            state['points']['P1'] = 'P1-C1'
            controller._update_tag_owner(state, entered_battle=True)
            controller._update_tag_owner(state)
            self.assertEqual(controller._tag_event_serial, 0)

            state['points']['P1'] = 'P1-C2'
            state['actions'] = {'P1-C1': 416, 'P1-C2': 418}
            controller._update_tag_owner(state)
            self.assertEqual(controller._tag_event_serial, 0)
            self.assertEqual(controller._tag_team_slots['P1'], 'P1-C2')

            state['points']['P1'] = 'P1-C1'
            state['actions'] = {'P1-C1': 428}
            controller._update_tag_owner(state)
            self.assertEqual(controller._tag_event_serial, 1)
            controller._last_fighter_music_change_at = now[0]

            now[0] += 0.5
            state['points']['P1'] = 'P1-C2'
            state['actions'] = {'P1-C2': 427}
            controller._update_tag_owner(state)
            self.assertEqual(controller._tag_event_serial, 1)
            self.assertEqual(controller._tag_team_slots['P1'], 'P1-C2')

            now[0] += 2.1
            state['points']['P1'] = 'P1-C1'
            state['actions'] = {'P1-C1': 421}
            controller._update_tag_owner(state)
            self.assertEqual(controller._tag_event_serial, 2)

    def test_action_ids_are_classified_separately(self):
        with TemporaryDirectory() as temp:
            controller = MusicController(root=Path(temp), start_sampler=False)
            for action, expected in ((418, 'tag_out'), (428, 'dhc'),
                                     (421, 'var'), (427, 'cross_counter')):
                state = {'actions': {'P1-C2': action}}
                self.assertEqual(controller._point_change_kind(state, 'P1-C1', 'P1-C2'),
                                 expected)
            self.assertIsNone(controller._point_change_kind({'actions': {}}, 'P1-C1', 'P1-C2'))


if __name__ == '__main__':
    unittest.main()

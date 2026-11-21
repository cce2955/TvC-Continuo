from pathlib import Path

from tvcgui.features.audio.music_player import MusicController


def test_yami_boundary_waits_during_loading_and_selects_fresh_form(tmp_path):
    controller = MusicController(root=Path(tmp_path), clock=lambda: 100.0, start_sampler=False)
    controller.enabled = True
    controller._mode = 'auto'
    controller._output = 'custom'
    controller._source = 'stage'
    controller._pending_match_boundary = True
    controller._observed = {'scene': 'loading', 'stage': 15, 'fighters': {'P2-C1': 24}, 'evidence': {}}
    controller._current_file = 'old-song.mp3'
    controller._current_owner = 'stages/s_bgm_33'
    controller._requested_mute = True
    gate_requests = []
    controller._maintain_bgm = lambda: gate_requests.append(controller._requested_mute)
    stopped = []
    def stop_audio(**kwargs):
        stopped.append(kwargs.get('reason'))
        controller._current_file = None
    controller._stop_audio = stop_audio
    controller._context = lambda _state: (_ for _ in ()).throw(AssertionError('selected while loading'))
    controller.tick()
    assert stopped == ['yami_form_pending']
    assert controller._current_owner is None
    assert gate_requests == [False] and controller._native_fallback
    assert 'Waiting' in controller._message

    controller._observed = {'scene': 'battle', 'stage': 15, 'fighters': {'P2-C1': 25}, 'evidence': {}}
    controller._pending_match_boundary = False
    assert controller._yami_form_resource(controller._observed) == 's_bgm_34'
    assert not controller._wait_for_yami_match_boundary()


def test_yami_wait_does_not_apply_to_overrides_or_other_scenes(tmp_path):
    controller = MusicController(root=Path(tmp_path), start_sampler=False)
    controller._mode = 'auto'
    controller._output = 'custom'
    controller._source = 'stage'
    controller._pending_match_boundary = True
    controller._observed = {'scene': 'loading', 'stage': 15, 'fighters': {}}
    assert controller._wait_for_yami_match_boundary()
    controller._stage_override = 15
    assert not controller._wait_for_yami_match_boundary()
    controller._stage_override = None
    controller._source = 'fighter'
    assert not controller._wait_for_yami_match_boundary()
    controller._source = 'stage'
    controller._observed['stage'] = 8
    assert not controller._wait_for_yami_match_boundary()

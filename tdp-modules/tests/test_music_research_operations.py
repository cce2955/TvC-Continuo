from pathlib import Path
import json
import time
from types import SimpleNamespace

import pytest

from tvcgui.features.audio.music_player import MusicController
from tvcgui.features.combat.research_capture import ChangeRecorder, CaptureService, utc_now
from tvcgui.features.combat import research_capture
from tvcgui.features.combat.research_capture_panel import _timeline_text


class Music:
    def __init__(self):
        self.fail = False

    def play(self, **kwargs):
        if self.fail:
            raise RuntimeError('play failed')

    def stop(self):
        pass


class Mixer:
    def __init__(self):
        self.music = Music()


def test_player_operations_have_sequence_timestamp_and_errors(tmp_path):
    mixer = Mixer()
    controller = MusicController(root=tmp_path, mixer=mixer, start_sampler=False)
    controller._play_music_segment(Path('first.mp3'), start=0, loops=0)
    controller._stop_audio(reason='new_match')
    mixer.music.fail = True
    with pytest.raises(RuntimeError):
        controller._play_music_segment(Path('next.mp3'), start=0, loops=0)
    history = controller.research_operations_since(0)
    events = history['events']
    assert [item['operation'] for item in events] == ['play', 'stop', 'play']
    assert [item['sequence'] for item in events] == [1, 2, 3]
    assert all(item['timestamp_utc'].endswith('+00:00') for item in events)
    assert events[-1]['result'] == 'error' and events[-1]['error'] == 'play failed'
    assert controller.research_operations_since(2)['events'] == events[2:]


def test_music_operations_are_preserved_and_formatted():
    recorder = ChangeRecorder(limit=2)
    recorder._event({'type': 'music_operation', 'timestamp_utc': '2026-11-10T14:54:26+00:00',
                     'operation': {'operation': 'play', 'track': 'song.mp3', 'result': 'ok'}})
    for value in range(5):
        recorder._event({'type': 'field_change', 'field': 'position_x', 'new_value': value})
    operation = recorder.retained_events()[0]
    assert operation['type'] == 'music_operation'
    assert 'play ok song.mp3' in _timeline_text(operation)


def test_gate_transitions_are_recorded_without_changing_fail_safe(tmp_path):
    controller = MusicController(root=tmp_path, mixer=Mixer(), start_sampler=False)
    controller.enabled = True
    controller._requested_mute = True
    controller._native_zero_confirmed = True
    controller._current_file = 'first.mp3'
    controller._current_owner = 'stages/17'
    controller._backend = lambda: object()
    controller._write_protected = lambda: False
    controller._ax_gate.apply = lambda *_args, **_kwargs: SimpleNamespace(
        muted=False, ready=False, text='AX BGM pair not verified (0/2); no writes', found=0, rerouted=False)
    controller._maintain_bgm(force=True)
    history = controller.research_operations_since(0)['events']
    assert history[0]['operation'] == 'gate_transition' and history[0]['found'] == 0
    assert history[1]['operation'] == 'stop' and history[1]['reason'] == 'ax_unverified'
    assert controller._current_file is None


def test_capture_exports_music_operations_once(monkeypatch, tmp_path):
    events = []
    def history(cursor):
        return {'events': [event for event in events if event['sequence'] > cursor],
                'latest_sequence': len(events), 'lost': 0}
    monkeypatch.setattr(research_capture, 'read_music_operations', history)

    class Sampler:
        def sample(self, slot, advanced=False):
            return {'slot': slot, 'actor': None, 'timestamp_utc': utc_now(),
                    'status': 'unavailable', 'fields': {}, 'read_failures': 0}

    service = CaptureService(sampler=Sampler(), interval=0.01, music_reader=lambda: {})
    try:
        service.set_advanced(True)
        service.start('P1-C1')
        events.append({'sequence': 1, 'timestamp_utc': utc_now(), 'operation': 'load',
                       'track': 'song.mp3', 'result': 'ok'})
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and service.state()['music_operation_status'] != 'history through 1; missed 0 this poll':
            time.sleep(0.01)
        service.stop()
        destination = tmp_path / 'events.jsonl'
        service.export_copy(destination, tmp_path / 'snapshots.csv')
        exported = [json.loads(line) for line in destination.read_text(encoding='utf8').splitlines()]
        operations = [event for event in exported if event['type'] == 'music_operation']
        assert len(operations) == 1 and operations[0]['operation']['track'] == 'song.mp3'
        performance = [event for event in exported if event['type'] == 'capture_performance']
        assert performance and {'music', 'P1-C1', 'assist_diagnostics', 'recording'} <= performance[0]['costs_seconds'].keys()
    finally:
        service.close()

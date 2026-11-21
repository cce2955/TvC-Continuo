from tvcgui.features.combat.research_capture import CaptureService
from tvcgui.features.combat.research_capture_panel import _live_slot_text


def test_live_slot_text_marks_unavailable_and_shows_observation_time():
    assert 'waiting for a sample' in _live_slot_text('P2-C1', None)
    sample = {'actor': None, 'status': 'unavailable: actor lost',
              'timestamp_utc': '2026-11-10T14:54:26.272000+00:00', 'fields': {}}
    text = _live_slot_text('P2-C1', sample)
    assert '14:54:26.272' in text and 'actor unavailable' in text
    assert 'unavailable: actor lost' in text
    assert 'input unavailable/unavailable/unavailable' in text


def test_start_clears_live_samples_from_previous_session():
    service = CaptureService(interval=10)
    try:
        with service.lock:
            service.latest['P2-C1'] = {'slot': 'P2-C1'}
            service.start('ALL')
            assert service.state()['latest_by_slot'] == {}
    finally:
        service.close()

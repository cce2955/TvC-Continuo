from tvcgui.runtime.megacrash import _megacrash_schedule_due


def test_delayed_burst_uses_elapsed_time_when_gui_ticks_are_slow():
    pending = {"fire_frame": 108, "fire_time": 10.1333}
    assert not _megacrash_schedule_due(pending, now=10.10, frame_idx=101)
    assert _megacrash_schedule_due(pending, now=10.15, frame_idx=102)


def test_older_schedule_without_time_uses_frame_fallback():
    pending = {"fire_frame": 108}
    assert not _megacrash_schedule_due(pending, now=10.15, frame_idx=107)
    assert _megacrash_schedule_due(pending, now=10.15, frame_idx=108)

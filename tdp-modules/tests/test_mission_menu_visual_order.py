from tvcgui.features.overlay.master_renderer import MasterOverlay


def test_stale_menu_visual_cannot_reopen_a_closed_selector():
    renderer = object.__new__(MasterOverlay)
    renderer._mission_menu_visual_start_ns = 1
    renderer._mission_menu_visual = {
        "slot": "P1-C1", "open": True, "index": 3,
        "sequence": 4, "updated_ns": 2,
    }
    display = renderer._mission_menu_display_data({
        "slot": "P1-C1", "selector_open": False,
        "selector_index": 0, "selector_command_sequence": 4,
    })
    assert display["selector_open"] is False
    assert display["selector_index"] == 0

    display = renderer._mission_menu_display_data({
        "slot": "P1-C1", "selector_open": False,
        "selector_index": 0, "selector_command_sequence": 3,
    })
    assert display["selector_open"] is True
    assert display["selector_index"] == 3

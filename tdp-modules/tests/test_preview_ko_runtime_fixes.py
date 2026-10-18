from __future__ import annotations

from unittest import mock

import pygame

from tvcgui.runtime import ko_control as ko
from tvcgui.ui import components
from tvcgui.ui import normal_preview as preview
from tvcgui.features.audio.music_player import MusicController


def test_preview_block_advantage_prefers_observed_then_saved_scan():
    move = {"move_name": "5C", "adv_block": -8, "startup": 9}
    assert preview._normal_advantage(move, "block", prefer_observed=True) == -8
    move["adv_block_observed"] = -6
    assert preview._normal_advantage(move, "block", prefer_observed=True) == -6


def test_live_punish_uses_saved_scan_advantage():
    slots = [
        {"slot_label": "P1-C1", "base": 1, "mv_id_display": 258,
         "moves": [{"id": 258, "move_name": "5C", "adv_block": -8, "startup": 9}]},
        {"slot_label": "P2-C1", "base": 2, "mv_id_display": 1,
         "moves": [{"id": 257, "move_name": "5A", "startup": 4, "damage": 1000}]},
    ]
    keys, roles, advantage, source = preview._normal_preview_live_punish_keys(slots)
    assert advantage == -8
    assert source == "P1-C1"
    assert keys["P2-C1"]
    assert roles["P2-C1"]["fast"] in keys["P2-C1"]


def test_block_highlight_uses_the_displayed_advantage():
    slot = {"slot_label": "P1-C1", "moves": [
        {"id": 257, "move_name": "5A", "adv_block": -8},
        {"id": 258, "move_name": "5B", "adv_block": -2},
    ]}
    keys, _, _, _ = preview._normal_preview_highlight_keys([slot], "adv_block", None)
    assert keys["P1-C1"] == {preview._normal_preview_key(slot["moves"][1])}


def test_ko_dead_detection_matches_v41_snapshot_and_live_reads():
    base = 0x90000000
    reads = {base + 0x28: 100, base + 0x1E8: 1,
             base + 0x1EC: 1, base + 0x218: 1}
    with mock.patch.object(ko, "rd32", side_effect=lambda addr: reads.get(addr)):
        assert ko._ko_ctrl_slot_is_dead({"base": 0x90000000, "cur": 0, "attA": 154})
        assert not ko._ko_ctrl_slot_is_dead({"base": 0x90000000, "cur": 100, "attA": 1})
        assert ko._ko_ctrl_slot_is_dead({"base": 0x90000000, "cur": 100, "baroque_local_hp32": 0, "attA": 1})


def test_ko_full_patch_failure_retries_like_v41():
    state = {"both_loaded": True, "any_team_dead": True, "summary": "P2:KO"}
    ko.tick_ko_control_auto.last_mode = "safe"
    ko.tick_ko_control_auto.post_ko_latched = False
    ko.tick_ko_control_auto.full_until = 0.0
    with (
        mock.patch.object(ko, "ko_ctrl_team_ko_state", return_value=state),
        mock.patch.object(ko, "apply_ko_control_auto_mode", return_value={"ok": False}) as apply,
    ):
        active, last, _, _ = ko.tick_ko_control_auto(True, False, {}, 10.0, 9.9)
        assert not active and last == 10.0
        ko.tick_ko_control_auto(True, active, {}, 10.1, last)
        assert apply.call_count == 2
        ko.tick_ko_control_auto(True, active, {}, 10.8, last)
        assert apply.call_count == 3


def test_ko_auto_enters_full_only_after_team_ko_like_v41():
    state = {"both_loaded": True, "any_team_dead": False, "summary": "live"}
    ko.tick_ko_control_auto.last_mode = "safe"
    ko.tick_ko_control_auto.post_ko_latched = False
    with (
        mock.patch.object(ko, "ko_ctrl_team_ko_state", return_value=state),
        mock.patch.object(ko, "read_ko_native_battle_state", side_effect=AssertionError("V41 does not use phase")),
        mock.patch.object(ko, "apply_ko_control_auto_mode", return_value={"ok": True}) as apply,
    ):
        active, _, _, result_state = ko.tick_ko_control_auto(True, False, {}, 10.0, 10.0)
    assert not active
    assert result_state["auto_mode"] == "safe"
    apply.assert_not_called()


def test_ko_full_packet_keeps_result_state_twelve_input_route():
    full = dict(ko.KO_CONTROL_FULL_PACKET)
    safe = dict(ko.KO_CONTROL_SAFE_PACKET)
    assert len(ko.KO_CONTROL_FULL_PACKET) == 16
    assert full[0x80076820] == 0x60000000
    assert full[0x80076A80] == 0x60000000
    assert 0x80076820 not in safe
    assert 0x80076A80 not in safe
    assert full[0x800447E0] == 0x54600188
    assert full[0x800447E8] == 0x4800000C


def test_ko_full_releases_only_living_winner_and_bridges_result_input():
    winner = 0x9246B9C0
    loser = 0x927EBA2C
    snaps = {
        "P1-C1": {"base": winner, "cur": 100, "attA": 0x2A, "point_active": True},
        "P2-C1": {"base": loser, "cur": 0, "attA": 0x9A, "point_active": True},
    }
    state = {"p1_dead": False, "p2_dead": True}
    ko._ko_ctrl_reset_survivor_release_state()
    with (
        mock.patch.object(ko, "rd32", return_value=0),
        mock.patch.object(ko, "_ko_ctrl_apply_neutral_baseline", return_value={"ok": True, "wrote": 13}) as release,
        mock.patch.object(ko, "read_ko_native_battle_state", return_value=11),
        mock.patch.object(ko, "_ko_ctrl_bridge_survivor_input", return_value={"bridged": 1, "wrote": 6}) as bridge,
    ):
        result = ko._ko_ctrl_release_survivors(snaps, state, 10.0)
    assert result["slots"] == ["P1-C1"]
    assert result["input_slots"] == ["P1-C1"]
    release.assert_called_once()
    bridge.assert_called_once_with("P1-C1", winner)


def test_ko_survivor_bridge_leaves_healthy_native_input_alone():
    base = 0x9246B9C0
    reads = {base + 0x1380: 0x80, base + 0x13CC: 0x800}
    ko._ko_ctrl_reset_survivor_release_state()
    with (
        mock.patch.object(ko, "rd32", side_effect=lambda addr: reads.get(addr)),
        mock.patch.object(ko, "_ko_wd32") as write,
    ):
        result = ko._ko_ctrl_bridge_survivor_input("P1-C1", base)
    assert result["bridged"] == 0
    write.assert_not_called()


def test_ko_idle_survivor_restores_its_own_neutral_control_words():
    base = 0x92B6BA00
    snap = {"base": base, "cur": 44000, "attA": 1, "point_active": True}
    baseline = {"P1-C2": {"neutral_values": {0x60: 0x00010001}}}
    ko._ko_ctrl_reset_survivor_release_state()
    with (
        mock.patch.object(ko, "rd32", return_value=1),
        mock.patch.object(ko, "_ko_ctrl_apply_neutral_controls", return_value={"ok": True, "wrote": 1}) as restore,
        mock.patch.object(ko, "read_ko_native_battle_state", return_value=12),
        mock.patch.object(ko, "_ko_ctrl_bridge_survivor_input", return_value={"bridged": 0, "wrote": 0}),
    ):
        result = ko._ko_ctrl_release_survivors(
            {"P1-C2": snap, "P2-C1": {"base": 0x927EB9E0, "cur": 0}},
            {"p1_dead": False, "p2_dead": True}, 10.0, baseline,
        )
    restore.assert_called_once_with("P1-C2", base, snap, baseline["P1-C2"])
    assert result["wrote"] == 1


def test_ko_auto_calls_survivor_release_only_in_full_mode():
    ko.tick_ko_control_auto.full_until = 0.0
    live = {"both_loaded": True, "any_team_dead": False, "summary": "live"}
    knocked_out = {"both_loaded": True, "any_team_dead": True,
                   "p1_dead": False, "p2_dead": True, "summary": "P2:KO"}
    with (
        mock.patch.object(ko, "ko_ctrl_team_ko_state", side_effect=[live, knocked_out]),
        mock.patch.object(ko, "apply_ko_control_auto_mode", return_value={"ok": True}),
        mock.patch.object(ko, "maintain_ko_survivor_control", return_value={"released": 1}) as release,
    ):
        ko.tick_ko_control_auto(True, False, {}, 10.0, 0.0)
        ko.tick_ko_control_auto(True, False, {}, 11.0, 10.0)
    assert release.call_args_list[0].args[0] is False
    assert release.call_args_list[1].args[0] is True


def test_large_gui_gradient_is_cached_between_frames():
    surface = pygame.Surface((1300, 520), pygame.SRCALPHA)
    rect = surface.get_rect()
    components._draw_horizontal_gradient(surface, rect, (3, 19, 31), (7, 11, 17))
    key = ("horizontal", 1300, 520, (3, 19, 31), (7, 11, 17), 255)
    cached = components._GRADIENT_CACHE[key][0]
    components._draw_horizontal_gradient(surface, rect, (3, 19, 31), (7, 11, 17))
    assert components._GRADIENT_CACHE[key][0] is cached


def test_dock_music_badge_does_not_request_audio_status():
    class AudioState:
        _desired_enabled = True
        def status(self):
            raise AssertionError("dock must not wait for audio diagnostics")

    state = AudioState()
    assert MusicController.dock_active(state) is True
    state._desired_enabled = False
    assert MusicController.dock_active(state) is False


def test_outlined_text_surface_is_reused_for_unchanged_label():
    pygame.font.init()
    font = pygame.font.Font(None, 18)
    first = components._render_outlined_text(font, "KO: ACTIVE", (255, 255, 255),
                                             (0, 0, 0), 140)
    second = components._render_outlined_text(font, "KO: ACTIVE", (255, 255, 255),
                                              (0, 0, 0), 140)
    assert first is second


def test_ko_baseline_uses_one_bulk_guest_read_per_fighter():
    offsets = ko.KO_REWIND_OFFSETS_U32
    start = min(offsets)
    size = max(offsets) + 4 - start
    block = bytearray(size)
    block[0x1E8 - start:0x1E8 - start + 4] = (42).to_bytes(4, "big")
    with (
        mock.patch.object(ko, "rbytes", return_value=bytes(block)) as bulk,
        mock.patch.object(ko, "rd32", side_effect=AssertionError("per-word read")),
    ):
        values = ko._ko_read_rewind_values(0x90000000)
    assert values[0x1E8] == 42
    assert len(values) == len(offsets)
    bulk.assert_called_once_with(0x90000000 + start, size)


def test_ko_button_distinguishes_armed_active_and_unavailable():
    label = components.ko_control_button_label
    assert label(False, False, False) == "KO Control: OFF"
    assert label(True, False, False) == "KO: ARMED"
    assert label(True, True, False) == "KO: ACTIVE"
    assert label(False, False, True) == "KO: PATCH BLOCKED"


def test_ko_packet_uses_verified_legacy_dme_writes():
    memory = {}

    def write_word(addr, value, *, bypass_quarantine=False):
        assert bypass_quarantine
        memory[addr] = value
        return True

    with (
        mock.patch.object(ko, "wd32", side_effect=write_word) as writes,
        mock.patch.object(ko, "rd32", side_effect=lambda addr: memory.get(addr)),
    ):
        result = ko.apply_ko_control_auto_mode("full", verify=True)

    assert result["ok"]
    assert result["wrote"] == result["total"]
    assert result["verified"] == result["total"]
    assert writes.call_count == result["total"]
    resolver_writes = [
        call.args[1] for call in writes.call_args_list
        if call.args[0] == 0x800447E0
    ]
    assert resolver_writes == [0x64600400, 0x54600188]
    assert memory[0x800447E0] == 0x54600188
    assert memory[0x800447E8] == 0x4800000C
    assert 0x800447E0 not in dict(ko.KO_CONTROL_SAFE_PACKET)
    assert 0x800447E8 not in dict(ko.KO_CONTROL_SAFE_PACKET)


def test_ko_full_to_safe_and_off_restores_every_result_input_gate():
    memory = {}
    originals = dict(ko.KO_DOL_ORIGINALS_U32)
    full = dict(ko.KO_CONTROL_FULL_PACKET)
    gates = (0x80076820, 0x80076A80, 0x800447E0, 0x800447E8)
    assert all(addr in originals and addr in full for addr in gates)

    def write_word(addr, value, *, bypass_quarantine=False):
        assert bypass_quarantine
        memory[addr] = value
        return True

    with (
        mock.patch.object(ko, "wd32", side_effect=write_word),
        mock.patch.object(ko, "rd32", side_effect=lambda addr: memory.get(addr)),
    ):
        assert ko.apply_ko_control_auto_mode("full", verify=True)["ok"]
        assert all(memory[addr] == full[addr] for addr in gates)
        assert ko.apply_ko_control_auto_mode("safe", verify=True)["ok"]
        assert all(memory[addr] == originals[addr] for addr in gates)
        assert ko.apply_ko_control_auto_mode("full", verify=True)["ok"]
        assert ko.apply_ko_control_auto_mode("off", verify=True)["ok"]
        assert all(memory[addr] == originals[addr] for addr in gates)


def test_ko_auto_returns_to_safe_after_winner_release():
    ko.tick_ko_control_auto.last_mode = "safe"
    ko.tick_ko_control_auto.post_ko_latched = False
    ko.tick_ko_control_auto.full_until = 0.0
    live = {"both_loaded": True, "any_team_dead": False, "summary": "live"}
    ko_state = {"both_loaded": True, "any_team_dead": True, "p1_dead": False,
                "p2_dead": True, "summary": "P2:KO"}
    with (
        mock.patch.object(ko, "ko_ctrl_team_ko_state", side_effect=[live, ko_state, ko_state, live]),
        mock.patch.object(ko, "apply_ko_control_auto_mode", return_value={"ok": True}) as apply,
        mock.patch.object(ko, "maintain_ko_survivor_control", return_value={"released": 1}) as release,
    ):
        active, last = False, 0.0
        for now in (10.0, 11.0, 11.2, 13.0):
            active, last, _, _ = ko.tick_ko_control_auto(True, active, {}, now, last)
    assert [call.args[0] for call in apply.call_args_list] == ["safe", "full", "safe"]
    assert [call.args[0] for call in release.call_args_list] == [False, True, True, False]

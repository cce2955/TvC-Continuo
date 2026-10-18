from __future__ import annotations

import importlib
import sys
import time
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _module():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if "dolphin_memory_engine" not in sys.modules:
        dme = types.ModuleType("dolphin_memory_engine")
        dme.is_hooked = lambda: False
        dme.hook = lambda: None
        dme.un_hook = lambda: None
        dme.read_byte = lambda *_args, **_kwargs: 0
        dme.read_bytes = lambda *_args, **_kwargs: b""
        dme.write_byte = lambda *_args, **_kwargs: None
        dme.write_bytes = lambda *_args, **_kwargs: None
        sys.modules["dolphin_memory_engine"] = dme
    return importlib.import_module("tvcgui.features.training.mission_manager")


def _manager():
    module = _module()
    return module.MissionManager({}, {}, {}, lambda: [], lambda *_args: "")


def _point_snap(name: str, base: int, char_id: int, label: str = "Idle") -> dict:
    return {
        "base": base,
        "name": name,
        "teamtag": "P1",
        "csv_char_id": char_id,
        "mv_label": label,
        "mv_id_display": 0x100 if label != "Idle" else 0,
        "cur": 40000,
        "meter": 50000,
    }


def test_assist_slot_swap_cannot_replace_pinned_point_character() -> None:
    manager = _manager()
    ken = _point_snap("Ken the Eagle", 0x90000000, 1)
    casshan = _point_snap("Casshan", 0x90010000, 2)
    manager._render_snap_by_slot = {"P1-C1": ken, "P1-C2": casshan}

    manager.toggle_active_slot("P1-C1")

    assist_perturbed = {"P1-C1": casshan, "P1-C2": ken}
    assert manager._mission_owner_name(assist_perturbed) == "Ken the Eagle"
    assert manager._mission_owner_slot(assist_perturbed) == "P1-C2"
    assert manager.active_slot == "P1-C1"


def test_real_tag_activates_new_point_fighters_missions() -> None:
    manager = _manager()
    ken = {**_point_snap("Ken the Eagle", 0x90000000, 1), "damage_point_active": True}
    casshan = {**_point_snap("Casshan", 0x90010000, 2), "damage_point_active": False}
    manager._render_snap_by_slot = {"P1-C1": ken, "P1-C2": casshan}
    manager.active_slot = "P1-C1"
    manager._capture_mission_owner("P1-C1", manager._render_snap_by_slot)

    tagged = {
        "P1-C1": {**ken, "damage_point_active": False},
        "P1-C2": {**casshan, "damage_point_active": True},
    }
    manager._follow_native_point_tag(tagged)
    assert manager._mission_owner["name"] == "Ken the Eagle"
    manager._follow_native_point_tag(tagged)
    assert manager.active_slot == "P1-C2"
    assert manager._mission_owner["name"] == "Casshan"
    assert manager._runtime["slot"] == "P1-C2"


def test_assist_activity_without_native_point_flip_keeps_current_missions() -> None:
    manager = _manager()
    ken = {**_point_snap("Ken the Eagle", 0x90000000, 1), "damage_point_active": True}
    casshan = {**_point_snap("Casshan", 0x90010000, 2, "Assist Attack"), "damage_point_active": False}
    snaps = {"P1-C1": ken, "P1-C2": casshan}
    manager.active_slot = "P1-C1"
    manager._capture_mission_owner("P1-C1", snaps)
    manager._follow_native_point_tag(snaps)
    manager._follow_native_point_tag(snaps)
    assert manager.active_slot == "P1-C1"
    assert manager._mission_owner["name"] == "Ken the Eagle"


def test_assist_native_action_is_not_recorded_for_point_route() -> None:
    manager = _manager()
    manager._mission_owner.update({"char_id": 1, "name": "Ken the Eagle"})

    manager._record_mission_action_sample(
        {"action_id": 0x220, "char_id": 2, "sample_ns": time.monotonic_ns()},
        10,
        1,
    )

    assert manager._runtime["mission_action_events"] == []


def _phoenix_payload() -> dict:
    return {
        "active": True,
        "slot": "P1-C1",
        "point_slot": "P1-C1",
        "character": "Ken the Eagle",
        "active_mission_id": "ken_006",
        "active_mission_steps": [
            {"label": "5C", "input": "5C"},
            {"label": "Phoenix", "input": "236XX"},
        ],
        "active_mission_goal": {},
        "active_mission_setup_meter_refill": False,
    }


def _prepare_phoenix_manager(monkeypatch, *, actual_hitstun: bool) -> tuple[object, dict]:
    manager = _manager()
    manager.active_slot = "P1-C1"
    manager._mission_owner.update({
        "slot": "P1-C1",
        "teamtag": "P1",
        "base": 0x90000000,
        "char_id": 1,
        "name": "Ken the Eagle",
    })
    manager._frame_idx = 20
    manager._runtime.update({
        "slot": "P1-C1",
        "mission_id": "ken_006",
        "progress_index": 1,
        "last_actual_hitstun": actual_hitstun,
        "last_seen_label": "5C",
        "last_seen_anim": 0x105,
    })
    now = time.monotonic_ns()
    manager._runtime["mission_action_events"] = [
        {
            "serial": 2,
            "frame": 20,
            "last_frame": 20,
            "sample_seq": 200,
            "sample_ns": now,
            "action_id": 0x220,
            "char_id": 1,
            "labels": ["Phoenix"],
            "input_serial": 2,
            "consumed": False,
        }
    ]
    snaps = {
        "P1-C1": {
            **_point_snap("Ken the Eagle", 0x90000000, 1, "Phoenix"),
            "mv_id_display": 0x220,
        },
        "P2-C1": {
            "base": 0x91000000,
            "name": "Ryu",
            "teamtag": "P2",
            "cur": 40000,
            "attA": 60 if actual_hitstun else 0,
        },
    }
    manager._render_snap_by_slot = snaps
    monkeypatch.setattr(manager, "_record_mission_input_stream", lambda *_args: {
        "pressed_buttons": 0,
        "fresh_attack": False,
        "baroque": False,
    })
    monkeypatch.setattr(
        manager,
        "_record_opponent_realtime_stream",
        lambda *_args: ([], False, actual_hitstun),
    )
    monkeypatch.setattr(manager, "_opponent_in_hitstun", lambda *_args: actual_hitstun)
    monkeypatch.setattr(manager, "_opponent_in_megacrash", lambda *_args: False)
    monkeypatch.setattr(manager, "_global_combo_count", lambda: 1 if actual_hitstun else 0)
    monkeypatch.setattr(manager, "_opponent_damage_this_frame", lambda *_args: [])
    monkeypatch.setattr(manager, "_step_input_matches", lambda *_args: True)
    return manager, snaps


def test_predicted_phoenix_start_does_not_count_as_completed_hit(monkeypatch) -> None:
    manager, snaps = _prepare_phoenix_manager(monkeypatch, actual_hitstun=False)

    result = manager._augment_payload_with_runtime(_phoenix_payload(), snaps)

    assert result["just_cleared"] is False
    assert result["completed_step_count"] == 1
    assert result["confirmed_step_count"] == 1
    assert result["predicted_step_count"] == 1
    assert result["current_step_index"] == 1


def test_five_c_hit_cannot_be_donated_to_whiffed_phoenix(monkeypatch) -> None:
    manager, snaps = _prepare_phoenix_manager(monkeypatch, actual_hitstun=True)
    manager._runtime["mission_hit_events"] = [
        {
            "serial": 1,
            "frame": 19,
            "last_frame": 19,
            "sample_seq": 190,
            "hp_seen": True,
            "combo_seen": True,
            "damage": 100,
            "owner_action_serial": 1,
            "consumed": False,
        }
    ]

    result = manager._augment_payload_with_runtime(_phoenix_payload(), snaps)

    assert result["just_cleared"] is False
    assert result["completed_step_count"] == 1
    assert manager._runtime["progress_index"] == 1


def test_phoenix_owned_hit_with_hitstun_completes_route(monkeypatch) -> None:
    manager, snaps = _prepare_phoenix_manager(monkeypatch, actual_hitstun=True)
    manager._runtime["mission_hit_events"] = [
        {
            "serial": 2,
            "frame": 20,
            "last_frame": 20,
            "sample_seq": 201,
            "hp_seen": True,
            "combo_seen": True,
            "damage": 100,
            "owner_action_serial": 2,
            "consumed": False,
        }
    ]

    result = manager._augment_payload_with_runtime(_phoenix_payload(), snaps)

    assert result["completed_step_count"] == 2
    assert result["confirmed_step_count"] == 2
    assert result["just_cleared"] is True


def test_delayed_attack_keeps_earlier_action_as_hit_candidate() -> None:
    """A later trigger action must not steal a delayed attack's confirmed hit."""
    manager = _manager()
    manager._mission_owner.update({"char_id": 2, "name": "Casshan"})
    now = time.monotonic_ns()
    manager._runtime["mission_action_events"] = [
        {
            "serial": 1,
            "frame": 10,
            "last_frame": 10,
            "sample_seq": 100,
            "sample_ns": now,
            "action_id": 0x161,
            "char_id": 2,
            "labels": ["Scrap Android"],
            "input_serial": 1,
            "consumed": False,
        },
        {
            "serial": 2,
            "frame": 12,
            "last_frame": 12,
            "sample_seq": 120,
            "sample_ns": now + 2_000_000,
            "action_id": 0x170,
            "char_id": 2,
            "labels": ["5A"],
            "input_serial": 2,
            "consumed": False,
        },
    ]

    candidates = manager._candidate_action_serials_for_hit(24)
    assert candidates == [1, 2]

    manager._runtime["mission_hit_events"] = [
        {
            "serial": 1,
            "frame": 24,
            "last_frame": 24,
            "hp_seen": True,
            "combo_seen": True,
            "damage": 100,
            "owner_action_serial": 2,
            "candidate_action_serials": candidates,
            "consumed": False,
        }
    ]

    pair = manager._find_action_hit_pair_for_step(
        {"label": "Scrap Android", "input": "236XX"},
        ["Scrap Android"],
        24,
    )

    assert pair is not None
    assert pair[0]["serial"] == 1
    assert pair[1]["serial"] == 1


def test_candidate_ownership_is_generic_not_character_specific() -> None:
    manager = _manager()
    hit = {
        "candidate_action_serials": [4, 7],
        "owner_action_serial": 7,
    }

    assert manager._hit_can_confirm_action(hit, 4) is True
    assert manager._hit_can_confirm_action(hit, 7) is True
    assert manager._hit_can_confirm_action(hit, 8) is False


def test_down_down_taunt_reaffirms_new_native_point_after_real_tag(monkeypatch) -> None:
    module = _module()
    monkeypatch.setattr(
        module,
        "build_overlay_payload",
        lambda name: {
            "character": name,
            "missions": [{"mission_id": "casshan_006" if name == "Casshan" else "ken_006"}],
            "active_mission_id": "casshan_006" if name == "Casshan" else "ken_006",
        },
    )
    manager = _manager()
    ken = {
        **_point_snap("Ken the Eagle", 0x90000000, 1),
        "damage_point_active": False,
    }
    casshan = {
        **_point_snap("Casshan", 0x90010000, 2),
        "damage_point_active": True,
    }
    manager._render_snap_by_slot = {"P1-C1": ken, "P1-C2": casshan}
    manager._active_slot = "P1-C1"
    manager._mission_owner.update({
        "slot": "P1-C1",
        "teamtag": "P1",
        "base": ken["base"],
        "char_id": 1,
        "name": "Ken the Eagle",
    })
    samples = [
        {"seq": 1, "held": 0x08, "pressed": 0x08, "released": 0},
        {"seq": 2, "held": 0x00, "pressed": 0x00, "released": 0x08},
        {"seq": 3, "held": 0x08, "pressed": 0x08, "released": 0},
        {"seq": 4, "held": 0x00, "pressed": 0x00, "released": 0x08},
        {"seq": 5, "held": 0x0C00, "pressed": 0x0C00, "released": 0},
    ]
    requested_slots: list[str] = []

    def provider(slot_label, _base):
        requested_slots.append(slot_label)
        return {}, samples

    manager.set_input_sample_provider(provider)
    manager._update_selector_from_inputs(manager._render_snap_by_slot, 20.0)

    assert requested_slots == ["P1-C2"]
    assert manager.selector_open is True
    assert manager.active_slot == "P1-C2"
    assert manager._mission_owner["name"] == "Casshan"
    assert manager._mission_owner["base"] == casshan["base"]


def test_native_point_flag_beats_assist_animation_fallback() -> None:
    manager = _manager()
    ken = {
        **_point_snap("Ken the Eagle", 0x90000000, 1, "Idle"),
        "damage_point_active": True,
        "attA": 0,
        "attB": 0,
    }
    casshan_assist = {
        **_point_snap("Casshan", 0x90010000, 2, "Assist Attack"),
        "damage_point_active": False,
        "attA": 0x102,
        "attB": 0,
    }
    snaps = {"P1-C1": ken, "P1-C2": casshan_assist}

    assert manager._native_point_slot("P1-C1", snaps) == "P1-C1"
    assert manager._team_active_slot("P1-C1", snaps) == "P1-C1"

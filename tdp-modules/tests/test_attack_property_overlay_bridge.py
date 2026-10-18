from __future__ import annotations

import sys
import types
from pathlib import Path

sys.modules.setdefault("dolphin_memory_engine", types.SimpleNamespace())

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tvcgui.features.overlay.manager import (
    HudOverlayManager,
    _bridge_attack_property_annotations,
)


class _FakeOverlayManager:
    def __init__(self):
        self.payload = None
        self._mission_event_manager = None
        self._mission_event_stream = types.SimpleNamespace(events_since=lambda *_args: [])

    def _set_input_sampler_targets(self, _snaps):
        return None

    def _input_snapshot_for_slot(self, _slot, _base):
        return ({}, [])

    def _refresh_realtime_fd_timing(self, _last_scan_normals):
        return None

    def _arm_realtime_hs_move(self, *_args, **_kwargs):
        return None

    def _queue_payload(self, payload):
        self.payload = payload


class _FakeMissionManager:
    active_slot = None

    def set_event_provider(self, _provider):
        return None

    def var_state(self, _slot, _snaps):
        return {}


def _native_definition():
    return {
        "status": "OK",
        "property_a": 0x0C,
        "property_b": 0x00040040,
        "phase_count": 1,
        "phases": [{
            "phase_index": 1,
            "property_a": 0x0C,
            "property_a_initial": 0x0C,
            "property_b_initial": 0x40,
            "property_b": 0x00040040,
            "result_clear_mask": 0x80042F00,
            "hit_result_raw": 0x80000200,
            "hit_reaction": 0x800002,
            "operation_count": 5,
            "operations": [
                {"operation": 1, "operation_name": "SET", "field_id": 0x240, "field_name": "A", "value": 0x0C},
                {"operation": 1, "operation_name": "SET", "field_id": 0x244, "field_name": "B", "value": 0x40},
                {"operation": 0x17, "operation_name": "CLEAR", "field_id": 0x240, "field_name": "A", "value": 0x80042F00},
                {"operation": 0x15, "operation_name": "OR", "field_id": 0x240, "field_name": "A", "value": 0x80000200},
                {"operation": 0x15, "operation_name": "OR", "field_id": 0x244, "field_name": "B", "value": 0x00040000},
            ],
        }],
    }


def test_profiler_annotations_cross_overlay_process_boundary():
    payload = {"attack_property": 0x0C, "attack_property_label": "MID"}
    snap = {
        "attack_property_display_active": True,
        "attack_property_display_source": "move_definition",
        "attack_property_packet_state": "CURRENT MOVE",
        "attack_property_packet_action_id": 0x13B,
        "attack_property_live_a": 0x0C,
        "attack_property_live_b": 0x00040040,
        "attack_property_definition_status": "OK",
        "attack_property_phases": _native_definition()["phases"],
    }

    _bridge_attack_property_annotations(payload, snap)

    assert payload["attack_property_display_active"] is True
    assert payload["attack_property_display_source"] == "move_definition"
    assert payload["attack_property_packet_action_id"] == 0x13B
    assert payload["attack_property_live_b"] == 0x00040040
    assert payload["attack_property_phases"][0]["hit_reaction"] == 0x800002


def test_write_data_keeps_static_native_definition_active_for_hud(monkeypatch):
    monkeypatch.setattr(
        "tvcgui.features.overlay.manager.resolve_live_attack_definition",
        lambda *_args, **_kwargs: {"status": "NO_ACTION_ENTRY"},
    )
    fake = _FakeOverlayManager()
    snaps = {
        "P1-C1": {
            "base": 0x9246B9C0,
            "name": "Ryu",
            "attA": 0x13B,
            "mv_label": "Donkey H",
            "mv_label_display": "Donkey H",
            "final_move_label": "Donkey H",
            "attack_property_display_active": True,
            "attack_property_display_source": "move_definition",
            "attack_property_packet_state": "CURRENT MOVE",
            "attack_property_packet_source": "move_definition",
            "attack_property_packet_action_id": 0x13B,
            "attack_property_packet_action_name": "Donkey H",
            "attack_property_live_a": 0x0C,
            "attack_property_live_b": 0x00040040,
            "attack_property_definition_status": "OK",
            "attack_property_phases": _native_definition()["phases"],
        }
    }

    HudOverlayManager.write_data(fake, snaps, [], _FakeMissionManager())

    row = fake.payload["P1-C1"]
    assert row["attack_property_display_active"] is True
    assert row["attack_property_display_source"] == "move_definition"
    assert row["attack_property_phases"][0]["hit_reaction"] == 0x800002
    assert "attack_property_profile_hits" not in row
    assert "attack_property_move_details" not in row


def test_write_data_uses_native_script_fallback_and_ignores_profile(monkeypatch):
    monkeypatch.setattr(
        "tvcgui.features.overlay.manager.resolve_live_attack_definition",
        lambda *_args, **_kwargs: _native_definition(),
    )
    fake = _FakeOverlayManager()
    snaps = {
        "P1-C1": {
            "base": 0x9246B9C0,
            "name": "Ryu",
            "attA": 0x13B,
            "mv_label": "Donkey H",
            "mv_label_display": "Donkey H",
            "final_move_label": "Donkey H",
        }
    }
    # Deliberately conflicting profile data. It must not enter Attack Property.
    scan = [{
        "slot_label": "P1-C1",
        "moves": [{
            "id": 0x13B,
            "attack_property": 0x21,
            "damage": 9999,
            "hitstun": 99,
            "hit_segments": [{"attack_property": 0x21, "damage": 9999}],
        }],
    }]

    HudOverlayManager.write_data(fake, snaps, scan, _FakeMissionManager())

    row = fake.payload["P1-C1"]
    assert row["attack_property_live_a"] == 0x0C
    assert row["attack_property_live_b"] == 0x00040040
    assert row["attack_property_definition_action_source"] == "native_action_script"
    assert row["attack_property_phases"][0]["hit_result_raw"] == 0x80000200
    assert "attack_property_profile_hits" not in row
    assert "attack_property_move_details" not in row


def test_native_script_overrides_conflicting_runtime_annotation(monkeypatch):
    monkeypatch.setattr(
        "tvcgui.features.overlay.manager.resolve_live_attack_definition",
        lambda *_args, **_kwargs: _native_definition(),
    )
    fake = _FakeOverlayManager()
    snaps = {
        "P1-C1": {
            "base": 0x9246B9C0,
            "name": "Ryu",
            "attA": 0x13B,
            "mv_label": "Donkey H",
            "mv_label_display": "Donkey H",
            "attack_property_display_active": True,
            "attack_property_display_source": "resolver_mailbox",
            "attack_property_packet_state": "CONTACT",
            "attack_property_live_a": 0x09,
            "attack_property_live_b": 0x40,
            "attack_property_live_damage": 9999,
            "attack_property_phases": [{"property_a": 0x09, "property_b": 0x40}],
        }
    }

    HudOverlayManager.write_data(fake, snaps, [], _FakeMissionManager())

    row = fake.payload["P1-C1"]
    assert row["attack_property_display_source"] == "move_definition"
    assert row["attack_property_packet_state"] == "CURRENT MOVE"
    assert row["attack_property_live_a"] == 0x0C
    assert row["attack_property_live_b"] == 0x00040040
    assert row["attack_property_phases"][0]["hit_reaction"] == 0x800002


def test_projectile_only_profiler_result_is_not_replaced_by_stale_native_script(monkeypatch):
    monkeypatch.setattr(
        "tvcgui.features.overlay.manager.resolve_live_attack_definition",
        lambda *_args, **_kwargs: _native_definition(),
    )
    fake = _FakeOverlayManager()
    projectile = {
        "projectile_index": 1,
        "projectile_id": 0x31,
        "property_a": 0x0C,
        "property_b": 0x01000040,
        "projectile_live": True,
        "packet_state": "LIVE PROJECTILE",
        "packet_source": "live_projectile_actor",
        "actor": 0x91B159B4,
        "linked": 0x91A6B774,
    }
    snaps = {
        "P1-C1": {
            "base": 0x9246B9C0,
            "name": "Ryu",
            "attA": 0x0161,
            "mv_label": "Shinkuu",
            "mv_label_display": "Shinkuu",
            "attack_property_display_active": True,
            "attack_property_display_source": "live_projectile_actor",
            "attack_property_packet_state": "LIVE PROJECTILE",
            "attack_property_packet_source": "live_projectile_actor",
            "attack_property_packet_action_id": 0x0161,
            "attack_property_packet_action_name": "Shinkuu",
            "attack_property_live_a": 0x0C,
            "attack_property_live_b": 0x01000040,
            "attack_property_phase_count": 0,
            "attack_property_phases": [],
            "attack_property_projectile_count": 1,
            "attack_property_projectiles": [projectile],
        }
    }

    HudOverlayManager.write_data(fake, snaps, [], _FakeMissionManager())

    row = fake.payload["P1-C1"]
    assert row["attack_property_display_source"] == "live_projectile_actor"
    assert row["attack_property_packet_state"] == "LIVE PROJECTILE"
    assert row["attack_property_packet_action_name"] == "Shinkuu"
    assert row["attack_property_phases"] == []
    assert row["attack_property_projectiles"][0]["property_b"] == 0x01000040

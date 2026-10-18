from __future__ import annotations

import importlib
import json
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MISSIONS = ROOT / "missions"


def _mission(filename: str, mission_id: str) -> dict:
    payload = json.loads((MISSIONS / filename).read_text(encoding="utf-8"))
    return next(
        mission
        for mission in payload["missions"]
        if mission.get("mission_id") == mission_id
    )


def _labels(mission: dict) -> list[str]:
    return [step["label"] for step in mission["steps"]]


def _manager_module():
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


def test_casshan_challenge_route_and_five_bar_setup() -> None:
    mission = _mission("casshan.json", "casshan_009")
    assert mission["name"] == "Challenge"
    assert mission["setup_meter_refill"] is True
    assert _labels(mission) == ["Scrap Android", "Brutal Axe"]


def test_ken_challenge_route_and_five_bar_setup() -> None:
    mission = _mission("ken_the_eagle.json", "ken_006")
    assert mission["name"] == "Challenge"
    assert mission["setup_meter_refill"] is True
    assert _labels(mission) == ["5C", "Phoenix"]


def test_zero_challenge_starts_with_rekkoha() -> None:
    mission = _mission("zero.json", "zero_008")
    assert mission["name"] == "Challenge"
    assert mission["setup_meter_refill"] is True
    assert _labels(mission) == ["Rekkoha", "3C", "j.C", "Sentsuizan B"]


def test_both_ryu_challenges_use_five_bar_refill() -> None:
    assert _mission("ryu.json", "ryu_007")["setup_meter_refill"] is True
    assert _mission("ryu.json", "ryu_008")["setup_meter_refill"] is True


def test_mission_loader_carries_declarative_meter_refill_flag() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    mission_mode = importlib.import_module("tvcgui.features.training.mission_mode")
    pack = mission_mode._load_mission_pack_uncached("Zero")
    mission = next(item for item in pack.missions if item.mission_id == "zero_008")
    assert mission.setup_meter_refill is True


def test_meter_refill_override_saves_and_restores_debug_flags() -> None:
    module = _manager_module()
    manager = module.MissionManager({}, {}, {}, lambda: [], lambda *_args: "")
    reads = {"P1Meter": 7, "BaroquePct": 3}
    writes: list[tuple[str, int]] = []
    manager._read_debug_flag = lambda name: reads[name]
    manager._write_debug_flag = lambda name, value: writes.append((name, value)) or True

    enabled = manager._sync_meter_refill_mission({
        "slot": "P1-C1",
        "active_mission_id": "zero_008",
        "active_mission_setup_meter_refill": True,
    })

    assert enabled is True
    assert manager._meter_refill_state["saved_p1meter_flag"] == 7
    assert manager._meter_refill_state["saved_baroque_flag"] == 3

    manager._restore_meter_refill_overrides()
    assert writes == [("P1Meter", 7), ("BaroquePct", 3)]
    manager.close()

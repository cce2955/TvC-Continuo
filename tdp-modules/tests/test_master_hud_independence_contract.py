from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "main.py"
COMPONENTS = ROOT / "tvcgui" / "ui" / "components.py"
HITBOX = ROOT / "tvcgui" / "features" / "hitboxes" / "renderer.py"
HUD = ROOT / "tvcgui" / "features" / "overlay" / "hud_renderer.py"
MASTER = ROOT / "tvcgui" / "features" / "overlay" / "master_renderer.py"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_modified_modules_parse() -> None:
    for path in (MAIN, COMPONENTS, HITBOX, HUD):
        ast.parse(_text(path), filename=str(path))
    if MASTER.exists():
        ast.parse(_text(MASTER), filename=str(MASTER))


def test_master_compositor_is_not_owned_by_overlay_toggle() -> None:
    src = _text(MAIN)
    assert "overlay_enabled       = True" in src
    assert 'overlay_enabled = bool(_existing_master.get("show_hud", True))' in src
    start = src.index("    def _sync_master_overlay_state():")
    end = src.index("\n    try:\n        if os.path.exists(MASTER_CONTROL_FILE):", start)
    block = src[start:end]
    assert "if not master_overlay_active:" in block
    assert "_launch_master_overlay()" in block
    assert "_stop_master_overlay()" not in block
    assert "want_process" not in block
    assert "_check_master_overlay_proc()\n        _sync_master_overlay_state()" in src


def test_mission_mode_never_enables_core_overlay() -> None:
    src = _text(MAIN)
    start = src.index("            # Mission mode buttons")
    end = src.index("            # Frame data buttons", start)
    block = src[start:end]
    assert "mission_mgr.toggle_active_slot(slot_label)" in block
    assert "overlay_enabled = True" not in block
    assert "_sync_master_overlay_state()" in block


def test_hitbox_toggle_does_not_change_ruler_toggle() -> None:
    src = _text(MAIN)
    assert "enable_range_ruler" not in src
    assert "Toggling hitboxes never changes it" in src


def test_ko_control_is_in_lab_setup_row() -> None:
    src = _text(COMPONENTS)
    layout_start = src.index("def _command_dock_layout")
    layout_end = src.index("def get_top_dock_height", layout_start)
    layout_block = src[layout_start:layout_end]
    training_start = layout_block.index("training_specs =")
    lab_start = layout_block.index("lab_specs =", training_start)
    assert '("ko", 122)' not in layout_block[training_start:lab_start]
    lab_block = layout_block[lab_start:]
    assert '("ko", 122)' in lab_block
    assert lab_block.index('(\"ko\", 122)') < lab_block.index('(\"dump\", 120)')


def test_ruler_runs_without_visible_hitboxes_or_hurtboxes() -> None:
    src = _text(HITBOX)
    assert "or _range_ruler_enabled()" in src
    assert "hitboxes_on or hurtboxes_on or ruler_on" in src
    assert "range_ruler_on = bool(ruler_on)" in src
    assert "_range_ruler_enabled() and hurtboxes_on" not in src


def test_core_hud_and_research_panels_can_draw_independently() -> None:
    src = _text(HUD)
    assert 'core_visible = control is None or getattr(control, "show_hud", True)' in src
    assert "if core_visible:" in src
    assert "else:\n        _draw_research_panels(" in src
    assert "if not hud_visible and not research_visible:" in src


def test_mission_renderer_has_no_core_hud_gate() -> None:
    if not MASTER.exists():
        return
    src = _text(MASTER)
    start = src.index("    def draw_mission_overlay(self) -> None:")
    end = src.index("\n\n    def present(self) -> None:", start)
    block = src[start:end]
    assert "self.mission_active" in block
    assert "show_hud" not in block

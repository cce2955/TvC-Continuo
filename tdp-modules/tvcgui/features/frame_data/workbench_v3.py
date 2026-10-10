from __future__ import annotations

"""Lean Frame Data workbench.

This module intentionally keeps the normal interaction path cache-only:
- no live polling
- no background probe workers
- no eager native/raw rendering
- only one page of table rows exists in Tk at a time

All cached move fields remain reachable from the inspector. Expensive raw
containers are only materialized after an explicit user action.
"""

import json
import math
import struct
import os
from datetime import datetime
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog, filedialog
from collections import defaultdict

from tvcgui.core.tk_host import tk_call
from tvcgui.core.paths import user_data_path
from tvcgui.features.combat.move_filters import filter_purged_moves_for_char

from .editors import FDCellEditorsMixin
from . import utils as U
from . import tree as fd_tree
from . import projectile_integration as FPI
from . import super_integration as FSI
from .patterns import (
    find_speed_mod_addr,
    find_attack_property_addr,
    fmt_attack_property,
    parse_attack_property,
)
from .write_helpers import (
    write_speed_mod_inline,
    write_u32_field_inline,
)
from .widgets import ask_integer_with_help, get_field_help, apply_titlebar_icon
from .timing_observations import apply_observations_to_scan_data
from . import patch_runtime as FDPatchRuntime

try:
    from tvcgui.tools.scanners.bone_scanner import BoneScanner
except Exception:
    BoneScanner = None

try:
    from tvcgui.features.training.protection_profiler import apply_runtime_protection_observations
except Exception:
    apply_runtime_protection_observations = None



_LABEL_OVERRIDE_FILE = user_data_path("frame_data", "frame_data_label_overrides.json")
_CUSTOM_COLUMNS_FILE = user_data_path("frame_data", "frame_data_custom_columns.json")

def _load_json_dict(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save_json_dict(path: str, data: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
    except Exception:
        pass

def _load_custom_columns() -> list[str]:
    doc = _load_json_dict(_CUSTOM_COLUMNS_FILE)
    cols = doc.get("columns") if isinstance(doc, dict) else None
    out = ["move"]
    if isinstance(cols, list):
        for col in cols:
            col = str(col)
            if col and col not in out:
                out.append(col)
    return out

def _save_custom_columns(cols: list[str]) -> None:
    _save_json_dict(_CUSTOM_COLUMNS_FILE, {"columns": [c for c in cols if c != "move"]})

# The table itself only needs columns that are used by a focused comparison
# view or by one of the existing named editors. The inspector is schema-free
# and reads directly from the move dictionary.
_TABLE_VIEW_GROUPS = {
    "core": (
        "move", "hits", "damage", "meter", "startup", "active", "recovery",
        "hitstun", "blockstun", "invuln", "armor", "hitstop",
    ),
    "defense": (
        "move", "hits", "hitstun", "blockstun", "hitstop",
        "invuln", "guard_point", "armor", "counter", "baroque_probe",
    ),
    "cancel": (
        "move", "startup", "active", "recovery", "custom_cancel_window",
        "baroque_probe", "adv_block_observed", "adv_block_derived",
        "adv_hit_derived", "attack_property", "hit_reaction",
    ),
    "launch": (
        "move", "hits", "damage", "hit_reaction", "hit_result_flags",
        "kb_type", "launch_profile", "kb_unknown", "ground_kb_mode",
        "ground_kb", "ground_kb_y", "kb_x", "air_kb", "combo_kb_mod",
    ),
    "hitbox": (
        "move", "hits", "startup", "active", "hb_main", "hit_spark",
        "stretch_part", "stretch_len", "stretch_width", "stretch_height",
        "stretch_time",
    ),
    "projectile": tuple(dict.fromkeys((
        "move", "damage", "kb_x", "air_kb", "proj_emit_count",
        *FPI.PROJECTILE_COLUMNS, "abs",
    ))),
    "super": tuple(dict.fromkeys((
        "move", "hits", "damage", "startup", "active", "recovery",
        *FSI.SUPER_DISPATCH_COLUMNS, "abs",
    ))),
    "research": (
        "move", "kind", "context", "source", "family_role", "table_rel",
        "native_definition_status", "native_field_op_count",
        "native_command_count", "generic_write_verified", "abs",
    ),
}

_VIEW_LABELS = {
    "core": "Core",
    "defense": "Defense",
    "cancel": "Cancels",
    "launch": "Launch / KB",
    "hitbox": "Hitbox / Reach",
    "projectile": "Projectiles",
    "super": "Supers",
    "research": "Research",
    "custom": "Custom",
}
_LABEL_TO_VIEW = {v: k for k, v in _VIEW_LABELS.items()}

_STANDARD_EDITABLE = {
    "damage", "meter", "startup", "active", "active2",
    "hitstun", "blockstun", "hitstop", "hit_spark",
    "stretch_part", "stretch_len", "stretch_width", "stretch_height",
    "stretch_time", "post_link", "kb_type", "launch_profile",
    "kb_unknown", "ground_kb", "ground_kb_y", "kb_x", "air_kb",
    "combo_kb_mod", "hit_reaction", "hit_result_flags", "superbg",
    "hb_main", "hb", "speed_mod", "attack_property",
}

# Columns expected by the existing edit mixin are kept as logical columns even
# when hidden. This lets proven write handlers update the row without forcing a
# giant spreadsheet on screen.
_LOGICAL_COLUMNS = tuple(dict.fromkeys(
    [c for group in _TABLE_VIEW_GROUPS.values() for c in group]
    + list(_STANDARD_EDITABLE)
    + ["move", "kind", "context", "abs", "proj_dmg"]
))

_COLUMN_WIDTH = {
    "move": 250,
    "hits": 70,
    "damage": 88,
    "meter": 75,
    "startup": 78,
    "active": 90,
    "recovery": 86,
    "hitstun": 82,
    "blockstun": 88,
    "invuln": 150,
    "armor": 110,
    "hitstop": 75,
    "guard_point": 140,
    "counter": 110,
    "custom_cancel_window": 145,
    "baroque_probe": 140,
    "adv_block_observed": 125,
    "adv_block_derived": 125,
    "adv_hit_derived": 115,
    "attack_property": 140,
    "hit_reaction": 125,
    "hit_result_flags": 130,
    "kb_type": 115,
    "launch_profile": 120,
    "kb_unknown": 115,
    "ground_kb_mode": 120,
    "ground_kb": 105,
    "ground_kb_y": 105,
    "kb_x": 90,
    "air_kb": 90,
    "combo_kb_mod": 105,
    "hb_main": 105,
    "hit_spark": 105,
    "stretch_part": 105,
    "stretch_len": 105,
    "stretch_width": 105,
    "stretch_height": 105,
    "stretch_time": 105,
    "kind": 90,
    "context": 260,
    "source": 150,
    "family_role": 135,
    "table_rel": 115,
    "native_definition_status": 150,
    "native_field_op_count": 100,
    "native_command_count": 100,
    "generic_write_verified": 120,
    "abs": 115,
}


_NATIVE_GUESSES = {
    0x0058: ("State/protection flags +0x0058", "guard-point/protection state bits"),
    0x01E8: ("Action state +0x01E8", "action handoff / action-state field"),
    0x0244: ("Guard height mask +0x0244", "Mid/High/Low guard-point acceptance mask"),
    0x1218: ("Phase timer +0x1218", "phase timer; protected phases also use it"),
    0x13CC: ("Input held +0x13CC", "held-input state"),
    0x13D0: ("Input pressed +0x13D0", "pressed-input state"),
    0x13D4: ("Input released +0x13D4", "released-input state"),
    0x13D8: ("Input buffered +0x13D8", "buffered-input state"),
    0x13DC: ("Input edge +0x13DC", "input edge / transition state"),
    0x444C: ("Baroque lockout +0x444C", "Baroque eligibility lockout, not the cancel window"),
    0x44A4: ("Combat/proration state +0x44A4", "combat-role / alternate proration lane; Baroque-related evidence"),
}
_NATIVE_PROTECTION_NEIGHBORS = {
    0x005C, 0x0060, 0x0064, 0x006C, 0x0074,
    0x4438, 0x4440, 0x4444, 0x4450, 0x4454, 0x4548,
}


def _native_identity(offset: int) -> tuple[str, str]:
    off = int(offset or 0)
    if off in _NATIVE_GUESSES:
        return _NATIVE_GUESSES[off]
    if off in _NATIVE_PROTECTION_NEIGHBORS:
        return (f"Unknown +0x{off:04X}", "protection/combat-state candidate")
    return (f"Unknown +0x{off:04X}", "unknown fighter-relative field")


def _empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"", "-", "none", "n/a", "not found"}
    if isinstance(value, (tuple, list, dict, set, bytes, bytearray)):
        return len(value) == 0
    return False


def _cheap_preview(value, limit: int = 240) -> str:
    """Never deeply stringify native containers during ordinary selection."""
    try:
        if value is None:
            return "not found"
        if isinstance(value, bool):
            return "True" if value else "False"
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float):
            return f"{value:g}"
        if isinstance(value, (bytes, bytearray, memoryview)):
            blob = bytes(value)
            head = blob[:24].hex(" ").upper()
            return f"<{len(blob)} bytes> {head}{' …' if len(blob) > 24 else ''}"
        if isinstance(value, dict):
            return f"<{len(value)} keys>"
        if isinstance(value, (list, tuple, set)):
            return f"<{len(value)} items>"
        text = str(value).replace("\r", " ").replace("\n", " ")
    except Exception:
        text = "<unprintable>"
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def _fmt_addr(value) -> str:
    try:
        return f"0x{int(value) & 0xFFFFFFFF:08X}"
    except Exception:
        return ""


def _fmt_adv(v) -> str:
    if v is None or v == "":
        return ""
    try:
        return f"{int(v):+d}"
    except Exception:
        return str(v)


class LeanFrameDataWindow(FDCellEditorsMixin):
    PAGE_SIZE = 120

    def __init__(self, master, slot_label: str, target_slot: dict):
        self.master = master
        self.slot_label = str(slot_label)
        self.target_slot = target_slot or {}
        self.root = tk.Toplevel(master)
        cname = str(self.target_slot.get("char_name") or "-")
        self.root.title(f"Frame Data Editor: {self.slot_label} ({cname})")
        self.root.geometry("1540x860")
        self.root.minsize(1120, 640)
        try:
            apply_titlebar_icon(self.root, master)
        except Exception:
            pass
        try:
            fd_tree.configure_styles(self.root)
        except Exception:
            pass

        # IMPORTANT: references only. No deep copy and no live observation/probe
        # pass in this window.
        self.moves = filter_purged_moves_for_char(self.target_slot, self.target_slot.get("moves", []) or [])
        self._all_moves = list(self.moves)
        self._filtered_moves = list(self.moves)
        self._page = 0
        self._item_to_move: dict[str, dict] = {}
        self.move_to_tree_item = self._item_to_move  # compatibility with editors
        self._selected_move: dict | None = None
        self._selected_item: str | None = None
        self._dirty: set[tuple[int, str]] = set()
        self._dirty_refs: dict[tuple[int, str], tuple[dict, str]] = {}
        self._field_row_to_key: dict[str, str] = {}
        self._unknown_row_to_index: dict[str, int] = {}
        self._raw_key_by_row: dict[str, str] = {}
        self._native_word_row_to_offset: dict[str, int] = {}
        self._search_after = None
        self._closing = False
        self._label_overrides = _load_json_dict(_LABEL_OVERRIDE_FILE)
        self._custom_columns = _load_custom_columns()
        self._current_view_key = "core"
        self._logical_columns = list(_LOGICAL_COLUMNS)
        for _col in self._custom_columns:
            if _col not in self._logical_columns:
                self._logical_columns.append(_col)

        self._status_var = tk.StringVar(master=self.root, value="Cache-only editor ready")
        self._search_var = tk.StringVar(master=self.root, value="")
        self._view_var = tk.StringVar(master=self.root, value="Core")
        self._scope_var = tk.StringVar(master=self.root, value="All")
        self._page_var = tk.StringVar(master=self.root, value="")
        self._selection_title_var = tk.StringVar(master=self.root, value="Select a move")
        self._selection_sub_var = tk.StringVar(master=self.root, value="No Dolphin reads occur when you browse.")
        self._field_search_var = tk.StringVar(master=self.root, value="")
        self._show_empty_var = tk.BooleanVar(master=self.root, value=False)
        self._raw_status_var = tk.StringVar(master=self.root, value="Select a cached raw field, then choose Preview or Load full.")

        self._build_ui()
        self._bind_events()
        self._apply_filters(reset_page=True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        outer = ttk.Frame(self.root)
        outer.pack(fill="both", expand=True)
        outer.rowconfigure(2, weight=1)
        outer.columnconfigure(0, weight=1)

        title = ttk.Frame(outer, style="WorkbenchBar.TFrame", padding=(14, 9))
        title.grid(row=0, column=0, sticky="ew")
        title.columnconfigure(1, weight=1)
        ttk.Label(title, text="Frame Data", style="WorkbenchTitle.TLabel").grid(row=0, column=0, sticky="w")
        cname = str(self.target_slot.get("char_name") or "-")
        ttk.Label(title, text=f"{cname}  |  {self.slot_label}", style="WorkbenchSubtitle.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Label(title, text=f"Cached moves: {len(self._all_moves):,}", style="BadgeGood.TLabel").grid(row=0, column=2, sticky="e")

        bar = ttk.Frame(outer, style="CommandBar.TFrame", padding=(8, 7))
        bar.grid(row=1, column=0, sticky="ew", pady=(1, 1))
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="Search", style="CommandLabel.TLabel").grid(row=0, column=0, padx=(0, 5))
        search = ttk.Entry(bar, textvariable=self._search_var)
        search.grid(row=0, column=1, sticky="ew", padx=(0, 7))
        self._search_entry = search
        ttk.Button(bar, text="Clear", style="Toolbar.TButton", command=self._clear_search).grid(row=0, column=2, padx=(0, 10))

        ttk.Label(bar, text="Rows", style="CommandLabel.TLabel").grid(row=0, column=3, padx=(0, 5))
        scope = ttk.Combobox(bar, textvariable=self._scope_var, state="readonly", width=13,
                             values=("All", "Normals", "Specials", "Supers", "Projectiles"))
        scope.grid(row=0, column=4, padx=(0, 10))
        self._scope_combo = scope

        ttk.Button(bar, text="◀", style="Toolbar.TButton", width=3, command=self._prev_page).grid(row=0, column=5)
        ttk.Label(bar, textvariable=self._page_var, style="CommandMuted.TLabel").grid(row=0, column=6, padx=7)
        ttk.Button(bar, text="▶", style="Toolbar.TButton", width=3, command=self._next_page).grid(row=0, column=7, padx=(0, 10))
        ttk.Button(bar, text="Copy addr", style="Toolbar.TButton", command=self._copy_selected_address).grid(row=0, column=8, padx=(0, 6))
        ttk.Button(bar, text="Refresh display", style="Toolbar.TButton", command=self._refresh_selected_display).grid(row=0, column=9, padx=(0, 6))
        ttk.Button(bar, text="Bones", style="Toolbar.TButton", command=self._show_bones).grid(row=0, column=10, padx=(0, 6))
        ttk.Button(bar, text="Save patch", style="ToolbarPrimary.TButton", command=self._save_patch).grid(row=0, column=11)

        viewbar = ttk.Frame(bar, style="CommandBar.TFrame")
        viewbar.grid(row=1, column=0, columnspan=12, sticky="ew", pady=(7, 0))
        ttk.Label(viewbar, text="View", style="CommandLabel.TLabel").pack(side="left", padx=(0, 6))
        self._view_buttons = {}
        for key in ("core", "defense", "cancel", "launch", "hitbox", "projectile", "super", "research", "custom"):
            label = _VIEW_LABELS[key]
            btn = ttk.Button(viewbar, text=label, style="Toolbar.TButton", command=lambda k=key: self._activate_view(k))
            btn.pack(side="left", padx=(0, 4))
            self._view_buttons[key] = btn

        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew")

        left = ttk.Frame(panes, padding=(5, 4, 3, 4))
        right = ttk.Frame(panes, padding=(3, 4, 5, 4))
        panes.add(left, weight=3)
        panes.add(right, weight=2)
        self._panes = panes

        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(left, columns=tuple(self._logical_columns), show="headings", selectmode="browse")
        y = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        x = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        self.protection_canvas = tk.Canvas(left, height=126, bg="#0F1724", highlightthickness=0)
        self.protection_canvas.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        self.protection_canvas.bind("<Configure>", lambda _e: self._refresh_protection_timeline(self._selected_move))
        for col in self._logical_columns:
            label = self._field_label(col)
            self.tree.heading(col, text=label)
            self.tree.column(col, width=_COLUMN_WIDTH.get(col, 105), minwidth=55, anchor="w" if col in {"move", "context", "source"} else "center", stretch=False)
        self._set_table_view("core")
        try:
            self._view_buttons["core"].configure(style="ToolbarPrimary.TButton")
        except Exception:
            pass

        right.rowconfigure(2, weight=1)
        right.columnconfigure(0, weight=1)
        hero = ttk.Frame(right, style="InspectorHero.TFrame", padding=(11, 9))
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(hero, textvariable=self._selection_title_var, style="InspectorHeroTitle.TLabel", wraplength=520).pack(anchor="w")
        ttk.Label(hero, textvariable=self._selection_sub_var, style="InspectorHeroSub.TLabel", wraplength=520).pack(anchor="w", pady=(2, 0))

        fieldbar = ttk.Frame(right, style="CommandBar.TFrame", padding=(7, 5))
        fieldbar.grid(row=1, column=0, sticky="ew", pady=(0, 5))
        fieldbar.columnconfigure(1, weight=1)
        ttk.Label(fieldbar, text="Find field", style="CommandLabel.TLabel").grid(row=0, column=0, padx=(0, 5))
        ttk.Entry(fieldbar, textvariable=self._field_search_var).grid(row=0, column=1, sticky="ew")
        ttk.Checkbutton(fieldbar, text="Show empty", variable=self._show_empty_var, command=self._refresh_fields).grid(row=0, column=2, padx=(8, 0))

        nb = ttk.Notebook(right)
        nb.grid(row=2, column=0, sticky="nsew")
        self._notebook = nb

        # Fields tab
        fields_tab = ttk.Frame(nb)
        fields_tab.rowconfigure(0, weight=1)
        fields_tab.columnconfigure(0, weight=1)
        nb.add(fields_tab, text="Fields")
        self.fields_tree = ttk.Treeview(fields_tab, columns=("key", "value", "mode", "address"), show="tree headings", selectmode="browse")
        fy = ttk.Scrollbar(fields_tab, orient="vertical", command=self.fields_tree.yview)
        self.fields_tree.configure(yscrollcommand=fy.set)
        self.fields_tree.grid(row=0, column=0, sticky="nsew")
        fy.grid(row=0, column=1, sticky="ns")
        self.fields_tree.heading("#0", text="Field")
        self.fields_tree.heading("key", text="Key")
        self.fields_tree.heading("value", text="Value")
        self.fields_tree.heading("mode", text="Mode")
        self.fields_tree.heading("address", text="Address")
        self.fields_tree.column("#0", width=180, minwidth=120, stretch=False)
        self.fields_tree.column("key", width=145, minwidth=100, stretch=False)
        self.fields_tree.column("value", width=245, minwidth=130, stretch=True)
        self.fields_tree.column("mode", width=58, minwidth=45, stretch=False, anchor="center")
        self.fields_tree.column("address", width=105, minwidth=95, stretch=False, anchor="center")
        fbtn = ttk.Frame(fields_tab, padding=(0, 5, 0, 0))
        fbtn.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(fbtn, text="Edit value", style="ToolbarPrimary.TButton", command=self._edit_selected_field).pack(side="left")
        ttk.Button(fbtn, text="Edit backing", style="Toolbar.TButton", command=self._edit_selected_field_backing).pack(side="left", padx=(6, 0))
        ttk.Button(fbtn, text="Relabel", style="Toolbar.TButton", command=self._relabel_selected_field).pack(side="left", padx=(6, 0))
        ttk.Button(fbtn, text="Pin to table", style="Toolbar.TButton", command=self._pin_selected_field).pack(side="left", padx=(6, 0))
        ttk.Button(fbtn, text="Unpin", style="Toolbar.TButton", command=self._unpin_selected_field).pack(side="left", padx=(6, 0))
        ttk.Button(fbtn, text="Copy value", style="Toolbar.TButton", command=self._copy_selected_field_value).pack(side="left", padx=(6, 0))
        ttk.Button(fbtn, text="Open raw", style="Toolbar.TButton", command=self._open_selected_field_raw).pack(side="left", padx=(6, 0))

        # Unknown/native tab
        native_tab = ttk.Frame(nb)
        native_tab.rowconfigure(0, weight=1)
        native_tab.columnconfigure(0, weight=1)
        nb.add(native_tab, text="Unknown / Native")
        self.native_tree = ttk.Treeview(native_tab, columns=("offset", "raw", "signed", "float", "guess", "address"), show="headings", selectmode="browse")
        ny = ttk.Scrollbar(native_tab, orient="vertical", command=self.native_tree.yview)
        self.native_tree.configure(yscrollcommand=ny.set)
        self.native_tree.grid(row=0, column=0, sticky="nsew")
        ny.grid(row=0, column=1, sticky="ns")
        for col, label, width in (
            ("offset", "Field", 170), ("raw", "Raw", 95), ("signed", "Signed", 85),
            ("float", "Float", 90), ("guess", "Best guess", 220), ("address", "Operand addr", 105),
        ):
            self.native_tree.heading(col, text=label)
            self.native_tree.column(col, width=width, minwidth=70, stretch=(col == "guess"))
        nbtn = ttk.Frame(native_tab, padding=(0, 5, 0, 0))
        nbtn.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(nbtn, text="Edit raw operand", style="ToolbarPrimary.TButton", command=self._edit_selected_native_operand).pack(side="left")
        ttk.Button(nbtn, text="Copy operand addr", style="Toolbar.TButton", command=self._copy_native_operand_address).pack(side="left", padx=(6, 0))

        # Native words tab: every aligned 32-bit word in the cached native span,
        # including words no semantic decoder has claimed yet. This is the
        # deliberate "show me what we have NOT named" surface.
        words_tab = ttk.Frame(nb)
        words_tab.rowconfigure(0, weight=1)
        words_tab.columnconfigure(0, weight=1)
        nb.add(words_tab, text="Native Words")
        self._native_words_tab = words_tab
        self.native_words_tree = ttk.Treeview(words_tab, columns=("offset", "address", "raw", "signed", "float", "claim"), show="headings", selectmode="browse")
        wy = ttk.Scrollbar(words_tab, orient="vertical", command=self.native_words_tree.yview)
        self.native_words_tree.configure(yscrollcommand=wy.set)
        self.native_words_tree.grid(row=0, column=0, sticky="nsew")
        wy.grid(row=0, column=1, sticky="ns")
        for col, label, width in (
            ("offset", "Offset", 80), ("address", "Address", 105), ("raw", "Raw", 95),
            ("signed", "Signed", 85), ("float", "Float", 95), ("claim", "Claim / status", 300),
        ):
            self.native_words_tree.heading(col, text=label)
            self.native_words_tree.column(col, width=width, minwidth=65, stretch=(col == "claim"))
        wbtn = ttk.Frame(words_tab, padding=(0, 5, 0, 0))
        wbtn.grid(row=1, column=0, columnspan=2, sticky="ew")
        ttk.Button(wbtn, text="Edit word", style="ToolbarPrimary.TButton", command=self._edit_selected_native_word).pack(side="left")
        ttk.Button(wbtn, text="Copy address", style="Toolbar.TButton", command=self._copy_selected_native_word_address).pack(side="left", padx=(6, 0))
        ttk.Label(wbtn, text="Unknown means no current semantic field claims that word.", style="CommandMuted.TLabel").pack(side="left", padx=(12, 0))

        # Raw tab. Nothing heavy is materialized on row selection.
        raw_tab = ttk.Frame(nb)
        raw_tab.rowconfigure(1, weight=1)
        raw_tab.columnconfigure(0, weight=1)
        nb.add(raw_tab, text="Raw")
        self._raw_tab = raw_tab
        raw_top = ttk.Frame(raw_tab, padding=(0, 0, 0, 5))
        raw_top.grid(row=0, column=0, sticky="ew")
        raw_top.columnconfigure(0, weight=1)
        self.raw_combo = ttk.Combobox(raw_top, state="readonly")
        self.raw_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(raw_top, text="Preview", style="Toolbar.TButton", command=lambda: self._render_raw_selected(full=False)).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(raw_top, text="Load full", style="ToolbarPrimary.TButton", command=lambda: self._render_raw_selected(full=True)).grid(row=0, column=2, padx=(6, 0))
        raw_frame = ttk.Frame(raw_tab)
        raw_frame.grid(row=1, column=0, sticky="nsew")
        raw_frame.rowconfigure(0, weight=1)
        raw_frame.columnconfigure(0, weight=1)
        self.raw_text = tk.Text(raw_frame, wrap="none", bg="#0F1724", fg="#E6EEF8", insertbackground="#FFFFFF", font=("Consolas", 9))
        ry = ttk.Scrollbar(raw_frame, orient="vertical", command=self.raw_text.yview)
        rx = ttk.Scrollbar(raw_frame, orient="horizontal", command=self.raw_text.xview)
        self.raw_text.configure(yscrollcommand=ry.set, xscrollcommand=rx.set)
        self.raw_text.grid(row=0, column=0, sticky="nsew")
        ry.grid(row=0, column=1, sticky="ns")
        rx.grid(row=1, column=0, sticky="ew")
        ttk.Label(raw_tab, textvariable=self._raw_status_var, style="CommandMuted.TLabel").grid(row=2, column=0, sticky="ew", pady=(4, 0))

        status = ttk.Label(outer, textvariable=self._status_var, style="Status.TLabel", anchor="w", padding=(7, 4))
        status.grid(row=3, column=0, sticky="ew")

    def _bind_events(self):
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_table_double_click)
        self.fields_tree.bind("<Double-1>", lambda _e: self._edit_selected_field())
        self.native_tree.bind("<Double-1>", lambda _e: self._edit_selected_native_operand())
        self.native_words_tree.bind("<Double-1>", lambda _e: self._edit_selected_native_word())
        self._notebook.bind("<<NotebookTabChanged>>", self._on_inspector_tab_changed)
        self._search_var.trace_add("write", lambda *_: self._debounce_filter())
        self._field_search_var.trace_add("write", lambda *_: self._refresh_fields())
        self._scope_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_filters(reset_page=True))
        self.root.bind("<Control-f>", lambda _e: self._focus_search())
        self.root.bind("<Escape>", lambda _e: self._clear_search())

    # ------------------------------------------------------------ Model/filter

    def _focus_search(self):
        try:
            self._search_entry.focus_set()
            self._search_entry.selection_range(0, "end")
        except Exception:
            pass
        return "break"

    def _debounce_filter(self):
        if self._closing:
            return
        try:
            if self._search_after:
                self.root.after_cancel(self._search_after)
        except Exception:
            pass
        self._search_after = self.root.after(120, lambda: self._apply_filters(reset_page=True))

    def _clear_search(self):
        self._search_var.set("")
        self._apply_filters(reset_page=True)

    def _scope_accepts(self, mv: dict) -> bool:
        scope = self._scope_var.get()
        if scope == "All":
            return True
        try:
            if scope == "Projectiles":
                return bool(FPI.is_projectile_row(mv))
            if scope == "Supers":
                return bool(FSI.is_super_row(mv) or str(mv.get("kind") or "").lower() in {"super", "hyper"})
        except Exception:
            pass
        kind = str(mv.get("kind") or "").lower()
        if scope == "Specials":
            return kind == "special"
        if scope == "Normals":
            return kind in {"", "normal", "hit"} and not FPI.is_projectile_row(mv) and not FSI.is_super_row(mv)
        return True

    def _search_text_for_move(self, mv: dict) -> str:
        aid = mv.get("id")
        parts = [
            mv.get("move_name"), mv.get("pretty_name"), mv.get("action_name"),
            mv.get("kind"), mv.get("family_role"), mv.get("family_link_label"),
            mv.get("source"), f"0x{int(aid):04x}" if isinstance(aid, int) else "",
        ]
        return " ".join(str(x) for x in parts if x is not None).lower()

    def _apply_filters(self, reset_page=False):
        self._search_after = None
        q = str(self._search_var.get() or "").strip().lower()
        filtered = []
        for mv in self._all_moves:
            if not isinstance(mv, dict) or not self._scope_accepts(mv):
                continue
            if q and q not in self._search_text_for_move(mv):
                continue
            filtered.append(mv)
        self._filtered_moves = filtered
        if reset_page:
            self._page = 0
        max_page = max(0, (len(filtered) - 1) // self.PAGE_SIZE)
        self._page = min(self._page, max_page)
        self._render_page()

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def _next_page(self):
        max_page = max(0, (len(self._filtered_moves) - 1) // self.PAGE_SIZE)
        if self._page < max_page:
            self._page += 1
            self._render_page()

    # --------------------------------------------------------------- Main table

    def _change_view(self):
        view_key = _LABEL_TO_VIEW.get(self._view_var.get(), "core")
        self._activate_view(view_key)

    def _activate_view(self, view_key: str):
        if view_key not in _VIEW_LABELS:
            view_key = "core"
        self._current_view_key = view_key
        self._view_var.set(_VIEW_LABELS[view_key])
        self._set_table_view(view_key)
        self._render_page()
        for key, btn in getattr(self, "_view_buttons", {}).items():
            try:
                btn.configure(style="ToolbarPrimary.TButton" if key == view_key else "Toolbar.TButton")
            except Exception:
                pass

    def _set_table_view(self, view_key: str):
        if view_key == "custom":
            cols = tuple(c for c in self._custom_columns if c in self._logical_columns)
            if not cols:
                cols = ("move",)
        else:
            cols = tuple(c for c in _TABLE_VIEW_GROUPS.get(view_key, _TABLE_VIEW_GROUPS["core"]) if c in self._logical_columns)
        self.tree.configure(displaycolumns=cols)

    def _field_label(self, key: str) -> str:
        key = str(key)
        override = str((self._label_overrides or {}).get(key) or "").strip()
        if override:
            return override
        return fd_tree.FD_LABELS.get(key, key.replace("_", " ").title())

    def _ensure_tree_column(self, key: str):
        key = str(key)
        if not key:
            return
        if key not in self._logical_columns:
            self._logical_columns.append(key)
            try:
                self.tree.configure(columns=tuple(self._logical_columns))
            except Exception:
                return
        try:
            self.tree.heading(key, text=self._field_label(key))
            self.tree.column(key, width=_COLUMN_WIDTH.get(key, 120), minwidth=60,
                             anchor="w" if key in {"move", "context", "source"} else "center", stretch=False)
        except Exception:
            pass

    def _pin_selected_field(self):
        key = self._selected_field_key()
        if not key:
            self._status_var.set("Select a field to pin first.")
            return
        self._ensure_tree_column(key)
        if key not in self._custom_columns:
            self._custom_columns.append(key)
            _save_custom_columns(self._custom_columns)
        self._activate_view("custom")
        self._status_var.set(f"Pinned {self._field_label(key)} to the Custom table view.")

    def _unpin_selected_field(self):
        key = self._selected_field_key()
        if not key:
            self._status_var.set("Select a field to unpin first.")
            return
        if key == "move":
            self._status_var.set("Move always stays in the Custom table view.")
            return
        if key in self._custom_columns:
            self._custom_columns.remove(key)
            _save_custom_columns(self._custom_columns)
            if self._current_view_key == "custom":
                self._set_table_view("custom")
                self._render_page()
            self._status_var.set(f"Unpinned {self._field_label(key)}.")
        else:
            self._status_var.set(f"{self._field_label(key)} is not pinned.")

    def _relabel_selected_field(self):
        key = self._selected_field_key()
        if not key:
            self._status_var.set("Select a field to relabel first.")
            return
        current = self._field_label(key)
        text = simpledialog.askstring(
            "Relabel field",
            f"Internal key: {key}\nCurrent label: {current}\n\nEnter your label. Leave blank to restore the built-in label.",
            parent=self.root, initialvalue=current,
        )
        if text is None:
            return
        text = str(text).strip()
        builtin = fd_tree.FD_LABELS.get(key, key.replace("_", " ").title())
        if not text or text == builtin:
            self._label_overrides.pop(key, None)
        else:
            self._label_overrides[key] = text
        _save_json_dict(_LABEL_OVERRIDE_FILE, self._label_overrides)
        try:
            if key in self._logical_columns:
                self.tree.heading(key, text=self._field_label(key))
        except Exception:
            pass
        self._refresh_fields()
        self._status_var.set(f"Label for {key}: {self._field_label(key)}")

    def _move_label(self, mv: dict) -> str:
        aid = mv.get("id")
        name = str(mv.get("pretty_name") or mv.get("move_name") or "").strip()
        if not name:
            if isinstance(aid, int):
                try:
                    name = U.pretty_move_name(aid, str(self.target_slot.get("char_name") or ""))
                except Exception:
                    name = f"anim_{aid:04X}"
            else:
                name = "Unknown move"
        if isinstance(aid, int) and f"0x{aid:04X}" not in name:
            name = f"{name} [0x{aid:04X}]"
        return name

    def _hit_count(self, mv: dict) -> str:
        count = mv.get("multi_hit_count")
        if count is None:
            segs = mv.get("hit_segments")
            if isinstance(segs, list) and len(segs) > 1:
                count = len(segs)
        if count is None:
            count = mv.get("hits")
        try:
            n = int(count)
            return str(n) if n > 0 else ""
        except Exception:
            return str(count or "")

    def _format_column(self, mv: dict, col: str) -> str:
        # Projectile / super owned fields have their own canonical formatters.
        try:
            if col in FPI.PROJECTILE_COLUMNS and FPI.is_projectile_row(mv):
                return FPI.format_projectile_value(mv, col)
        except Exception:
            pass
        try:
            if col in FSI.SUPER_DISPATCH_COLUMNS and FSI.is_super_row(mv):
                return FSI.format_super_value(mv, col)
        except Exception:
            pass

        if col == "move": return self._move_label(mv)
        if col == "hits": return self._hit_count(mv)
        if col == "damage": return "" if mv.get("damage") is None else str(mv.get("damage"))
        if col == "meter": return "" if mv.get("meter") is None else str(mv.get("meter"))
        if col == "startup":
            v = mv.get("active_start") if mv.get("active_start") is not None else mv.get("startup")
            return "" if v is None else str(v)
        if col == "active":
            s = mv.get("active_start")
            e = mv.get("active_end")
            return f"{s}-{e}" if s is not None and e is not None else _cheap_preview(mv.get("active"), 80)
        if col == "active2":
            s, e = mv.get("active2_start"), mv.get("active2_end")
            return f"{s}-{e}" if s is not None and e is not None else ""
        if col == "recovery": return "" if mv.get("recovery") is None else str(mv.get("recovery"))
        if col == "hitstun": return U.fmt_stun(mv.get("hitstun"))
        if col == "blockstun": return U.fmt_stun(mv.get("blockstun"))
        if col == "hitstop": return U.fmt_stun(mv.get("hitstop"))
        if col == "adv_block_observed":
            return _fmt_adv(mv.get("adv_block_observed") if mv.get("adv_block_observed") is not None else mv.get("observed_adv_block"))
        if col == "adv_block_derived":
            return _fmt_adv(mv.get("adv_block_derived") if mv.get("adv_block_derived") is not None else mv.get("adv_block"))
        if col == "adv_hit_derived": return _fmt_adv(mv.get("adv_hit_derived") if mv.get("adv_hit_derived") is not None else mv.get("adv_hit"))
        if col == "attack_property":
            try: return fmt_attack_property(mv.get("attack_property")) if mv.get("attack_property") is not None else ""
            except Exception: return _cheap_preview(mv.get("attack_property"), 100)
        if col == "hit_reaction": return U.fmt_hit_reaction(mv.get("hit_reaction"))
        if col == "hit_result_flags": return U.fmt_hit_result_flags_ui(mv)
        if col == "kb_type": return U.fmt_kb_type_ui(mv)
        if col == "launch_profile": return U.fmt_launch_profile_ui(mv)
        if col == "kb_unknown": return U.fmt_kb_unknown_ui(mv)
        if col == "ground_kb": return U.fmt_ground_kb_ui(mv)
        if col == "ground_kb_y": return U.fmt_ground_kb_y_ui(mv)
        if col == "kb_x": return U.fmt_kb_x_ui(mv)
        if col == "air_kb": return U.fmt_air_kb_ui(mv)
        if col == "combo_kb_mod":
            v = mv.get("combo_kb_mod")
            try: return f"{int(v) & 0xFF} (0x{int(v) & 0xFF:02X})" if v is not None else ""
            except Exception: return _cheap_preview(v, 80)
        if col == "hb_main":
            try: return f"{float(mv.get('hb_r')):.1f}" if mv.get("hb_r") is not None else ""
            except Exception: return ""
        if col == "hit_spark": return U.fmt_hit_spark_ui(mv)
        if col == "stretch_part": return U.fmt_stretch_part_ui(mv)
        if col == "stretch_len": return U.fmt_stretch_len_ui(mv)
        if col == "stretch_width": return U.fmt_stretch_width_ui(mv)
        if col == "stretch_height": return U.fmt_stretch_height_ui(mv)
        if col == "stretch_time": return U.fmt_stretch_time_ui(mv)
        if col == "speed_mod": return U.fmt_speed_mod_ui(mv.get("speed_mod")) if mv.get("speed_mod") is not None else ""
        if col == "superbg": return U.fmt_superbg(mv.get("superbg_val")) if mv.get("superbg_val") is not None else ""
        if col == "abs": return _fmt_addr(mv.get("abs"))
        if col == "table_rel":
            try: return f"+0x{int(mv.get('table_rel')):X}" if mv.get("table_rel") is not None else ""
            except Exception: return _cheap_preview(mv.get("table_rel"), 80)
        value = mv.get(col)
        if isinstance(value, int) and (col.endswith("_addr") or col in {"source", "linked_owner_abs"}):
            return _fmt_addr(value)
        if isinstance(value, (list, tuple)):
            # Pinned sidebar fields should remain useful on the main grid.
            try:
                if value and all(isinstance(row, (list, tuple)) and len(row) >= 2 for row in value[:8]):
                    parts = []
                    for row in value[:8]:
                        if len(row) >= 3:
                            parts.append(f"{row[0]}-{row[1]}:{row[2]}")
                        else:
                            parts.append(f"{row[0]}-{row[1]}")
                    text = ", ".join(parts)
                    if len(value) > 8: text += ", …"
                    return text
                text = json.dumps(list(value)[:8], ensure_ascii=False, default=str)
                return text[:180] + ("…" if len(text) > 180 else "")
            except Exception:
                pass
        if isinstance(value, dict):
            try:
                text = json.dumps(dict(list(value.items())[:8]), ensure_ascii=False, default=str)
                return text[:180] + ("…" if len(text) > 180 else "")
            except Exception:
                pass
        return "" if value is None else _cheap_preview(value, 180)

    def _render_page(self):
        if self._closing:
            return
        keep_move = self._selected_move if isinstance(self._selected_move, dict) else None
        keep_item = None
        try:
            self.tree.delete(*self.tree.get_children(""))
        except Exception:
            return
        self._item_to_move.clear()
        start = self._page * self.PAGE_SIZE
        end = min(len(self._filtered_moves), start + self.PAGE_SIZE)
        display_cols = list(self.tree["displaycolumns"])
        for idx, mv in enumerate(self._filtered_moves[start:end], start=start):
            iid = f"m{idx}"
            values = [self._format_column(mv, col) if col in display_cols else "" for col in self._logical_columns]
            try:
                self.tree.insert("", "end", iid=iid, values=values)
                self._item_to_move[iid] = mv
                if mv is keep_move:
                    keep_item = iid
            except Exception:
                continue
        pages = max(1, math.ceil(len(self._filtered_moves) / self.PAGE_SIZE))
        self._page_var.set(f"{self._page + 1}/{pages}  ({len(self._filtered_moves):,})")
        if start < end:
            self._status_var.set(f"Showing {start + 1:,}-{end:,} of {len(self._filtered_moves):,}. Selection is cache-only.")
        else:
            self._status_var.set("No moves match the current filter.")
        if keep_item and keep_move is not None:
            self._selected_move = keep_move
            self._selected_item = keep_item
            try:
                self.tree.selection_set(keep_item)
                self.tree.focus(keep_item)
            except Exception:
                pass
            self._selection_title_var.set(self._move_label(keep_move))
            addr = _fmt_addr(keep_move.get("abs")) or "no native anchor"
            kind = str(keep_move.get("kind") or "normal")
            self._selection_sub_var.set(f"{kind}  |  {addr}  |  cached fields: {len(keep_move):,}")
            self._refresh_visible_inspector_tab()
            self._refresh_protection_timeline(keep_move)
        else:
            self._selected_move = None
            self._selected_item = None
            self._selection_title_var.set("Select a move")
            self._selection_sub_var.set("No Dolphin reads occur when you browse.")
            self._clear_inspector()

    def _refresh_row(self, item: str, mv: dict):
        if not item or not self.tree.exists(item):
            return
        display_cols = list(self.tree["displaycolumns"])
        for col in display_cols:
            try:
                self.tree.set(item, col, self._format_column(mv, col))
            except Exception:
                pass

    # --------------------------------------------------------------- Inspector

    def _on_select(self, _evt=None):
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        mv = self._item_to_move.get(item)
        if not isinstance(mv, dict):
            return
        self._selected_item = item
        self._selected_move = mv
        self._selection_title_var.set(self._move_label(mv))
        addr = _fmt_addr(mv.get("abs")) or "no native anchor"
        kind = str(mv.get("kind") or "normal")
        self._selection_sub_var.set(f"{kind}  |  {addr}  |  cached fields: {len(mv):,}")
        # Only the visible inspector tab is populated.
        self._refresh_visible_inspector_tab()
        self._refresh_protection_timeline(mv)

    def _coerce_ranges(self, value):
        out = []
        if isinstance(value, (list, tuple)):
            for row in value:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    try:
                        a, b = int(row[0]), int(row[1])
                        if b >= a:
                            out.append((a, b))
                    except Exception:
                        pass
        return out

    def _coerce_segments(self, value):
        out = []
        if isinstance(value, (list, tuple)):
            for row in value:
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    try:
                        a, b = int(row[0]), int(row[1])
                        if b >= a:
                            out.append((a, b))
                    except Exception:
                        pass
        return out

    def _refresh_protection_timeline(self, mv=None):
        canvas = getattr(self, "protection_canvas", None)
        if canvas is None:
            return
        try:
            canvas.delete("all")
            width = max(400, int(canvas.winfo_width() or 400))
        except Exception:
            return
        if not isinstance(mv, dict):
            canvas.create_text(10, 10, anchor="nw", text="Protection timeline: select a move", fill="#8FA2B8", font=("Segoe UI", 9))
            return
        runtime = mv.get("runtime_protection") if isinstance(mv.get("runtime_protection"), dict) else {}
        lanes = [
            ("INVULN", self._coerce_ranges(mv.get("invuln_ranges") or runtime.get("invuln_ranges")), "#1F6AA5"),
            ("COUNTER", self._coerce_segments(mv.get("counter_segments") or runtime.get("counter_segments")), "#A65A2A"),
            ("GUARD", self._coerce_segments(mv.get("guard_point_segments") or runtime.get("guard_segments")), "#5B7F37"),
            ("ARMOR", self._coerce_ranges(mv.get("armor_segments") or runtime.get("armor_segments")), "#785A9C"),
            ("BAROQUE", self._coerce_ranges(mv.get("baroque_ranges") or runtime.get("baroque_ranges")), "#8A4B63"),
        ]
        ends = [int(mv.get("animation_total_frames") or 0), int(mv.get("recovery") or 0), int(mv.get("active_end") or 0), 20]
        for _name, ranges, _color in lanes:
            ends.extend(b for _a, b in ranges)
        end_frame = max(ends) if ends else 20
        left = 70
        right = max(left + 100, width - 10)
        def fx(frame):
            return left + (max(0, int(frame)) / max(1, end_frame + 1)) * (right - left)
        canvas.create_text(10, 5, anchor="nw", text=f"PROTECTION TIMELINE  |  1-{end_frame}F", fill="#C9D7E8", font=("Segoe UI", 8, "bold"))
        y = 27
        for name, ranges, color in lanes:
            canvas.create_text(10, y + 5, anchor="w", text=name, fill="#9FB2C8", font=("Segoe UI", 7, "bold"))
            canvas.create_rectangle(left, y, right, y + 10, fill="#223247", outline="")
            for a, b in ranges:
                canvas.create_rectangle(fx(a), y, max(fx(b + 1) - 1, fx(a) + 2), y + 10, fill=color, outline="")
            if not ranges:
                label_present = bool(mv.get(name.lower()) or (name == "GUARD" and mv.get("guard_point")) or (name == "BAROQUE" and mv.get("baroque_probe")))
                if label_present:
                    canvas.create_text(left + 4, y + 5, anchor="w", text="detected / frames unprofiled", fill="#AAB8C8", font=("Segoe UI", 7))
            y += 19

    def _clear_inspector(self):
        for tree in (getattr(self, "fields_tree", None), getattr(self, "native_tree", None), getattr(self, "native_words_tree", None)):
            if tree is not None:
                try: tree.delete(*tree.get_children(""))
                except Exception: pass
        self._field_row_to_key.clear()
        self._unknown_row_to_index.clear()
        self._native_word_row_to_offset.clear()
        try:
            self._refresh_protection_timeline(None)
        except Exception:
            pass
        try:
            self.raw_combo.configure(values=())
            self.raw_combo.set("")
            self.raw_text.delete("1.0", "end")
        except Exception:
            pass

    def _on_inspector_tab_changed(self, _evt=None):
        self._refresh_visible_inspector_tab()

    def _refresh_visible_inspector_tab(self):
        if not isinstance(self._selected_move, dict):
            return
        try:
            tab = self._notebook.tab(self._notebook.select(), "text")
        except Exception:
            tab = "Fields"
        if tab == "Fields":
            self._refresh_fields()
        elif tab == "Unknown / Native":
            self._refresh_native_ops()
        elif tab == "Native Words":
            self._refresh_native_words()
        elif tab == "Raw":
            self._refresh_raw_keys()

    def _known_schema_keys(self):
        seen = []
        have = set()
        for _title, fields in getattr(fd_tree, "FD_INSPECTOR_GROUPS", ()): 
            for key in fields:
                key = str(key)
                if key not in have:
                    have.add(key); seen.append(key)
        for key in self._logical_columns:
            if key not in have:
                have.add(key); seen.append(key)
        return seen

    def _field_mode(self, mv: dict, key: str) -> str:
        if key in _STANDARD_EDITABLE:
            return "EDIT"
        try:
            if FPI.is_projectile_row(mv) and FPI.projectile_editable(key):
                return "EDIT"
        except Exception:
            pass
        try:
            if FSI.is_super_row(mv) and FSI.super_editable(key):
                return "EDIT"
        except Exception:
            pass
        if key in {"native_property_a", "native_property_b", "native_hit_result_raw"}:
            addr_key = {
                "native_property_a": "native_property_a_addr",
                "native_property_b": "native_property_b_addr",
                "native_hit_result_raw": "native_hit_result_addr",
            }[key]
            if mv.get(addr_key):
                return "RAW"
        if key == "native_field_ops":
            return "RAW"
        # A semantic label is not a lock. If the cache carries an exact native
        # backing address, expose a raw editor even when we do not have a
        # verified high-level writer for that interpretation yet.
        if self._field_address_for(mv, key):
            return "RAW"
        if key.startswith("_"):
            return "INT"
        return "READ"

    def _field_address_for(self, mv: dict, key: str) -> int | None:
        try:
            if FPI.is_projectile_row(mv):
                a = FPI.projectile_field_addr(mv, key)
                if a: return int(a)
        except Exception:
            pass
        try:
            if FSI.is_super_row(mv):
                a = FSI.super_field_addr(mv, key)
                if a: return int(a)
        except Exception:
            pass
        mapping = {
            "damage": ("damage_addr", 0), "meter": ("meter_addr", 0),
            "startup": ("active_addr", 0), "active": ("active_addr", 0),
            "active2": ("active2_addr", 0),
            "hitstun": ("hitstun_addr", 0), "blockstun": ("blockstun_addr", 0),
            "hitstop": ("hitstop_addr", 0), "hit_spark": ("hit_spark_addr", 0),
            "stretch_part": ("stretch_part_addr", 0), "stretch_len": ("stretch_len_addr", 0),
            "stretch_width": ("stretch_width_addr", 0), "stretch_height": ("stretch_height_addr", 0),
            "stretch_time": ("stretch_time_addr", 0), "post_link": ("post_link_addr", 0),
            "kb_type": ("knockback_addr", 1), "launch_profile": ("knockback_addr", 4),
            "kb_unknown": ("knockback_addr", 8), "kb_x": ("knockback_addr", 12),
            "air_kb": ("knockback_addr", 16), "combo_kb_mod": ("combo_kb_mod_addr", 0),
            "speed_mod": ("speed_mod_addr", 0), "attack_property": ("attack_property_addr", 0),
            "superbg": ("superbg_addr", 0), "abs": ("abs", 0),
        }
        key_off = mapping.get(key)
        if key_off:
            addr_key, off = key_off
            addr = mv.get(addr_key)
            if not addr and key in {"hitstun", "blockstun", "hitstop"} and mv.get("stun_addr"):
                addr = int(mv["stun_addr"])
                off = {"hitstun": 15, "blockstun": 31, "hitstop": 38}[key]
            if addr:
                return int(addr) + int(off)
        # A direct <field>_addr is still useful as evidence, even if there is
        # no generic writer for that semantic field.
        direct = mv.get(f"{key}_addr")
        if isinstance(direct, int) and direct:
            return int(direct)
        return None

    def _display_field_value(self, key: str, value) -> str:
        if _empty(value):
            return "not found"
        if isinstance(value, int) and (key.endswith("_addr") or key in {"abs", "parent_abs", "linked_owner_abs", "generic_write_owner_start", "generic_write_owner_end"}):
            return _fmt_addr(value)
        if key == "table_rel" and isinstance(value, int):
            return f"+0x{value:X}"
        # Reuse the friendly table formatter when this is a known combat field.
        if key in self._logical_columns and isinstance(self._selected_move, dict):
            friendly = self._format_column(self._selected_move, key)
            if friendly != "":
                return friendly
        return _cheap_preview(value, 280)

    def _refresh_fields(self):
        tree = self.fields_tree
        try: tree.delete(*tree.get_children(""))
        except Exception: return
        self._field_row_to_key.clear()
        mv = self._selected_move
        if not isinstance(mv, dict):
            return
        q = str(self._field_search_var.get() or "").strip().lower()
        show_empty = bool(self._show_empty_var.get())
        keys = list(self._known_schema_keys()) if show_empty else []
        seen = set(keys)
        for key in mv.keys():
            key = str(key)
            if key not in seen:
                seen.add(key); keys.append(key)

        grouped: dict[str, list[tuple[str, str, str, str, str]]] = defaultdict(list)
        for key in keys:
            present = key in mv
            value = mv.get(key) if present else None
            if not show_empty and _empty(value):
                continue
            label = self._field_label(key)
            text = self._display_field_value(key, value)
            mode = self._field_mode(mv, key) if present else "-"
            address = self._field_address_for(mv, key) if present else None
            hay = f"{label} {key} {text} {mode} {_fmt_addr(address)}".lower()
            if q and q not in hay:
                continue
            group = fd_tree.inspector_group_for_key(key)
            grouped[group].append((key, label, text, mode, _fmt_addr(address)))

        order = [title for title, _fields in getattr(fd_tree, "FD_INSPECTOR_GROUPS", ())]
        order += ["Other / cached", "Internal / scanner"]
        default_open = {"Core", "Defense", "Cancels / advantage", "Attack behavior", "Launch / knockback", "Hitbox / reach / FX"}
        if q:
            default_open = set(order)
        for gi, group in enumerate(order):
            rows = grouped.get(group)
            if not rows:
                continue
            gid = f"g{gi}"
            try: tree.insert("", "end", iid=gid, text=group, values=("", f"{len(rows)} fields", "", ""), open=group in default_open)
            except Exception: continue
            for ri, (key, label, text, mode, addr) in enumerate(rows):
                iid = f"f{gi}_{ri}"
                try:
                    tree.insert(gid, "end", iid=iid, text=label, values=(key, text, mode, addr))
                    self._field_row_to_key[iid] = key
                except Exception:
                    pass

    def _selected_field_key(self) -> str | None:
        sel = self.fields_tree.selection()
        if not sel:
            return None
        return self._field_row_to_key.get(sel[0])

    def _copy_selected_field_value(self):
        key = self._selected_field_key()
        mv = self._selected_move
        if not key or not isinstance(mv, dict):
            self._status_var.set("Select a field first.")
            return
        value = mv.get(key)
        try:
            if isinstance(value, bytes):
                payload = value.hex(" ").upper()
            elif isinstance(value, (dict, list, tuple, set)):
                payload = json.dumps(list(value) if isinstance(value, set) else value, indent=2, ensure_ascii=False, default=str)
            else:
                payload = str(value)
            self.root.clipboard_clear(); self.root.clipboard_append(payload)
            self._status_var.set(f"Copied {key}")
        except Exception as exc:
            self._status_var.set(f"Copy failed: {exc}")

    def _open_selected_field_raw(self):
        key = self._selected_field_key()
        if not key:
            self._status_var.set("Select a field first.")
            return
        try:
            self._notebook.select(self._raw_tab)
        except Exception:
            pass
        self._refresh_raw_keys(preselect=key)

    def _edit_field_backing_key(self, key: str, mv: dict):
        if not key or not isinstance(mv, dict):
            self._status_var.set("Select a field with a backing address first.")
            return
        addr = self._field_address_for(mv, key)
        if not addr:
            self._status_var.set(f"{key} has no exact backing address in this cached row.")
            return
        try:
            from tvcgui.platform.dolphin import rbytes, wbytes
        except Exception:
            messagebox.showerror("Raw backing edit", "Dolphin memory I/O is unavailable.", parent=self.root)
            return
        try:
            current = rbytes(int(addr), 4) or b""
            current = bytes(current[:4]).ljust(4, b"\x00")
            raw = int.from_bytes(current, "big", signed=False)
            fval = struct.unpack(">f", current)[0]
        except Exception:
            raw = 0
            fval = 0.0
        text = simpledialog.askstring(
            "Edit native backing",
            f"{self._field_label(key)}\nInternal key: {key}\nAddress: 0x{int(addr):08X}\n"
            f"Current first 4 bytes: 0x{raw:08X} / f32 {fval:g}\n\n"
            "Enter a typed value:\n"
            "u8:255   u16:0x1234   u32:0x12345678   s32:-1   f32:1.25\n\n"
            "This bypasses the semantic label and writes the backing memory directly.",
            parent=self.root, initialvalue=f"u32:0x{raw:08X}",
        )
        if text is None:
            return
        token = str(text).strip()
        try:
            prefix, payload = token.split(":", 1)
            prefix = prefix.strip().lower()
            payload = payload.strip()
            if prefix == "u8":
                blob = int(payload, 0).to_bytes(1, "big", signed=False)
            elif prefix == "u16":
                blob = int(payload, 0).to_bytes(2, "big", signed=False)
            elif prefix == "u32":
                blob = (int(payload, 0) & 0xFFFFFFFF).to_bytes(4, "big", signed=False)
            elif prefix == "s32":
                blob = int(payload, 0).to_bytes(4, "big", signed=True)
            elif prefix == "f32":
                blob = struct.pack(">f", float(payload))
            else:
                raise ValueError("Use u8, u16, u32, s32, or f32")
        except Exception as exc:
            messagebox.showerror("Invalid raw value", str(exc), parent=self.root)
            return
        try:
            ok = wbytes(int(addr), blob)
        except Exception:
            ok = False
        if ok is False:
            messagebox.showerror("Write failed", f"Could not write 0x{int(addr):08X}.", parent=self.root)
            return
        _dk = (id(mv), f"raw:{key}")
        self._dirty.add(_dk); self._dirty_refs[_dk] = (mv, f"raw:{key}")
        self._status_var.set(
            f"Raw backing write: {self._field_label(key)} @ 0x{int(addr):08X}. "
            "The cached semantic interpretation is intentionally unchanged."
        )

    def _edit_selected_field_backing(self):
        key = self._selected_field_key()
        mv = self._selected_move
        self._edit_field_backing_key(key, mv)

    def _edit_selected_field(self):
        key = self._selected_field_key()
        mv = self._selected_move
        item = self._selected_item
        if not key or not isinstance(mv, dict) or not item:
            self._status_var.set("Select a writable field first.")
            return
        mode = self._field_mode(mv, key)
        if mode == "EDIT":
            current = self._display_field_value(key, mv.get(key))
            self._route_standard_edit(key, item, mv, current)
            self._refresh_row(item, mv)
            self._refresh_fields()
            return
        if mode == "RAW" and key in {"native_property_a", "native_property_b", "native_hit_result_raw"}:
            self._edit_direct_cached_u32(key, mv)
            self._refresh_fields()
            return
        if mode == "RAW" and key != "native_field_ops":
            self._edit_selected_field_backing()
            return
        if key == "native_field_ops":
            self._notebook.select(1)
            self._refresh_native_ops()
            self._status_var.set("Select an operand and edit it from Unknown / Native.")
            return
        self._status_var.set(f"{key} is readable but has no verified semantic writer. If it has a native field op, edit that raw operand instead.")

    # -------------------------------------------------------- Unknown/native ops

    def _refresh_native_ops(self):
        tree = self.native_tree
        try: tree.delete(*tree.get_children(""))
        except Exception: return
        self._unknown_row_to_index.clear()
        mv = self._selected_move
        ops = list((mv or {}).get("native_field_ops") or []) if isinstance(mv, dict) else []
        for index, op in enumerate(ops):
            if not isinstance(op, dict):
                continue
            try:
                off = int(op.get("target_offset") or 0)
                raw = int(op.get("value_raw") or 0) & 0xFFFFFFFF
                signed = raw if raw < 0x80000000 else raw - 0x100000000
                fv = op.get("value_float")
                if fv is None:
                    try:
                        test = struct.unpack(">f", struct.pack(">I", raw))[0]
                        fv = test if math.isfinite(test) and abs(test) < 1e12 else None
                    except Exception:
                        fv = None
                label, guess = _native_identity(off)
                packet = int(op.get("packet_abs") or 0)
                addr = packet + 12 if packet else 0
                iid = f"u{index}"
                tree.insert("", "end", iid=iid, values=(label, f"0x{raw:08X}", str(signed), "" if fv is None else f"{float(fv):g}", guess, _fmt_addr(addr)))
                self._unknown_row_to_index[iid] = index
            except Exception:
                continue
        self._status_var.set(f"{len(ops)} cached native field operand(s). No scan was run.")

    def _selected_native_op(self):
        mv = self._selected_move
        if not isinstance(mv, dict): return None, None
        sel = self.native_tree.selection()
        if not sel: return mv, None
        index = self._unknown_row_to_index.get(sel[0])
        ops = mv.get("native_field_ops") or []
        if index is None or index < 0 or index >= len(ops): return mv, None
        return mv, ops[index]

    def _copy_native_operand_address(self):
        _mv, op = self._selected_native_op()
        if not isinstance(op, dict):
            self._status_var.set("Select a native operand first.")
            return
        packet = int(op.get("packet_abs") or 0)
        if not packet:
            self._status_var.set("This operand has no exact packet address.")
            return
        addr = packet + 12
        self.root.clipboard_clear(); self.root.clipboard_append(f"0x{addr:08X}")
        self._status_var.set(f"Copied 0x{addr:08X}")

    def _edit_selected_native_operand(self):
        mv, op = self._selected_native_op()
        if not isinstance(mv, dict) or not isinstance(op, dict):
            self._status_var.set("Select a native operand first.")
            return
        packet = int(op.get("packet_abs") or 0)
        if not packet:
            self._status_var.set("This operand has no exact packet address.")
            return
        addr = packet + 12
        off = int(op.get("target_offset") or 0)
        old = int(op.get("value_raw") or 0) & 0xFFFFFFFF
        label, guess = _native_identity(off)
        text = simpledialog.askstring(
            "Edit native operand",
            f"{label}\nBest guess: {guess}\nOperand address: 0x{addr:08X}\n\n"
            "Enter 0xHEX or decimal. Prefix float with f: and signed integer with s:.",
            initialvalue=f"0x{old:08X}", parent=self.root,
        )
        if text is None: return
        try:
            token = text.strip()
            if token.lower().startswith("f:"):
                raw = struct.unpack(">I", struct.pack(">f", float(token[2:].strip())))[0]
            elif token.lower().startswith("s:"):
                raw = int(token[2:].strip(), 0) & 0xFFFFFFFF
            else:
                raw = int(token, 0) & 0xFFFFFFFF
        except Exception:
            messagebox.showerror("Invalid value", "Use 0xHEX, decimal, f:<float>, or s:<signed integer>.", parent=self.root)
            return
        temp = {"operand_addr": addr, "operand_value": old}
        if not write_u32_field_inline(temp, "operand_addr", "operand_value", raw):
            messagebox.showerror("Write failed", "Could not write the native operand.", parent=self.root)
            return
        op["original_value_raw"] = op.get("original_value_raw", old)
        op["value_raw"] = raw
        op["value_signed"] = raw if raw < 0x80000000 else raw - 0x100000000
        try:
            fv = struct.unpack(">f", struct.pack(">I", raw))[0]
            op["value_float"] = fv if math.isfinite(fv) and abs(fv) < 1e12 else None
        except Exception:
            op["value_float"] = None
        _dk = (id(mv), f"native:+0x{off:04X}")
        self._dirty.add(_dk); self._dirty_refs[_dk] = (mv, f"native:+0x{off:04X}")
        self._status_var.set(f"RAW write: {label} = 0x{raw:08X} @ 0x{addr:08X}")
        self._refresh_native_ops()

    # ------------------------------------------------------------ Native words

    def _native_span_bytes(self, mv: dict) -> bytes:
        value = mv.get("native_raw_span_hex")
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value)
        if isinstance(value, str) and value.strip():
            try:
                cleaned = "".join(ch for ch in value if ch in "0123456789abcdefABCDEF")
                if len(cleaned) % 2:
                    cleaned = cleaned[:-1]
                return bytes.fromhex(cleaned)
            except Exception:
                pass
        value = mv.get("native_script_head_hex")
        if isinstance(value, str) and value.strip():
            try:
                cleaned = "".join(ch for ch in value if ch in "0123456789abcdefABCDEF")
                if len(cleaned) % 2:
                    cleaned = cleaned[:-1]
                return bytes.fromhex(cleaned)
            except Exception:
                pass
        return b""

    def _native_word_claims(self, mv: dict, blob_len: int) -> dict[int, list[str]]:
        base = int(mv.get("abs") or 0)
        claims: dict[int, list[str]] = defaultdict(list)
        if not base or blob_len <= 0:
            return claims
        for key, value in mv.items():
            if not str(key).endswith("_addr"):
                continue
            try:
                addr = int(value or 0)
            except Exception:
                continue
            if not (base <= addr < base + blob_len):
                continue
            word_off = ((addr - base) // 4) * 4
            claims[word_off].append(self._field_label(str(key[:-5] or key)))
        for op in list(mv.get("native_field_ops") or []):
            if not isinstance(op, dict):
                continue
            try:
                addr = int(op.get("packet_abs") or 0) + 12
                target = int(op.get("target_offset") or 0)
            except Exception:
                continue
            if base <= addr < base + blob_len:
                word_off = ((addr - base) // 4) * 4
                label, _guess = _native_identity(target)
                claims[word_off].append(f"field operand: {label}")
        return claims

    def _refresh_native_words(self):
        tree = self.native_words_tree
        try:
            tree.delete(*tree.get_children(""))
        except Exception:
            return
        self._native_word_row_to_offset.clear()
        mv = self._selected_move
        if not isinstance(mv, dict):
            return
        blob = self._native_span_bytes(mv)
        base = int(mv.get("abs") or 0)
        if not blob or not base:
            try:
                tree.insert("", "end", values=("-", "-", "-", "-", "-", "No cached native span for this move"))
            except Exception:
                pass
            return
        claims = self._native_word_claims(mv, len(blob))
        # Bound normal browsing to the cached span. Rendering occurs only when
        # this explicit tab is opened, so a large move does not tax selection.
        usable = len(blob) - (len(blob) % 4)
        for off in range(0, usable, 4):
            chunk = blob[off:off+4]
            raw = int.from_bytes(chunk, "big", signed=False)
            signed = raw if raw < 0x80000000 else raw - 0x100000000
            try:
                fv = struct.unpack(">f", chunk)[0]
                ftxt = f"{fv:g}" if math.isfinite(fv) and abs(fv) < 1e12 else ""
            except Exception:
                ftxt = ""
            claim_list = claims.get(off) or []
            claim = " / ".join(dict.fromkeys(claim_list)) if claim_list else "UNKNOWN / UNCLAIMED"
            iid = f"nw_{off:06X}"
            try:
                tree.insert("", "end", iid=iid, values=(f"+0x{off:04X}", f"0x{base+off:08X}", f"0x{raw:08X}", str(signed), ftxt, claim))
                self._native_word_row_to_offset[iid] = off
            except Exception:
                pass

    def _selected_native_word(self):
        mv = self._selected_move
        if not isinstance(mv, dict):
            return (None, None, None)
        sel = self.native_words_tree.selection()
        if not sel:
            return (mv, None, None)
        off = self._native_word_row_to_offset.get(sel[0])
        if off is None:
            return (mv, None, None)
        try:
            addr = int(mv.get("abs") or 0) + int(off)
        except Exception:
            addr = None
        return (mv, off, addr)

    def _copy_selected_native_word_address(self):
        _mv, _off, addr = self._selected_native_word()
        if not addr:
            self._status_var.set("Select a native word first.")
            return
        text = f"0x{int(addr):08X}"
        self.root.clipboard_clear(); self.root.clipboard_append(text)
        self._status_var.set(f"Copied {text}")

    def _edit_selected_native_word(self):
        mv, off, addr = self._selected_native_word()
        if not isinstance(mv, dict) or off is None or not addr:
            self._status_var.set("Select a native word first.")
            return
        blob = self._native_span_bytes(mv)
        if off + 4 > len(blob):
            return
        old_raw = int.from_bytes(blob[off:off+4], "big", signed=False)
        text = simpledialog.askstring(
            "Edit native word",
            f"Move-relative offset +0x{off:04X}\nAddress: 0x{addr:08X}\nCurrent: 0x{old_raw:08X}\n\n"
            "Enter 0xHEX/decimal, f:1.25, or s:-1. This is an immediate raw write.",
            parent=self.root, initialvalue=f"0x{old_raw:08X}",
        )
        if text is None:
            return
        try:
            token = str(text).strip()
            if token.lower().startswith("f:"):
                raw = struct.unpack(">I", struct.pack(">f", float(token[2:].strip())))[0]
            elif token.lower().startswith("s:"):
                raw = int(token[2:].strip(), 0) & 0xFFFFFFFF
            else:
                raw = int(token, 0) & 0xFFFFFFFF
            payload = raw.to_bytes(4, "big")
            from tvcgui.platform.dolphin import wbytes
            ok = wbytes(int(addr), payload)
        except Exception as exc:
            messagebox.showerror("Native word write", str(exc), parent=self.root)
            return
        if ok is False:
            messagebox.showerror("Native word write", f"Could not write 0x{addr:08X}.", parent=self.root)
            return
        # Update the cached raw span so repeated experimentation stays coherent
        # without forcing a live rescan.
        updated = bytearray(blob)
        updated[off:off+4] = payload
        mv["native_raw_span_hex"] = bytes(updated).hex(" ").upper()
        _dk = (id(mv), f"native_word:+0x{off:04X}")
        self._dirty.add(_dk); self._dirty_refs[_dk] = (mv, f"native_word:+0x{off:04X}")
        self._status_var.set(f"RAW word write +0x{off:04X} = 0x{raw:08X}")
        self._refresh_native_words()

    # ---------------------------------------------------------------- Raw tab

    def _refresh_raw_keys(self, preselect: str | None = None):
        mv = self._selected_move
        if not isinstance(mv, dict):
            self.raw_combo.configure(values=())
            return
        # Prefer genuinely heavy/native values first, but every cached key can
        # be selected so nothing is hidden from the user.
        heavy = []
        normal = []
        for key, value in mv.items():
            if isinstance(value, (dict, list, tuple, set, bytes, bytearray)) or (isinstance(value, str) and len(value) > 240) or key.startswith("native_"):
                heavy.append(str(key))
            else:
                normal.append(str(key))
        keys = sorted(set(heavy)) + [k for k in sorted(set(normal)) if k not in set(heavy)]
        self.raw_combo.configure(values=keys)
        if preselect in keys:
            self.raw_combo.set(preselect)
        elif self.raw_combo.get() not in keys:
            self.raw_combo.set(keys[0] if keys else "")
        self._raw_status_var.set(f"{len(keys)} cached field(s). Nothing is rendered until Preview or Load full.")

    def _raw_payload_text(self, value, full: bool) -> str:
        if isinstance(value, (bytes, bytearray, memoryview)):
            blob = bytes(value)
            if not full:
                blob = blob[:1024]
            # Hex dump, 16 bytes per line.
            lines = []
            for off in range(0, len(blob), 16):
                chunk = blob[off:off+16]
                hx = " ".join(f"{b:02X}" for b in chunk)
                asc = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
                lines.append(f"{off:06X}  {hx:<47}  {asc}")
            if not full and len(value) > len(blob):
                lines.append(f"\n... previewed {len(blob):,} of {len(value):,} bytes ...")
            return "\n".join(lines)
        if isinstance(value, set):
            value = list(value)
        if isinstance(value, (dict, list, tuple)):
            if not full:
                if isinstance(value, dict):
                    sample = dict(list(value.items())[:20])
                else:
                    sample = list(value)[:20]
                text = json.dumps(sample, indent=2, ensure_ascii=False, default=str)
                if len(value) > 20:
                    text += f"\n\n... previewed 20 of {len(value):,} entries ..."
                return text
            return json.dumps(value, indent=2, ensure_ascii=False, default=str)
        text = str(value)
        if not full and len(text) > 8000:
            return text[:8000] + f"\n\n... previewed 8,000 of {len(text):,} characters ..."
        return text

    def _render_raw_selected(self, full=False):
        mv = self._selected_move
        key = self.raw_combo.get()
        if not isinstance(mv, dict) or not key:
            self._raw_status_var.set("Select a move and raw field first.")
            return
        value = mv.get(key)
        try:
            text = self._raw_payload_text(value, full=bool(full))
        except Exception as exc:
            text = f"Could not render {key}: {exc}"
        self.raw_text.delete("1.0", "end")
        self.raw_text.insert("1.0", text)
        self._raw_status_var.set(f"{key}: {'full value' if full else 'bounded preview'}")

    # --------------------------------------------------------------- Editing

    def _on_table_double_click(self, event):
        item = self.tree.identify_row(event.y)
        column_token = self.tree.identify_column(event.x)
        if not item or not column_token:
            return
        try:
            display = list(self.tree["displaycolumns"])
            index = int(column_token[1:]) - 1
            col = display[index]
        except Exception:
            return
        if col == "move":
            self._status_var.set("Move replacement is intentionally outside the lean click path. Use the main move-edit tools for animation replacement.")
            return
        mv = self._item_to_move.get(item)
        if not isinstance(mv, dict): return
        current = self.tree.set(item, col)
        self._route_standard_edit(col, item, mv, current)
        self._refresh_row(item, mv)
        if mv is self._selected_move:
            self._refresh_visible_inspector_tab()

    def _route_standard_edit(self, col_name: str, item: str, mv: dict, current_val: str) -> None:
        # Projectile and super rows own many additional typed fields.
        try:
            if FSI.is_super_row(mv) and FSI.super_editable(col_name):
                self._edit_super_value(col_name, item, mv)
                return
        except Exception:
            pass
        try:
            if FPI.is_projectile_row(mv) and FPI.projectile_editable(col_name):
                self._edit_projectile_value(col_name, item, mv)
                return
        except Exception:
            pass

        routes = {
            "damage": self._edit_damage,
            "meter": self._edit_meter,
            "startup": self._edit_startup,
            "active": self._edit_active,
            "active2": self._edit_active2,
            "hitstun": self._edit_hitstun,
            "blockstun": self._edit_blockstun,
            "hitstop": self._edit_hitstop,
            "hit_spark": self._edit_hit_spark,
            "stretch_part": self._edit_stretch_part,
            "stretch_len": self._edit_stretch_len,
            "stretch_width": self._edit_stretch_width,
            "stretch_height": self._edit_stretch_height,
            "stretch_time": self._edit_stretch_time,
            "post_link": self._edit_post_link,
            "kb_type": self._edit_kb_type,
            "launch_profile": self._edit_launch_profile,
            "kb_unknown": self._edit_kb_unknown,
            "ground_kb": self._edit_ground_kb,
            "ground_kb_y": self._edit_ground_kb_y,
            "kb_x": self._edit_kb_x,
            "air_kb": self._edit_air_kb,
            "combo_kb_mod": self._edit_combo_kb_mod,
            "hit_reaction": self._edit_hit_reaction,
            "hit_result_flags": self._edit_hit_result_flags,
            "hb_main": self._edit_hitbox_main,
            "hb": self._edit_hitbox,
        }
        fn = routes.get(col_name)
        if fn:
            fn(item, mv, current_val)
            return
        if col_name == "superbg":
            self._toggle_superbg(item, mv); return
        if col_name == "speed_mod":
            self._edit_speed_mod(item, mv, current_val); return
        if col_name == "attack_property":
            self._edit_attack_property(item, mv, current_val); return
        if self._field_address_for(mv, col_name):
            self._edit_field_backing_key(col_name, mv)
            return
        self._status_var.set(f"{self._field_label(col_name)} has no exact writable backing address. It is cached/provenance data, not a game value.")

    def _edit_projectile_value(self, col: str, item: str, mv: dict):
        info = FPI.PROJECTILE_FIELD_INFO.get(col)
        if not info:
            self._status_var.set(f"No projectile writer for {col}"); return
        _key, label, typ = info
        current = FPI.format_projectile_value(mv, col)
        text = simpledialog.askstring(f"Edit {label}", f"{label}\nType: {typ}\nCurrent: {current}\n\nNew value:", parent=self.root, initialvalue=str(current or "0"))
        if text is None: return
        try:
            value = float(text) if typ == "f32" else int(text, 0)
            ok = FPI.write_projectile_value(mv, col, value)
        except Exception as exc:
            messagebox.showerror("Invalid value", str(exc), parent=self.root); return
        if not ok:
            messagebox.showerror("Write failed", f"Could not write {label}.", parent=self.root); return
        _dk = (id(mv), col); self._dirty.add(_dk); self._dirty_refs[_dk] = (mv, col); self._refresh_row(item, mv)
        self._status_var.set(f"Wrote {label}")

    def _edit_super_value(self, col: str, item: str, mv: dict):
        info = FSI.super_field_edit_info(col)
        if not info:
            self._status_var.set(f"No super writer for {col}"); return
        _key, label, typ = info
        current = FSI.format_super_value(mv, col)
        text = simpledialog.askstring(f"Edit {label}", f"{label}\nType: {typ}\nCurrent: {current}\n\nNew value:", parent=self.root, initialvalue=str(current or "0"))
        if text is None: return
        try:
            value = float(text) if typ == "f32" else int(text, 0)
            ok = FSI.write_super_value(mv, col, value)
        except Exception as exc:
            messagebox.showerror("Invalid value", str(exc), parent=self.root); return
        if not ok:
            messagebox.showerror("Write failed", f"Could not write {label}.", parent=self.root); return
        _dk = (id(mv), col); self._dirty.add(_dk); self._dirty_refs[_dk] = (mv, col); self._refresh_row(item, mv)
        self._status_var.set(f"Wrote {label}")

    def _ensure_speed_mod(self, mv):
        if mv.get("speed_mod_addr") is not None: return
        move_abs = mv.get("abs")
        if not move_abs: return
        try:
            from tvcgui.platform.dolphin import rbytes
            addr, cur, sig = find_speed_mod_addr(move_abs, rbytes)
        except Exception:
            addr, cur, sig = (None, None, None)
        if addr:
            mv["speed_mod_addr"] = addr; mv["speed_mod"] = cur; mv["speed_mod_sig"] = sig

    def _edit_speed_mod(self, item, mv, current):
        self._ensure_speed_mod(mv)
        addr = mv.get("speed_mod_addr")
        if not addr:
            messagebox.showerror("Speed Modifier", "Signature not found for this move.", parent=self.root); return
        cur = mv.get("speed_mod")
        try: cur = int(cur if cur is not None else str(current).split()[0])
        except Exception: cur = 0
        new = ask_integer_with_help(self.root, title="Edit Speed Modifier", prompt="New speed modifier byte (0-255)", help_text=get_field_help("speed_mod"), initialvalue=cur, minvalue=0, maxvalue=255, address=int(addr))
        if new is None: return
        if write_speed_mod_inline(mv, int(new), U.WRITER_AVAILABLE):
            mv["speed_mod"] = int(new); self.tree.set(item, "speed_mod", U.fmt_speed_mod_ui(new)); self._after_cell_write(item, mv, "speed_mod")
        else:
            messagebox.showerror("Speed Modifier", "Failed to write speed modifier byte.", parent=self.root)

    def _ensure_attack_property(self, mv):
        if mv.get("attack_property_addr") is not None: return
        move_abs = mv.get("abs")
        if not move_abs: return
        try:
            from tvcgui.platform.dolphin import rbytes
            addr, cur, sig = find_attack_property_addr(move_abs, rbytes)
        except Exception:
            addr, cur, sig = (None, None, None)
        if addr:
            mv["attack_property_addr"] = addr; mv["attack_property"] = cur; mv["attack_property_sig"] = sig

    def _edit_attack_property(self, item, mv, current):
        self._ensure_attack_property(mv)
        addr = mv.get("attack_property_addr")
        if not addr:
            messagebox.showerror("Attack Property", "Signature not found for this move.", parent=self.root); return
        cur = mv.get("attack_property")
        if cur is None: cur = parse_attack_property(current) or 0
        text = simpledialog.askstring("Edit Attack Property", f"Current: {fmt_attack_property(cur)}\nAddress: 0x{int(addr):08X}\n\nEnter byte as decimal or 0xHEX:", initialvalue=f"0x{int(cur)&0xFF:02X}", parent=self.root)
        if text is None: return
        try: value = int(text.strip(), 0) & 0xFF
        except Exception:
            messagebox.showerror("Invalid value", "Use decimal or 0xHEX.", parent=self.root); return
        try:
            from tvcgui.platform.dolphin import wbytes
            wbytes(int(addr), bytes([value])); ok = True
        except Exception:
            ok = False
        if not ok:
            messagebox.showerror("Write failed", "Failed to write attack property byte.", parent=self.root); return
        mv["attack_property"] = value; self.tree.set(item, "attack_property", fmt_attack_property(value)); self._after_cell_write(item, mv, "attack_property")

    def _edit_direct_cached_u32(self, key: str, mv: dict):
        addr_key = {"native_property_a": "native_property_a_addr", "native_property_b": "native_property_b_addr", "native_hit_result_raw": "native_hit_result_addr"}.get(key)
        addr = mv.get(addr_key) if addr_key else None
        if not addr:
            self._status_var.set(f"No exact write address for {key}."); return
        old = int(mv.get(key) or 0) & 0xFFFFFFFF
        text = simpledialog.askstring("Edit raw native value", f"{key}\nAddress: 0x{int(addr):08X}\n\nEnter decimal or 0xHEX:", initialvalue=f"0x{old:08X}", parent=self.root)
        if text is None: return
        try: value = int(text.strip(), 0) & 0xFFFFFFFF
        except Exception:
            messagebox.showerror("Invalid value", "Use decimal or 0xHEX.", parent=self.root); return
        if not write_u32_field_inline(mv, addr_key, key, value):
            messagebox.showerror("Write failed", f"Could not write {key}.", parent=self.root); return
        _dk = (id(mv), key); self._dirty.add(_dk); self._dirty_refs[_dk] = (mv, key); self._status_var.set(f"Wrote {key} = 0x{value:08X}")

    # FDCellEditorsMixin callbacks / compatibility.
    def _after_cell_write(self, item, mv, col_name=None):
        if col_name:
            _dk = (id(mv), str(col_name))
            self._dirty.add(_dk); self._dirty_refs[_dk] = (mv, str(col_name))
        self._refresh_row(item, mv)
        if mv is self._selected_move:
            self._refresh_visible_inspector_tab()
        self._status_var.set(f"Wrote {fd_tree.FD_LABELS.get(str(col_name), str(col_name))}")

    def _apply_row_tags(self, item, mv):
        return

    def _set_status_for_item(self, item, mv):
        if isinstance(mv, dict):
            self._status_var.set(f"{self._move_label(mv)}  |  {_fmt_addr(mv.get('abs')) or 'no address'}")

    # --------------------------------------------------------------- Patch save

    def _patch_group_for_col(self, col: str) -> str | None:
        mapping = {
            "startup": "active", "active": "active", "active2": "active2",
            "damage": "damage", "meter": "meter", "hitstun": "hitstun",
            "blockstun": "blockstun", "hitstop": "hitstop",
            "hit_spark": "hit_spark", "stretch_part": "stretch_part",
            "stretch_len": "stretch_len", "stretch_width": "stretch_width",
            "stretch_height": "stretch_height", "stretch_time": "stretch_time",
            "post_link": "post_link", "launch_profile": "launch_profile",
            "kb_unknown": "kb_unknown", "kb_x": "kb_x", "air_kb": "air_kb",
            "speed_mod": "speed_mod", "attack_property": "attack_property",
            "hit_reaction": "hit_reaction", "hit_result_flags": "hit_result_flags",
            "superbg": "superbg", "combo_kb_mod": "combo_kb_mod",
            "proj_dmg": "proj_dmg", "hb_main": "hb", "hb": "hb",
        }
        return mapping.get(str(col))

    def _patch_value(self, mv: dict, group: str):
        if group == "active":
            s, e = mv.get("active_start"), mv.get("active_end")
            return None if s is None or e is None else {"start": int(s), "end": int(e)}
        if group == "active2":
            s, e = mv.get("active2_start"), mv.get("active2_end")
            return None if s is None or e is None else {"start": int(s), "end": int(e)}
        if group == "superbg":
            raw = mv.get("superbg_val")
            return None if raw is None else {"enabled": bool(int(raw) == 0x04), "raw": int(raw) & 0xFF}
        if group == "hb":
            return None if mv.get("hb_r") is None else float(mv.get("hb_r"))
        if group in {"kb_x", "air_kb", "stretch_len", "stretch_width", "stretch_height"}:
            v = mv.get(group)
            return None if v is None else float(v)
        v = mv.get(group)
        return v

    def _patch_addresses(self, mv: dict) -> dict:
        keys = (
            "abs", "damage_addr", "meter_addr", "active_addr", "active2_addr",
            "stun_addr", "knockback_addr", "speed_mod_addr", "attack_property_addr",
            "hit_reaction_addr", "hit_result_addr", "superbg_addr", "combo_kb_mod_addr",
            "hit_spark_addr", "stretch_part_addr", "stretch_len_addr", "stretch_width_addr",
            "stretch_height_addr", "stretch_time_addr", "post_link_addr", "proj_tpl", "hb_off",
        )
        out = {}
        base = mv.get("abs")
        for key in keys:
            value = mv.get(key)
            if value in (None, ""):
                continue
            if key == "hb_off":
                try: out[key] = int(value)
                except Exception: out[key] = str(value)
                continue
            try:
                iv = int(value)
                out[key] = f"0x{iv:08X}"
                if base and key != "abs":
                    out[f"{key}_rel"] = iv - int(base)
            except Exception:
                out[key] = str(value)
        return out

    def _save_patch(self):
        if not self._dirty_refs:
            self._status_var.set("No semantic frame-data edits to save yet.")
            return
        entries = []
        seen = set()
        skipped = 0
        for _dk, (mv, col) in list(self._dirty_refs.items()):
            group = self._patch_group_for_col(col)
            if not group:
                skipped += 1
                continue
            dedupe = (id(mv), group)
            if dedupe in seen:
                continue
            seen.add(dedupe)
            value = self._patch_value(mv, group)
            if value is None:
                continue
            entries.append({
                "group": group,
                "value": value,
                "original": None,
                "display": {},
                "selector": {
                    "character": str(self.target_slot.get("char_name") or self.slot_label),
                    "move_label": self._move_label(mv),
                    "move_id": mv.get("id"),
                    "current_move_id": mv.get("id"),
                    "kind": mv.get("kind"),
                    "segment_index": mv.get("_hit_segment_index"),
                    "tier": mv.get("dup_index"),
                    "scan_index": mv.get("_scan_index"),
                    "abs": _fmt_addr(mv.get("abs")) or None,
                    "projectile": bool(FPI.is_projectile_row(mv)),
                },
                "addresses": self._patch_addresses(mv),
            })
        if not entries:
            self._status_var.set("The current edits are raw/research writes and are not supported by the normal patch runtime. They remain live in this session.")
            return
        default_dir = FDPatchRuntime.patch_default_dir()
        os.makedirs(default_dir, exist_ok=True)
        char = str(self.target_slot.get("char_name") or "character")
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Save frame-data patch",
            initialdir=default_dir,
            initialfile=f"{char}_frame_patch.json",
            defaultextension=".json",
            filetypes=[("JSON patch", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        now = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        key = str(self.target_slot.get("char_name") or self.slot_label or "Unknown")
        doc = {
            "schema": FDPatchRuntime.SCHEMA,
            "title": f"{key} frame-data patch",
            "created_by": "TvC Continuo Lean Frame Data Workbench",
            "created_at": now,
            "updated_at": now,
            "characters": {
                key: {
                    "character": key,
                    "slot_label": self.slot_label,
                    "change_count": len(entries),
                    "changes": entries,
                }
            },
        }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
        except Exception as exc:
            messagebox.showerror("Save patch", str(exc), parent=self.root)
            return
        msg = f"Saved {len(entries)} semantic change(s) to {os.path.basename(path)}"
        if skipped:
            msg += f". {skipped} raw/research write(s) are session-only."
        self._status_var.set(msg)

    # ------------------------------------------------------------- Misc actions

    def _show_bones(self):
        if BoneScanner is None:
            messagebox.showerror("Bones", "Bone scanner is unavailable in this build.", parent=self.root)
            return
        try:
            base = int(self.target_slot.get("fighter_base_abs") or self.target_slot.get("fighter_base") or 0)
        except Exception:
            base = 0
        if not base:
            messagebox.showerror("Bones", "This cached slot has no fighter_base. Open the window from a live fighter slot.", parent=self.root)
            return
        win = tk.Toplevel(self.root)
        try: apply_titlebar_icon(win, self.root)
        except Exception: pass
        win.title(f"Bones: {self.slot_label} @ 0x{base:08X}")
        win.geometry("1040x560")
        top = ttk.Frame(win, padding=8); top.pack(fill="x")
        start_var = tk.StringVar(value="0x3000")
        len_var = tk.StringVar(value="0x5000")
        budget_var = tk.StringVar(value="96")
        status_var = tk.StringVar(value="Explicit live scan. Close this window when finished.")
        ttk.Label(top, text="Start +").pack(side="left")
        ttk.Entry(top, textvariable=start_var, width=10).pack(side="left", padx=(4, 12))
        ttk.Label(top, text="Length").pack(side="left")
        ttk.Entry(top, textvariable=len_var, width=10).pack(side="left", padx=(4, 12))
        ttk.Label(top, text="Budget").pack(side="left")
        ttk.Entry(top, textvariable=budget_var, width=6).pack(side="left", padx=(4, 12))
        tree = ttk.Treeview(win, columns=("addr", "floats", "zeros", "changes", "patterns", "score", "sample"), show="headings")
        for col, label, width in (("addr","Address",120),("floats","Floats",70),("zeros","Zeros",70),("changes","Changes",75),("patterns","Patterns",75),("score","Score",75),("sample","Sample",480)):
            tree.heading(col, text=label); tree.column(col, width=width, anchor="w" if col=="sample" else "center", stretch=(col=="sample"))
        tree.pack(fill="both", expand=True, padx=8, pady=(0,6))
        ttk.Label(win, textvariable=status_var, anchor="w").pack(fill="x", padx=8, pady=(0,8))
        state = {"scanner": None, "running": True, "signature": None}
        def parse(token, default):
            try: return int(str(token).strip(), 0)
            except Exception: return default
        def rebuild():
            start = parse(start_var.get(), 0x3000); length = parse(len_var.get(), 0x5000)
            try:
                state["scanner"] = BoneScanner(base, start_off=start, scan_len=length, max_results=256)
                state["running"] = True
                status_var.set(f"Scanning 0x{base+start:08X}..0x{base+start+length:08X}")
            except Exception as exc:
                messagebox.showerror("Bones", str(exc), parent=win)
        def toggle():
            state["running"] = not state["running"]
            pause_btn.configure(text="Pause" if state["running"] else "Resume")
        ttk.Button(top, text="Apply range", command=rebuild).pack(side="left", padx=(0,6))
        pause_btn = ttk.Button(top, text="Pause", command=toggle); pause_btn.pack(side="left")
        def edit_selected(_evt=None):
            sel = tree.selection()
            if not sel: return
            try: addr = int(tree.set(sel[0], "addr"), 16)
            except Exception: return
            try:
                from tvcgui.platform.dolphin import rbytes, wbytes
                raw = rbytes(addr, 0x20) or b""
                if len(raw) < 0x20: raise RuntimeError("read failed")
            except Exception as exc:
                messagebox.showerror("Bones", str(exc), parent=win); return
            vals = [struct.unpack(">f", raw[i:i+4])[0] for i in range(0, 0x20, 4)]
            text = simpledialog.askstring("Edit bone block", "Eight f32 values, comma-separated:\n" + ", ".join(f"{v:g}" for v in vals), parent=win, initialvalue=", ".join(f"{v:g}" for v in vals))
            if text is None: return
            try:
                new_vals = [float(x.strip()) for x in text.split(",")]
                if len(new_vals) != 8: raise ValueError("Enter exactly 8 floats")
                payload = b"".join(struct.pack(">f", v) for v in new_vals)
                wbytes(addr, payload)
                status_var.set(f"Wrote 8 floats @ 0x{addr:08X}")
            except Exception as exc:
                messagebox.showerror("Bones", str(exc), parent=win)
        tree.bind("<Double-1>", edit_selected)
        def tick():
            if not win.winfo_exists(): return
            scanner = state.get("scanner")
            if state.get("running") and scanner is not None:
                try: scanner.step(budget_blocks=max(16, min(512, parse(budget_var.get(), 96))))
                except Exception: pass
                rows = list(getattr(scanner, "results", []) or [])[:96]
                sig = tuple((r.addr, round(r.score,2), r.change_count) for r in rows)
                if sig != state.get("signature"):
                    state["signature"] = sig
                    tree.delete(*tree.get_children(""))
                    for i, r in enumerate(rows):
                        sample = ", ".join(f"{v:.3g}" for v in (r.sample or ()))
                        tree.insert("", "end", iid=f"b{i}", values=(f"0x{r.addr:08X}", r.float_count, r.zero_count, r.change_count, r.pattern_hits, f"{r.score:.2f}", sample))
            win.after(100, tick)
        rebuild(); tick()

    def _copy_selected_address(self):
        mv = self._selected_move
        if not isinstance(mv, dict) or not mv.get("abs"):
            self._status_var.set("Select a move with a native address first.")
            return
        text = _fmt_addr(mv.get("abs"))
        self.root.clipboard_clear(); self.root.clipboard_append(text)
        self._status_var.set(f"Copied {text}")

    def _refresh_selected_display(self):
        """Refresh only UI formatting from the current cached dictionary.

        This is deliberately not a scanner button. Expensive re-probing stays
        outside normal workbench use so the editor cannot become hungry again.
        """
        mv, item = self._selected_move, self._selected_item
        if isinstance(mv, dict) and item:
            self._refresh_row(item, mv)
            self._refresh_visible_inspector_tab()
            self._status_var.set("Refreshed selected row from cache. No scan was run.")
        else:
            self._status_var.set("Select a move first.")

    def _on_close(self):
        self._closing = True
        try:
            if self._search_after:
                self.root.after_cancel(self._search_after)
        except Exception:
            pass
        try: self.root.destroy()
        except Exception: pass


def open_editable_frame_data_window(slot_label, scan_data):
    if not scan_data:
        return
    target = None
    for slot in scan_data:
        if isinstance(slot, dict) and slot.get("slot_label") == slot_label:
            target = slot; break
    if not target:
        return
    # Preserve observed timing enrichment, but only for the requested fighter.
    # The old entry point walked every slot before even deciding which window
    # to open.
    try:
        apply_observations_to_scan_data([target])
    except Exception:
        pass
    # Runtime protection capture is a small JSON overlay and restores the exact
    # invulnerability / guard / counter / armor / Baroque windows without
    # starting any live poller or scanner.
    if apply_runtime_protection_observations is not None:
        try:
            char_id = int(target.get("char_id") or target.get("id") or target.get("character_id") or 0)
        except Exception:
            char_id = 0
        if char_id > 0:
            try:
                apply_runtime_protection_observations(target.get("moves", []) or [], char_id)
            except Exception:
                pass

    def create(master_root):
        LeanFrameDataWindow(master_root, slot_label, target)

    tk_call(create)

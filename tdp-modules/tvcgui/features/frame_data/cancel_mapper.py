"""Classify candidate cancel routes using scanned evidence and a limited model.

The modeled normal ladder is a diagnostic aid. Character-specific state,
timing, and command requirements still need independent verification.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


# This ordering is the current mapper model, not a decoded universal game rule.
GROUND_NORMAL_LADDER: Tuple[str, ...] = (
    "5A",
    "2A",
    "5B",
    "2B",
    "5C",
    "2C",
)
GROUND_NORMAL_RANK = {name: index for index, name in enumerate(GROUND_NORMAL_LADDER)}
TERMINAL_COMMAND_NORMALS = {"3C", "6B"}
NORMAL_EXCEPTIONS = {
    "5C": {"3C"},
}

# These IDs are shared by the standard TvC action layout and are a more
# reliable notation source than scanner labels such as "Second" or helper rows.
ACTION_NOTATION = {
    0x0100: "5A",
    0x0101: "5B",
    0x0102: "5C",
    0x0103: "2A",
    0x0104: "2B",
    0x0105: "2C",
    0x0106: "6C",
    0x0108: "3C",
    0x0109: "j.A",
    0x010A: "j.B",
    0x010B: "j.C",
    0x010E: "6B",
}


_STATUS_ORDER = {"ALLOWED": 0, "ELIGIBLE": 1, "BLOCKED": 2, "UNKNOWN": 3}


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except Exception:
        return None


def display_name(move: Dict[str, Any], char_name: str = "") -> str:
    for key in ("move_name", "pretty_name", "name", "label", "family_group_label", "family_label"):
        value = str(move.get(key) or "").strip()
        if value:
            return value
    aid = _as_int(move.get("id"))
    if aid is not None:
        notation = ACTION_NOTATION.get(aid)
        if notation:
            return notation
        return f"Action 0x{aid:04X}"
    address = _as_int(move.get("abs"))
    return f"Move 0x{address:08X}" if address else "Unknown move"


def _name_text(move: Dict[str, Any]) -> str:
    values = []
    for key in (
        "move_name",
        "pretty_name",
        "name",
        "label",
        "family_group_label",
        "family_label",
        "family_link_label",
    ):
        value = move.get(key)
        if value:
            values.append(str(value))
    return " ".join(values)


def notation_for_move(move: Dict[str, Any]) -> str:
    aid = _as_int(move.get("id"))
    if aid in ACTION_NOTATION:
        return ACTION_NOTATION[aid]

    text = _name_text(move)
    low = text.lower()
    compact = re.sub(r"[\s_.\-]+", "", low)

    air_match = re.search(r"(?:^|[^a-z0-9])j[.\s_-]*([abc])(?:$|[^a-z0-9])", low)
    if air_match:
        return f"j.{air_match.group(1).upper()}"

    ground_match = re.search(r"(?:^|[^0-9])([23456])\s*[.\s_-]*([abc])(?:$|[^a-z0-9])", low)
    if ground_match:
        return f"{ground_match.group(1)}{ground_match.group(2).upper()}"

    for token in ("5a", "2a", "5b", "2b", "5c", "2c", "3c", "6b", "6c", "4c"):
        if compact.startswith(token):
            return token.upper()
    return ""


def move_kind(move: Dict[str, Any]) -> str:
    kind = str(move.get("kind") or "").strip().lower()
    text = _name_text(move).lower()
    aid = _as_int(move.get("id"))

    # Standard normal IDs win over noisy helper labels and scanner kinds.
    if aid in ACTION_NOTATION:
        return "normal"
    if "throw" in text or "thrown" in text or "assist" in text or "taunt" in text:
        return "other"
    if kind in {"super", "hyper"} or any(token in text for token in (" super", "hyper", "shinku", "shin sho", "shin shoryu")):
        return "super"
    if kind == "special":
        return "special"
    if aid is not None and 0x0160 <= aid < 0x0180:
        return "super"
    if aid is not None and 0x0130 <= aid < 0x0160:
        return "special"
    if notation_for_move(move):
        return "normal"
    if any(token in text for token in ("hado", "tatsu", "shoryu", "denko", "bird run", "bird shoot", "eagle rush")):
        return "special"
    return "other"


def _candidate_score(move: Dict[str, Any]) -> Tuple[int, int, int, int]:
    source = str(move.get("move_name_source") or "").strip().lower()
    named = 0 if source == "lookup" else 1
    kind = move_kind(move)
    kind_score = {"normal": 0, "special": 1, "super": 2}.get(kind, 9)
    scan_index = _as_int(move.get("_scan_index")) or 0
    address = _as_int(move.get("abs")) or 0xFFFFFFFF
    return named, kind_score, scan_index, address


def canonical_moves(moves: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Return one user-facing row per action ID for the mapper.

    The normal scanner can emit many helper and duplicate rows. The mapper is
    about selectable actions, so only named normal, special, and super roots are
    retained, with the best row chosen for each action ID.
    """
    chosen: Dict[Tuple[Any, ...], Dict[str, Any]] = {}
    for move in moves or []:
        if not isinstance(move, dict):
            continue
        kind = move_kind(move)
        if kind not in {"normal", "special", "super"}:
            continue
        aid = _as_int(move.get("id"))
        address = _as_int(move.get("abs"))
        if aid is None or not address:
            continue

        name = display_name(move)
        low_name = name.lower()
        notation = notation_for_move(move)
        is_named = bool(notation) or (
            "anim_" not in low_name
            and "filler" not in low_name
            and "unknown" not in low_name
            and not low_name.startswith("action 0x")
        )
        if not is_named:
            continue
        if any(token in low_name for token in ("(second)", " tier", "hit 1", "hit 2", "hit 3", "projectile definitions", "action graph")):
            continue
        # Named command roots normally live in the standard normal block or in
        # the 0x130+ special/super command ranges. Low-ID special rows are
        # usually helper actions and would make the mapper unreadable.
        if kind in {"special", "super"} and aid < 0x0130:
            continue

        key = (aid,)
        current = chosen.get(key)
        if current is None or _candidate_score(move) < _candidate_score(current):
            chosen[key] = move

    return sorted(
        chosen.values(),
        key=lambda move: (
            {"normal": 0, "special": 1, "super": 2}.get(move_kind(move), 9),
            GROUND_NORMAL_RANK.get(notation_for_move(move), 100),
            _as_int(move.get("id")) or 0xFFFF,
            _as_int(move.get("abs")) or 0xFFFFFFFF,
        ),
    )


def direct_route_targets(source: Dict[str, Any]) -> Dict[int, Dict[str, Any]]:
    targets: Dict[int, Dict[str, Any]] = {}
    for probe in source.get("cancel_windows") or []:
        if str(probe.get("kind") or "").strip().lower() != "normal":
            continue
        target_id = _as_int(probe.get("target_id"))
        if target_id is None:
            continue
        existing = targets.get(target_id)
        if existing is None:
            targets[target_id] = probe
            continue
        old_addr = _as_int(existing.get("addr")) or 0xFFFFFFFF
        new_addr = _as_int(probe.get("addr")) or 0xFFFFFFFF
        if new_addr < old_addr:
            targets[target_id] = probe
    return targets


def classify_cancel(source: Dict[str, Any], target: Dict[str, Any]) -> Dict[str, Any]:
    source_id = _as_int(source.get("id"))
    target_id = _as_int(target.get("id"))
    source_notation = notation_for_move(source)
    target_notation = notation_for_move(target)
    source_kind = move_kind(source)
    target_kind = move_kind(target)
    routes = direct_route_targets(source)
    route = routes.get(target_id) if target_id is not None else None

    base = {
        "status": "UNKNOWN",
        "reason": "Current mapper does not model this source move yet.",
        "evidence_addr": None,
        "branch_addr": None,
        "source_notation": source_notation,
        "target_notation": target_notation,
        "source_kind": source_kind,
        "target_kind": target_kind,
    }

    if source_id is not None and target_id == source_id:
        base.update(status="BLOCKED", reason="Same action, chain rules require a later destination.")
        return base

    if route is not None:
        base.update(
            status="ALLOWED",
            reason="Direct command-normal route found in the source move script.",
            evidence_addr=_as_int(route.get("addr")),
            branch_addr=_as_int(route.get("branch_addr")),
        )
        return base

    modeled_source = source_notation in GROUND_NORMAL_RANK or source_notation in TERMINAL_COMMAND_NORMALS
    if not modeled_source or source_kind != "normal":
        return base

    if target_kind == "special":
        base.update(
            status="ELIGIBLE",
            reason=(
                "Special-cancel category is available, but this target's ground/air, "
                "command, and character-state requirements are not decoded yet."
            ),
        )
        return base
    if target_kind == "super":
        base.update(
            status="ELIGIBLE",
            reason=(
                "Super-cancel category is available, but this target's ground/air, "
                "command, meter, and character-state requirements are not decoded yet."
            ),
        )
        return base
    if target_kind != "normal":
        base.update(status="UNKNOWN", reason="Destination is outside the current normal, special, and super model.")
        return base

    if source_notation in TERMINAL_COMMAND_NORMALS:
        base.update(status="BLOCKED", reason=f"{source_notation} is terminal for normal chains in the current model.")
        return base

    source_rank = GROUND_NORMAL_RANK.get(source_notation)
    target_rank = GROUND_NORMAL_RANK.get(target_notation)
    if source_rank is not None and target_rank is not None:
        if target_rank > source_rank:
            base.update(
                status="ALLOWED",
                reason=f"Ground chain rank advances from {source_notation} to {target_notation}.",
            )
        else:
            base.update(
                status="BLOCKED",
                reason=f"Ground chain rank does not advance from {source_notation} to {target_notation}.",
            )
        return base

    if target_notation in NORMAL_EXCEPTIONS.get(source_notation, set()):
        base.update(
            status="ALLOWED",
            reason=f"Known command-normal exception: {source_notation} may route to {target_notation}.",
        )
        return base

    base.update(
        status="BLOCKED",
        reason="No normal-chain rank step or direct command route exists in the current model.",
    )
    return base


def build_cancel_rows(
    source: Dict[str, Any],
    moves: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for target in canonical_moves(moves):
        result = classify_cancel(source, target)
        result.update(
            target=target,
            target_id=_as_int(target.get("id")),
            target_addr=_as_int(target.get("abs")),
            target_name=display_name(target),
        )
        rows.append(result)
    return sorted(
        rows,
        key=lambda row: (
            _STATUS_ORDER.get(str(row.get("status") or "UNKNOWN"), 9),
            {"normal": 0, "special": 1, "super": 2}.get(str(row.get("target_kind") or ""), 9),
            GROUND_NORMAL_RANK.get(str(row.get("target_notation") or ""), 100),
            row.get("target_id") if row.get("target_id") is not None else 0xFFFF,
            row.get("target_addr") if row.get("target_addr") is not None else 0xFFFFFFFF,
        ),
    )


# ---------------------------------------------------------------------------
# Standalone Cancel Mapper window
# ---------------------------------------------------------------------------

_ACTIVE_STANDALONE_MAPPER = None


def _slot_label(row: Dict[str, Any], fallback: str = "P1-C1") -> str:
    value = str(row.get("slot_label") or row.get("slot") or fallback).strip().upper()
    return value or fallback


def open_standalone_cancel_mapper(
    parent,
    slot_rows: Sequence[Dict[str, Any]],
    *,
    initial_slot: str | None = None,
    initial_source: Dict[str, Any] | None = None,
    status_callback: Callable[[str], None] | None = None,
    profile_refresh_callback: Callable[[int | None], None] | None = None,
):
    """Open Cancel Mapper without requiring the Frame Data workbench.

    The main pygame GUI passes its current rich frame-data rows here. The
    window can switch between every supplied live slot and inspect the current
    cancel model without opening or controlling the separate Cancel Lab.
    """
    global _ACTIVE_STANDALONE_MAPPER

    import tkinter as tk
    from tkinter import messagebox, ttk

    from .widgets import apply_titlebar_icon
    from . import tree as fd_tree

    rows: list[Dict[str, Any]] = []
    seen_slots: set[str] = set()
    for raw in slot_rows or []:
        if not isinstance(raw, dict):
            continue
        slot = _slot_label(raw)
        moves = canonical_moves(list(raw.get("moves") or []))
        if not moves or slot in seen_slots:
            continue
        row = dict(raw)
        row["slot_label"] = slot
        row["moves"] = list(raw.get("moves") or [])
        rows.append(row)
        seen_slots.add(slot)

    if not rows:
        messagebox.showinfo(
            "Cancel Mapper",
            "No rich character profile is ready yet. Build or load a Frame Data profile, then open Cancel Mapper again.",
            parent=parent,
        )
        return None

    try:
        old = _ACTIVE_STANDALONE_MAPPER
        if old is not None and bool(old.winfo_exists()):
            old.destroy()
    except Exception:
        pass

    dlg = tk.Toplevel(parent)
    _ACTIVE_STANDALONE_MAPPER = dlg
    fd_tree.configure_styles(dlg)
    apply_titlebar_icon(dlg, parent)
    dlg.title("Cancel Mapper")
    dlg.geometry("1240x760")
    dlg.minsize(940, 560)

    slot_by_label: dict[str, Dict[str, Any]] = {}
    slot_labels: list[str] = []
    for row in rows:
        slot = _slot_label(row)
        char_name = str(row.get("char_name") or row.get("name") or "Unknown")
        label = f"{slot} | {char_name}"
        slot_by_label[label] = row
        slot_labels.append(label)

    initial_slot_text = str(initial_slot or "").upper()
    selected_slot_label = next(
        (
            label
            for label, row in slot_by_label.items()
            if _slot_label(row).upper() == initial_slot_text
        ),
        slot_labels[0],
    )

    shell = ttk.Frame(dlg, style="FD.TFrame", padding=(12, 12))
    shell.pack(fill="both", expand=True)

    hero = ttk.Frame(shell, style="Hero.TFrame", padding=(14, 12))
    hero.pack(fill="x", pady=(0, 10))
    ttk.Label(hero, text="CANCEL MAPPER", style="HeroTitle.TLabel").pack(anchor="w")
    ttk.Label(
        hero,
        text=(
            "Browse stock cancel relationships for the live character. "
            "Allowed is a modeled stock route. Eligible means the category is available but execution conditions are not fully decoded."
        ),
        style="HeroSub.TLabel",
        wraplength=1160,
        justify="left",
    ).pack(anchor="w", pady=(3, 0))

    controls = ttk.Frame(shell, style="Card.TFrame", padding=(10, 8))
    controls.pack(fill="x", pady=(0, 10))
    controls.grid_columnconfigure(1, weight=1)
    controls.grid_columnconfigure(3, weight=2)

    slot_var = tk.StringVar(master=dlg, value=selected_slot_label)
    source_var = tk.StringVar(master=dlg, value="")
    filter_var = tk.StringVar(master=dlg, value="ALL")
    search_var = tk.StringVar(master=dlg, value="")
    summary_var = tk.StringVar(master=dlg, value="")
    source_summary_var = tk.StringVar(master=dlg, value="")

    ttk.Label(controls, text="Fighter", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
    slot_combo = ttk.Combobox(
        controls,
        textvariable=slot_var,
        values=slot_labels,
        state="readonly",
        width=28,
    )
    slot_combo.grid(row=0, column=1, sticky="ew", padx=(0, 14))

    ttk.Label(controls, text="Source move", style="Card.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
    source_combo = ttk.Combobox(controls, textvariable=source_var, state="readonly", width=58)
    source_combo.grid(row=0, column=3, sticky="ew")

    action_bar = ttk.Frame(shell, style="FD.TFrame")
    action_bar.pack(fill="x", pady=(0, 8))

    source_line = ttk.Frame(shell, style="Card.TFrame", padding=(10, 7))
    source_line.pack(fill="x", pady=(0, 8))
    ttk.Label(source_line, textvariable=source_summary_var, style="Card.TLabel").pack(side="left", fill="x", expand=True)

    columns = ("status", "target", "kind", "action", "rule", "address", "evidence")
    table_frame = ttk.Frame(shell, style="FD.TFrame")
    table_frame.pack(fill="both", expand=True)
    mapper_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
    yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=mapper_tree.yview)
    xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=mapper_tree.xview)
    mapper_tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
    mapper_tree.grid(row=0, column=0, sticky="nsew")
    yscroll.grid(row=0, column=1, sticky="ns")
    xscroll.grid(row=1, column=0, sticky="ew")
    table_frame.grid_rowconfigure(0, weight=1)
    table_frame.grid_columnconfigure(0, weight=1)

    headings = {
        "status": "Status",
        "target": "Target move",
        "kind": "Kind",
        "action": "Action ID",
        "rule": "Current rule",
        "address": "Target address",
        "evidence": "Route evidence",
    }
    widths = {
        "status": 90,
        "target": 205,
        "kind": 80,
        "action": 90,
        "rule": 440,
        "address": 115,
        "evidence": 160,
    }
    for column in columns:
        mapper_tree.heading(column, text=headings[column])
        mapper_tree.column(column, width=widths[column], minwidth=60, stretch=(column in {"target", "rule"}))

    mapper_tree.tag_configure("allowed", foreground="#91E5B0")
    mapper_tree.tag_configure("eligible", foreground="#8FD5FF")
    mapper_tree.tag_configure("blocked", foreground="#FFB0B7")
    mapper_tree.tag_configure("unknown", foreground="#B8C6D8")

    row_payloads: dict[str, Dict[str, Any]] = {}
    source_by_label: dict[str, Dict[str, Any]] = {}
    current_moves: list[Dict[str, Any]] = []

    def announce(text: str) -> None:
        if callable(status_callback):
            try:
                status_callback(str(text))
            except Exception:
                pass

    def current_slot_row() -> Dict[str, Any]:
        return slot_by_label.get(slot_var.get()) or rows[0]

    def current_source() -> Dict[str, Any] | None:
        value = source_by_label.get(source_var.get())
        return value if isinstance(value, dict) else None

    def copy_value(value: Any, description: str) -> None:
        try:
            text = str(value)
            dlg.clipboard_clear()
            dlg.clipboard_append(text)
            dlg.update_idletasks()
            announce(f"Copied {description}: {text}")
        except Exception:
            pass

    def rebuild_sources(*_args) -> None:
        nonlocal current_moves
        row = current_slot_row()
        current_moves = list(row.get("moves") or [])
        candidates = canonical_moves(current_moves)
        source_by_label.clear()
        labels: list[str] = []
        for move in candidates:
            name = display_name(move, str(row.get("char_name") or ""))
            action_id = _as_int(move.get("id"))
            address = _as_int(move.get("abs"))
            aid_text = f"0x{action_id:04X}" if action_id is not None else "no ID"
            addr_text = f"0x{address:08X}" if address else "no address"
            label = f"{name} [{aid_text}] @ {addr_text}"
            if label in source_by_label:
                label = f"{label} #{len(labels) + 1}"
            source_by_label[label] = move
            labels.append(label)

        source_combo.configure(values=labels)
        preferred = ""
        if isinstance(initial_source, dict):
            preferred_id = _as_int(initial_source.get("id"))
            preferred = next(
                (
                    label
                    for label, move in source_by_label.items()
                    if _as_int(move.get("id")) == preferred_id
                ),
                "",
            )
        if source_var.get() not in source_by_label:
            source_var.set(preferred or (labels[0] if labels else ""))
        refresh_rows()

    def set_filter(value: str) -> None:
        filter_var.set(value)
        refresh_rows()

    for label, value in (
        ("All", "ALL"),
        ("Allowed", "ALLOWED"),
        ("Eligible", "ELIGIBLE"),
        ("Blocked", "BLOCKED"),
        ("Unknown", "UNKNOWN"),
    ):
        ttk.Button(action_bar, text=label, command=lambda v=value: set_filter(v)).pack(side="left", padx=(0, 5))

    ttk.Label(action_bar, text="Search", style="Top.TLabel").pack(side="left", padx=(14, 6))
    ttk.Entry(action_bar, textvariable=search_var, width=30).pack(side="left")
    ttk.Label(action_bar, textvariable=summary_var, style="Muted.Top.TLabel").pack(side="right")

    def refresh_rows(*_args) -> None:
        source = current_source()
        row = current_slot_row()
        for item in mapper_tree.get_children(""):
            mapper_tree.delete(item)
        row_payloads.clear()
        if not isinstance(source, dict):
            source_summary_var.set("No mapped source move is available for this profile.")
            summary_var.set("")
            return

        try:
            source_name = display_name(source, str(row.get("char_name") or ""))
            source_id = int(source.get("id"))
            source_addr = int(source.get("abs"))
            source_note = notation_for_move(source) or move_kind(source).title()
            source_summary_var.set(
                f"{_slot_label(row)} | {row.get('char_name') or 'Unknown'} | "
                f"Source: {source_name} [0x{source_id:04X}] at 0x{source_addr:08X} | Model class: {source_note}"
            )
            dlg.title(f"Cancel Mapper: {source_name}")
        except Exception:
            source_summary_var.set("Source move information is incomplete.")

        wanted = filter_var.get().strip().upper()
        needle = search_var.get().strip().lower()
        totals = {"ALLOWED": 0, "ELIGIBLE": 0, "BLOCKED": 0, "UNKNOWN": 0}
        for result in build_cancel_rows(source, current_moves):
            status = str(result.get("status") or "UNKNOWN").upper()
            totals[status] = totals.get(status, 0) + 1
            searchable = " ".join(
                str(result.get(key) or "")
                for key in ("target_name", "target_kind", "target_notation", "reason")
            ).lower()
            if wanted != "ALL" and status != wanted:
                continue
            if needle and needle not in searchable:
                continue

            target_id = result.get("target_id")
            target_addr = result.get("target_addr")
            evidence_addr = result.get("evidence_addr")
            branch_addr = result.get("branch_addr")
            evidence_parts: list[str] = []
            if evidence_addr:
                evidence_parts.append(f"test 0x{int(evidence_addr):08X}")
            if branch_addr:
                evidence_parts.append(f"branch 0x{int(branch_addr):08X}")
            iid = mapper_tree.insert(
                "",
                "end",
                values=(
                    status,
                    result.get("target_name") or "Unknown",
                    str(result.get("target_kind") or "other").title(),
                    f"0x{int(target_id):04X}" if target_id is not None else "",
                    result.get("reason") or "",
                    f"0x{int(target_addr):08X}" if target_addr else "",
                    ", ".join(evidence_parts) if evidence_parts else (
                        "category model" if status == "ELIGIBLE" else "rule model"
                    ),
                ),
                tags=(status.lower(),),
            )
            row_payloads[iid] = result

        summary_var.set(
            f"Allowed {totals.get('ALLOWED', 0)} | Eligible {totals.get('ELIGIBLE', 0)} | "
            f"Blocked {totals.get('BLOCKED', 0)} | Unknown {totals.get('UNKNOWN', 0)}"
        )

    def show_context_menu(event) -> None:
        item = mapper_tree.identify_row(event.y)
        if not item:
            return
        mapper_tree.selection_set(item)
        result = row_payloads.get(item)
        source = current_source()
        if not result or not isinstance(source, dict):
            return
        menu = tk.Menu(dlg, tearoff=0)
        target_addr = result.get("target_addr")
        evidence_addr = result.get("evidence_addr")
        branch_addr = result.get("branch_addr")
        target_id = result.get("target_id")
        source_addr = source.get("abs")
        if target_addr:
            menu.add_command(
                label=f"Copy Target Address (0x{int(target_addr):08X})",
                command=lambda value=f"0x{int(target_addr):08X}": copy_value(value, "target address"),
            )
        if evidence_addr:
            menu.add_command(
                label=f"Copy Route Test Address (0x{int(evidence_addr):08X})",
                command=lambda value=f"0x{int(evidence_addr):08X}": copy_value(value, "route test address"),
            )
        if branch_addr:
            menu.add_command(
                label=f"Copy Route Branch Address (0x{int(branch_addr):08X})",
                command=lambda value=f"0x{int(branch_addr):08X}": copy_value(value, "route branch address"),
            )
        if target_id is not None:
            menu.add_command(
                label=f"Copy Target Action ID (0x{int(target_id):04X})",
                command=lambda value=f"0x{int(target_id):04X}": copy_value(value, "target action ID"),
            )
        if source_addr:
            menu.add_separator()
            menu.add_command(
                label=f"Copy Source Address (0x{int(source_addr):08X})",
                command=lambda value=f"0x{int(source_addr):08X}": copy_value(value, "source address"),
            )
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def copy_selected_target(_event=None):
        selection = mapper_tree.selection()
        if not selection:
            return "break"
        result = row_payloads.get(selection[0])
        if result and result.get("target_addr"):
            copy_value(f"0x{int(result['target_addr']):08X}", "target address")
        return "break"

    slot_combo.bind("<<ComboboxSelected>>", rebuild_sources)
    source_combo.bind("<<ComboboxSelected>>", refresh_rows)
    search_var.trace_add("write", refresh_rows)
    mapper_tree.bind("<Button-3>", show_context_menu)
    mapper_tree.bind("<Double-Button-1>", copy_selected_target)
    mapper_tree.bind("<Return>", copy_selected_target)
    dlg.bind("<Escape>", lambda _event: dlg.destroy())

    bottom = ttk.Frame(shell, style="FD.TFrame")
    bottom.pack(fill="x", pady=(8, 0))
    ttk.Button(
        bottom,
        text="Copy source address",
        command=lambda: copy_value(
            f"0x{int((current_source() or {}).get('abs')):08X}",
            "source address",
        ) if (current_source() or {}).get("abs") else None,
    ).pack(side="left", padx=(6, 0))
    ttk.Label(
        bottom,
        text="Right click a target to copy its action, address, or route evidence.",
        style="Muted.Top.TLabel",
    ).pack(side="right")

    rebuild_sources()
    try:
        source_combo.focus_set()
    except Exception:
        pass
    announce("Opened Cancel Mapper from the main GUI.")
    return dlg

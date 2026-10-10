'Read-only scout for static move-script signals the Frame Data editor does not decode yet.\n\nThis deliberately does *not* name a signal "invulnerability", "armor", or any other\nmechanic.  It inventories structured field-operation packets and lets the operator compare\nwhere they appear before a decoder exists.\n'
from __future__ import annotations

import struct
import threading
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable

import tkinter as tk
from tkinter import ttk

from tvcgui.core.tk_host import tk_call_widget

try:
    from .widgets import apply_titlebar_icon
except Exception:  # pragma: no cover - optional cosmetic integration
    def apply_titlebar_icon(*_args, **_kwargs):
        return None


# The packet shape is empirically stable in the loaded chr_tbl move blocks:
#   04 <sub-op> 60|67 00 <u32 target offset> <type/format u32> <u32 value>
# Recognize the *structure* only.  The sub-op and target semantics remain
# intentionally unmapped until live/static evidence confirms them.
_FIELD_MARKERS = {0x60, 0x67}
_PACKET_SIZE = 16
_DEFAULT_SCAN_LEN = 0x800
_MAX_SCAN_LEN = 0x1800
_MIN_SCAN_LEN = 0x80


@dataclass(frozen=True)
class SignalSignature:
    target_offset: int
    subop: int
    marker: int
    value_type: int
    value_raw: int


@dataclass
class SignalOccurrence:
    move_abs: int
    move_id: int | None
    move_name: str
    packet_abs: int
    signature: SignalSignature
    context: bytes


def _u32_be(buf: bytes, off: int) -> int:
    return int.from_bytes(buf[off:off + 4], "big", signed=False)


def _as_f32(raw: int) -> float | None:
    try:
        value = struct.unpack(">f", int(raw & 0xFFFFFFFF).to_bytes(4, "big"))[0]
    except Exception:
        return None
    # Keep text readable; raw bits are always shown, so don't force bogus floats.
    if value != value or value in (float("inf"), float("-inf")):
        return None
    if abs(value) > 1000000:
        return None
    return value


def _entry_length(move_abs: int, next_abs_map: dict[int, int] | None) -> int:
    """Bound a read to this entry when a next-table address is known."""
    try:
        move_abs = int(move_abs)
    except Exception:
        return _DEFAULT_SCAN_LEN
    nxt = None
    try:
        nxt = (next_abs_map or {}).get(move_abs)
        nxt = int(nxt) if nxt else None
    except Exception:
        nxt = None
    if nxt and nxt > move_abs:
        return max(_MIN_SCAN_LEN, min(_MAX_SCAN_LEN, nxt - move_abs))
    return _DEFAULT_SCAN_LEN


def extract_unmapped_field_ops(
    move: dict,
    rbytes: Callable[[int, int], bytes],
    *,
    next_abs_map: dict[int, int] | None = None,
) -> list[SignalOccurrence]:
    """Return all structured field-op packets from one move entry.

    The scanner does not discard repeated packets: repeated set/clear-like
    structures are often the useful clue.  Grouping happens in the UI layer.
    """
    try:
        move_abs = int(move.get("abs") or 0)
    except Exception:
        move_abs = 0
    if not move_abs:
        return []

    length = _entry_length(move_abs, next_abs_map)
    try:
        buf = rbytes(move_abs, length) or b""
    except Exception:
        return []
    if len(buf) < _PACKET_SIZE:
        return []

    move_name = str(move.get("move_name") or move.get("pretty_name") or move.get("name") or "unnamed")
    try:
        move_id = int(move.get("id")) if move.get("id") is not None else None
    except Exception:
        move_id = None

    out: list[SignalOccurrence] = []
    # Require a fully-contained 16-byte record before decoding it.  This avoids
    # reading the next table entry as a fake operand at the boundary.
    for off in range(0, len(buf) - _PACKET_SIZE + 1):
        if buf[off] != 0x04 or buf[off + 2] not in _FIELD_MARKERS:
            continue
        # The record form uses zero in byte 3.  A non-zero value could be a
        # different grammar; leave it for a future decoder rather than faking
        # an interpretation now.
        if buf[off + 3] != 0x00:
            continue
        target = _u32_be(buf, off + 4)
        # Runtime-relative fields in this game stay comfortably below this. A
        # larger number is overwhelmingly likely to be a pointer/payload, not a
        # fighter-relative field offset.
        # TvC keeps several proven combat-state fields in the +0x44xx/+0x45xx
        # range (Baroque/protection research included), so do not discard them
        # from the raw field-op inventory. 0x5000 is still a conservative
        # fighter-relative ceiling for this scout.
        if target > 0x5000:
            continue
        signature = SignalSignature(
            target_offset=target,
            subop=buf[off + 1],
            marker=buf[off + 2],
            value_type=_u32_be(buf, off + 8),
            value_raw=_u32_be(buf, off + 12),
        )
        ctx_start = max(0, off - 8)
        ctx_end = min(len(buf), off + 24)
        out.append(
            SignalOccurrence(
                move_abs=move_abs,
                move_id=move_id,
                move_name=move_name,
                packet_abs=move_abs + off,
                signature=signature,
                context=bytes(buf[ctx_start:ctx_end]),
            )
        )
    return out


def scan_unmapped_field_ops(
    moves: Iterable[dict],
    rbytes: Callable[[int, int], bytes],
    *,
    next_abs_map: dict[int, int] | None = None,
) -> tuple[list[SignalOccurrence], int]:
    """Scan unique static entries and return occurrences plus entry count."""
    seen_abs: set[int] = set()
    all_rows: list[SignalOccurrence] = []
    scanned = 0
    for move in moves or []:
        try:
            addr = int((move or {}).get("abs") or 0)
        except Exception:
            addr = 0
        if not addr or addr in seen_abs:
            continue
        seen_abs.add(addr)
        scanned += 1
        all_rows.extend(extract_unmapped_field_ops(move, rbytes, next_abs_map=next_abs_map))
    return all_rows, scanned


def _sig_text(sig: SignalSignature) -> tuple[str, str, str, str]:
    target = f"+0x{sig.target_offset:03X}"
    command = f"04 {sig.subop:02X} {sig.marker:02X}"
    value = f"0x{sig.value_raw:08X}"
    f = _as_f32(sig.value_raw)
    typed = f"type 0x{sig.value_type:08X}"
    if f is not None:
        typed += f"  /  f32 {f:g}"
    return target, command, value, typed


def _rarity_label(move_count: int) -> str:
    if move_count <= 1:
        return "unique"
    if move_count <= 3:
        return "rare"
    if move_count <= 12:
        return "shared"
    return "common"


class UnknownSignalScout:
    """A non-blocking read-only static-signal explorer."""

    def __init__(
        self,
        master,
        *,
        moves: list[dict],
        next_abs_map: dict[int, int] | None,
        scope_label: str,
        char_label: str,
    ) -> None:
        self.master = master
        self.moves = list(moves or [])
        self.next_abs_map = dict(next_abs_map or {})
        self.scope_label = scope_label
        self.char_label = char_label
        self._all_occurrences: list[SignalOccurrence] = []
        self._groups: dict[SignalSignature, list[SignalOccurrence]] = {}
        self._tree_groups: dict[str, SignalSignature] = {}
        self._scan_done = False

        self.root = tk.Toplevel(master)
        apply_titlebar_icon(self.root, master)
        self.root.title(f"Unmapped Static Signals  -  {char_label}")
        self.root.geometry("1260x700")
        self.root.minsize(920, 520)
        try:
            self.root.configure(bg="#101722")
        except Exception:
            pass

        self.status_var = tk.StringVar(master=self.root, value="Preparing static scan…")
        self.filter_var = tk.StringVar(master=self.root, value="")
        self.details_var = tk.StringVar(master=self.root, value="Select a signal to inspect its raw packets.")

        self._build()
        self.root.after(80, self._start_scan)

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill="x")
        ttk.Label(top, text="Unmapped static signals", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(
            top,
            text=(
                f"Scope: {self.scope_label}.  This is a read-only inventory of structured 04 xx 60/67 field operations. "
                "A row is evidence, not a mechanic label."
            ),
            wraplength=1120,
            justify="left",
        ).pack(anchor="w", pady=(4, 0))

        toolbar = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="Filter").pack(side="left")
        ent = ttk.Entry(toolbar, textvariable=self.filter_var, width=42)
        ent.pack(side="left", padx=(6, 0))
        ent.bind("<KeyRelease>", lambda _evt: self._populate_tree())
        ttk.Button(toolbar, text="Clear", command=lambda: self.filter_var.set("") or self._populate_tree()).pack(side="left", padx=(6, 0))
        ttk.Button(toolbar, text="Copy selected", command=self._copy_selected).pack(side="right")

        body = ttk.Panedwindow(self.root, orient="vertical")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 8))
        list_frame = ttk.Frame(body)
        detail_frame = ttk.Frame(body)
        body.add(list_frame, weight=4)
        body.add(detail_frame, weight=2)

        cols = ("rarity", "moves", "occ", "target", "command", "value", "type")
        self.tree = ttk.Treeview(list_frame, columns=cols, show="headings", height=18)
        headings = {
            "rarity": "Rarity",
            "moves": "Moves",
            "occ": "Packets",
            "target": "Target",
            "command": "Command",
            "value": "Raw value",
            "type": "Operand",
        }
        widths = {"rarity": 76, "moves": 64, "occ": 70, "target": 92, "command": 110, "value": 112, "type": 280}
        for col in cols:
            self.tree.heading(col, text=headings[col], command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=widths[col], anchor="w")
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        self.tree.bind("<Double-Button-1>", self._show_detail)

        ttk.Label(detail_frame, textvariable=self.details_var).pack(anchor="w", pady=(0, 4))
        detail_holder = ttk.Frame(detail_frame)
        detail_holder.pack(fill="both", expand=True)
        self.detail = tk.Text(
            detail_holder,
            wrap="none",
            height=10,
            font=("Consolas", 10),
            bg="#0f1113",
            fg="#e8e8e8",
            insertbackground="#e8e8e8",
        )
        d_vsb = ttk.Scrollbar(detail_holder, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=d_vsb.set)
        self.detail.pack(side="left", fill="both", expand=True)
        d_vsb.pack(side="right", fill="y")
        self.detail.configure(state="disabled")

        footer = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        footer.pack(fill="x")
        ttk.Label(footer, textvariable=self.status_var).pack(side="left")

    def _start_scan(self) -> None:
        def worker() -> None:
            try:
                from tvcgui.platform.dolphin import rbytes
                rows, scanned = scan_unmapped_field_ops(self.moves, rbytes, next_abs_map=self.next_abs_map)
                tk_call_widget(self.root, lambda: self._finish_scan(rows, scanned, None))
            except Exception as exc:  # Keep errors in UI instead of the console.
                tk_call_widget(self.root, lambda: self._finish_scan([], 0, exc))

        self.status_var.set(f"Scanning {len(self.moves):,} profile rows in the background…")
        threading.Thread(target=worker, name="fd-unknown-scout", daemon=True).start()

    def _finish_scan(self, rows: list[SignalOccurrence], scanned: int, error: Exception | None) -> None:
        if not self.root.winfo_exists():
            return
        if error is not None:
            self.status_var.set(f"Static scan failed: {error}")
            return
        self._all_occurrences = rows
        groups: dict[SignalSignature, list[SignalOccurrence]] = defaultdict(list)
        for row in rows:
            groups[row.signature].append(row)
        self._groups = dict(groups)
        self._scan_done = True
        self._populate_tree()
        self.status_var.set(
            f"Scanned {scanned:,} unique entries. Found {len(rows):,} packets grouped into {len(self._groups):,} unmapped signatures."
        )

    def _sorted_groups(self):
        query = self.filter_var.get().strip().lower()
        rows = []
        for sig, occurrences in self._groups.items():
            move_names = sorted({o.move_name for o in occurrences})
            target, command, value, typed = _sig_text(sig)
            hay = " ".join((target, command, value, typed, *move_names)).lower()
            if query and query not in hay:
                continue
            rows.append((sig, occurrences, move_names, target, command, value, typed))
        rows.sort(key=lambda x: (len({o.move_abs for o in x[1]}), x[0].target_offset, x[0].subop, x[0].value_raw))
        return rows

    def _populate_tree(self) -> None:
        if not self._scan_done:
            return
        self.tree.delete(*self.tree.get_children())
        self._tree_groups.clear()
        for idx, (sig, occurrences, _move_names, target, command, value, typed) in enumerate(self._sorted_groups()):
            move_count = len({o.move_abs for o in occurrences})
            iid = f"sig_{idx}"
            self._tree_groups[iid] = sig
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    _rarity_label(move_count),
                    move_count,
                    len(occurrences),
                    target,
                    command,
                    value,
                    typed,
                ),
            )

    def _sort_by(self, col: str) -> None:
        # The default rarity ordering is the useful exploratory order.  A small
        # deterministic click-sort is enough for ad-hoc inspection.
        data = [(self.tree.set(i, col), i) for i in self.tree.get_children("")]
        try:
            data.sort(key=lambda pair: int(pair[0]))
        except Exception:
            data.sort(key=lambda pair: pair[0].lower())
        for index, (_val, item) in enumerate(data):
            self.tree.move(item, "", index)

    def _selected_signature(self) -> SignalSignature | None:
        sel = self.tree.selection()
        return self._tree_groups.get(sel[0]) if sel else None

    def _show_detail(self, _event=None) -> None:
        sig = self._selected_signature()
        if sig is None:
            return
        rows = self._groups.get(sig, [])
        target, command, value, typed = _sig_text(sig)
        self.details_var.set(f"{target} via {command}: {value} ({typed})  -  {len(rows)} packet(s)")
        lines = [
            "No mechanic name is assigned here.",
            "Use this list to compare positives/controls, then promote only proven signals into a decoder.",
            "",
        ]
        for row in rows:
            aid = "----" if row.move_id is None else f"{row.move_id:04X}"
            ctx = " ".join(f"{b:02X}" for b in row.context)
            lines.extend(
                [
                    f"{row.move_name} [0x{aid}]  entry 0x{row.move_abs:08X}  packet 0x{row.packet_abs:08X}",
                    f"  {ctx}",
                    "",
                ]
            )
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(lines))
        self.detail.configure(state="disabled")

    def _copy_selected(self) -> None:
        sig = self._selected_signature()
        if sig is None:
            return
        rows = self._groups.get(sig, [])
        target, command, value, typed = _sig_text(sig)
        lines = [f"{target} | {command} | {value} | {typed}"]
        for row in rows:
            aid = "----" if row.move_id is None else f"{row.move_id:04X}"
            ctx = " ".join(f"{b:02X}" for b in row.context)
            lines.append(f"{row.move_name} [0x{aid}] 0x{row.packet_abs:08X}  {ctx}")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))
            self.status_var.set(f"Copied {len(rows)} raw packet(s).")
        except Exception as exc:
            self.status_var.set(f"Clipboard failed: {exc}")


def open_unknown_signal_scout(
    master,
    *,
    moves: list[dict],
    next_abs_map: dict[int, int] | None,
    scope_label: str,
    char_label: str,
) -> UnknownSignalScout:
    return UnknownSignalScout(
        master,
        moves=moves,
        next_abs_map=next_abs_map,
        scope_label=scope_label,
        char_label=char_label,
    )

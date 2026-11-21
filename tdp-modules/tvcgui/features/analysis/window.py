"""Separate analysis workspace; all reports are opt in and off the game HUD."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tvcgui.core.tk_host import tk_call
from tvcgui.features.analysis.store import (
    BAR_UNITS, archive_history_path, compare_attempts, default_history_path,
    has_analysis_data, load_archived_matches, matchup_report, store,
)

_OPEN: tk.Toplevel | None = None
_BG = "#101522"
_PANEL = "#192235"
_FG = "#edf2ff"
_MUTED = "#aebbd4"
_ACCENT = "#83b6ff"


def _fmt_time(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%H:%M:%S")
    except (ValueError, OSError):
        return "—"


def _tree(parent: tk.Misc, columns: tuple[str, ...], widths: tuple[int, ...]) -> ttk.Treeview:
    frame = tk.Frame(parent, bg=_PANEL)
    frame.pack(fill="both", expand=True, padx=10, pady=8)
    view = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
    for name, width in zip(columns, widths):
        view.heading(name, text=name)
        view.column(name, width=width, minwidth=60, stretch=True)
    scroll = ttk.Scrollbar(frame, orient="vertical", command=view.yview)
    view.configure(yscrollcommand=scroll.set)
    view.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    return view


class AnalysisWindow:
    def __init__(self, root: tk.Misc, event_sequence) -> None:
        self.event_sequence = event_sequence
        self.win = tk.Toplevel(root)
        self.win.title("Continuo Analysis")
        self.win.geometry("1000x680")
        self.win.minsize(820, 540)
        self.win.configure(bg=_BG)
        self.win.protocol("WM_DELETE_WINDOW", self._close)
        style = ttk.Style(self.win)
        style.configure("Analysis.TNotebook", background=_BG)
        style.configure("Analysis.TNotebook.Tab", padding=(14, 8))
        style.configure("Analysis.Treeview", background=_PANEL, foreground=_FG,
                        fieldbackground=_PANEL, rowheight=26)
        style.configure("Analysis.Treeview.Heading", background="#28364f", foreground=_FG)

        header = tk.Frame(self.win, bg=_BG, padx=14, pady=10)
        header.pack(fill="x")
        tk.Label(header, text="ANALYSIS", bg=_BG, fg=_FG,
                 font=("Segoe UI", 17, "bold")).pack(side="left")
        self.status = tk.StringVar(value="Recording off")
        tk.Label(header, textvariable=self.status, bg=_BG, fg=_MUTED,
                 font=("Segoe UI", 10)).pack(side="left", padx=20)
        for label, command in (("Start new", self._start), ("End round", self._end_round), ("Stop", self._stop),
                               ("Save", self._save), ("Open", self._open)):
            tk.Button(header, text=label, command=command, bg="#2a3c5a", fg=_FG,
                      activebackground="#3d5a88", activeforeground=_FG,
                      relief="flat", padx=10, pady=5).pack(side="right", padx=3)

        self.tabs = ttk.Notebook(self.win, style="Analysis.TNotebook")
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.pages = {}
        groups = {
            "Overview": ("Round Review",),
            "Matchups": ("Teams", "Characters", "Point & Partner", "Point Exposure", "Moves Taken"),
            "Combos": ("Combo Lab", "Conversions", "Personal Bests"),
            "Resources": ("Meter Planner", "Resources"),
            "Moves": ("Moves & Damage", "Defense", "Assists", "Positions"),
            "Activity": ("Recent Activity",),
        }
        for label, sublabels in groups.items():
            page = tk.Frame(self.tabs, bg=_BG)
            self.tabs.add(page, text=label)
            if len(sublabels) == 1:
                self.pages[sublabels[0]] = page
            else:
                nested = ttk.Notebook(page, style="Analysis.TNotebook")
                nested.pack(fill="both", expand=True)
                for sublabel in sublabels:
                    child = tk.Frame(nested, bg=_BG)
                    nested.add(child, text=sublabel)
                    self.pages[sublabel] = child

        matchup_page = self.pages["Teams"].master.master
        controls = tk.Frame(matchup_page, bg=_BG)
        controls.pack(fill="x", padx=10, pady=6, before=self.pages["Teams"].master)
        self.match_tracking = tk.BooleanVar(value=store.match_tracking)
        tk.Checkbutton(controls, text="Track verified match results", variable=self.match_tracking,
                       command=self._toggle_matches, bg=_BG, fg=_FG, selectcolor=_PANEL,
                       activebackground=_BG, activeforeground=_FG).pack(side="left")
        tk.Label(controls, text="Use for actual matches", bg=_BG, fg=_MUTED).pack(side="left", padx=(8, 0))
        tk.Label(controls, text="Your side:", bg=_BG, fg=_FG).pack(side="left", padx=(20, 5))
        self.perspective = tk.StringVar(value="P1")
        side = ttk.Combobox(controls, textvariable=self.perspective, values=("P1", "P2"),
                            width=4, state="readonly")
        side.pack(side="left")
        side.bind("<<ComboboxSelected>>", lambda _event: self._refresh(force=True))
        tk.Button(controls, text="Load saved match history", command=self._load_match_history).pack(side="right")
        tk.Button(controls, text="Reset match stats", command=self._reset_match_stats).pack(side="right", padx=6)
        self.match_summary = tk.StringVar(value="No verified matches yet")
        tk.Label(matchup_page, textvariable=self.match_summary, bg=_BG, fg=_ACCENT,
                 anchor="w", font=("Segoe UI", 11, "bold")).pack(
                     fill="x", padx=14, pady=(0, 4), before=self.pages["Teams"].master)
        self.archived_matches = []
        self.team_tree = _tree(self.pages["Teams"],
                               ("Your team", "Opponent team", "Wins", "Losses", "Matches"),
                               (260, 260, 75, 75, 85))
        self.character_tree = _tree(self.pages["Characters"],
                                    ("Your character", "Opponent character", "Wins", "Losses", "Matches"),
                                    (200, 200, 75, 75, 85))
        self.role_tree = _tree(self.pages["Point & Partner"],
                               ("Opponent character", "Starting role", "Wins", "Losses", "Matches"),
                               (230, 180, 90, 90, 100))
        tk.Label(self.pages["Point & Partner"],
                 text="Role means the opponent's starting slot. Win/loss counts describe the whole match.",
                 bg=_BG, fg=_MUTED).pack(anchor="w", padx=12, pady=5)
        self.exposure_tree = _tree(self.pages["Point Exposure"],
                                   ("Opponent", "Point time", "Hits taken", "Damage taken", "Matches seen point", "W", "L", "No point seen W/L"),
                                   (145, 95, 85, 105, 135, 50, 50, 110))
        self.match_moves_tree = _tree(self.pages["Moves Taken"],
                                      ("Opponent move", "Hits taken", "Damage taken", "Matches hit", "W", "L"),
                                      (330, 105, 115, 110, 65, 65))
        tk.Label(self.pages["Moves Taken"],
                 text="W/L counts matches where the move hit you at least once. Unknown moves remain separate.",
                 bg=_BG, fg=_MUTED).pack(anchor="w", padx=12, pady=5)

        self.review = tk.Text(self.pages["Round Review"], bg=_PANEL, fg=_FG,
                              insertbackground=_FG, relief="flat", wrap="word",
                              font=("Segoe UI", 11), padx=18, pady=14, state="disabled")
        self.review.pack(fill="both", expand=True, padx=10, pady=10)

        combo = self.pages["Combo Lab"]
        tk.Label(combo, text="Select two attempts to compare. Input, action, and damage edges share one timeline.",
                 bg=_BG, fg=_MUTED).pack(anchor="w", padx=12, pady=(10, 0))
        self.attempt_tree = _tree(combo, ("Time", "Starter", "Hits", "Damage", "Meter Δ", "Note"),
                                  (85, 180, 65, 90, 90, 300))
        self.attempt_tree.bind("<<TreeviewSelect>>", self._attempt_selected)
        combo_bottom = tk.Frame(combo, bg=_BG)
        combo_bottom.pack(fill="x", padx=12, pady=6)
        tk.Button(combo_bottom, text="Compare selected", command=self._compare).pack(side="left")
        tk.Label(combo_bottom, text="Note:", bg=_BG, fg=_FG).pack(side="left", padx=(18, 5))
        self.note = tk.Entry(combo_bottom, bg=_PANEL, fg=_FG, insertbackground=_FG,
                             relief="flat", width=52)
        self.note.pack(side="left", fill="x", expand=True)
        tk.Button(combo_bottom, text="Save note", command=self._save_note).pack(side="left", padx=6)

        self.resource_tree = _tree(self.pages["Resources"],
                                   ("Team / fighter", "Metric", "Value"), (180, 260, 160))
        planner = self.pages["Meter Planner"]
        planner_controls = tk.Frame(planner, bg=_BG)
        planner_controls.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(planner_controls, text="Ending meter target (bars):", bg=_BG, fg=_FG).pack(side="left")
        self.meter_target = tk.StringVar(value="3")
        target = ttk.Combobox(planner_controls, textvariable=self.meter_target,
                              values=("1", "2", "3", "4", "5"), width=5, state="readonly")
        target.pack(side="left", padx=8)
        target.bind("<<ComboboxSelected>>", lambda _event: self._refresh(force=True))
        tk.Label(planner_controls,
                 text="Uses observed team meter at combo boundaries; capture begins after the first contact.",
                 bg=_BG, fg=_MUTED).pack(side="left", padx=12)
        self.planner_tree = _tree(planner, ("Starter", "Damage", "P1 ending", "P2 ending", "Target reached"),
                                  (210, 100, 130, 130, 150))
        self.conversions_tree = _tree(self.pages["Conversions"],
                                      ("Starter", "Attempts", "Best damage", "Best hits"),
                                      (260, 120, 150, 150))
        self.best_tree = _tree(self.pages["Personal Bests"],
                               ("Starter", "Best damage", "Best hits", "Best meter Δ"),
                               (260, 150, 150, 150))
        self.moves_tree = _tree(self.pages["Moves & Damage"],
                                ("Category", "Fighter / move", "Value"), (150, 340, 140))
        tk.Label(self.pages["Defense"],
                 text="Repeated hits are grouped by likely source. Earlier own actions show context, not a cause.",
                 bg=_BG, fg=_MUTED).pack(anchor="w", padx=12, pady=(10, 0))
        self.defense_tree = _tree(self.pages["Defense"],
                                  ("Fighter hit", "Source", "Hits", "Damage", "Recent own action"),
                                  (170, 260, 70, 100, 240))
        tk.Label(self.pages["Assists"],
                 text="Possible assist entries use partner action changes; contacts use clean timing observations.",
                 bg=_BG, fg=_MUTED).pack(anchor="w", padx=12, pady=(10, 0))
        self.assist_tree = _tree(self.pages["Assists"],
                                 ("Partner", "Possible entries", "Likely contacts"),
                                 (260, 170, 170))
        tk.Label(self.pages["Positions"],
                 text="Position is sampled when the GUI receives the hit. Corner labels need verified stage bounds.",
                 bg=_BG, fg=_MUTED).pack(anchor="w", padx=12, pady=(10, 0))
        self.position_tree = _tree(self.pages["Positions"],
                                   ("Fighter hit", "Damage", "Sampled X", "Sampled spacing"),
                                   (220, 100, 150, 170))
        self.activity_tree = _tree(self.pages["Recent Activity"],
                                   ("Time", "Type", "Event"), (100, 120, 600))
        self._last_signature = None
        self._refresh()
        self._load_match_history()

    def _close(self) -> None:
        global _OPEN
        _OPEN = None
        self.win.destroy()

    def _start(self) -> None:
        previous = store.snapshot()
        has_data = bool(previous.get("started_at") and (
            previous["attempts"] or previous["activity"] or previous.get("rounds") or
            previous.get("move_attempts") or previous.get("damage_positions") or
            any(previous.get("meter_seconds", {}).get(team) for team in ("P1", "P2"))
        ))
        if has_data and not messagebox.askyesno(
            "Start new analysis", "Discard the current history and start a new recording? Save it first if needed.",
            parent=self.win,
        ):
            return
        store.start(int(self.event_sequence()))
        self._refresh(force=True)

    def _stop(self) -> None:
        if not store.enabled:
            return
        store.stop()
        data = store.snapshot()
        saved_name = ""
        if has_analysis_data(data):
            try:
                path = archive_history_path(data["started_at"])
                store.save(path)
                saved_name = path.name
            except Exception as exc:
                messagebox.showwarning("Auto-save failed", f"The analysis is still open. Save it manually.\n{exc}", parent=self.win)
        self._refresh(force=True)
        if saved_name:
            self.status.set(f"Saved {saved_name}")

    def _end_round(self) -> None:
        if not store.enabled:
            messagebox.showinfo("Analysis off", "Start recording before marking a round.", parent=self.win)
            return
        store.mark_round_end()
        self._refresh(force=True)

    def _toggle_matches(self) -> None:
        store.set_match_tracking(self.match_tracking.get(), cursor=int(self.event_sequence()))
        self._refresh(force=True)

    def _reset_match_stats(self) -> None:
        store.reset_match_stats(cursor=int(self.event_sequence()))
        self.archived_matches = []
        self._refresh(force=True)
        self.status.set("Match stats reset. Saved analysis files are untouched.")

    def _load_match_history(self) -> None:
        self.status.set("Loading saved match reports…")
        def worker():
            records = load_archived_matches(reset_before=store.reset_before)
            def apply(_root):
                if self.win.winfo_exists():
                    self.archived_matches = [item for item in records
                                             if float(item.get("ended_at") or 0) >= store.reset_before]
                    self._refresh(force=True)
                    self.status.set(f"Loaded {len(records)} saved match records")
            tk_call(apply)
        threading.Thread(target=worker, daemon=True, name="AnalysisMatchHistory").start()

    def _save(self) -> None:
        path = filedialog.asksaveasfilename(parent=self.win, defaultextension=".json",
                                             initialdir=str(default_history_path().parent),
                                             initialfile=default_history_path().name,
                                             filetypes=[("Continuo analysis", "*.json")])
        if not path:
            return
        try:
            store.save(Path(path))
            self.status.set(f"Saved {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc), parent=self.win)

    def _open(self) -> None:
        if store.enabled:
            messagebox.showinfo("Stop recording", "Stop recording before opening a saved history.", parent=self.win)
            return
        path = filedialog.askopenfilename(parent=self.win,
                                          initialdir=str(default_history_path().parent),
                                          filetypes=[("Continuo analysis", "*.json")])
        if not path:
            return
        try:
            store.load(Path(path))
            self.match_tracking.set(store.match_tracking)
            self._refresh(force=True)
        except Exception as exc:
            messagebox.showerror("Open failed", str(exc), parent=self.win)

    def _attempt_selected(self, _event=None) -> None:
        chosen = self.attempt_tree.selection()
        if len(chosen) != 1:
            return
        attempts = {a["id"]: a for a in store.snapshot()["attempts"]}
        self.note.delete(0, "end")
        self.note.insert(0, str(attempts.get(chosen[0], {}).get("note") or ""))

    def _save_note(self) -> None:
        chosen = self.attempt_tree.selection()
        if len(chosen) != 1:
            messagebox.showinfo("Select an attempt", "Select one attempt to add a note.", parent=self.win)
            return
        store.set_note(chosen[0], self.note.get())
        self._refresh(force=True)

    def _compare(self) -> None:
        chosen = self.attempt_tree.selection()
        if len(chosen) != 2:
            messagebox.showinfo("Compare attempts", "Select exactly two attempts.", parent=self.win)
            return
        by_id = {a["id"]: a for a in store.snapshot()["attempts"]}
        a, b = (by_id.get(item) for item in chosen)
        if not a or not b:
            return
        difference = compare_attempts(a, b)
        win = tk.Toplevel(self.win)
        win.title("Route comparison")
        win.geometry("900x520")
        view = tk.Text(win, bg=_PANEL, fg=_FG, font=("Consolas", 10), wrap="none")
        view.pack(fill="both", expand=True)
        def route(attempt):
            start_ns = int(attempt.get("timeline", [{}])[0].get("time_ns") or 0)
            rows = []
            for edge in attempt.get("timeline", []):
                if edge.get("kind") not in {"INPUT", "ACTION", "DAMAGE"}:
                    continue
                frame = round((int(edge.get("time_ns") or start_ns) - start_ns) / (1_000_000_000 / 60))
                value = (f"press 0x{int(edge.get('pressed') or 0):X}" if edge["kind"] == "INPUT" else
                         f"action 0x{int(edge.get('action_id') or 0):04X}" if edge["kind"] == "ACTION" else
                         f"{edge.get('damage', 0)} damage to {edge.get('slot', '')}")
                rows.append(f"{frame:+4}f  {value}")
            return rows
        left, right = route(a), route(b)
        lines = [f"A: {a['hits']} hits / {a['damage']} damage{'':24}B: {b['hits']} hits / {b['damage']} damage", ""]
        if difference["kind"] == "timing_difference":
            left, right = difference["left"], difference["right"]
            lines.append(
                f"First measured difference: action 0x{left['action_id']:04X} began "
                f"{left['after_hit_frames']:.1f}f vs {right['after_hit_frames']:.1f}f after the previous hit."
            )
        elif difference["kind"] == "different_action":
            lines.append(f"First route difference: action #{difference['index'] + 1} has a different recognized ID.")
        elif difference["kind"] == "missing_action":
            lines.append(f"First route difference: action #{difference['index'] + 1} is missing from one attempt.")
        else:
            lines.append("Recognized action order and measured offsets match; inspect spacing and contact outcomes below.")
        lines.append("")
        for index in range(max(len(left), len(right))):
            lines.append(f"{(left[index] if index < len(left) else ''):<45}  {right[index] if index < len(right) else ''}")
        view.insert("1.0", "\n".join(lines))
        view.configure(state="disabled")

    def _refresh(self, force: bool = False) -> None:
        if not self.win.winfo_exists():
            return
        data = store.snapshot(include_timelines=False)
        signature = (data["enabled"], len(data["attempts"]), len(data["activity"]),
                     sum(data["move_attempts"].values()), sum(data["meter_gain"].values()),
                     sum(data["damage_by_victim"].values()),
                     sum(data["megacrashes"].values()), len(data.get("rounds") or []),
                     len(data.get("match_records") or []),
                     int(sum(sum(levels.values()) for levels in data["meter_seconds"].values())))
        if force or signature != self._last_signature:
            self._last_signature = signature
            self.status.set("Recording · opt in" if data["enabled"] else "Recording off")
            self._draw(data)
        self.win.after(500, self._refresh)

    def _draw(self, data) -> None:
        for view in (self.team_tree, self.character_tree, self.role_tree, self.exposure_tree, self.match_moves_tree,
                     self.attempt_tree, self.conversions_tree, self.best_tree, self.planner_tree,
                     self.resource_tree, self.moves_tree, self.defense_tree, self.assist_tree,
                     self.position_tree, self.activity_tree):
            view.delete(*view.get_children())
        records = {str(item["id"]): item for item in self.archived_matches if item.get("id")}
        records.update({str(item["id"]): item for item in data.get("match_records") or [] if item.get("id")})
        report = matchup_report(list(records.values()), self.perspective.get())
        side = self.perspective.get()
        wins = sum(item.get("winner") == side for item in records.values())
        losses = sum(item.get("winner") in {"P1", "P2"} and item.get("winner") != side for item in records.values())
        unresolved = sum(item.get("winner") not in {"P1", "P2"} for item in records.values())
        self.match_summary.set(
            f"{side} · {wins} wins / {losses} losses · {wins + losses} verified · {unresolved} unresolved"
            if wins + losses or unresolved else
            "No verified matches yet · play a match to begin" if data.get("match_tracking") else
            "Match tracking off · earlier reports remain available"
        )
        if data.get("match_persist_error"):
            self.match_summary.set(f"Match stats could not save: {data['match_persist_error']}")
        for row in report["teams"]:
            self.team_tree.insert("", "end", values=(row["your_team"], row["opponent_team"], row["wins"], row["losses"], row["matches"]))
        for row in report["characters"]:
            self.character_tree.insert("", "end", values=(row["your_character"], row["opponent_character"], row["wins"], row["losses"], row["matches"]))
        for row in report["opponent_roles"]:
            self.role_tree.insert("", "end", values=(
                row["opponent_character"], row["start_role"], row["wins"], row["losses"], row["matches"],
            ))
        for row in report["point_exposure"]:
            self.exposure_tree.insert("", "end", values=(
                row["opponent_character"], f"{row.get('seconds', 0):.1f}s",
                row.get("hits_taken", 0), row.get("damage_taken", 0),
                row.get("matches_seen_point", 0), row.get("wins_seen_point", 0),
                row.get("losses_seen_point", 0),
                f"{row.get('wins_no_point_observed', 0)}/{row.get('losses_no_point_observed', 0)}",
            ))
        for row in report["moves"]:
            self.match_moves_tree.insert("", "end", values=(
                row["move"], row.get("hits_taken", 0), row.get("damage_taken", 0),
                row.get("matches_hit", 0), row.get("wins_when_hit", 0), row.get("losses_when_hit", 0),
            ))
        groups = {}
        for attempt in data["attempts"]:
            start, end = attempt.get("meter_start") or {}, attempt.get("meter_end") or {}
            delta = sum(end.values()) - sum(start.values()) if end else 0
            starter = attempt.get("starter") or "Unknown"
            group = groups.setdefault(starter, {"count": 0, "damage": 0, "hits": 0, "meter": float("-inf")})
            group["count"] += 1
            group["damage"] = max(group["damage"], int(attempt.get("damage") or 0))
            group["hits"] = max(group["hits"], int(attempt.get("hits") or 0))
            group["meter"] = max(group["meter"], delta / BAR_UNITS)
            self.attempt_tree.insert("", "end", iid=attempt["id"],
                                     values=(_fmt_time(attempt["started_at"]), attempt.get("starter") or "—",
                                             attempt.get("hits", 0), attempt.get("damage", 0),
                                             f"{delta / BAR_UNITS:+.2f}", attempt.get("note") or ""))
            target_bars = float(self.meter_target.get())
            self.planner_tree.insert("", "end", values=(
                starter, attempt.get("damage", 0),
                f"{end.get('P1', 0) / BAR_UNITS:.2f}" if end else "—",
                f"{end.get('P2', 0) / BAR_UNITS:.2f}" if end else "—",
                "Yes" if end and max(end.values(), default=0) >= target_bars * BAR_UNITS else "No" if end else "Unknown",
            ))
        for starter, group in sorted(groups.items(), key=lambda item: -item[1]["count"]):
            self.conversions_tree.insert("", "end", values=(starter, group["count"], group["damage"], group["hits"]))
            self.best_tree.insert("", "end", values=(starter, group["damage"], group["hits"], f"{group['meter']:+.2f} bars"))
        for team in ("P1", "P2"):
            self.resource_tree.insert("", "end", values=(team, "Meter gained", f"{data['meter_gain'].get(team, 0) / BAR_UNITS:.2f} bars"))
            self.resource_tree.insert("", "end", values=(team, "Meter spent", f"{data['meter_spent'].get(team, 0) / BAR_UNITS:.2f} bars"))
            self.resource_tree.insert("", "end", values=(team, "Megacrashes", data["megacrashes"].get(team, 0)))
            for level, seconds in sorted(data["meter_seconds"].get(team, {}).items(), key=lambda x: int(x[0])):
                self.resource_tree.insert("", "end", values=(team, f"Time at {level}+ bars", f"{seconds:.1f}s observed"))
        for slot, peak in sorted(data["peak_baroque"].items()):
            self.resource_tree.insert("", "end", values=(slot, "Highest Baroque", f"{peak:.1f}%"))
            self.resource_tree.insert("", "end", values=(slot, "Time holding peak", f"{data['peak_baroque_seconds'].get(slot, 0):.1f}s observed"))
        for name, value in sorted(data["damage_by_victim"].items(), key=lambda item: -item[1]):
            self.moves_tree.insert("", "end", values=("Damage taken", name, value))
        for name, value in sorted(data["damage_by_source"].items(), key=lambda item: -item[1]):
            self.moves_tree.insert("", "end", values=("Damage source", name, value))
        for name, value in sorted(data["move_attempts"].items(), key=lambda item: -item[1]):
            self.moves_tree.insert("", "end", values=("Move starts", name, value))
        for name, value in sorted(data["move_hits"].items(), key=lambda item: -item[1]):
            self.moves_tree.insert("", "end", values=("Likely contacts", name, value))
        defense_groups = {}
        for hit in data.get("defense_events") or []:
            key = (hit["victim"], hit["source"])
            item = defense_groups.setdefault(key, {"hits": 0, "damage": 0, "actions": {}})
            item["hits"] += 1
            item["damage"] += int(hit["damage"])
            action = hit.get("own_action") or "Unknown"
            item["actions"][action] = item["actions"].get(action, 0) + 1
        for (victim, source), item in sorted(defense_groups.items(), key=lambda pair: -pair[1]["hits"]):
            common_action = max(item["actions"], key=item["actions"].get)
            self.defense_tree.insert("", "end", values=(victim, source, item["hits"], item["damage"], common_action))
        for name in sorted(set(data["assist_entries"]) | set(data["assist_contacts"])):
            self.assist_tree.insert("", "end", values=(
                name, data["assist_entries"].get(name, 0), data["assist_contacts"].get(name, 0),
            ))
        for position in reversed(data.get("damage_positions") or []):
            spacing = position.get("spacing")
            self.position_tree.insert("", "end", values=(
                position.get("victim", ""), position.get("damage", 0),
                f"{float(position.get('victim_x') or 0):.2f}",
                f"{float(spacing):.2f}" if spacing is not None else "Unknown",
            ))
        for entry in reversed(data["activity"]):
            self.activity_tree.insert("", "end", values=(_fmt_time(entry["time"]), entry["kind"], entry["text"]))
        lines = ["ROUND / SESSION REVIEW", "", f"Recorded combo attempts: {len(data['attempts'])}",
                 f"Total recorded damage: {sum(data['damage_by_victim'].values())}",
                 f"Megacrashes: P1 {data['megacrashes'].get('P1', 0)} · P2 {data['megacrashes'].get('P2', 0)}", ""]
        lines.extend(["Round reviews (mark each round with End round):"])
        if data.get("rounds"):
            for index, round_data in enumerate(data["rounds"][:10], 1):
                lines.append(
                    f"  {index}. {_fmt_time(round_data['started_at'])}–{_fmt_time(round_data['ended_at'])}: "
                    f"{sum(round_data.get('damage_by_victim', {}).values())} damage, "
                    f"{round_data.get('attempts', 0)} combos, "
                    f"best {round_data.get('best_damage', 0)}"
                )
        else:
            lines.append("  No rounds marked yet. The totals below cover the recording.")
        if data["attempts"]:
            best = max(data["attempts"], key=lambda a: a.get("damage", 0))
            lines.append(f"Best recorded combo: {best['damage']} damage / {best['hits']} hits")
        lines.extend(["", "Damage taken by fighter:"])
        lines.extend(f"  {name}: {value}" for name, value in sorted(data["damage_by_victim"].items(), key=lambda p: -p[1]))
        lines.extend(["", "Damage source:"])
        lines.extend(f"  {name}: {value}" for name, value in data["damage_by_source"].items())
        lines.extend(["", "Move starts:"])
        lines.extend(f"  {name}: {value}" for name, value in sorted(data["move_attempts"].items(), key=lambda p: -p[1])[:8])
        self.review.configure(state="normal")
        self.review.delete("1.0", "end")
        self.review.insert("1.0", "\n".join(lines))
        self.review.configure(state="disabled")


def open_analysis_window(event_sequence) -> None:
    def show(root):
        global _OPEN
        try:
            if _OPEN is not None and _OPEN.winfo_exists():
                _OPEN.lift()
                return
        except tk.TclError:
            pass
        window = AnalysisWindow(root, event_sequence)
        _OPEN = window.win
    tk_call(show)

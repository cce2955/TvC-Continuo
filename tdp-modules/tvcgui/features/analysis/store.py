"""Bounded, opt-in analysis of existing combat events and HUD snapshots.

No memory reads occur here. The caller supplies the same event stream and
fighter snapshots already used by Mission Mode and the HUD.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any
from tvcgui.features.overlay.versus_win_tracker import VersusWinTracker

BAR_UNITS = 10_000
MAX_ATTEMPTS = 200
MAX_ACTIVITY = 300
MAX_MATCHES = 1000


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _team(slot: str) -> str:
    return "P1" if str(slot).startswith("P1") else "P2" if str(slot).startswith("P2") else ""


def _event_dict(event: Any) -> dict[str, Any]:
    return {
        "seq": int(event.sequence),
        "time_ns": int(event.timestamp_ns),
        "kind": str(event.kind),
        "slot": str(event.slot),
        "char_id": int(event.char_id),
        "action_id": int(event.action_id),
        "action_frame": int(event.action_frame),
        "held": int(event.held),
        "pressed": int(event.pressed),
        "damage": int(event.damage),
        "hp": int(event.current_hp),
        "combo": int(event.combo_count),
        "hitstun": int(event.hitstun_remaining),
        "point_active": getattr(event, "point_active", None),
    }


def compare_attempts(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Find the first differing recognized action and its observed timing.

    Timing is relative to the previous DAMAGE edge, or the first edge if no
    damage has occurred. This compares observations; it does not infer why a
    move missed or whether the game accepted a raw input.
    """
    def actions(attempt):
        timeline = attempt.get("timeline") or []
        if not timeline:
            return []
        origin = int(timeline[0].get("time_ns") or 0)
        last_hit = origin
        rows = []
        for edge in timeline:
            stamp = int(edge.get("time_ns") or 0)
            if edge.get("kind") == "DAMAGE":
                last_hit = stamp
            elif edge.get("kind") == "ACTION" and int(edge.get("action_id") or 0) >= 0x0100:
                rows.append({"action_id": int(edge["action_id"]),
                             "after_hit_frames": round((stamp - last_hit) * 60 / 1_000_000_000, 1),
                             "from_start_frames": round((stamp - origin) * 60 / 1_000_000_000, 1)})
        return rows
    left, right = actions(a), actions(b)
    for index in range(max(len(left), len(right))):
        if index >= len(left) or index >= len(right):
            return {"index": index, "kind": "missing_action",
                    "left": left[index] if index < len(left) else None,
                    "right": right[index] if index < len(right) else None}
        if left[index]["action_id"] != right[index]["action_id"]:
            return {"index": index, "kind": "different_action", "left": left[index], "right": right[index]}
        if abs(left[index]["after_hit_frames"] - right[index]["after_hit_frames"]) >= 1:
            return {"index": index, "kind": "timing_difference", "left": left[index], "right": right[index]}
    return {"index": None, "kind": "same_recognized_actions", "left": None, "right": None}


class AnalysisStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.enabled = False
        self.cursor = 0
        self.started_at = 0.0
        self.activity: deque[dict[str, Any]] = deque(maxlen=MAX_ACTIVITY)
        self.attempts: deque[dict[str, Any]] = deque(maxlen=MAX_ATTEMPTS)
        self.rounds: deque[dict[str, Any]] = deque(maxlen=50)
        self._round_baseline: dict[str, Any] = {}
        self._open_attempt: dict[str, Any] | None = None
        self._last_action: dict[str, dict[str, Any]] = {}
        self._recent_edges: deque[dict[str, Any]] = deque(maxlen=128)
        self._recent_damage: deque[dict[str, Any]] = deque(maxlen=16)
        self._pending_damage: deque[dict[str, Any]] = deque(maxlen=200)
        self._timing_sequences: set[int] = set()
        self._timing_recent: deque[int] = deque(maxlen=2048)
        self._names: dict[str, str] = {}
        self._last_meter: dict[str, float] = {}
        self._last_sample_time: float | None = None
        self._last_snapshot: dict[str, dict[str, Any]] = {}
        self.meter_seconds: dict[str, Counter[int]] = {"P1": Counter(), "P2": Counter()}
        self.meter_gain: Counter[str] = Counter()
        self.meter_spent: Counter[str] = Counter()
        self.megacrashes: Counter[str] = Counter()
        self.peak_baroque: dict[str, float] = {}
        self.peak_baroque_seconds: Counter[str] = Counter()
        self.damage_by_victim: Counter[str] = Counter()
        self.damage_by_source: Counter[str] = Counter()
        self.damage_positions: deque[dict[str, Any]] = deque(maxlen=500)
        self.defense_events: deque[dict[str, Any]] = deque(maxlen=500)
        self.move_attempts: Counter[str] = Counter()
        self.move_hits: Counter[str] = Counter()
        self.assist_entries: Counter[str] = Counter()
        self.assist_contacts: Counter[str] = Counter()
        self._last_assist_entry_ns: dict[str, int] = {}
        self.match_tracking = True
        self.match_records: deque[dict[str, Any]] = deque(maxlen=MAX_MATCHES)
        self._match_tracker = VersusWinTracker()
        self._match_context: dict[str, Any] | None = None
        self._point_slot: dict[str, str] = {}
        self._last_match_sample_time: float | None = None
        self._persistent = False
        self.reset_before = 0.0
        self.last_persist_error = ""

    def start(self, cursor: int = 0) -> None:
        with self._lock:
            self.__init_state(cursor)
            self.enabled = True

    def __init_state(self, cursor: int) -> None:
        self.cursor = int(cursor)
        self.started_at = time.time()
        self.activity.clear()
        self.attempts.clear()
        self.rounds.clear()
        self._open_attempt = None
        self._last_action.clear()
        self._recent_edges.clear()
        self._recent_damage.clear()
        self._pending_damage.clear()
        self._timing_sequences.clear()
        self._timing_recent.clear()
        self._names.clear()
        self._last_meter.clear()
        self._last_sample_time = None
        self._last_snapshot.clear()
        self.meter_seconds = {"P1": Counter(), "P2": Counter()}
        self.meter_gain.clear()
        self.meter_spent.clear()
        self.megacrashes.clear()
        self.peak_baroque.clear()
        self.peak_baroque_seconds.clear()
        self.damage_by_victim.clear()
        self.damage_by_source.clear()
        self.damage_positions.clear()
        self.defense_events.clear()
        self.move_attempts.clear()
        self.move_hits.clear()
        self.assist_entries.clear()
        self.assist_contacts.clear()
        self._last_assist_entry_ns.clear()
        self._round_baseline = self._round_totals()

    def enable_persistence(self) -> None:
        """Load small match statistics once; full Combo Lab capture stays off."""
        path = match_stats_path()
        with self._lock:
            self._persistent = True
            try:
                if not path.is_file() or path.stat().st_size > 10_000_000:
                    return
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("format") != "continuo-match-stats" or payload.get("version") != 1:
                    return
                self.match_tracking = bool(payload.get("tracking", True))
                self.reset_before = float(payload.get("reset_before") or 0)
                self.match_records.clear()
                self.match_records.extend(
                    record for record in (payload.get("records") or [])[-MAX_MATCHES:]
                    if isinstance(record, dict) and float(record.get("ended_at") or 0) >= self.reset_before
                )
            except (OSError, ValueError, TypeError, AttributeError):
                return

    def _persist_match_stats(self) -> None:
        if not self._persistent:
            return
        try:
            path = match_stats_path()
            payload = {"format": "continuo-match-stats", "version": 1,
                       "tracking": self.match_tracking, "reset_before": self.reset_before,
                       "records": list(self.match_records)}
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(temporary, path)
            self.last_persist_error = ""
        except (OSError, TypeError, ValueError) as exc:
            self.last_persist_error = str(exc)

    def reset_match_stats(self, *, cursor: int | None = None) -> None:
        """Hide previous results without deleting saved analysis sessions."""
        with self._lock:
            self.reset_before = time.time()
            self.match_records.clear()
            self._match_context = None
            self._match_tracker = VersusWinTracker()
            self._point_slot.clear()
            self._pending_damage.clear()
            self._last_match_sample_time = None
            if cursor is not None and not self.enabled:
                self.cursor = int(cursor)
            self._persist_match_stats()

    def set_match_tracking(self, enabled: bool, *, cursor: int | None = None) -> None:
        with self._lock:
            enabled = bool(enabled)
            if enabled != self.match_tracking:
                if not enabled and self._match_context is not None:
                    self._close_match_context("", "tracking_disabled")
                self.match_tracking = enabled
                self._match_tracker = VersusWinTracker()
                if cursor is not None and not self.enabled:
                    self.cursor = int(cursor)
                self._activity("match", "Match result tracking on" if enabled else "Match result tracking off")
                self._persist_match_stats()

    def _record_match_damage(self, event: dict[str, Any], names: dict[str, str]) -> None:
        context = self._match_context
        if context is None:
            return
        victim_team = _team(event["slot"])
        attacker_team = "P2" if victim_team == "P1" else "P1"
        point_slot = self._point_slot.get(attacker_team)
        point_name = names.get(point_slot or "") or "Unknown point"
        context["damage_taken_by_point"][victim_team][point_name] += event["damage"]
        context["hits_taken_by_point"][victim_team][point_name] += 1
        context["damage_taken_by_move"][victim_team]["Unknown move"] += event["damage"]
        context["hits_taken_by_move"][victim_team]["Unknown move"] += 1

    def ingest_match_background(self, events: tuple[Any, ...], snapshots: dict[str, dict],
                                point_slots: dict[str, bool] | None = None,
                                now: float | None = None) -> None:
        """Cheap match-only path while Combo Lab recording is off."""
        with self._lock:
            if self.enabled or not self.match_tracking:
                return
            now = time.monotonic() if now is None else float(now)
            dt = (0.0 if self._last_match_sample_time is None else
                  max(0.0, min(now - self._last_match_sample_time, 0.10)))
            self._last_match_sample_time = now
            names = {slot: str(snap.get("name")) for slot, snap in (snapshots or {}).items()
                     if isinstance(snap, dict) and snap.get("name")}
            for slot, active in (point_slots or {}).items():
                if active and slot in names:
                    self._point_slot[_team(slot)] = slot
            if self._match_context is not None:
                for team, slot in self._point_slot.items():
                    if slot in names:
                        self._match_context["point_seconds"][team][names[slot]] += dt
            for raw in events:
                if raw.sequence <= self.cursor:
                    continue
                self.cursor = int(raw.sequence)
                if raw.kind != "DAMAGE":
                    continue
                event = _event_dict(raw)
                self._pending_damage.append(event)
                self.damage_by_source["Unknown source"] += event["damage"]
                self._record_match_damage(event, names)

    def _close_match_context(self, winner: str, proof: str) -> None:
        context = self._match_context
        if context is None:
            return
        self.match_records.appendleft({
            "id": context["id"], "started_at": context["started_at"],
            "ended_at": time.time(), "winner": winner, "proof": proof,
            "rosters": {team: sorted(names) for team, names in context["rosters"].items()},
            "starting_point": dict(context["starting_point"]),
            "point_seconds": {team: dict(counts) for team, counts in context["point_seconds"].items()},
            "damage_taken_by_point": {team: dict(counts) for team, counts in context["damage_taken_by_point"].items()},
            "hits_taken_by_point": {team: dict(counts) for team, counts in context["hits_taken_by_point"].items()},
            "damage_taken_by_move": {team: dict(counts) for team, counts in context["damage_taken_by_move"].items()},
            "hits_taken_by_move": {team: dict(counts) for team, counts in context["hits_taken_by_move"].items()},
            "start_seq": context["start_seq"], "end_seq": self.cursor,
        })
        self._match_context = None
        self._persist_match_stats()

    def observe_match(self, phase: int | None, snapshots: dict[str, dict],
                      point_slots: dict[str, bool] | None = None) -> None:
        """Record one result only when the shared native-result tracker proves it."""
        with self._lock:
            if not self.match_tracking:
                return
            if phase is None:
                if self._match_tracker.armed and not snapshots:
                    self._match_tracker.observe(None, {})
                return
            slots = {}
            for slot, snap in (snapshots or {}).items():
                if not isinstance(snap, dict):
                    continue
                base = int(_number(snap.get("base")))
                if base:
                    slots[slot] = (base, int(_number(snap.get("cur"))), False)
            was_armed = self._match_tracker.armed
            winner = self._match_tracker.observe(phase, slots)
            rearmed_unresolved = bool(self._match_tracker.unresolved_transition and self._match_context)
            if rearmed_unresolved:
                self._close_match_context("", "unresolved_transition")
                self._match_tracker.unresolved_transition = False
                self._activity("match", "Previous match unresolved; no win or loss credited")
            if self._match_tracker.armed and (not was_armed or self._match_context is None):
                self._match_context = {
                    "id": f"{int(time.time() * 1000)}-{self.cursor}",
                    "started_at": time.time(), "start_seq": self.cursor,
                    "rosters": {"P1": set(), "P2": set()},
                    "starting_point": {"P1": "", "P2": ""},
                    "point_seconds": {"P1": Counter(), "P2": Counter()},
                    "damage_taken_by_point": {"P1": Counter(), "P2": Counter()},
                    "hits_taken_by_point": {"P1": Counter(), "P2": Counter()},
                    "damage_taken_by_move": {"P1": Counter(), "P2": Counter()},
                    "hits_taken_by_move": {"P1": Counter(), "P2": Counter()},
                }
            context = self._match_context
            if context is None:
                return
            for slot, snap in (snapshots or {}).items():
                team = _team(slot)
                if team and isinstance(snap, dict) and snap.get("name"):
                    context["rosters"][team].add(str(snap["name"]))
            for slot, active in (point_slots or {}).items():
                if active and slot in snapshots:
                    team = _team(slot)
                    if team:
                        self._point_slot[team] = slot
                        if not context["starting_point"][team]:
                            name = (snapshots.get(slot) or {}).get("name")
                            if name:
                                context["starting_point"][team] = str(name)
            if winner:
                proof = self._match_tracker.last_reason
                self._close_match_context(winner, proof)
                self._activity("match", f"Verified result: {winner} won · {proof}")

    def stop(self) -> None:
        with self._lock:
            self.enabled = False
            self._finish_attempt("Recording stopped")
            self.mark_round_end("Recording stopped")

    def shutdown(self) -> None:
        with self._lock:
            if self._match_context is not None:
                self._close_match_context("", "app_closed_before_result")

    def _round_totals(self) -> dict[str, Any]:
        return {
            "time": time.time(), "attempts": len(self.attempts),
            "damage": dict(self.damage_by_victim),
            "meter_gain": dict(self.meter_gain),
            "meter_spent": dict(self.meter_spent),
            "megacrashes": dict(self.megacrashes),
        }

    def mark_round_end(self, reason: str = "Marked by user") -> None:
        """End a review period explicitly; native round boundaries remain unverified."""
        with self._lock:
            current = self._round_totals()
            base = self._round_baseline
            attempts = [a for a in self.attempts if a.get("started_at", 0) >= base.get("time", 0)]
            damage = {key: value - base.get("damage", {}).get(key, 0)
                      for key, value in current["damage"].items()
                      if value - base.get("damage", {}).get(key, 0) > 0}
            if not damage and not attempts and current["megacrashes"] == base.get("megacrashes", {}):
                self._round_baseline = current
                return
            delta = lambda key: {team: current[key].get(team, 0) - base.get(key, {}).get(team, 0)
                                 for team in ("P1", "P2")}
            self.rounds.appendleft({
                "started_at": base.get("time", current["time"]), "ended_at": current["time"],
                "reason": reason, "damage_by_victim": damage,
                "meter_gain": delta("meter_gain"), "meter_spent": delta("meter_spent"),
                "megacrashes": delta("megacrashes"),
                "attempts": len(attempts),
                "best_damage": max((a["damage"] for a in attempts), default=0),
            })
            self._round_baseline = current
            self._activity("round", f"Review period ended · {sum(damage.values())} damage")

    def _name(self, slot: str) -> str:
        return self._names.get(slot) or slot

    def _action_name(self, slot: str, action_id: int, char_id: int = 0, label_for=None) -> str:
        if label_for is not None:
            try:
                label = label_for(char_id, action_id)
                if label:
                    return str(label)
            except Exception:
                pass
        snap = self._last_snapshot.get(slot) or {}
        if int(snap.get("action_id") or 0) == action_id and snap.get("move"):
            return str(snap["move"])
        return f"0x{action_id:04X}"

    def _activity(self, kind: str, text: str, *, seq: int = 0) -> None:
        self.activity.append({"time": time.time(), "kind": kind, "text": text, "seq": seq})

    def _finish_attempt(self, reason: str) -> None:
        attempt = self._open_attempt
        if attempt is None:
            return
        attempt["ended_at"] = time.time()
        attempt["end_reason"] = reason
        if len(attempt["timeline"]) > 500:
            attempt["timeline"] = attempt["timeline"][:500]
            attempt["timeline_truncated"] = True
        attempt["hits"] = max(attempt["hits"], len(attempt["damage_events"]))
        self.attempts.appendleft(attempt)
        self._activity("combo", f"{attempt['hits']} hits, {attempt['damage']} damage · {reason}")
        self._open_attempt = None

    def ingest(self, events: tuple[Any, ...], snapshots: dict[str, dict], now: float | None = None,
               label_for=None, point_slots: dict[str, bool] | None = None) -> None:
        """Accept ordered MissionEvents and current HUD snapshots once per GUI tick."""
        with self._lock:
            if not self.enabled:
                return
            now = time.monotonic() if now is None else float(now)
            current: dict[str, dict[str, Any]] = {}
            for slot, snap in (snapshots or {}).items():
                if not isinstance(snap, dict) or not snap.get("name"):
                    continue
                current[slot] = {
                    "name": str(snap.get("name") or slot),
                    "action_id": int(_number(snap.get("mv_id_display") or snap.get("attA"))),
                    "move": str(snap.get("mv_label") or ""),
                    "hp": int(_number(snap.get("cur"))),
                    "meter": _number(snap.get("meter")),
                    "baroque": _number(snap.get("baroque_red_pct_max")),
                    "x": _number(snap.get("x")),
                }
                self._names[slot] = current[slot]["name"]
            # A gap while the game is paused or the window is blocked does not
            # count toward time spent at a meter level.
            dt = 0.0 if self._last_sample_time is None else max(0.0, min(now - self._last_sample_time, 0.10))
            self._last_sample_time = now
            active_match = any(s.startswith("P1") for s in current) and any(s.startswith("P2") for s in current)
            if active_match:
                for team in ("P1", "P2"):
                    point = current.get(f"{team}-C1") or current.get(f"{team}-C2")
                    if point:
                        meter = point["meter"]
                        old = self._last_meter.get(team)
                        if old is not None:
                            self.meter_seconds[team][max(0, int(old // BAR_UNITS))] += dt
                            delta = meter - old
                            if 0 < delta < 50_000:
                                self.meter_gain[team] += int(delta)
                            elif -50_000 < delta < 0:
                                self.meter_spent[team] += int(-delta)
                        self._last_meter[team] = meter
                for slot, snap in current.items():
                    peak = self.peak_baroque.get(slot, 0.0)
                    value = snap["baroque"]
                    if value > peak + 0.01:
                        self.peak_baroque[slot] = value
                        self.peak_baroque_seconds[slot] = 0.0
                    elif abs(value - peak) <= 0.01 and peak > 0:
                        self.peak_baroque_seconds[slot] += dt
            self._last_snapshot = current
            for slot, active in (point_slots or {}).items():
                if active and slot in current:
                    self._point_slot[_team(slot)] = slot
            if active_match and self._match_context is not None:
                for team, slot in self._point_slot.items():
                    if slot in current:
                        self._match_context["point_seconds"][team][current[slot]["name"]] += dt

            for raw in events:
                event = _event_dict(raw)
                if event["seq"] <= self.cursor:
                    continue
                self.cursor = event["seq"]
                kind, slot = event["kind"], event["slot"]
                if kind == "ACTION":
                    if event["action_id"] >= 0x0100:
                        self._last_action[slot] = event
                        label = self._action_name(slot, event["action_id"], event["char_id"], label_for)
                        self.move_attempts[f"{self._name(slot)} · {label}"] += 1
                        if event["point_active"] is False and slot.endswith("C2"):
                            last = self._last_assist_entry_ns.get(slot, 0)
                            if event["time_ns"] - last > 500_000_000:
                                self.assist_entries[self._name(slot)] += 1
                                self._last_assist_entry_ns[slot] = event["time_ns"]
                    self._recent_edges.append(event)
                    if self._open_attempt is not None:
                        self._open_attempt["timeline"].append(event)
                elif kind == "INPUT":
                    if event["pressed"]:
                        self._recent_edges.append(event)
                    if self._open_attempt is not None and event["pressed"]:
                        self._open_attempt["timeline"].append(event)
                elif kind == "COMBO_BEGIN":
                    self._finish_attempt("New combo")
                    lead_in = [edge for edge in self._recent_edges
                               if 0 <= event["time_ns"] - edge["time_ns"] <= 500_000_000]
                    opening_damage = [edge for edge in self._recent_damage
                                      if 0 <= event["time_ns"] - edge["time_ns"] <= 50_000_000]
                    attacker_team = "P2" if _team(slot) == "P1" else "P1"
                    attacks = [edge for edge in lead_in if edge["kind"] == "ACTION"
                               and _team(edge["slot"]) == attacker_team and edge["action_id"] >= 0x0100]
                    starter = (self._action_name(attacks[-1]["slot"], attacks[-1]["action_id"],
                                                 attacks[-1]["char_id"], label_for) if attacks else "Unknown")
                    self._open_attempt = {
                        "id": f"{int(time.time() * 1000)}-{event['seq']}",
                        "started_at": time.time(), "ended_at": None,
                        "starter": starter, "hits": max(1, event["combo"]),
                        "damage": sum(edge["damage"] for edge in opening_damage),
                        "damage_events": opening_damage.copy(),
                        "timeline": lead_in + opening_damage + [event],
                        "meter_start": dict(self._last_meter), "meter_end": {},
                        "note": "", "end_reason": "",
                    }
                elif kind == "COMBO_CHANGE" and self._open_attempt is not None:
                    self._open_attempt["hits"] = max(self._open_attempt["hits"], event["combo"])
                    self._open_attempt["timeline"].append(event)
                elif kind == "COMBO_END":
                    if self._open_attempt is not None:
                        self._open_attempt["timeline"].append(event)
                        self._open_attempt["meter_end"] = dict(self._last_meter)
                    self._finish_attempt("Combo ended")
                elif kind == "DAMAGE":
                    self._recent_damage.append(event)
                    self._pending_damage.append(event)
                    self.damage_by_victim[self._name(slot)] += event["damage"]
                    # Victim HP is authoritative; source attribution remains
                    # unknown until a unique attacker can be proven.
                    self.damage_by_source["Unknown source"] += event["damage"]
                    self._record_match_damage(event, {key: value["name"] for key, value in current.items()})
                    previous = self._last_action.get(slot) or {}
                    recent_own_action = (
                        previous if 0 <= event["time_ns"] - int(previous.get("time_ns") or 0) <= 500_000_000
                        else None
                    )
                    own_action = (
                        self._action_name(slot, recent_own_action["action_id"],
                                          recent_own_action["char_id"], label_for)
                        if recent_own_action else "No recent recognized attack"
                    )
                    defense = {"seq": event["seq"], "victim": self._name(slot),
                               "damage": event["damage"], "source": "Unknown",
                               "own_action": own_action, "time_ns": event["time_ns"]}
                    self.defense_events.append(defense)
                    victim = current.get(slot)
                    opponent_team = "P2" if _team(slot) == "P1" else "P1"
                    opponent = current.get(f"{opponent_team}-C1") or current.get(f"{opponent_team}-C2")
                    if victim is not None:
                        self.damage_positions.append({
                            "victim": self._name(slot), "damage": event["damage"],
                            "victim_x": victim["x"],
                            "opponent_x": opponent["x"] if opponent else None,
                            "spacing": abs(victim["x"] - opponent["x"]) if opponent else None,
                        })
                    if self._open_attempt is not None:
                        self._open_attempt["damage"] += event["damage"]
                        self._open_attempt["damage_events"].append(event)
                        self._open_attempt["timeline"].append(event)
                    self._activity("damage", f"{self._name(slot)} took {event['damage']} damage", seq=event["seq"])
                elif kind == "MEGACRASH_BEGIN":
                    self.megacrashes[_team(slot)] += 1
                    self._activity("megacrash", f"{self._name(slot)} used Megacrash", seq=event["seq"])

    def ingest_timing(self, results: list[Any]) -> None:
        """Count clean likely contacts without assigning unrelated HP deltas."""
        with self._lock:
            if not (self.enabled or self.match_tracking):
                return
            for result in results:
                seq = int(getattr(result, "sequence", 0) or 0)
                if seq in self._timing_sequences or getattr(result, "kind", "") != "hit":
                    continue
                if len(self._timing_recent) == self._timing_recent.maxlen:
                    self._timing_sequences.discard(self._timing_recent[0])
                self._timing_recent.append(seq)
                self._timing_sequences.add(seq)
                if not bool(getattr(result, "clean", False)) or int(getattr(result, "contact_count", 0)) != 1:
                    continue
                label = str(getattr(result, "action_name", "") or f"0x{int(getattr(result, 'action_id', 0)):04X}")
                attacker = str(getattr(result, "attacker_name", "") or getattr(result, "attacker_slot", "Unknown"))
                self.move_hits[f"Likely: {attacker} · {label}"] += 1
                attacker_slot = str(getattr(result, "attacker_slot", ""))
                last_action = self._last_action.get(attacker_slot) or {}
                if attacker_slot.endswith("C2") and last_action.get("point_active") is False:
                    self.assist_contacts[attacker] += 1
                amount = max(0, int(getattr(result, "damage", 0) or 0))
                defender_slot = str(getattr(result, "defender_slot", ""))
                now_ns = time.monotonic_ns()
                candidates = [edge for edge in self._pending_damage
                              if edge["slot"] == defender_slot and edge["damage"] == amount
                              and 0 <= now_ns - edge["time_ns"] <= 5_000_000_000]
                if len(candidates) == 1 and amount > 0:
                    source = f"Likely: {attacker} · {label}"
                    self.damage_by_source[source] += amount
                    self.damage_by_source["Unknown source"] -= amount
                    if self.damage_by_source["Unknown source"] <= 0:
                        del self.damage_by_source["Unknown source"]
                    self._pending_damage.remove(candidates[0])
                    victim_team = _team(defender_slot)
                    linked = candidates[0]["seq"]
                    targets = ([self._match_context] if self._match_context else []) + list(self.match_records)
                    for match in targets:
                        if match and match.get("start_seq", 0) <= linked <= match.get("end_seq", self.cursor):
                            damage_map = match["damage_taken_by_move"][victim_team]
                            hit_map = match["hits_taken_by_move"][victim_team]
                            damage_map["Unknown move"] -= amount
                            hit_map["Unknown move"] -= 1
                            if damage_map["Unknown move"] <= 0:
                                damage_map.pop("Unknown move", None)
                            if hit_map["Unknown move"] <= 0:
                                hit_map.pop("Unknown move", None)
                            damage_map[source] = damage_map.get(source, 0) + amount
                            hit_map[source] = hit_map.get(source, 0) + 1
                            break
                    for defense in self.defense_events:
                        if defense["seq"] == candidates[0]["seq"]:
                            defense["source"] = source
                            break

    def snapshot(self, *, include_timelines: bool = True) -> dict[str, Any]:
        with self._lock:
            attempts = (list(self.attempts) if include_timelines else [
                {key: value for key, value in attempt.items()
                 if key not in {"timeline", "damage_events"}}
                for attempt in self.attempts
            ])
            return deepcopy({
                "enabled": self.enabled, "started_at": self.started_at,
                "activity": list(self.activity), "attempts": attempts,
                "rounds": list(self.rounds),
                "open_attempt": self._open_attempt if include_timelines else None,
                "meter_seconds": {k: dict(v) for k, v in self.meter_seconds.items()},
                "meter_gain": dict(self.meter_gain), "meter_spent": dict(self.meter_spent),
                "megacrashes": dict(self.megacrashes),
                "peak_baroque": dict(self.peak_baroque),
                "peak_baroque_seconds": dict(self.peak_baroque_seconds),
                "damage_by_victim": dict(self.damage_by_victim),
                "damage_by_source": dict(self.damage_by_source),
                "damage_positions": list(self.damage_positions),
                "defense_events": list(self.defense_events),
                "move_attempts": dict(self.move_attempts),
                "move_hits": dict(self.move_hits),
                "assist_entries": dict(self.assist_entries),
                "assist_contacts": dict(self.assist_contacts),
                "match_tracking": self.match_tracking,
                "match_records": list(self.match_records),
                "match_persist_error": self.last_persist_error,
            })

    def set_note(self, attempt_id: str, note: str) -> bool:
        with self._lock:
            for attempt in self.attempts:
                if attempt["id"] == attempt_id:
                    attempt["note"] = str(note)[:2000]
                    return True
        return False

    def save(self, path: Path) -> None:
        payload = {"format": "continuo-analysis", "version": 1, "data": self.snapshot()}
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)

    def load(self, path: Path) -> None:
        if path.stat().st_size > 30_000_000:
            raise ValueError("Analysis history is too large")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("format") != "continuo-analysis" or payload.get("version") != 1:
            raise ValueError("Unsupported analysis history")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("attempts"), list):
            raise ValueError("Invalid analysis history")
        with self._lock:
            if self.enabled:
                raise RuntimeError("Stop recording before opening a saved history")
            self.__init_state(self.cursor)
            self.started_at = _number(data.get("started_at"))
            self.activity.extend(data.get("activity", [])[-MAX_ACTIVITY:])
            self.attempts.extend(data["attempts"][-MAX_ATTEMPTS:])
            self.rounds.extend((data.get("rounds") or [])[-50:])
            self.meter_seconds = {
                team: Counter({int(k): _number(v) for k, v in (data.get("meter_seconds", {}).get(team) or {}).items()})
                for team in ("P1", "P2")
            }
            for field in ("meter_gain", "meter_spent", "megacrashes", "peak_baroque_seconds",
                          "damage_by_victim", "damage_by_source", "move_attempts", "move_hits"):
                setattr(self, field, Counter(data.get(field) or {}))
            self.peak_baroque = dict(data.get("peak_baroque") or {})
            self.damage_positions.extend((data.get("damage_positions") or [])[-500:])
            self.defense_events.extend((data.get("defense_events") or [])[-500:])
            self.assist_entries = Counter(data.get("assist_entries") or {})
            self.assist_contacts = Counter(data.get("assist_contacts") or {})
            self._round_baseline = self._round_totals()


def default_history_path() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / "TvCContinuo" / "analysis" / "analysis_history.json"


def match_stats_path() -> Path:
    return default_history_path().parent / "match_stats.json"


def archive_history_path(started_at: float) -> Path:
    start = float(started_at or time.time())
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(start))
    return default_history_path().parent / f"analysis-{stamp}-{int(start * 1000) % 1000:03d}.json"


def has_analysis_data(data: dict[str, Any]) -> bool:
    return any((
        data.get("activity"), data.get("attempts"), data.get("move_attempts"),
        data.get("peak_baroque"), data.get("damage_positions"),
        data.get("match_records"),
        any(data.get("meter_seconds", {}).get(team) for team in ("P1", "P2")),
    ))


def load_archived_matches(directory: Path | None = None, *, limit: int = 200,
                          reset_before: float = 0.0) -> list[dict[str, Any]]:
    """Read match summaries from a bounded set of local analysis archives."""
    directory = directory or default_history_path().parent
    if not directory.is_dir():
        return []
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(directory.glob("analysis-*.json"), reverse=True)[:max(0, limit)]:
        try:
            if path.stat().st_size > 30_000_000:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("format") != "continuo-analysis" or payload.get("version") != 1:
                continue
            for record in (payload.get("data") or {}).get("match_records") or []:
                if (isinstance(record, dict) and record.get("id") and
                        record.get("winner") in {"P1", "P2", ""} and
                        float(record.get("ended_at") or 0) >= reset_before):
                    records.setdefault(str(record["id"]), record)
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return sorted(records.values(), key=lambda item: item.get("ended_at", 0), reverse=True)


def matchup_report(records: list[dict[str, Any]], perspective: str = "P1") -> dict[str, list[dict[str, Any]]]:
    """Aggregate raw W/L and point exposure without causal claims."""
    own = "P2" if perspective == "P2" else "P1"
    other = "P1" if own == "P2" else "P2"
    teams: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    characters: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    opponent_roles: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    point_exposure: dict[str, Counter[str]] = defaultdict(Counter)
    moves: dict[str, Counter[str]] = defaultdict(Counter)
    for record in records:
        winner = str(record.get("winner") or "")
        if winner not in {"P1", "P2"}:
            continue
        outcome = "wins" if winner == own else "losses"
        rosters = record.get("rosters") or {}
        mine = sorted(set(rosters.get(own) or []))
        theirs = sorted(set(rosters.get(other) or []))
        if not mine or not theirs:
            continue
        teams[(" + ".join(mine), " + ".join(theirs))][outcome] += 1
        for my_char in mine:
            for their_char in theirs:
                characters[(my_char, their_char)][outcome] += 1
        starting_point = (record.get("starting_point") or {}).get(other) or ""
        for their_char in theirs:
            role = "starting point" if starting_point == their_char else "starting partner" if starting_point else "unknown start role"
            opponent_roles[(their_char, role)][outcome] += 1
        taken = (record.get("damage_taken_by_point") or {}).get(own) or {}
        hits = (record.get("hits_taken_by_point") or {}).get(own) or {}
        seconds = (record.get("point_seconds") or {}).get(other) or {}
        for char in set(taken) | set(hits) | set(seconds):
            point_exposure[char]["damage_taken"] += int(taken.get(char, 0))
            point_exposure[char]["hits_taken"] += int(hits.get(char, 0))
            point_exposure[char]["seconds"] += float(seconds.get(char, 0))
        for their_char in theirs:
            key = "seen_point" if float(seconds.get(their_char, 0)) > 0 else "no_point_observed"
            point_exposure[their_char][f"matches_{key}"] += 1
            point_exposure[their_char][f"wins_{key}" if winner == own else f"losses_{key}"] += 1
        move_damage = (record.get("damage_taken_by_move") or {}).get(own) or {}
        move_hits = (record.get("hits_taken_by_move") or {}).get(own) or {}
        for move in set(move_damage) | set(move_hits):
            moves[move]["damage_taken"] += int(move_damage.get(move, 0))
            moves[move]["hits_taken"] += int(move_hits.get(move, 0))
            if int(move_hits.get(move, 0)) > 0:
                moves[move]["matches_hit"] += 1
                moves[move]["wins_when_hit" if winner == own else "losses_when_hit"] += 1
    def rows(mapping, labels):
        return [dict(zip(labels, key), wins=value["wins"], losses=value["losses"],
                     matches=value["wins"] + value["losses"])
                for key, value in sorted(mapping.items(), key=lambda item: -(item[1]["wins"] + item[1]["losses"]))]
    return {
        "teams": rows(teams, ("your_team", "opponent_team")),
        "characters": rows(characters, ("your_character", "opponent_character")),
        "opponent_roles": rows(opponent_roles, ("opponent_character", "start_role")),
        "point_exposure": [dict(opponent_character=char, **dict(counts)) for char, counts in
                           sorted(point_exposure.items(), key=lambda item: -item[1]["damage_taken"])],
        "moves": [dict(move=move, **dict(counts)) for move, counts in
                  sorted(moves.items(), key=lambda item: -item[1]["damage_taken"])],
    }


store = AnalysisStore()
store.enable_persistence()

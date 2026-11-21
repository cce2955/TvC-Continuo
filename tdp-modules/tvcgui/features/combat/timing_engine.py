"""Share read-only hitstop and stun observations across Continuo views.

Sampling follows the application loop, whose counter is not a validated game
frame. Block advantage remains unavailable until attacker actionability can be
established; an animation transition alone does not prove control is restored.
"""
from __future__ import annotations

import struct
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Any, Iterable

try:
    from tvcgui.platform.dolphin import rd32
except Exception:
    rd32 = None

OFF_ACTION_FRAME_FLOAT = 0x01D8
OFF_BLOCKSTUN_REMAINING = 0x1204
# +0x1210 is both the freshly assigned value and the live countdown.
# +0x1228 is a separate 0x300-family reaction timer, not general hitstun.
OFF_HITSTUN_REMAINING = 0x1210
OFF_HITSTUN_TOTAL = OFF_HITSTUN_REMAINING  # compatibility alias
OFF_REACTION_TIMER = 0x1228
OFF_ACTIVE_HITSTOP = 0x211C
OFF_PENDING_HITSTOP = 0x2120

MAX_RESULT_HISTORY = 80
CONTACT_TIMEOUT_FRAMES = 180


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _read_u32(addr: int, default: int = 0) -> int:
    if rd32 is None:
        return int(default) & 0xFFFFFFFF
    try:
        value = rd32(int(addr))
    except Exception:
        value = None
    if value is None:
        return int(default) & 0xFFFFFFFF
    return int(value) & 0xFFFFFFFF


def _decode_action_frame(word: int) -> int:
    try:
        value = struct.unpack(">f", struct.pack(">I", int(word) & 0xFFFFFFFF))[0]
    except Exception:
        return 0
    if value != value or value < 0.0 or value > 4000.0:
        return 0
    return max(0, int(round(value - 1.0)))


def _team_for_slot(slot: str, snap: dict[str, Any] | None = None) -> str:
    if isinstance(snap, dict):
        team = str(snap.get("teamtag") or "").upper()
        if team in {"P1", "P2"}:
            return team
    return "P1" if str(slot).upper().startswith("P1") else "P2"


@dataclass(frozen=True)
class LiveTimingState:
    slot: str
    team: str
    base: int
    char_id: int
    action_id: int
    action_name: str
    action_frame: int
    hp: int
    blockstun_remaining: int
    hitstun_total: int
    hitstun_remaining: int
    reaction_timer: int
    active_hitstop: int
    pending_hitstop: int
    x: float = 0.0
    y: float = 0.0

    @property
    def hitstop(self) -> int:
        return max(int(self.active_hitstop), int(self.pending_hitstop))


@dataclass(frozen=True)
class TimingResult:
    sequence: int
    timestamp: str
    frame_idx: int
    kind: str
    attacker_slot: str
    defender_slot: str
    attacker_name: str
    defender_name: str
    char_id: int
    action_id: int
    action_name: str
    blockstun: int = 0
    hitstun: int = 0
    hitstop: int = 0
    damage: int = 0
    block_advantage: int | None = None
    attacker_ready_frames: int | None = None
    defender_ready_frames: int | None = None
    contact_count: int = 1
    cancelled: bool = False
    clean: bool = False
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TimingEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, LiveTimingState] = {}
        self._previous: dict[str, LiveTimingState] = {}
        self._active: dict[str, dict[str, Any]] = {}
        self._results: deque[TimingResult] = deque(maxlen=MAX_RESULT_HISTORY)
        self._sequence = 0
        self._last_frame = -1

    def reset(self) -> None:
        with self._lock:
            self._states.clear()
            self._previous.clear()
            self._active.clear()
            self._results.clear()
            self._sequence = 0
            self._last_frame = -1

    def _state_from_snapshot(self, slot: str, snap: dict[str, Any]) -> LiveTimingState | None:
        base = _int(snap.get("base"), 0)
        if base <= 0:
            return None
        action_id = _int(snap.get("mv_id_display") or snap.get("attA") or snap.get("attB"), 0) & 0xFFFF
        char_id = _int(snap.get("csv_char_id") or snap.get("id") or snap.get("char_id"), 0)
        action_name = str(snap.get("mv_label") or f"0x{action_id:04X}")

        def live(key: str, offset: int) -> int:
            if key in snap:
                return max(0, _int(snap.get(key), 0))
            return max(0, _read_u32(base + offset, 0))

        if "timing_action_frame" in snap:
            action_frame = max(0, _int(snap.get("timing_action_frame"), 0))
        else:
            action_frame = _decode_action_frame(_read_u32(base + OFF_ACTION_FRAME_FLOAT, 0))

        return LiveTimingState(
            slot=str(slot),
            team=_team_for_slot(slot, snap),
            base=base,
            char_id=char_id,
            action_id=action_id,
            action_name=action_name,
            action_frame=action_frame,
            hp=max(0, _int(snap.get("cur") or snap.get("hp"), 0)),
            blockstun_remaining=live("timing_blockstun", OFF_BLOCKSTUN_REMAINING),
            hitstun_total=live("timing_hitstun_remaining", OFF_HITSTUN_REMAINING),
            hitstun_remaining=live("timing_hitstun_remaining", OFF_HITSTUN_REMAINING),
            reaction_timer=live("timing_reaction_timer", OFF_REACTION_TIMER),
            active_hitstop=live("timing_hitstop_active", OFF_ACTIVE_HITSTOP),
            pending_hitstop=live("timing_hitstop_pending", OFF_PENDING_HITSTOP),
            x=float(snap.get("x") or snap.get("pos_x") or 0.0),
            y=float(snap.get("y") or snap.get("pos_y") or 0.0),
        )

    @staticmethod
    def _distance2(a: LiveTimingState, b: LiveTimingState) -> float:
        dx = float(a.x) - float(b.x)
        dy = float(a.y) - float(b.y)
        return dx * dx + dy * dy

    def _pick_attacker(self, defender: LiveTimingState, states: Iterable[LiveTimingState]) -> LiveTimingState | None:
        candidates = [state for state in states if state.team != defender.team and state.base > 0]
        if not candidates:
            return None

        def score(state: LiveTimingState) -> tuple[int, int, float]:
            attacking = 1 if int(state.action_id) >= 0x0100 else 0
            impact = 1 if int(state.hitstop) > 0 else 0
            return (impact, attacking, -self._distance2(state, defender))

        candidates.sort(key=score, reverse=True)
        best = candidates[0]
        return best if int(best.action_id) >= 0x0100 else None

    @staticmethod
    def _contact_edge(previous: LiveTimingState | None, current: LiveTimingState) -> str | None:
        prev_block = int(previous.blockstun_remaining) if previous is not None else 0
        prev_hit = int(previous.hitstun_remaining) if previous is not None else 0
        if int(current.blockstun_remaining) > 0 and prev_block <= 0:
            return "block"
        if int(current.hitstun_remaining) > 0 and prev_hit <= 0:
            return "hit"
        if previous is not None and int(previous.hp) > 0 and int(current.hp) < int(previous.hp):
            return "hit"
        return None

    def _begin_contact(
        self,
        frame_idx: int,
        kind: str,
        attacker: LiveTimingState,
        defender: LiveTimingState,
        previous_defender: LiveTimingState | None,
        snapshots: dict[str, dict[str, Any]],
    ) -> None:
        damage = 0
        if previous_defender is not None:
            damage = max(0, int(previous_defender.hp) - int(defender.hp))
        self._active[defender.slot] = {
            "kind": str(kind),
            "contact_frame": int(frame_idx),
            "attacker_slot": attacker.slot,
            "defender_slot": defender.slot,
            "attacker_base": attacker.base,
            "defender_base": defender.base,
            "attacker_name": str((snapshots.get(attacker.slot) or {}).get("name") or attacker.slot),
            "defender_name": str((snapshots.get(defender.slot) or {}).get("name") or defender.slot),
            "char_id": int(attacker.char_id),
            "action_id": int(attacker.action_id),
            "action_name": str(attacker.action_name),
            "blockstun": int(defender.blockstun_remaining),
            "hitstun": max(int(defender.hitstun_total), int(defender.hitstun_remaining)),
            "hitstop": max(int(attacker.hitstop), int(defender.hitstop)),
            "damage": int(damage),
            "attacker_ready": None,
            "defender_ready": None,
            "contact_count": 1,
            "cancelled": False,
            "last_stun": max(int(defender.blockstun_remaining), int(defender.hitstun_remaining)),
        }

    def _finalize(self, frame_idx: int, active: dict[str, Any], reason: str = "") -> TimingResult:
        self._sequence += 1
        # Leaving the attack action range is only a recovery-state transition,
        # not proof that the attacker can accept a new command.  Treating that
        # edge as actionable collapsed many unrelated moves to 0 or -2.  Keep
        # observed block advantage quarantined until a real control-ready signal
        # is decoded.  The decoded blockstun, hitstun, hitstop, and damage fields
        # remain valid and continue to feed the monitor and scanner.
        attacker_ready = None
        defender_ready = active.get("defender_ready")
        advantage = None
        clean = bool(
            active.get("contact_count") == 1
            and not active.get("cancelled")
            and (
                (active.get("kind") == "block" and int(active.get("blockstun") or 0) > 0)
                or (active.get("kind") == "hit" and int(active.get("hitstun") or 0) > 0)
            )
        )
        notes = str(reason or "")
        if active.get("kind") == "block" and not notes:
            notes = "block advantage withheld until attacker actionability is decoded"
        result = TimingResult(
            sequence=self._sequence,
            timestamp=time.strftime("%H:%M:%S"),
            frame_idx=int(frame_idx),
            kind=str(active.get("kind") or "unknown"),
            attacker_slot=str(active.get("attacker_slot") or ""),
            defender_slot=str(active.get("defender_slot") or ""),
            attacker_name=str(active.get("attacker_name") or active.get("attacker_slot") or ""),
            defender_name=str(active.get("defender_name") or active.get("defender_slot") or ""),
            char_id=int(active.get("char_id") or 0),
            action_id=int(active.get("action_id") or 0),
            action_name=str(active.get("action_name") or ""),
            blockstun=int(active.get("blockstun") or 0),
            hitstun=int(active.get("hitstun") or 0),
            hitstop=int(active.get("hitstop") or 0),
            damage=int(active.get("damage") or 0),
            block_advantage=advantage,
            attacker_ready_frames=None if attacker_ready is None else int(attacker_ready) - int(active.get("contact_frame") or 0),
            defender_ready_frames=None if defender_ready is None else int(defender_ready) - int(active.get("contact_frame") or 0),
            contact_count=int(active.get("contact_count") or 1),
            cancelled=bool(active.get("cancelled")),
            clean=clean,
            notes=notes,
        )
        self._results.append(result)
        return result

    def _update_active(self, frame_idx: int, states: dict[str, LiveTimingState]) -> list[TimingResult]:
        completed: list[TimingResult] = []
        for defender_slot, active in list(self._active.items()):
            attacker = states.get(str(active.get("attacker_slot") or ""))
            defender = states.get(defender_slot)
            previous_defender = self._previous.get(defender_slot)
            if attacker is None or defender is None:
                completed.append(self._finalize(frame_idx, active, "fighter pointer changed"))
                self._active.pop(defender_slot, None)
                continue
            if int(attacker.base) != int(active.get("attacker_base") or 0) or int(defender.base) != int(active.get("defender_base") or 0):
                completed.append(self._finalize(frame_idx, active, "fighter allocation changed"))
                self._active.pop(defender_slot, None)
                continue

            active["blockstun"] = max(int(active.get("blockstun") or 0), int(defender.blockstun_remaining))
            active["hitstun"] = max(int(active.get("hitstun") or 0), int(defender.hitstun_total), int(defender.hitstun_remaining))
            active["hitstop"] = max(int(active.get("hitstop") or 0), int(attacker.hitstop), int(defender.hitstop))
            if previous_defender is not None:
                active["damage"] = int(active.get("damage") or 0) + max(0, int(previous_defender.hp) - int(defender.hp))

            stun_now = int(defender.blockstun_remaining if active.get("kind") == "block" else defender.hitstun_remaining)
            last_stun = int(active.get("last_stun") or 0)
            if stun_now > last_stun + 1:
                active["contact_count"] = int(active.get("contact_count") or 1) + 1
            active["last_stun"] = stun_now

            if int(attacker.action_id) != int(active.get("action_id") or 0):
                if int(attacker.action_id) >= 0x0100:
                    active["cancelled"] = True
                # A transition to an action below 0x0100 is not necessarily
                # actionable.  Recovery bridges, landing states, and stance
                # transitions also live in that range, so do not publish an
                # attacker-ready edge from the action ID alone.

            if active.get("defender_ready") is None and previous_defender is not None:
                if active.get("kind") == "block":
                    if int(previous_defender.blockstun_remaining) > 0 and int(defender.blockstun_remaining) == 0:
                        active["defender_ready"] = int(frame_idx)
                else:
                    if int(previous_defender.hitstun_remaining) > 0 and int(defender.hitstun_remaining) == 0:
                        active["defender_ready"] = int(frame_idx)

            if active.get("kind") == "hit" and active.get("defender_ready") is not None:
                completed.append(self._finalize(frame_idx, active))
                self._active.pop(defender_slot, None)
                continue

            if active.get("kind") == "block" and active.get("defender_ready") is not None:
                if active.get("cancelled"):
                    completed.append(self._finalize(frame_idx, active, "attacker cancelled before recovery"))
                else:
                    completed.append(self._finalize(frame_idx, active))
                self._active.pop(defender_slot, None)
                continue

            if int(frame_idx) - int(active.get("contact_frame") or frame_idx) > CONTACT_TIMEOUT_FRAMES:
                completed.append(self._finalize(frame_idx, active, "timing contact timed out"))
                self._active.pop(defender_slot, None)
        return completed

    def update(self, frame_idx: int, snapshots: dict[str, dict[str, Any]] | None) -> list[TimingResult]:
        snapshots = snapshots if isinstance(snapshots, dict) else {}
        with self._lock:
            if int(frame_idx) == self._last_frame:
                return []
            self._last_frame = int(frame_idx)
            previous_states = dict(self._states)
            self._previous = previous_states
            states: dict[str, LiveTimingState] = {}
            for slot, snap in snapshots.items():
                if not isinstance(snap, dict):
                    continue
                state = self._state_from_snapshot(str(slot), snap)
                if state is not None:
                    states[str(slot)] = state

            completed = self._update_active(int(frame_idx), states)

            for slot, defender in states.items():
                if slot in self._active:
                    continue
                previous = self._previous.get(slot)
                kind = self._contact_edge(previous, defender)
                if kind is None:
                    continue
                attacker = self._pick_attacker(defender, states.values())
                if attacker is None:
                    continue
                self._begin_contact(int(frame_idx), kind, attacker, defender, previous, snapshots)

            self._states = states
            return completed

    def decorate_snapshots(self, snapshots: dict[str, dict[str, Any]] | None) -> None:
        if not isinstance(snapshots, dict):
            return
        with self._lock:
            for slot, snap in snapshots.items():
                state = self._states.get(str(slot))
                if state is None or not isinstance(snap, dict):
                    continue
                snap["timing_action_frame"] = int(state.action_frame)
                snap["timing_blockstun"] = int(state.blockstun_remaining)
                # Keep the legacy total key as a compatibility mirror. The
                # confirmed field is one live countdown at +0x1210.
                snap["timing_hitstun_total"] = int(state.hitstun_remaining)
                snap["timing_hitstun_remaining"] = int(state.hitstun_remaining)
                snap["timing_reaction_timer"] = int(state.reaction_timer)
                snap["timing_hitstop_active"] = int(state.active_hitstop)
                snap["timing_hitstop_pending"] = int(state.pending_hitstop)
                snap["timing_hitstop"] = int(state.hitstop)

    def get_live_state(self, slot: str) -> LiveTimingState | None:
        with self._lock:
            return self._states.get(str(slot))

    def latest_result(self) -> TimingResult | None:
        with self._lock:
            return self._results[-1] if self._results else None

    def result_history(self) -> list[TimingResult]:
        with self._lock:
            return list(self._results)

    def overlay_payload(self) -> dict[str, Any]:
        with self._lock:
            latest = self._results[-1].to_dict() if self._results else None
            return {
                "latest": latest,
                "active": [
                    {
                        "kind": str(active.get("kind") or ""),
                        "attacker_slot": str(active.get("attacker_slot") or ""),
                        "defender_slot": str(active.get("defender_slot") or ""),
                        "action_id": int(active.get("action_id") or 0),
                        "action_name": str(active.get("action_name") or ""),
                        "blockstun": int(active.get("blockstun") or 0),
                        "hitstun": int(active.get("hitstun") or 0),
                        "hitstop": int(active.get("hitstop") or 0),
                    }
                    for active in self._active.values()
                ],
            }


TIMING_ENGINE = TimingEngine()


__all__ = [
    "OFF_BLOCKSTUN_REMAINING",
    "OFF_HITSTUN_TOTAL",
    "OFF_HITSTUN_REMAINING",
    "OFF_REACTION_TIMER",
    "OFF_ACTIVE_HITSTOP",
    "OFF_PENDING_HITSTOP",
    "LiveTimingState",
    "TimingResult",
    "TimingEngine",
    "TIMING_ENGINE",
]

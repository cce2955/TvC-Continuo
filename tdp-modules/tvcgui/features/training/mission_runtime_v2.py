"""Deterministic realtime Mission Mode runtime.

The legacy MissionManager grew into a frame-snapshot evaluator with several
parallel queues and partial-reset paths.  This module deliberately does less:

* consume the immutable 240 Hz MissionEvent stream in sequence order;
* compile human-authored mission rows into a small set of step kinds;
* make attempt boundaries explicit;
* advance a step from native action/input evidence and confirm damaging moves
  from opponent damage edges;
* restart by moving one event floor, so stale evidence can never leak into the
  next attempt;
* keep goal missions on the same realtime event source.

It owns no files, Dolphin reads, rendering, mission selection, or persistence.
MissionManager remains the adapter for those responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Callable, Iterable, Sequence

from tvcgui.runtime.mission_events import (
    EVENT_ACTION,
    EVENT_BLOCKSTUN_BEGIN,
    EVENT_BLOCKSTUN_END,
    EVENT_COMBO_BEGIN,
    EVENT_COMBO_CHANGE,
    EVENT_COMBO_END,
    EVENT_DAMAGE,
    EVENT_HITSTUN_BEGIN,
    EVENT_HITSTUN_END,
    EVENT_REACTION_BEGIN,
    EVENT_REACTION_END,
    EVENT_RECOVERY_IDLE,
    EVENT_INPUT,
    EVENT_JUMP,
    EVENT_MEGACRASH_BEGIN,
    EVENT_MEGACRASH_END,
    MISSION_REACTION_ACTIONS,
    MISSION_COMBO_KEEPALIVE_STATES,
    MISSION_KNOCKDOWN_ACTIONS,
    MISSION_RECOVERY_BOUNDARY_ACTIONS,
    MISSION_WAKEUP_ACTIONS,
    MISSION_AIR_RECOVERY_ACTIONS,
    MISSION_JUMP_ACTIONS,
    MissionEvent,
)


INPUT_DIRECTION_MASK = 0x0F
INPUT_A = 0x80
INPUT_B = 0x40
INPUT_C = 0x20
INPUT_P = 0x10
INPUT_ATTACK_MASK = INPUT_A | INPUT_B | INPUT_C
# Raw TvC direction bits, not numpad notation. The input nibble encodes:
#   0x04 = 8 (up), 0x05 = 9 (up-forward), 0x06 = 7 (up-back).
# Comparing this nibble to literal 7/8/9 silently misses every real jump.
JUMP_DIRECTION_BITS = frozenset({0x04, 0x05, 0x06})
BAROQUE_MASKS = (INPUT_A | INPUT_P, INPUT_B | INPUT_P, INPUT_C | INPUT_P)

# Explicit non-damaging native state rows. These are observable labels in the
# same 720 Hz action stream as attacks, so a mission can require them directly.
NON_DAMAGE_STATE_LABELS = frozenset({
    "idle", "stand", "standing", "crouch", "crouched", "crouching",
    "jump", "jump forward", "jump back", "landing", "rising",
})

# Native state labels that prove the player actually entered a jump phase.
# Exact ``Jump Cancel`` is stronger evidence than raw Up input: it means TvC
# itself accepted a cancel. Generic jump labels still require the recent Up edge
# and a non-passive source action.
JUMP_STATE_LABELS = frozenset({
    "pre jump", "jump", "jump forward", "jump back",
    "pre super jump", "super jump", "super jump forward", "super jump back",
    "rising",
})
JUMP_CANCEL_LABELS = frozenset({"jump cancel", "jumpcancel"})

# Do not use ``action_id < 0x100`` as a passive-state test. TvC has meaningful
# attack/action IDs below 0x100. Reject only states we actually know are passive.
NON_ATTACK_JUMP_SOURCE_ACTIONS = frozenset({
    1, 2, 3, 6, 7, 8, 9, 10, 11, 13,
    19, 20, 21, 22, 25, 28, 29, 30, 31, 35, 36,
    48, 49, 50, 52, 53,
})

# HP and attacker action edges can be published in either order because the
# realtime sampler visits fighter slots independently.  This is only used to
# pair an already-observed HP edge to an action that lands in the same instant.
PRE_ACTION_DAMAGE_NS = 55_000_000
MASH_INPUT_WINDOW_NS = int(42 * (1_000_000_000 / 60.0))
MASH_ACTION_LATCH_NS = 160_000_000
MASH_REQUIRED_COUNT = 3
JUMP_CONFIRM_WINDOW_NS = 150_000_000
PENDING_ACTION_FALLBACK_NS = int(45 * (1_000_000_000 / 60.0))


class AttemptPhase(str, Enum):
    ARMED = "ARMED"
    RUNNING = "RUNNING"
    CLEAR = "CLEAR"


class StepKind(str, Enum):
    ACTION = "ACTION"
    JUMP_CANCEL = "JUMP_CANCEL"
    BAROQUE = "BAROQUE"
    ASSIST = "ASSIST"


@dataclass(frozen=True, slots=True)
class CompiledStep:
    index: int
    labels: tuple[str, ...]
    display: str
    input_notation: str
    kind: StepKind
    grace_frames: int = 0
    grace_keeps_alive_only: bool = False
    immediate: bool = False
    mash_button: str = ""
    mash_family: str = ""
    delayed_confirm_frames: int = 0

    @property
    def label(self) -> str:
        if self.display:
            return self.display
        return " / ".join(self.labels)


@dataclass(slots=True)
class PendingAction:
    step_index: int
    sequence: int
    timestamp_ns: int
    action_id: int
    labels: tuple[str, ...] = ()
    deadline_ns: int = 0


@dataclass(slots=True)
class RecentDamage:
    sequence: int
    timestamp_ns: int
    damage: int
    consumed: bool = False


@dataclass(slots=True)
class AttemptState:
    attempt_id: int = 1
    phase: AttemptPhase = AttemptPhase.ARMED
    event_floor: int = 0
    progress_index: int = 0
    pending_action: PendingAction | None = None
    combo_live: bool = False
    reaction_live: bool = False
    native_stun_live: bool = False
    special_reaction_live: bool = False
    reaction_seen: bool = False
    # Route continuity is armed only by a confirmed damaging hit. Once armed,
    # the defender's native hitstun/reaction state is the *only* failure clock.
    continuity_started: bool = False
    # Timestamp/sequence of the newest confirmed damaging hit. Recovery/state
    # edges sampled at or before this hit belong to the pre-hit timeline even
    # if a fast-lane flush publishes them later. They may never fail the newly
    # confirmed mission step.
    continuity_last_hit_ns: int = 0
    continuity_last_hit_sequence: int = 0
    continuity_grace_frames: int = 0
    # The first confirmed damaging hit binds continuity to that exact defender.
    # Reserve/assist slots on the same team are never allowed to recover it.
    continuity_defender_slot: str = ""
    grace_deadline_ns: int = 0
    grace_keeps_alive_only: bool = False
    last_event_ns: int = 0
    restart_count: int = 0
    last_restart_sequence: int = 0
    last_restart_reason: str = ""
    restart_pending_reason: str = ""
    restart_pending_sequence: int = 0
    last_advance_sequence: int = 0
    buffered_advances: int = 0
    recent_damage: list[RecentDamage] = field(default_factory=list)
    goal_damage: int = 0
    goal_hits: int = 0
    goal_blockstun_frames: int = 0
    goal_failed: bool = False
    last_completed_action_id: int = 0
    jump_input_sequence: int = 0
    jump_input_ns: int = 0
    # Native jump evidence may arrive before the defender HP edge that confirms
    # the preceding attack, because the owner slot is sampled first. Keep it.
    buffered_jump_event: MissionEvent | None = None
    buffered_jump_exact_cancel: bool = False
    mash_press_ns: dict[str, list[int]] = field(default_factory=lambda: {"A": [], "B": [], "C": []})


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    progress_index: int
    current_step_label: str | None
    cleared: bool
    phase: str
    attempt_id: int
    event_floor: int
    event_cursor: int
    buffered_advances: int
    restart_count: int
    last_restart_reason: str
    # The correct native action can be known before its hit/damage edge arrives.
    # Expose that READY state so the overlay acknowledges the player immediately
    # without falsely confirming a whiff as a completed mission step.
    ready_progress_index: int = 0
    prediction_active: bool = False
    goal_progress_type: str | None = None
    goal_current_frames: int = 0
    goal_needed_frames: int = 0
    goal_timer_active: bool = False
    goal_damage: int = 0
    goal_damage_needed: int = 0
    goal_hits: int = 0
    goal_max_hits: int = 0


LabelMatcher = Callable[[str, Sequence[str]], bool]
ActionLabelResolver = Callable[[MissionEvent], Sequence[str]]
WhiffPolicy = Callable[[Sequence[str], object], bool]
JumpCancelChecker = Callable[[int], bool]
ActionTimeoutResolver = Callable[[int], int]


def _step_values(step: object) -> tuple[tuple[str, ...], str, str, int, bool, bool]:
    if isinstance(step, dict):
        raw = step.get("labels")
        labels = tuple(str(value).strip() for value in (raw or []) if str(value).strip()) if isinstance(raw, list) else ()
        if not labels:
            label = str(step.get("label") or "").strip()
            labels = (label,) if label else ()
        display = str(step.get("display") or step.get("display_label") or "").strip()
        notation = str(step.get("input") or step.get("command") or step.get("notation") or "").strip()
        try:
            grace = max(0, int(step.get("grace", 0) or 0))
        except Exception:
            grace = 0
        pass_step = bool(step.get("pass", False))
        keep_alive_only = bool(step.get("grace_keeps_alive_only", False))
        return labels, display, notation, grace, pass_step, keep_alive_only
    if isinstance(step, list):
        labels = tuple(str(value).strip() for value in step if str(value).strip())
        return labels, "", "", 0, False, False
    label = str(step or "").strip()
    return ((label,) if label else ()), "", "", 0, False, False


def compile_steps(
    raw_steps: Sequence[object],
    *,
    whiff_policy: WhiffPolicy | None = None,
) -> tuple[CompiledStep, ...]:
    """Compile compatibility mission rows into deterministic matcher kinds."""
    compiled: list[CompiledStep] = []
    for index, raw_step in enumerate(raw_steps or ()):  # type: ignore[arg-type]
        labels, display, notation, grace, pass_step, keep_alive_only = _step_values(raw_step)
        text = " ".join((*labels, display, notation)).lower()
        compact = "".join(ch for ch in text.upper() if not ch.isspace())
        if "jump cancel" in text or "7/8/9" in compact:
            kind = StepKind.JUMP_CANCEL
            immediate = True
        elif "baroque cancel" in text:
            kind = StepKind.BAROQUE
            immediate = True
        elif "assist" in text or any(token in compact for token in ("A+P", "B+P", "C+P", "ATK+P")):
            kind = StepKind.ASSIST
            immediate = True
        else:
            kind = StepKind.ACTION
            state_names = {
                " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())
                for value in (*labels, display)
                if str(value or "").strip()
            }
            immediate = bool(pass_step or any(name in NON_DAMAGE_STATE_LABELS for name in state_names))
            if whiff_policy is not None:
                try:
                    immediate = immediate or bool(whiff_policy(labels, raw_step))
                except Exception:
                    pass

        mash_button = ""
        mash_family = ""
        delayed_confirm_frames = 0
        if isinstance(raw_step, dict):
            try:
                delayed_confirm_frames = max(0, int(raw_step.get("delayed_confirm_frames", 0) or 0))
            except Exception:
                delayed_confirm_frames = 0
        normalized_text = " ".join((*labels, display, notation)).lower()
        if any(token in normalized_text for token in ("legs", "lightning legs", "hyakuretsu")):
            mash_family = "legs"
            # Prefer the authored move suffix, then fall back to a repeated
            # input notation such as "C C C".
            for candidate in (*labels, display):
                match = re.search(r"(?:^|\s)([ABC])$", str(candidate or "").strip(), flags=re.IGNORECASE)
                if match:
                    mash_button = match.group(1).upper()
                    break
            if not mash_button:
                notation_buttons = re.findall(r"[ABC]", str(notation or "").upper())
                if notation_buttons and len(set(notation_buttons)) == 1:
                    mash_button = notation_buttons[0]

        compiled.append(
            CompiledStep(
                index=index,
                labels=labels,
                display=display,
                input_notation=notation,
                kind=kind,
                grace_frames=grace,
                grace_keeps_alive_only=keep_alive_only,
                immediate=immediate,
                mash_button=mash_button,
                mash_family=mash_family,
                delayed_confirm_frames=delayed_confirm_frames,
            )
        )
    return tuple(compiled)


class MissionRuntimeV2:
    """Small event-driven state machine for one active Mission Mode route."""

    def __init__(self) -> None:
        self._mission_key: tuple[str, str, str] | None = None
        self._steps: tuple[CompiledStep, ...] = ()
        self._goal: dict = {}
        self._state = AttemptState()
        self._latest_sequence = 0

    @property
    def state(self) -> AttemptState:
        return self._state

    @property
    def mission_key(self) -> tuple[str, str, str] | None:
        return self._mission_key

    def reset_all(self, event_floor: int = 0) -> None:
        self._mission_key = None
        self._steps = ()
        self._goal = {}
        self._latest_sequence = max(0, int(event_floor or 0))
        self._state = AttemptState(event_floor=self._latest_sequence)

    def configure(
        self,
        *,
        slot: str,
        character: str,
        mission_id: str,
        raw_steps: Sequence[object],
        goal: dict | None,
        event_floor: int,
        whiff_policy: WhiffPolicy | None = None,
    ) -> None:
        key = (str(slot or ""), str(character or ""), str(mission_id or ""))
        if key == self._mission_key:
            return
        self._mission_key = key
        self._steps = compile_steps(raw_steps, whiff_policy=whiff_policy)
        self._goal = dict(goal or {})
        floor = max(0, int(event_floor or 0))
        self._latest_sequence = floor
        self._state = AttemptState(event_floor=floor)

    def rearm(self, event_floor: int, *, preserve_attempt_counter: bool = True) -> None:
        old = self._state
        attempt_id = old.attempt_id + 1 if preserve_attempt_counter else 1
        restart_count = old.restart_count if preserve_attempt_counter else 0
        self._state = AttemptState(
            attempt_id=attempt_id,
            event_floor=max(0, int(event_floor or 0)),
            restart_count=restart_count,
        )
        self._latest_sequence = max(self._latest_sequence, int(event_floor or 0))

    @staticmethod
    def _team(slot: str) -> str:
        return "P1" if str(slot).startswith("P1") else "P2" if str(slot).startswith("P2") else ""

    @staticmethod
    def _is_enemy(event: MissionEvent, owner_team: str) -> bool:
        event_team = event.team
        return bool(owner_team and event_team and event_team != owner_team)

    def _current_step(self) -> CompiledStep | None:
        index = int(self._state.progress_index)
        return self._steps[index] if 0 <= index < len(self._steps) else None

    def _grace_active(self, event_ns: int) -> bool:
        deadline = int(self._state.grace_deadline_ns or 0)
        return bool(deadline > 0 and int(event_ns or 0) <= deadline)

    def _advance(self, event: MissionEvent, *, count_buffered: bool = False) -> None:
        """Advance one authored row without changing combo continuity.

        PASS/WHIFF, Jump Cancel, Baroque and other non-hit rows are route
        notation/evidence only. They must never cancel a pending continuity
        failure or start a new grace window. Only a confirmed damaging hit does
        that via ``_confirm_hit_continuity`` below.
        """
        step = self._current_step()
        if step is None:
            return
        self._state.progress_index += 1
        self._state.pending_action = None
        self._state.phase = AttemptPhase.RUNNING
        self._state.last_advance_sequence = int(event.sequence)
        if count_buffered:
            self._state.buffered_advances += 1
        if self._state.progress_index >= len(self._steps):
            self._state.phase = AttemptPhase.CLEAR

    def _confirm_hit_continuity(self, step: CompiledStep, event: MissionEvent) -> None:
        """Arm/resume route continuity from one confirmed damaging hit.

        A hit inside an authored grace window is the only thing that rescues
        that gap. Merely starting the next move, pressing a button, jumping, or
        crossing a PASS/WHIFF row does not.
        """
        state = self._state
        state.continuity_started = True
        if event.slot:
            state.continuity_defender_slot = str(event.slot)
        action_id = int(event.action_id or 0) & 0x7FFF
        if (
            action_id in MISSION_COMBO_KEEPALIVE_STATES
            or action_id in MISSION_KNOCKDOWN_ACTIONS
            or int(event.hitstun_remaining or 0) > 0
            or int(event.untech_remaining or 0) > 0
        ):
            state.native_stun_live = True
            state.reaction_live = True
            state.reaction_seen = True
        state.continuity_last_hit_ns = max(
            int(state.continuity_last_hit_ns or 0),
            int(event.timestamp_ns or 0),
        )
        state.continuity_last_hit_sequence = max(
            int(state.continuity_last_hit_sequence or 0),
            int(event.sequence or 0),
        )
        state.continuity_grace_frames = max(0, int(step.grace_frames or 0))
        state.grace_keeps_alive_only = bool(step.grace_keeps_alive_only)
        state.grace_deadline_ns = 0
        state.restart_pending_reason = ""
        state.restart_pending_sequence = 0
        # Do not manufacture a recovery-armed state just because HP changed.
        # reaction_seen is set only by actual defender reaction evidence
        # (native protected action, HITSTUN_BEGIN, or a damage packet carrying
        # a live stun clock). This prevents a stale pre-hit Idle edge from
        # immediately failing a newly confirmed step.
        if state.native_stun_live or state.special_reaction_live or state.reaction_live:
            state.reaction_seen = True

    def _begin_continuity_break(self, event: MissionEvent, reason: str) -> None:
        """Fail now or start grace when the defender actually recovers.

        Grace begins at the recovery edge, never at the preceding hit. Repeated
        recovery labels in the same transition do not extend the deadline.
        """
        state = self._state
        if not state.continuity_started or not self._route_has_work():
            return
        # Continuity belongs to the fighter that actually took the confirmed
        # mission hit, not every reserve/assist on the opposing team.
        defender_slot = str(state.continuity_defender_slot or "")
        if defender_slot and str(event.slot or "") != defender_slot:
            return
        # A recovery edge cannot end a hit that had not happened yet. Fast
        # action edges can be queued and published after cross-slot DAMAGE even
        # when their sample timestamp is older. Use game-sample time, not event
        # publication order, as the continuity boundary.
        if int(event.timestamp_ns or 0) <= int(state.continuity_last_hit_ns or 0):
            return
        # The route becomes fail-able only after we have actually observed the
        # defender in a protected hit/reaction/knockdown state for this attempt.
        # This is the lock the player expects after the first confirmed hit.
        if not state.reaction_seen:
            return
        if state.restart_pending_reason:
            return
        frames = max(0, int(state.continuity_grace_frames or 0))
        if frames <= 0:
            self._restart(event, reason)
            return
        state.grace_deadline_ns = int(event.timestamp_ns or 0) + int(
            frames * (1_000_000_000 / 60.0)
        )
        state.restart_pending_reason = str(reason)
        state.restart_pending_sequence = int(event.sequence)

    def _restart_at(self, *, sequence: int, timestamp_ns: int, reason: str) -> None:
        previous = self._state
        floor = max(int(previous.event_floor or 0), int(sequence or 0))
        self._state = AttemptState(
            attempt_id=previous.attempt_id + 1,
            event_floor=floor,
            restart_count=previous.restart_count + 1,
            last_restart_sequence=floor,
            last_restart_reason=str(reason),
            last_event_ns=int(timestamp_ns or 0),
        )

    def _restart(self, event: MissionEvent, reason: str) -> None:
        self._restart_at(
            sequence=int(event.sequence),
            timestamp_ns=int(event.timestamp_ns),
            reason=reason,
        )

    def _expire_pending_restart_at(self, timestamp_ns: int, newest_sequence: int) -> None:
        """Finish a scheduled route reset even when the stream goes quiet.

        The combo counter is deliberately *not* a keep-alive signal here. TvC can
        leave it nonzero after the defender has recovered. Native reaction state
        is authoritative; if that has ended and the authored grace has elapsed,
        the route is over regardless of a stale hit counter.
        """
        state = self._state
        if not state.restart_pending_reason or state.restart_pending_sequence <= 0:
            return
        now_ns = int(timestamp_ns or 0)
        if self._grace_active(now_ns):
            return
        if not state.continuity_started or not self._route_has_work():
            state.restart_pending_reason = ""
            state.restart_pending_sequence = 0
            state.grace_deadline_ns = 0
            return
        floor = max(int(state.restart_pending_sequence), int(newest_sequence or 0))
        reason = str(state.restart_pending_reason or "grace_expired")
        self._restart_at(sequence=floor, timestamp_ns=now_ns, reason=reason)

    def _expire_pending_restart_before(self, event: MissionEvent) -> None:
        self._expire_pending_restart_at(int(event.timestamp_ns), int(event.sequence) - 1)

    def _expire_pending_action_at(self, timestamp_ns: int, newest_sequence: int) -> None:
        """No-op by design: READY is invalidated by native action transitions.

        A frame-data timeout must never decide whether a route failed or whether
        a late active frame counted. The next owner ACTION supersedes a pending
        candidate; defender recovery owns route failure.
        """
        _ = timestamp_ns
        _ = newest_sequence

    def _expire_jump_arm_at(self, timestamp_ns: int) -> None:
        input_ns = int(self._state.jump_input_ns or 0)
        if input_ns <= 0:
            return
        if int(timestamp_ns or 0) - input_ns > JUMP_CONFIRM_WINDOW_NS:
            self._state.jump_input_sequence = 0
            self._state.jump_input_ns = 0

    def _route_has_work(self) -> bool:
        return bool(self._state.progress_index > 0 or self._state.pending_action is not None)

    @staticmethod
    def _normalized_action_text(value: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).split())

    @classmethod
    def _is_legs_family_label(cls, value: str) -> bool:
        text = cls._normalized_action_text(value)
        compact = text.replace(" ", "")
        return bool(
            "legs" in text
            or "lightninglegs" in compact
            or "hyakuretsukyaku" in compact
            or "hyakuretsu" in compact
        )

    def _record_mash_edges(self, event: MissionEvent) -> None:
        pressed = int(event.pressed or 0) & 0xFFFF
        now_ns = int(event.timestamp_ns or 0)
        if now_ns <= 0:
            return
        mapping = (("A", INPUT_A), ("B", INPUT_B), ("C", INPUT_C))
        for name, mask in mapping:
            history = self._state.mash_press_ns.setdefault(name, [])
            cutoff = now_ns - MASH_INPUT_WINDOW_NS
            history[:] = [stamp for stamp in history if int(stamp) >= cutoff]
            if pressed & mask:
                history.append(now_ns)
                del history[:-8]

    def _mash_requirement_satisfied(self, step: CompiledStep, event_ns: int) -> bool:
        button = str(step.mash_button or "").upper()
        if not button:
            return False
        now_ns = int(event_ns or 0)
        history = list(self._state.mash_press_ns.get(button, []))
        if now_ns <= 0 or len(history) < MASH_REQUIRED_COUNT:
            return False
        cutoff = now_ns - MASH_INPUT_WINDOW_NS
        recent = [stamp for stamp in history if int(stamp) >= cutoff and int(stamp) <= now_ns]
        if len(recent) < MASH_REQUIRED_COUNT:
            return False
        return now_ns - int(recent[-1]) <= MASH_ACTION_LATCH_NS

    def _match_step_action(
        self,
        event: MissionEvent,
        labels: Sequence[str],
        label_matcher: LabelMatcher,
    ) -> bool:
        step = self._current_step()
        if step is None or step.kind != StepKind.ACTION:
            return False
        expected = list(step.labels)
        if step.input_notation:
            expected.append(step.input_notation)
        if not expected:
            return False
        if any(label_matcher(str(candidate), expected) for candidate in labels if str(candidate).strip()):
            return True

        # Chun-Li's Lightning Legs strengths can share a generic action family.
        # Resolve A/B/C from the direct 240 Hz mash edges, then accept the native
        # Legs action even if the move map has no strength suffix. This is not a
        # history-label heuristic: both the command and action come from the
        # realtime stream.
        if step.mash_family == "legs" and self._mash_requirement_satisfied(step, int(event.timestamp_ns or 0)):
            labeled = [str(candidate) for candidate in labels if str(candidate).strip()]
            if labeled:
                return any(self._is_legs_family_label(candidate) for candidate in labeled)
            # Some shared mash phases are absent from the pretty move map. A
            # correctly timed three-button mash immediately followed by an
            # otherwise-unlabeled authored action is still sufficient to arm
            # the step; damage must still confirm it below.
            return int(event.action_id or 0) >= 0x100
        return False

    def _recent_damage_for_action(self, action: MissionEvent) -> RecentDamage | None:
        for damage in reversed(self._state.recent_damage):
            if damage.consumed:
                continue
            delta = int(action.timestamp_ns) - int(damage.timestamp_ns)
            if 0 <= delta <= PRE_ACTION_DAMAGE_NS:
                return damage
        return None

    def _consume_action(
        self,
        event: MissionEvent,
        labels: Sequence[str],
        label_matcher: LabelMatcher,
        action_timeout_resolver: ActionTimeoutResolver | None,
        *,
        preserve_pending_for_jump: bool = False,
    ) -> None:
        step = self._current_step()
        if step is None or step.kind != StepKind.ACTION:
            return

        # One native action transition ends the previous candidate instance. This
        # replaces fragile wall-clock whiff expiry and prevents a later wrong
        # move's damage from confirming an older expected move.
        pending = self._state.pending_action
        if (
            pending is not None
            and int(event.sequence) > int(pending.sequence)
            and not preserve_pending_for_jump
        ):
            self._state.pending_action = None

        matched = self._match_step_action(event, labels, label_matcher)
        if not matched:
            # Cross-slot sampling can publish DAMAGE a few ms before the owner
            # ACTION edge that caused it. If this action is not the expected row,
            # claim those nearby damage edges as unrelated so the next expected
            # move cannot steal them.
            for damage in reversed(self._state.recent_damage):
                if damage.consumed:
                    continue
                delta = int(event.timestamp_ns) - int(damage.timestamp_ns)
                if delta < 0:
                    continue
                if delta > PRE_ACTION_DAMAGE_NS:
                    break
                damage.consumed = True
            return

        if self._state.phase == AttemptPhase.ARMED:
            self._state.phase = AttemptPhase.RUNNING

        if step.immediate:
            self._state.last_completed_action_id = int(event.action_id)
            self._advance(event, count_buffered=True)
            return

        recent_damage = self._recent_damage_for_action(event)
        if recent_damage is not None:
            recent_damage.consumed = True
            self._confirm_hit_continuity(step, event)
            self._state.last_completed_action_id = int(event.action_id)
            self._advance(event, count_buffered=True)
            return

        timeout_ns = PENDING_ACTION_FALLBACK_NS
        if action_timeout_resolver is not None:
            try:
                resolved = int(action_timeout_resolver(int(event.action_id)) or 0)
                if resolved > 0:
                    timeout_ns = resolved
            except Exception:
                pass
        if int(step.delayed_confirm_frames or 0) > 0:
            timeout_ns = max(
                timeout_ns,
                int(step.delayed_confirm_frames * (1_000_000_000 / 60.0)),
            )
        self._state.pending_action = PendingAction(
            step_index=int(self._state.progress_index),
            sequence=int(event.sequence),
            timestamp_ns=int(event.timestamp_ns),
            action_id=int(event.action_id),
            labels=tuple(str(label) for label in labels if str(label).strip()),
            deadline_ns=int(event.timestamp_ns) + max(1, int(timeout_ns)),
        )

    def _consume_input(self, event: MissionEvent) -> None:
        self._record_mash_edges(event)
        step = self._current_step()
        if step is None:
            return
        held = int(event.held or 0) & 0xFFFF
        pressed = int(event.pressed or 0) & 0xFFFF
        buttons = held | pressed
        direction = held & INPUT_DIRECTION_MASK

        # Keep a tiny cross-step up-input latch. Damage is observed on the
        # defender slot, so sampler ordering can place the player's up edge a
        # fraction before the previous attack's hit-confirm event advances the
        # mission onto its Jump Cancel row. The native jump transition is still
        # required below, and this latch expires after 150 ms.
        if direction in JUMP_DIRECTION_BITS:
            self._state.jump_input_sequence = int(event.sequence)
            self._state.jump_input_ns = int(event.timestamp_ns)

        if step.kind == StepKind.JUMP_CANCEL:
            # Input arms the JC, but does not complete it. The native JUMP
            # transition confirms that TvC actually accepted the cancel.
            return

        matched = False
        if step.kind == StepKind.BAROQUE:
            matched = any((buttons & mask) == mask for mask in BAROQUE_MASKS)
        elif step.kind == StepKind.ASSIST:
            matched = bool((buttons & INPUT_P) and (buttons & INPUT_ATTACK_MASK))

        if matched:
            if self._state.phase == AttemptPhase.ARMED:
                self._state.phase = AttemptPhase.RUNNING
            self._advance(event, count_buffered=True)

    @staticmethod
    def _normalized_action_labels(labels: Sequence[str]) -> set[str]:
        return {
            " ".join(re.sub(r"[^a-z0-9]+", " ", str(label or "").lower()).split())
            for label in labels
            if str(label or "").strip()
        }

    def _buffer_jump_transition(
        self,
        event: MissionEvent,
        labels: Sequence[str] = (),
    ) -> bool:
        """Remember native jump evidence regardless of the current mission row."""
        names = self._normalized_action_labels(labels)
        exact_cancel = bool(names & JUMP_CANCEL_LABELS)
        is_jump = bool(
            exact_cancel
            or names & JUMP_STATE_LABELS
            or (int(event.action_id or 0) & 0x7FFF) in MISSION_JUMP_ACTIONS
        )
        if not is_jump:
            return False
        previous = self._state.buffered_jump_event
        if previous is None or int(event.timestamp_ns or 0) >= int(previous.timestamp_ns or 0):
            self._state.buffered_jump_event = event
            self._state.buffered_jump_exact_cancel = exact_cancel
        return True

    def _try_consume_buffered_jump(
        self,
        jump_cancel_checker: JumpCancelChecker | None,
    ) -> bool:
        step = self._current_step()
        event = self._state.buffered_jump_event
        if step is None or step.kind != StepKind.JUMP_CANCEL or event is None:
            return False

        exact_cancel = bool(self._state.buffered_jump_exact_cancel)
        input_ns = int(self._state.jump_input_ns or 0)
        jump_ns = int(event.timestamp_ns or 0)
        if not exact_cancel:
            if input_ns <= 0:
                return False
            delta_ns = jump_ns - input_ns
            if delta_ns < 0 or delta_ns > JUMP_CONFIRM_WINDOW_NS:
                return False

        prior_action = int(self._state.last_completed_action_id or 0) & 0x7FFF
        source_action = int(getattr(event, "previous_action_id", 0) or 0) & 0x7FFF
        if not exact_cancel and source_action in NON_ATTACK_JUMP_SOURCE_ACTIONS:
            return False
        if not exact_cancel and jump_cancel_checker is not None and prior_action > 0:
            try:
                if not bool(jump_cancel_checker(prior_action)):
                    return False
            except Exception:
                return False

        if self._state.phase == AttemptPhase.ARMED:
            self._state.phase = AttemptPhase.RUNNING
        self._state.jump_input_sequence = 0
        self._state.jump_input_ns = 0
        self._state.buffered_jump_event = None
        self._state.buffered_jump_exact_cancel = False
        self._advance(event, count_buffered=True)
        return True

    def _consume_jump(
        self,
        event: MissionEvent,
        jump_cancel_checker: JumpCancelChecker | None,
    ) -> None:
        self._buffer_jump_transition(event)
        self._try_consume_buffered_jump(jump_cancel_checker)

    def _consume_damage(self, event: MissionEvent) -> None:
        if int(event.damage or 0) <= 0:
            return
        recent = RecentDamage(
            sequence=int(event.sequence),
            timestamp_ns=int(event.timestamp_ns),
            damage=int(event.damage),
        )
        self._state.recent_damage.append(recent)
        del self._state.recent_damage[:-12]

        # A real hit inside grace reconnects the defender before the matching
        # owner ACTION edge is guaranteed to arrive (slots are sampled
        # independently). Rescue continuity immediately; move identity still
        # controls whether the authored step advances below.
        if self._state.continuity_started and self._state.restart_pending_reason:
            if self._grace_active(int(event.timestamp_ns or 0)):
                self._state.restart_pending_reason = ""
                self._state.restart_pending_sequence = 0
                self._state.grace_deadline_ns = 0

        if int(event.hitstun_remaining or 0) > 0 or int(event.untech_remaining or 0) > 0:
            self._state.native_stun_live = True
            self._state.reaction_live = True
            self._state.reaction_seen = True

        pending = self._state.pending_action
        if pending is not None and pending.step_index == self._state.progress_index:
            step = self._current_step()
            if step is None:
                return
            recent.consumed = True
            self._confirm_hit_continuity(step, event)
            self._state.last_completed_action_id = int(pending.action_id)
            self._advance(event, count_buffered=True)

    def _consume_route_event(
        self,
        event: MissionEvent,
        *,
        owner_slot: str,
        owner_team: str,
        label_matcher: LabelMatcher,
        action_label_resolver: ActionLabelResolver,
        jump_cancel_checker: JumpCancelChecker | None,
        action_timeout_resolver: ActionTimeoutResolver | None,
    ) -> None:
        state = self._state
        is_owner = str(event.slot) == str(owner_slot)
        is_enemy = self._is_enemy(event, owner_team)

        if event.kind == EVENT_INPUT and is_owner:
            self._consume_input(event)
            return

        if event.kind == EVENT_JUMP and is_owner:
            self._consume_jump(event, jump_cancel_checker)
            return

        if event.kind == EVENT_ACTION:
            if is_owner:
                labels = tuple(action_label_resolver(event) or ())
                is_jump_transition = self._buffer_jump_transition(event, labels)
                self._consume_action(
                    event,
                    labels,
                    label_matcher,
                    action_timeout_resolver,
                    preserve_pending_for_jump=is_jump_transition,
                )
                return
            if is_enemy:
                # The 720 Hz ACTION lane is excellent evidence for labels, but
                # it is not allowed to reset a combo. It is intentionally a
                # partial packet and may arrive out of order with the complete
                # combat snapshot. Only the exact defender locked by DAMAGE may
                # update continuity, and only a full HITSTUN_END/RECOVERY_IDLE
                # edge may break it below.
                defender_slot = str(state.continuity_defender_slot or "")
                if defender_slot and str(event.slot or "") != defender_slot:
                    return
                if not defender_slot:
                    return
                action_id = int(event.action_id or 0) & 0x7FFF
                if action_id in MISSION_COMBO_KEEPALIVE_STATES:
                    state.native_stun_live = True
                    state.special_reaction_live = True
                    state.reaction_live = True
                    state.reaction_seen = True
                    return
                if action_id in MISSION_KNOCKDOWN_ACTIONS:
                    state.native_stun_live = True
                    state.reaction_live = True
                    state.reaction_seen = True
                    return
                if action_id in MISSION_REACTION_ACTIONS:
                    state.special_reaction_live = True
                    state.reaction_live = True
                    state.reaction_seen = True
                    return
                return

        if event.kind == EVENT_DAMAGE and is_enemy:
            defender_slot = str(state.continuity_defender_slot or "")
            if defender_slot and str(event.slot or "") != defender_slot:
                return
            # Full combat packets carry the native point flag. Do not let
            # collateral damage to an assist/reserve confirm the mission route.
            if not defender_slot and event.point_active is False:
                return
            self._consume_damage(event)
            return

        if event.kind == EVENT_COMBO_BEGIN:
            state.combo_live = True
            return
        if event.kind == EVENT_COMBO_CHANGE:
            state.combo_live = int(event.combo_count or 0) > 0
            return
        if event.kind == EVENT_COMBO_END:
            # Combo count is display/goal telemetry only. It is intentionally
            # forbidden from resetting a route; native defender reaction owns
            # continuity.
            state.combo_live = False
            return

        if is_enemy and state.continuity_defender_slot and str(event.slot or "") != str(state.continuity_defender_slot):
            return
        if is_enemy and not state.continuity_defender_slot:
            # Before the first confirmed hit there is no defender to recover.
            return

        if is_enemy and event.kind in (EVENT_HITSTUN_BEGIN, EVENT_MEGACRASH_BEGIN):
            state.native_stun_live = True
            state.reaction_live = True
            state.reaction_seen = True
            return
        if is_enemy and event.kind == EVENT_REACTION_BEGIN:
            state.special_reaction_live = True
            state.reaction_live = True
            state.reaction_seen = True
            return
        if is_enemy and event.kind in (EVENT_HITSTUN_END, EVENT_MEGACRASH_END):
            action_id = int(event.action_id or 0) & 0x7FFF
            # This edge comes only from the complete 240 Hz combat packet and is
            # computed from the literal primary action ID + literal stun clocks.
            # Protected hit/knockdown IDs therefore cannot manufacture an end.
            if action_id in MISSION_COMBO_KEEPALIVE_STATES or action_id in MISSION_KNOCKDOWN_ACTIONS:
                state.native_stun_live = True
                state.reaction_live = True
                state.reaction_seen = True
                return
            state.native_stun_live = False
            state.special_reaction_live = False
            state.reaction_live = False
            if action_id in MISSION_WAKEUP_ACTIONS:
                reason = "wakeup"
            elif action_id in MISSION_AIR_RECOVERY_ACTIONS:
                reason = "air_recovery"
            elif action_id in MISSION_RECOVERY_BOUNDARY_ACTIONS:
                reason = "recovery_idle"
            else:
                reason = "hitstun_end"
            self._begin_continuity_break(event, reason)
            return
        if is_enemy and event.kind == EVENT_REACTION_END:
            action_id = int(event.action_id or 0) & 0x7FFF
            if action_id in MISSION_COMBO_KEEPALIVE_STATES or action_id in MISSION_KNOCKDOWN_ACTIONS:
                state.special_reaction_live = True
                state.reaction_live = True
                state.reaction_seen = True
                return
            # Same rule as HITSTUN_END: update telemetry, never fail the route
            # from a timer-derived end edge.
            state.special_reaction_live = False
            state.reaction_live = bool(state.native_stun_live)
            return

        if is_enemy and event.kind == EVENT_RECOVERY_IDLE:
            # Idle is explicit observable proof that the defender disengaged.
            # It is a failure boundary only after the first confirmed route hit.
            state.native_stun_live = False
            state.special_reaction_live = False
            state.reaction_live = False
            self._begin_continuity_break(event, "recovery_idle")
            return

    def _consume_goal_event(self, event: MissionEvent, *, owner_team: str) -> None:
        goal_type = str(self._goal.get("type") or "").strip().lower()
        is_enemy = self._is_enemy(event, owner_team)
        if goal_type == "state_duration":
            if not is_enemy:
                return
            if event.kind == EVENT_BLOCKSTUN_BEGIN:
                self._state.goal_blockstun_frames = max(
                    self._state.goal_blockstun_frames,
                    int(event.blockstun_remaining or 0),
                )
            elif event.kind == EVENT_BLOCKSTUN_END:
                # Preserve the max duration reached for the result display.
                pass
            return

        if goal_type in {"combo_damage", "damage_under_hits"}:
            if event.kind == EVENT_COMBO_BEGIN:
                self._state.combo_live = True
                self._state.goal_damage = 0
                self._state.goal_hits = max(0, int(event.combo_count or 0))
                self._state.goal_failed = False
            elif event.kind == EVENT_COMBO_CHANGE:
                self._state.combo_live = int(event.combo_count or 0) > 0
                self._state.goal_hits = max(self._state.goal_hits, int(event.combo_count or 0))
            elif event.kind == EVENT_DAMAGE and is_enemy:
                self._state.goal_damage += max(0, int(event.damage or 0))
                if int(event.combo_count or 0) > 0:
                    self._state.goal_hits = max(self._state.goal_hits, int(event.combo_count or 0))
                elif self._state.goal_hits <= 0:
                    self._state.goal_hits += 1
            elif event.kind == EVENT_COMBO_END:
                self._state.combo_live = False

    def _goal_cleared(self) -> bool:
        goal_type = str(self._goal.get("type") or "").strip().lower()
        if goal_type == "state_duration":
            needed = max(0, int(self._goal.get("frames", 0) or 0))
            return bool(needed > 0 and self._state.goal_blockstun_frames >= needed)
        if goal_type == "combo_damage":
            needed = max(0, int(self._goal.get("damage", 0) or 0))
            return bool(needed > 0 and self._state.goal_damage >= needed)
        if goal_type == "damage_under_hits":
            needed = max(0, int(self._goal.get("damage", 0) or 0))
            max_hits = max(0, int(self._goal.get("max_hits", 0) or 0))
            if max_hits > 0 and self._state.goal_hits > max_hits:
                self._state.goal_failed = True
            return bool(
                needed > 0
                and self._state.goal_damage >= needed
                and (max_hits <= 0 or self._state.goal_hits <= max_hits)
                and not self._state.goal_failed
            )
        return False

    def _goal_result(self, *, cursor: int) -> RuntimeResult:
        goal_type = str(self._goal.get("type") or "").strip().lower() or None
        cleared = self._goal_cleared()
        if goal_type == "state_duration":
            needed = max(0, int(self._goal.get("frames", 0) or 0))
            current = int(self._state.goal_blockstun_frames)
            label = f"{current}/{needed} frames"
            return RuntimeResult(
                progress_index=1 if cleared else 0,
                current_step_label=label,
                cleared=cleared,
                phase=AttemptPhase.CLEAR.value if cleared else self._state.phase.value,
                attempt_id=self._state.attempt_id,
                event_floor=self._state.event_floor,
                event_cursor=cursor,
                buffered_advances=self._state.buffered_advances,
                restart_count=self._state.restart_count,
                last_restart_reason=self._state.last_restart_reason,
                goal_progress_type=goal_type,
                goal_current_frames=current,
                goal_needed_frames=needed,
                goal_timer_active=current > 0 and not cleared,
            )
        needed_damage = max(0, int(self._goal.get("damage", 0) or 0))
        max_hits = max(0, int(self._goal.get("max_hits", 0) or 0))
        label = (
            f"{self._state.goal_damage}/{needed_damage} damage, {self._state.goal_hits}/{max_hits} hits"
            if goal_type == "damage_under_hits"
            else f"{self._state.goal_damage}/{needed_damage} combo damage"
        )
        return RuntimeResult(
            progress_index=1 if cleared else 0,
            current_step_label=label,
            cleared=cleared,
            phase=AttemptPhase.CLEAR.value if cleared else self._state.phase.value,
            attempt_id=self._state.attempt_id,
            event_floor=self._state.event_floor,
            event_cursor=cursor,
            buffered_advances=self._state.buffered_advances,
            restart_count=self._state.restart_count,
            last_restart_reason=self._state.last_restart_reason,
            goal_progress_type=goal_type,
            goal_damage=self._state.goal_damage,
            goal_damage_needed=needed_damage,
            goal_hits=self._state.goal_hits,
            goal_max_hits=max_hits,
        )

    def consume(
        self,
        events: Iterable[MissionEvent],
        *,
        owner_slot: str,
        label_matcher: LabelMatcher,
        action_label_resolver: ActionLabelResolver,
        jump_cancel_checker: JumpCancelChecker | None = None,
        action_timeout_resolver: ActionTimeoutResolver | None = None,
        newest_sequence: int = 0,
    ) -> RuntimeResult:
        owner_team = self._team(owner_slot)
        ordered = sorted(
            (
                event for event in events
                if isinstance(event, MissionEvent)
                and int(event.sequence) > int(self._state.event_floor)
            ),
            key=lambda event: int(event.sequence),
        )

        for event in ordered:
            # A reset within this same batch advances event_floor. Never let an
            # older event from the abandoned attempt donate evidence afterward.
            if int(event.sequence) <= int(self._state.event_floor):
                continue
            self._expire_pending_restart_before(event)
            self._expire_pending_action_at(int(event.timestamp_ns), int(event.sequence) - 1)
            self._expire_jump_arm_at(int(event.timestamp_ns))
            if int(event.sequence) <= int(self._state.event_floor):
                continue
            self._state.last_event_ns = max(self._state.last_event_ns, int(event.timestamp_ns or 0))
            self._latest_sequence = max(self._latest_sequence, int(event.sequence))
            if self._goal:
                self._consume_goal_event(event, owner_team=owner_team)
                if self._goal_cleared():
                    self._state.phase = AttemptPhase.CLEAR
                    break
            else:
                self._consume_route_event(
                    event,
                    owner_slot=owner_slot,
                    owner_team=owner_team,
                    label_matcher=label_matcher,
                    action_label_resolver=action_label_resolver,
                    jump_cancel_checker=jump_cancel_checker,
                    action_timeout_resolver=action_timeout_resolver,
                )
                # P1 jump evidence can arrive before P2 DAMAGE confirms the
                # preceding attack. Re-check after every event so the DAMAGE
                # edge can advance the attack and immediately consume that JC.
                self._try_consume_buffered_jump(jump_cancel_checker)
                # Do NOT stop consuming at CLEAR. A completed route remains
                # latched until the defender actually reaches a recovery
                # boundary (or authored grace expires). Stopping here used to
                # discard wakeup/Idle events later in the same provider batch.

        cursor = max(self._latest_sequence, int(newest_sequence or 0))
        return self._result(cursor=cursor)

    def _result(self, *, cursor: int) -> RuntimeResult:
        if self._goal:
            return self._goal_result(cursor=cursor)

        step = self._current_step()
        confirmed_progress = int(self._state.progress_index)
        pending = self._state.pending_action
        prediction_active = bool(
            pending is not None
            and int(pending.step_index) == confirmed_progress
            and confirmed_progress < len(self._steps)
        )
        ready_progress = min(
            len(self._steps),
            confirmed_progress + (1 if prediction_active else 0),
        )
        return RuntimeResult(
            progress_index=confirmed_progress,
            current_step_label=step.label if step is not None else None,
            cleared=self._state.phase == AttemptPhase.CLEAR,
            phase=self._state.phase.value,
            attempt_id=int(self._state.attempt_id),
            event_floor=int(self._state.event_floor),
            event_cursor=int(cursor),
            buffered_advances=int(self._state.buffered_advances),
            restart_count=int(self._state.restart_count),
            last_restart_reason=str(self._state.last_restart_reason),
            ready_progress_index=ready_progress,
            prediction_active=prediction_active,
        )

    def tick(self, *, now_ns: int, newest_sequence: int = 0) -> RuntimeResult:
        """Advance wall-clock-only mission deadlines with no new game event.

        This is essential for recovery-edge grace and READY expiry. Neutral gameplay can
        produce no event at all, so tying deadlines to ``consume()`` makes the
        next player input accidentally become the reset trigger.
        """
        now = max(0, int(now_ns or 0))
        newest = max(self._latest_sequence, int(newest_sequence or 0))
        self._latest_sequence = newest
        if not self._goal:
            # Recovery grace must keep ticking even after the final authored
            # step is CLEAR. CLEAR is a display latch, not immunity from the
            # defender continuity boundary.
            self._expire_pending_restart_at(now, newest)
            if self._state.phase != AttemptPhase.CLEAR:
                self._expire_pending_action_at(now, newest)
                self._expire_jump_arm_at(now)
        self._state.last_event_ns = max(int(self._state.last_event_ns or 0), now)
        return self._result(cursor=newest)

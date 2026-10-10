"""High-frequency immutable event stream for Mission Mode.

The Dolphin sampler publishes small packets here. Mission Mode consumes the
resulting ordered events using independent cursors. The producer performs no
JSON or disk I/O and never calls MissionManager.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
import time
from typing import Callable, Iterable


EVENT_INPUT = "INPUT"
EVENT_ACTION = "ACTION"
EVENT_DAMAGE = "DAMAGE"
EVENT_HITSTUN_BEGIN = "HITSTUN_BEGIN"
EVENT_HITSTUN_END = "HITSTUN_END"
EVENT_REACTION_BEGIN = "REACTION_BEGIN"
EVENT_REACTION_END = "REACTION_END"
EVENT_BLOCKSTUN_BEGIN = "BLOCKSTUN_BEGIN"
EVENT_BLOCKSTUN_END = "BLOCKSTUN_END"
EVENT_COMBO_BEGIN = "COMBO_BEGIN"
EVENT_COMBO_CHANGE = "COMBO_CHANGE"
EVENT_COMBO_END = "COMBO_END"
EVENT_MEGACRASH_BEGIN = "MEGACRASH_BEGIN"
EVENT_MEGACRASH_END = "MEGACRASH_END"
EVENT_JUMP = "JUMP"
EVENT_RECOVERY_IDLE = "RECOVERY_IDLE"

from tvcgui.runtime.mission_reaction_policy import (
    MISSION_COMBO_KEEPALIVE_STATES,
    MISSION_REACTION_ACTIONS,
    MISSION_KNOCKDOWN_ACTIONS,
    MISSION_WAKEUP_ACTIONS,
    MISSION_AIR_RECOVERY_ACTIONS,
    MISSION_IDLE_ACTIONS,
    MISSION_RECOVERY_BOUNDARY_ACTIONS,
    MISSION_MEGACRASH_STATES,
)

MISSION_MEGACRASH_ACTIONS = MISSION_MEGACRASH_STATES

# Native passive states used when the game accepts a normal/super jump.
# JUMP remains a dedicated semantic event for cancel validation, while ACTION
# now also preserves labeled low-ID state transitions for mission evidence and
# diagnostics.
MISSION_JUMP_ACTIONS = frozenset({19, 20, 21, 22, 28, 29, 31})


@dataclass(frozen=True, slots=True)
class MissionEvent:
    """One immutable observation from the realtime sampler."""

    sequence: int
    sample_sequence: int
    timestamp_ns: int
    kind: str
    slot: str
    fighter_base: int = 0
    char_id: int = 0
    action_id: int = 0
    previous_action_id: int = 0
    action_frame: int = 0
    held: int = 0
    pressed: int = 0
    released: int = 0
    current_hp: int = 0
    damage: int = 0
    hitstun_remaining: int = 0
    untech_remaining: int = 0
    blockstun_remaining: int = 0
    reaction_timer_remaining: int = 0
    combo_count: int = 0
    point_active: bool | None = None

    @property
    def team(self) -> str:
        return "P1" if self.slot.startswith("P1") else "P2" if self.slot.startswith("P2") else ""

    def as_packet(self) -> dict:
        """Compatibility packet for legacy notation helpers during migration."""
        return {
            "seq": self.sequence,
            "sample_seq": self.sample_sequence,
            "sample_ns": self.timestamp_ns,
            "mission_event_kind": self.kind,
            "slot": self.slot,
            "base": self.fighter_base,
            "char_id": self.char_id,
            "action_id": self.action_id,
            "previous_action_id": self.previous_action_id,
            "action_frame": self.action_frame,
            "held": self.held,
            "pressed": self.pressed if self.kind == EVENT_INPUT else 0,
            "released": self.released if self.kind == EVENT_INPUT else 0,
            "current_hp": self.current_hp,
            "damage": self.damage,
            "hitstun_remaining": self.hitstun_remaining,
            "untech_remaining": self.untech_remaining,
            "blockstun_remaining": self.blockstun_remaining,
            "reaction_timer_remaining": self.reaction_timer_remaining,
            "combo_count": self.combo_count,
            "point_active": self.point_active,
        }


@dataclass(slots=True)
class _SlotState:
    fighter_base: int = 0
    char_id: int = 0
    action_id: int = 0
    action_frame: int = 0
    combat_sample_ns: int = 0
    held: int = 0
    current_hp: int = 0
    hitstun: bool = False
    blockstun: bool = False
    special_reaction: bool = False
    megacrash: bool = False
    recovery_idle: bool = False
    point_active: bool = False
    hitstun_remaining: int = 0
    untech_remaining: int = 0
    blockstun_remaining: int = 0
    reaction_timer_remaining: int = 0


class MissionEventStream:
    """Fuse realtime input/combat and action-edge lanes into one ordered stream.

    Mission Mode receives two sampler feeds with different contracts:

    * the 240 Hz combat lane is a complete fighter snapshot and exclusively owns
      held/pressed input, HP, stun clocks, reaction state, and combo state;
    * the 720 Hz action lane is intentionally partial and exclusively reports
      native action-instance edges.

    Fast action packets are buffered briefly and committed at the next combat
    sample.  This gives input edges deterministic ordering before the action they
    caused, while preserving one-frame native action transitions.  A tiny timeout
    flush prevents an action from waiting forever if the combat lane stalls.
    """

    # Two normal 240 Hz samples are ~8.3 ms apart. Waiting 10 ms is enough to
    # order an action behind its complete input snapshot without adding visible
    # latency, and still lets the fast lane survive a delayed combat sample.
    _FAST_EDGE_FLUSH_NS = 10_000_000
    _ACTION_DEDUPE_NS = 8_000_000
    _FAST_QUEUE_LIMIT = 64

    def __init__(self, capacity: int = 4096) -> None:
        self._capacity = max(256, int(capacity))
        self._events: deque[MissionEvent] = deque(maxlen=self._capacity)
        self._slot_state: dict[str, _SlotState] = {}
        self._fast_edges: dict[str, deque[dict]] = {}
        self._last_action_emit: dict[str, tuple[int, int]] = {}
        self._sequence = 0
        self._combo_count: int | None = None
        self._lock = threading.RLock()

    @property
    def latest_sequence(self) -> int:
        with self._lock:
            self._flush_expired_fast_edges_locked(time.monotonic_ns())
            return int(self._sequence)

    def _append(
        self,
        *,
        kind: str,
        slot: str,
        packet: dict,
        damage: int = 0,
        previous_action_id: int = 0,
    ) -> MissionEvent:
        self._sequence += 1
        event = MissionEvent(
            sequence=self._sequence,
            sample_sequence=int(packet.get("seq", 0) or 0),
            timestamp_ns=int(packet.get("sample_ns", 0) or time.monotonic_ns()),
            kind=str(kind),
            slot=str(slot),
            fighter_base=int(packet.get("base", 0) or 0),
            char_id=int(packet.get("char_id", 0) or 0),
            action_id=int(packet.get("action_id", 0) or 0) & 0x7FFF,
            previous_action_id=int(previous_action_id or packet.get("previous_action_id", 0) or 0) & 0x7FFF,
            action_frame=max(0, int(packet.get("action_frame", 0) or 0)),
            held=int(packet.get("held", 0) or 0) & 0xFFFF,
            pressed=int(packet.get("pressed", 0) or 0) & 0xFFFF,
            released=int(packet.get("released", 0) or 0) & 0xFFFF,
            current_hp=int(packet.get("current_hp", 0) or 0),
            damage=max(0, int(damage or packet.get("damage", 0) or 0)),
            hitstun_remaining=max(0, int(packet.get("hitstun_remaining", 0) or 0)),
            untech_remaining=max(0, int(packet.get("untech_remaining", 0) or 0)),
            blockstun_remaining=max(0, int(packet.get("blockstun_remaining", 0) or 0)),
            reaction_timer_remaining=max(0, int(packet.get("reaction_timer_remaining", 0) or 0)),
            combo_count=max(0, int(packet.get("combo_count", 0) or 0)),
            point_active=(bool(packet.get("point_active")) if "point_active" in packet else None),
        )
        self._events.append(event)
        return event

    @staticmethod
    def _packet_hitstun(packet: dict) -> bool:
        """Return Mission Mode's authoritative vulnerable/reaction state.

        Action phase outranks residual timers. Knockdown is still hitstun for
        route continuity; wakeup/air-recovery is not, even if +0x1210/+0x1220
        has not reached zero on that exact sampler tick yet.
        """
        action_id = int(packet.get("action_id", 0) or 0) & 0x7FFF
        if action_id in MISSION_RECOVERY_BOUNDARY_ACTIONS:
            return False
        # The historical Mission Mode no-reset table is authoritative.  Never
        # let a transient scalar timer drop override a verified native reaction
        # state that the old runtime explicitly protected.
        if action_id in MISSION_COMBO_KEEPALIVE_STATES:
            return True
        if action_id in MISSION_KNOCKDOWN_ACTIONS:
            return True
        return bool(
            int(packet.get("untech_remaining", 0) or 0) > 0
            or int(packet.get("hitstun_remaining", 0) or 0) > 0
        )

    @staticmethod
    def _packet_recovery_idle(packet: dict) -> bool:
        action_id = int(packet.get("action_id", 0) or 0) & 0x7FFF
        if action_id not in MISSION_IDLE_ACTIONS:
            return False
        if MissionEventStream._packet_hitstun(packet):
            return False
        if max(0, int(packet.get("blockstun_remaining", 0) or 0)) > 0:
            return False
        if MissionEventStream._packet_special_reaction(packet):
            return False
        if action_id in MISSION_MEGACRASH_ACTIONS:
            return False
        return True

    @staticmethod
    def _packet_special_reaction(packet: dict) -> bool:
        action_id = int(packet.get("action_id", 0) or 0) & 0x7FFF
        # Recovery action identity is authoritative. A stale reaction timer must
        # never turn Wakeup/Get Up/Air Recovery back into combo continuity.
        if action_id in MISSION_RECOVERY_BOUNDARY_ACTIONS:
            return False
        if action_id in MISSION_COMBO_KEEPALIVE_STATES:
            return True
        timer = max(0, int(packet.get("reaction_timer_remaining", 0) or 0))
        native_stun = MissionEventStream._packet_hitstun(packet)
        return bool(timer > 0 or (not native_stun and action_id in MISSION_REACTION_ACTIONS))

    @staticmethod
    def _state_packet(state: _SlotState, edge: dict) -> dict:
        """Build an action event packet without inventing absent combat fields."""
        return {
            "seq": int(edge.get("seq", 0) or 0),
            "sample_ns": int(edge.get("sample_ns", 0) or time.monotonic_ns()),
            "base": int(edge.get("base", 0) or state.fighter_base),
            "char_id": int(edge.get("char_id", 0) or state.char_id),
            "action_id": int(edge.get("action_id", 0) or 0) & 0x7FFF,
            "action_frame": max(0, int(edge.get("action_frame", 0) or 0)),
            "held": int(state.held),
            "pressed": 0,
            "released": 0,
            "current_hp": int(state.current_hp),
            # Fast action packets are partial, but the event stream already owns
            # the latest complete combat snapshot. Carry those exact live fields
            # forward instead of turning them into synthetic zeroes.
            "hitstun_remaining": int(state.hitstun_remaining),
            "untech_remaining": int(state.untech_remaining),
            "blockstun_remaining": int(state.blockstun_remaining),
            "reaction_timer_remaining": int(state.reaction_timer_remaining),
            "point_active": bool(state.point_active),
        }

    def _emit_action_instance_locked(
        self,
        slot: str,
        edge: dict,
        state: _SlotState,
        *,
        update_state: bool = True,
    ) -> None:
        action_id = int(edge.get("action_id", 0) or 0) & 0x7FFF
        action_frame = max(0, int(edge.get("action_frame", 0) or 0))
        sample_ns = int(edge.get("sample_ns", 0) or time.monotonic_ns())
        prior_action = int(state.action_id)

        # Different sampler lanes can observe the same new action a few
        # milliseconds apart. Suppress only same-ID near-duplicates; a genuine
        # same-ID repeat cannot begin inside this sub-frame window.
        last_emit = self._last_action_emit.get(slot)
        duplicate = bool(
            last_emit
            and int(last_emit[0]) == action_id
            and abs(sample_ns - int(last_emit[1])) <= self._ACTION_DEDUPE_NS
        )

        packet = self._state_packet(state, edge)
        if action_id > 0 and not duplicate:
            self._append(
                kind=EVENT_ACTION,
                slot=slot,
                packet=packet,
                previous_action_id=prior_action,
            )
            self._last_action_emit[slot] = (action_id, sample_ns)

        if (
            action_id in MISSION_JUMP_ACTIONS
            and prior_action not in MISSION_JUMP_ACTIONS
            and not duplicate
        ):
            self._append(
                kind=EVENT_JUMP,
                slot=slot,
                packet=packet,
                previous_action_id=prior_action,
            )

        # A timeout flush can deliver a fast edge after a newer full combat
        # sample has already committed. The event is still useful evidence, but
        # it must never roll the authoritative current action backwards and
        # provoke a duplicate fallback transition on the next combat sample.
        if update_state:
            state.fighter_base = int(edge.get("base", 0) or state.fighter_base)
            state.char_id = int(edge.get("char_id", 0) or state.char_id)
            state.action_id = action_id
            state.action_frame = action_frame

    def _queue_fast_action_locked(self, slot: str, packet: dict) -> None:
        if bool(packet.get("move_history_reset", False)):
            self._fast_edges.pop(slot, None)
            self._last_action_emit.pop(slot, None)
            # The fast lane may initialize a few milliseconds after the full
            # combat lane. Do not let that partial-lane reset erase valid HP,
            # input, or stun baselines for the *same* fighter. Only discard the
            # complete snapshot when the fighter base genuinely changed.
            state = self._slot_state.get(slot)
            reset_base = int(packet.get("base", 0) or 0)
            if (
                state is not None
                and reset_base
                and state.fighter_base
                and reset_base != state.fighter_base
            ):
                self._slot_state.pop(slot, None)
            return
        if not bool(packet.get("move_instance", False)):
            return

        queue = self._fast_edges.setdefault(slot, deque())
        queue.append(dict(packet))
        while len(queue) > self._FAST_QUEUE_LIMIT:
            queue.popleft()

    def _flush_fast_edges_through_locked(self, slot: str, through_ns: int) -> None:
        state = self._slot_state.get(slot)
        queue = self._fast_edges.get(slot)
        if state is None or not queue:
            return

        ready: list[dict] = []
        while queue:
            edge = queue[0]
            edge_ns = int(edge.get("sample_ns", 0) or 0)
            if edge_ns > int(through_ns):
                break
            queue.popleft()
            edge_base = int(edge.get("base", 0) or 0)
            if edge_base and state.fighter_base and edge_base != state.fighter_base:
                continue
            ready.append(edge)
        if not queue:
            self._fast_edges.pop(slot, None)

        ready.sort(key=lambda item: (int(item.get("sample_ns", 0) or 0), int(item.get("seq", 0) or 0)))
        for edge in ready:
            edge_ns = int(edge.get("sample_ns", 0) or 0)
            self._emit_action_instance_locked(
                slot,
                edge,
                state,
                update_state=not state.combat_sample_ns or edge_ns > state.combat_sample_ns,
            )

    def _flush_expired_fast_edges_locked(self, now_ns: int) -> None:
        cutoff = int(now_ns) - self._FAST_EDGE_FLUSH_NS
        for slot in tuple(self._fast_edges):
            self._flush_fast_edges_through_locked(slot, cutoff)

    def publish_sample(self, slot_label: str, packet: dict) -> None:
        """Translate one sampler packet into deterministic ordered mission events."""
        if not isinstance(packet, dict):
            return
        slot = str(slot_label or packet.get("slot") or "")
        if not slot:
            return

        with self._lock:
            if bool(packet.get("fast_action_edge", False)):
                self._queue_fast_action_locked(slot, packet)
                return

            packet_ns = int(packet.get("sample_ns", 0) or time.monotonic_ns())
            previous = self._slot_state.get(slot)
            packet_base = int(packet.get("base", 0) or 0)
            if (
                previous is not None
                and packet_base
                and previous.fighter_base
                and packet_base != previous.fighter_base
            ):
                # A full combat snapshot is authoritative for fighter identity.
                # Establish fresh HP/stun/input baselines instead of comparing a
                # newly mapped fighter against the previous occupant of the slot.
                self._fast_edges.pop(slot, None)
                self._last_action_emit.pop(slot, None)
                self._slot_state.pop(slot, None)
                previous = None
            action_id = int(packet.get("action_id", 0) or 0) & 0x7FFF
            action_frame = max(0, int(packet.get("action_frame", 0) or 0))
            held = int(packet.get("held", 0) or 0) & 0xFFFF
            pressed = int(packet.get("pressed", 0) or 0) & 0xFFFF
            released = int(packet.get("released", 0) or 0) & 0xFFFF
            hp = int(packet.get("current_hp", 0) or 0)
            hitstun = self._packet_hitstun(packet)
            blockstun = max(0, int(packet.get("blockstun_remaining", 0) or 0)) > 0
            special_reaction = self._packet_special_reaction(packet)
            megacrash = action_id in MISSION_MEGACRASH_ACTIONS
            recovery_idle = self._packet_recovery_idle(packet)

            if previous is not None and previous.combat_sample_ns and packet_ns <= previous.combat_sample_ns:
                # The complete combat lane is single-threaded, so this should
                # only happen after a slot reset/race. Never replay old input or
                # HP evidence into a newer attempt.
                return

            if previous is None:
                state = _SlotState(
                    fighter_base=int(packet.get("base", 0) or 0),
                    char_id=int(packet.get("char_id", 0) or 0),
                    action_id=action_id,
                    action_frame=action_frame,
                    combat_sample_ns=packet_ns,
                    held=held,
                    current_hp=hp,
                    hitstun=hitstun,
                    blockstun=blockstun,
                    special_reaction=special_reaction,
                    megacrash=megacrash,
                    recovery_idle=recovery_idle,
                    point_active=bool(packet.get("point_active", False)),
                    hitstun_remaining=max(0, int(packet.get("hitstun_remaining", 0) or 0)),
                    untech_remaining=max(0, int(packet.get("untech_remaining", 0) or 0)),
                    blockstun_remaining=max(0, int(packet.get("blockstun_remaining", 0) or 0)),
                    reaction_timer_remaining=max(0, int(packet.get("reaction_timer_remaining", 0) or 0)),
                )
                self._slot_state[slot] = state
                if pressed or released:
                    self._append(kind=EVENT_INPUT, slot=slot, packet=packet)
                # Flush any action edge that arrived before the first complete
                # sample. INPUT is deliberately emitted first.
                self._flush_fast_edges_through_locked(slot, packet_ns)
                state = self._slot_state[slot]
                # Fallback when the complete sample is newer than the queued
                # fast edges (or the fast lane missed this particular action).
                action_restarted = bool(
                    action_id >= 0x100
                    and action_id == state.action_id
                    and action_frame <= 2
                    and state.action_frame > action_frame
                )
                action_changed = action_id != state.action_id
                if action_id > 0 and (action_changed or action_restarted):
                    self._emit_action_instance_locked(slot, packet, state)
                elif action_id >= 0x100 and (action_frame <= 2 or bool(pressed & 0x0CF0)):
                    # Preserve the original first-snapshot safety net. If the
                    # fast edge already emitted this same instance, the
                    # sub-frame dedupe inside _emit_action_instance_locked
                    # makes this a no-op.
                    self._emit_action_instance_locked(slot, packet, state)
                else:
                    state.action_id = action_id
                    state.action_frame = action_frame
            else:
                # Input always commits before action. This is the central
                # ordering guarantee for motions, mash strengths, and jump
                # cancels regardless of which sampler callback ran first.
                if held != previous.held or pressed or released:
                    self._append(kind=EVENT_INPUT, slot=slot, packet=packet)

                self._flush_fast_edges_through_locked(slot, packet_ns)
                state = self._slot_state.get(slot) or previous

                # 240 Hz fallback for action transitions the fast lane did not
                # report. Because flushed fast edges already updated ``state``,
                # this cannot duplicate the normal case.
                action_restarted = bool(
                    action_id >= 0x100
                    and action_id == state.action_id
                    and action_frame <= 2
                    and state.action_frame > action_frame
                )
                action_changed = action_id != state.action_id
                if action_id > 0 and (action_changed or action_restarted):
                    self._emit_action_instance_locked(slot, packet, state)
                else:
                    state.action_id = action_id
                    state.action_frame = action_frame

                if hp > 0 and previous.current_hp > 0 and hp < previous.current_hp:
                    self._append(
                        kind=EVENT_DAMAGE,
                        slot=slot,
                        packet=packet,
                        damage=previous.current_hp - hp,
                    )
                if special_reaction and not previous.special_reaction:
                    self._append(kind=EVENT_REACTION_BEGIN, slot=slot, packet=packet)
                elif previous.special_reaction and not special_reaction:
                    self._append(kind=EVENT_REACTION_END, slot=slot, packet=packet)
                if hitstun and not previous.hitstun:
                    self._append(kind=EVENT_HITSTUN_BEGIN, slot=slot, packet=packet)
                elif previous.hitstun and not hitstun:
                    self._append(kind=EVENT_HITSTUN_END, slot=slot, packet=packet)
                if blockstun and not previous.blockstun:
                    self._append(kind=EVENT_BLOCKSTUN_BEGIN, slot=slot, packet=packet)
                elif previous.blockstun and not blockstun:
                    self._append(kind=EVENT_BLOCKSTUN_END, slot=slot, packet=packet)
                if megacrash and not previous.megacrash:
                    self._append(kind=EVENT_MEGACRASH_BEGIN, slot=slot, packet=packet)
                elif previous.megacrash and not megacrash:
                    self._append(kind=EVENT_MEGACRASH_END, slot=slot, packet=packet)
                if recovery_idle and not previous.recovery_idle:
                    self._append(kind=EVENT_RECOVERY_IDLE, slot=slot, packet=packet)

                state.fighter_base = int(packet.get("base", 0) or state.fighter_base)
                state.char_id = int(packet.get("char_id", 0) or state.char_id)
                state.combat_sample_ns = packet_ns
                state.held = held
                state.current_hp = hp
                state.hitstun = hitstun
                state.blockstun = blockstun
                state.special_reaction = special_reaction
                state.megacrash = megacrash
                state.recovery_idle = recovery_idle
                state.point_active = bool(packet.get("point_active", state.point_active))
                state.hitstun_remaining = max(0, int(packet.get("hitstun_remaining", 0) or 0))
                state.untech_remaining = max(0, int(packet.get("untech_remaining", 0) or 0))
                state.blockstun_remaining = max(0, int(packet.get("blockstun_remaining", 0) or 0))
                state.reaction_timer_remaining = max(0, int(packet.get("reaction_timer_remaining", 0) or 0))
                self._slot_state[slot] = state

            combo_count = max(0, int(packet.get("combo_count", 0) or 0))
            if self._combo_count is None:
                self._combo_count = combo_count
            elif combo_count != self._combo_count:
                if self._combo_count <= 0 < combo_count:
                    combo_kind = EVENT_COMBO_BEGIN
                elif self._combo_count > 0 and combo_count <= 0:
                    combo_kind = EVENT_COMBO_END
                else:
                    combo_kind = EVENT_COMBO_CHANGE
                self._append(kind=combo_kind, slot=slot, packet=packet)
                self._combo_count = combo_count

    def snapshot_for_slot(self, slot_label: str) -> dict:
        """Return Mission Mode's latest complete combat snapshot for one slot.

        This is cache-only and never reads Dolphin. It exposes the same primary
        action ID and stun counters used to build continuity edges.
        """
        slot = str(slot_label or "")
        with self._lock:
            state = self._slot_state.get(slot)
            if state is None:
                return {}
            return {
                "slot": slot,
                "base": int(state.fighter_base),
                "char_id": int(state.char_id),
                "action_id": int(state.action_id) & 0x7FFF,
                "action_frame": int(state.action_frame),
                "current_hp": int(state.current_hp),
                "hitstun_remaining": int(state.hitstun_remaining),
                "untech_remaining": int(state.untech_remaining),
                "blockstun_remaining": int(state.blockstun_remaining),
                "reaction_timer_remaining": int(state.reaction_timer_remaining),
                "point_active": bool(state.point_active),
                "hitstun": bool(state.hitstun),
                "special_reaction": bool(state.special_reaction),
                "recovery_idle": bool(state.recovery_idle),
                "sample_ns": int(state.combat_sample_ns),
            }

    # Listener alias used by HudOverlayManager.
    on_sample = publish_sample

    def events_since(
        self,
        cursor: int,
        slot_label: str | None = None,
    ) -> tuple[int, tuple[MissionEvent, ...]]:
        requested = max(0, int(cursor or 0))
        slot_filter = str(slot_label) if slot_label else None
        with self._lock:
            self._flush_expired_fast_edges_locked(time.monotonic_ns())
            newest = int(self._sequence)
            events = tuple(
                event
                for event in self._events
                if event.sequence > requested
                and (slot_filter is None or event.slot == slot_filter)
            )
        return newest, events

    def snapshot(self) -> tuple[MissionEvent, ...]:
        with self._lock:
            self._flush_expired_fast_edges_locked(time.monotonic_ns())
            return tuple(self._events)


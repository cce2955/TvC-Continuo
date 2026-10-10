"""Dedicated high-frequency Dolphin combat sampler.

This scheduler is the only realtime lane used by Mission Mode and the input
HUD. It reads the small combat packet at 240 Hz, stores a bounded cache, and
publishes packets to listeners. It never writes JSON and never calls mission or
overlay code.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from tvcgui.runtime import input_monitor


REALTIME_SAMPLER_HZ = 240.0
REALTIME_SAMPLE_QUEUE_LIMIT = 128
REALTIME_ACTION_EDGE_HZ = 720.0


class RealtimeCombatSampler:
    """Read inputs, actions, HP, hitstun, and combo state off the GUI thread."""

    def __init__(
        self,
        *,
        hz: float = REALTIME_SAMPLER_HZ,
        queue_limit: int = REALTIME_SAMPLE_QUEUE_LIMIT,
        read_packet_fn: Callable | None = None,
        read_combo_fn: Callable | None = None,
        autostart: bool = True,
    ) -> None:
        self._hz = max(60.0, float(hz or REALTIME_SAMPLER_HZ))
        self._queue_limit = max(16, int(queue_limit or REALTIME_SAMPLE_QUEUE_LIMIT))
        self._read_packet = read_packet_fn or input_monitor.read_overlay_input_packet
        self._read_combo = read_combo_fn or input_monitor.read_global_combo_count

        self._sample_sequence = 0
        self._samples_by_slot: dict[str, list[dict]] = {}
        self._targets: dict[str, int] = {}
        self._latest_by_slot: dict[str, dict] = {}
        self._raw_state_by_slot: dict[str, tuple] = {}
        # (fighter base, action id, action frame).  The frame is required to
        # detect a repeated same-ID move: TvC restarts the native action frame
        # without necessarily visiting a different action ID between two fast
        # normals (for example 2A -> 2A).
        self._fast_action_state: dict[str, tuple[int, int, int]] = {}
        self._listeners: list[Callable] = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="TvCRealtimeCombatSampler",
            daemon=True,
        )
        self._action_thread = threading.Thread(
            target=self._run_action_edges,
            name="TvCRealtimeActionEdges",
            daemon=True,
        )
        if autostart:
            self._thread.start()
            self._action_thread.start()

    def set_targets(self, render_snap_by_slot: dict) -> None:
        """Update slot to fighter-base targets without performing any reads."""
        targets: dict[str, int] = {}
        for slot_label, snap in (render_snap_by_slot or {}).items():
            if not isinstance(snap, dict):
                continue
            try:
                base = int(snap.get("base") or 0)
            except Exception:
                base = 0
            if base:
                targets[str(slot_label)] = base
        with self._lock:
            self._targets = targets

    def add_listener(self, listener) -> None:
        if not callable(listener):
            return
        with self._lock:
            if listener not in self._listeners:
                self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        with self._lock:
            self._listeners = [item for item in self._listeners if item is not listener]

    def publish_packet(self, slot_label: str, packet: dict) -> None:
        """Normalize and publish one packet.

        This public entry point also makes the realtime lane deterministic in
        tests without starting a Dolphin connection.
        """
        if not isinstance(packet, dict):
            return
        slot = str(slot_label or packet.get("slot") or "")
        if not slot:
            return

        held = int(packet.get("held", 0) or 0) & 0xFFFF
        raw_pressed = int(packet.get("pressed", 0) or 0) & 0xFFFF
        raw_released = int(packet.get("released", 0) or 0) & 0xFFFF
        action_id = int(packet.get("action_id", 0) or 0) & 0x7FFF
        action_frame = max(0, int(packet.get("action_frame", 0) or 0))
        current_hp = int(packet.get("current_hp", 0) or 0)
        current_meter = max(0, int(packet.get("current_meter", 0) or 0))
        blockstun_remaining = max(0, int(packet.get("blockstun_remaining", 0) or 0))
        hitstun_remaining = max(0, int(packet.get("hitstun_remaining", 0) or 0))
        untech_remaining = max(0, int(packet.get("untech_remaining", 0) or 0))
        reaction_timer_remaining = max(0, int(packet.get("reaction_timer_remaining", 0) or 0))
        impact_freeze_remaining = max(0, int(packet.get("impact_freeze_remaining", 0) or 0))
        fighter_combo_count = max(0, int(packet.get("fighter_combo_count", 0) or 0))
        decay_counter = max(0, int(packet.get("decay_counter", 0) or 0))
        state_flags_6c = int(packet.get("state_flags_6c", 0) or 0) & 0xFFFFFFFF
        combo_count = max(0, int(packet.get("combo_count", 0) or 0))
        point_active = bool(
            packet.get(
                "point_active",
                packet.get("damage_point_active", packet.get("damage_is_point", False)),
            )
        )

        with self._lock:
            previous = self._raw_state_by_slot.get(slot)
            if previous is None:
                previous_held = held
                previous_pressed = 0
                previous_released = 0
                previous_action = action_id
                previous_action_frame = action_frame
                previous_hp = current_hp
                previous_meter = current_meter
                previous_blockstun = blockstun_remaining
                previous_hitstun = hitstun_remaining
                previous_combo = combo_count
                previous_point_active = point_active
                previous_untech = untech_remaining
                previous_reaction_timer = reaction_timer_remaining
                previous_impact_freeze = impact_freeze_remaining
                previous_fighter_combo = fighter_combo_count
                previous_decay_counter = decay_counter
                previous_state_flags_6c = state_flags_6c
            else:
                (
                    previous_held,
                    previous_pressed,
                    previous_released,
                    previous_action,
                    previous_action_frame,
                    previous_hp,
                    previous_meter,
                    previous_blockstun,
                    previous_hitstun,
                    previous_combo,
                    previous_point_active,
                    previous_untech,
                    previous_reaction_timer,
                    previous_impact_freeze,
                    previous_fighter_combo,
                    previous_decay_counter,
                    previous_state_flags_6c,
                ) = previous

            # Native +0x13D0/+0x13D4 are the preferred edge fields, but also
            # derive button edges from the held word. This closes the sampling
            # hole where a very short native pressed pulse can be missed between
            # polls even though the 240 Hz lane still observes the held change.
            # Direction is a nibble encoding rather than independent bits, so
            # only derive button edges here; direction transitions continue to
            # travel through held_changed below.
            button_mask = 0x0CF0  # A/B/C/P plus the composite taunt bits
            held_pressed = (held & ~int(previous_held)) & button_mask
            held_released = (int(previous_held) & ~held) & button_mask
            fresh_pressed = (raw_pressed & ~int(previous_pressed)) | held_pressed
            fresh_released = (raw_released & ~int(previous_released)) | held_released
            held_changed = previous is None or held != int(previous_held)
            action_changed = previous is None or action_id != int(previous_action)
            action_frame_changed = (
                previous is None or action_frame != int(previous_action_frame)
            )
            hp_changed = previous is None or current_hp != int(previous_hp)
            meter_changed = previous is None or current_meter != int(previous_meter)
            blockstun_changed = (
                previous is None or blockstun_remaining != int(previous_blockstun)
            )
            hitstun_changed = (
                previous is None or hitstun_remaining != int(previous_hitstun)
            )
            combo_changed = previous is None or combo_count != int(previous_combo)
            point_changed = previous is None or point_active != bool(previous_point_active)
            untech_changed = previous is None or untech_remaining != int(previous_untech)
            reaction_timer_changed = previous is None or reaction_timer_remaining != int(previous_reaction_timer)
            impact_freeze_changed = previous is None or impact_freeze_remaining != int(previous_impact_freeze)
            fighter_combo_changed = previous is None or fighter_combo_count != int(previous_fighter_combo)
            decay_counter_changed = previous is None or decay_counter != int(previous_decay_counter)
            state_flags_changed = previous is None or state_flags_6c != int(previous_state_flags_6c)

            self._raw_state_by_slot[slot] = (
                held,
                raw_pressed,
                raw_released,
                action_id,
                action_frame,
                current_hp,
                current_meter,
                blockstun_remaining,
                hitstun_remaining,
                combo_count,
                point_active,
                untech_remaining,
                reaction_timer_remaining,
                impact_freeze_remaining,
                fighter_combo_count,
                decay_counter,
                state_flags_6c,
            )

            meaningful_change = bool(
                held_changed
                or fresh_pressed
                or fresh_released
                or action_changed
                or hp_changed
                or meter_changed
                or blockstun_changed
                or hitstun_changed
                or combo_changed
                or point_changed
                or untech_changed
                or reaction_timer_changed
                or impact_freeze_changed
                or fighter_combo_changed
                or decay_counter_changed
                or state_flags_changed
            )
            if not meaningful_change and not action_frame_changed:
                return

            # Action-frame-only samples still reach realtime listeners for
            # schedulers that need precise timing, but only meaningful edges
            # receive a queue sequence and enter the bounded compatibility queue.
            if meaningful_change:
                self._sample_sequence += 1
            sample = {
                "seq": self._sample_sequence if meaningful_change else 0,
                "slot": slot,
                "base": int(packet.get("base", 0) or 0),
                "held": held,
                "pressed": fresh_pressed,
                "released": fresh_released,
                "char_id": int(packet.get("char_id", 0) or 0),
                "action_id": action_id,
                "action_frame": action_frame,
                "current_hp": current_hp,
                "current_meter": current_meter,
                "blockstun_remaining": blockstun_remaining,
                "hitstun_remaining": hitstun_remaining,
                "untech_remaining": untech_remaining,
                "reaction_timer_remaining": reaction_timer_remaining,
                "impact_freeze_remaining": impact_freeze_remaining,
                "fighter_combo_count": fighter_combo_count,
                "decay_counter": decay_counter,
                "state_flags_6c": state_flags_6c,
                "combo_count": combo_count,
                "point_active": point_active,
                "sample_ns": int(packet.get("sample_ns", 0) or time.monotonic_ns()),
            }
            self._latest_by_slot[slot] = dict(sample)
            if meaningful_change:
                queue = self._samples_by_slot.setdefault(slot, [])
                queue.append(dict(sample))
                del queue[:-self._queue_limit]
            listeners = tuple(self._listeners)

        # Listener work runs outside the sampler lock. The event publishers are
        # intentionally tiny, and a failed listener cannot stop the scheduler.
        for listener in listeners:
            try:
                listener(slot, dict(sample))
            except Exception:
                continue

    def snapshot_for_slot(
        self,
        slot_label: str,
        fighter_base: int = 0,
    ) -> tuple[dict, list[dict]]:
        """Return cached state only. This method never reads Dolphin."""
        slot = str(slot_label or "")
        with self._lock:
            latest = dict(self._latest_by_slot.get(slot) or {})
            samples = [dict(item) for item in self._samples_by_slot.get(slot, ())]
        return latest, samples

    def _run(self) -> None:
        interval = 1.0 / self._hz
        next_tick = time.perf_counter()
        while not self._stop.is_set():
            with self._lock:
                targets = dict(self._targets)
            try:
                combo_count = max(0, int(self._read_combo() or 0))
            except Exception:
                combo_count = 0
            for slot_label, base in targets.items():
                try:
                    packet = self._read_packet(
                        slot_label,
                        base,
                        combo_count=combo_count,
                    )
                except TypeError:
                    try:
                        packet = self._read_packet(slot_label, base)
                    except Exception:
                        continue
                except Exception:
                    continue
                if packet:
                    self.publish_packet(slot_label, packet)

            next_tick += interval
            delay = next_tick - time.perf_counter()
            if delay <= 0.0:
                next_tick = time.perf_counter()
                delay = 0.001
            self._stop.wait(delay)

    def _run_action_edges(self) -> None:
        """Publish only confirmed native action-instance edges.

        This lane is deliberately independent of the 240 Hz combat packet.
        It performs one tiny atomic-ish read of action ID + action frame and
        emits an event only when a new native action instance begins.  The
        overlay manager therefore receives a dumb event stream instead of
        trying to reconstruct history from competing samplers.
        """
        interval = 1.0 / REALTIME_ACTION_EDGE_HZ
        next_tick = time.perf_counter()
        while not self._stop.is_set():
            with self._lock:
                targets = dict(self._targets)
            for slot_label, base in targets.items():
                try:
                    edge = input_monitor.read_realtime_action_edge(base)
                    action_id = int(edge.get("action_id", 0) or 0) & 0x7FFF
                    action_frame = max(0, int(edge.get("action_frame", 0) or 0))
                except Exception:
                    continue

                listeners = ()
                event = None
                with self._lock:
                    previous = self._fast_action_state.get(slot_label)
                    current_key = (int(base), action_id, action_frame)
                    self._fast_action_state[slot_label] = current_key

                    # A new fighter pointer owns this slot.  Emit an explicit
                    # reset so the visible five-move FIFO cannot bleed across
                    # tag/match transitions, then establish the baseline.
                    if previous is None or int(previous[0]) != int(base):
                        self._sample_sequence += 1
                        cached = dict(self._latest_by_slot.get(slot_label) or {})
                        event = {
                            "seq": self._sample_sequence,
                            "slot": slot_label,
                            "base": int(base),
                            "char_id": int(cached.get("char_id", 0) or 0),
                            "action_id": action_id,
                            "action_frame": action_frame,
                            "sample_ns": time.monotonic_ns(),
                            "fast_action_edge": True,
                            "move_history_reset": True,
                            "move_instance": False,
                        }
                        listeners = tuple(self._listeners)
                    else:
                        previous_action = int(previous[1])
                        previous_frame = max(0, int(previous[2]))
                        action_restarted = bool(
                            previous_action == action_id and action_frame < previous_frame
                        )
                        if previous_action != action_id or action_restarted:
                            self._sample_sequence += 1
                            cached = dict(self._latest_by_slot.get(slot_label) or {})
                            event = {
                                "seq": self._sample_sequence,
                                "slot": slot_label,
                                "base": int(base),
                                "char_id": int(cached.get("char_id", 0) or 0),
                                "action_id": action_id,
                                "action_frame": action_frame,
                                "sample_ns": time.monotonic_ns(),
                                "fast_action_edge": True,
                                "move_history_reset": False,
                                "move_instance": True,
                            }
                            # Keep only action identity current here.  HP, input,
                            # stun, etc. remain owned by the regular sampler.
                            cached.update({
                                "slot": slot_label,
                                "base": int(base),
                                "action_id": action_id,
                                "action_frame": action_frame,
                                "sample_ns": event["sample_ns"],
                            })
                            self._latest_by_slot[slot_label] = cached
                            listeners = tuple(self._listeners)

                if event is not None:
                    for listener in listeners:
                        try:
                            listener(slot_label, dict(event))
                        except Exception:
                            continue

            next_tick += interval
            delay = next_tick - time.perf_counter()
            if delay <= 0.0:
                next_tick = time.perf_counter()
                delay = 0.0005
            self._stop.wait(delay)

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=1.0)
        if self._action_thread.is_alive():
            self._action_thread.join(timeout=1.0)

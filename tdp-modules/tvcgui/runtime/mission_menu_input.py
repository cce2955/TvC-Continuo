"""Independent realtime interpreter for Mission Select controls.

This module owns the Down, Down, Taunt gesture and the Mission Select menu's
navigation inputs. It listens directly to the 240 Hz sampler and emits compact,
immutable commands. MissionManager never tracks controller edges, gesture
history, or input cursors.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import threading
import time

from tvcgui.core.paths import user_data_path

MISSION_MENU_OPEN = "open"
MISSION_MENU_NEXT = "next"
MISSION_MENU_PREVIOUS = "previous"
MISSION_MENU_SELECT = "select"
MISSION_MENU_CLOSE = "close"
MISSION_MENU_LOCK = "lock"
MISSION_MENU_UNLOCK = "unlock"

MISSION_MENU_DIRECTION_MASK = 0x0F
MISSION_MENU_BUTTON_A = 0x80
MISSION_MENU_BUTTON_B = 0x40
MISSION_MENU_BUTTON_C = 0x20
MISSION_MENU_BUTTON_PARTNER = 0x10
MISSION_MENU_BUTTON_TAUNT = 0x0C00
MISSION_MENU_DOWN_DIRECTIONS = frozenset((0x08, 0x09, 0x0A))
MISSION_MENU_UP_DIRECTIONS = frozenset((0x04, 0x05, 0x06))
MISSION_MENU_RIGHT_DIRECTION = 0x01
MISSION_MENU_LEFT_DIRECTION = 0x02
MISSION_MENU_REPEAT_WINDOW_SEC = 1.35
MISSION_MENU_COMMAND_LIMIT = 64
MISSION_MENU_VISUAL_FILE = user_data_path("training", "mission_menu_visual.json")


@dataclass(frozen=True, slots=True)
class MissionMenuCommand:
    sequence: int
    kind: str
    teamtag: str
    slot: str
    base: int
    char_id: int
    sample_ns: int


class MissionMenuInputInterpreter:
    """Interpret Mission Select controls only while Mission Mode is enabled.

    Disabled:
        All controller input passes through untouched. No gesture state is kept.

    Closed menu while enabled:
        Down, Down, Taunt -> OPEN

    Open menu:
        Down -> NEXT
        Up -> PREVIOUS
        Right -> LOCK
        Left -> UNLOCK
        Taunt -> SELECT

    Point ownership comes from fighter+0x44A0 sampled on the realtime lane. A
    real tag immediately moves ownership to the new point slot and clears the
    old fighter's partial gesture. Assist activity cannot steal ownership.
    """

    def __init__(
        self,
        *,
        repeat_window_sec: float = MISSION_MENU_REPEAT_WINDOW_SEC,
        command_limit: int = MISSION_MENU_COMMAND_LIMIT,
    ) -> None:
        self._repeat_window_sec = max(0.25, float(repeat_window_sec))
        self._command_limit = max(16, int(command_limit))
        self._lock = threading.RLock()
        self._command_sequence = 0
        self._commands: list[MissionMenuCommand] = []
        self._enabled: bool = False
        self._point_slot_by_team: dict[str, str] = {}
        self._menu_open_by_team: dict[str, bool] = {}
        self._menu_slot_by_team: dict[str, str] = {}
        self._state_by_slot: dict[str, dict] = {}
        self._visual_count_by_slot: dict[str, int] = {}
        self._visual_index_by_slot: dict[str, int] = {}

    def _publish_visual(self, slot: str, is_open: bool) -> None:
        """Give the 60 Hz overlay a cursor update directly from input sampling."""
        payload = {
            "slot": str(slot), "open": bool(is_open),
            "index": int(self._visual_index_by_slot.get(slot, 0)),
            "sequence": int(self._command_sequence),
            "updated_ns": time.time_ns(),
        }
        try:
            os.makedirs(os.path.dirname(MISSION_MENU_VISUAL_FILE), exist_ok=True)
            temporary = MISSION_MENU_VISUAL_FILE + ".tmp"
            with open(temporary, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, separators=(",", ":"))
            os.replace(temporary, MISSION_MENU_VISUAL_FILE)
        except OSError:
            pass

    def configure_menu(self, slot: str, count: int, index: int) -> None:
        with self._lock:
            slot = str(slot)
            self._visual_count_by_slot[slot] = max(1, int(count))
            self._visual_index_by_slot[slot] = int(index) % self._visual_count_by_slot[slot]
            self._publish_visual(slot, True)

    @staticmethod
    def _teamtag(slot: str) -> str:
        return "P2" if str(slot).startswith("P2") else "P1"

    @staticmethod
    def _sample_time_seconds(sample: dict) -> float:
        try:
            sample_ns = int(sample.get("sample_ns", 0) or 0)
        except Exception:
            sample_ns = 0
        return sample_ns / 1_000_000_000.0 if sample_ns > 0 else time.monotonic()

    def _slot_state(self, slot: str) -> dict:
        return self._state_by_slot.setdefault(
            slot,
            {
                "last_direction": 0,
                "last_taunt_held": False,
                "down_times": [],
                "blocked": False,
            },
        )

    def _reset_gesture(self, slot: str, *, keep_edges: bool = True) -> None:
        state = self._slot_state(slot)
        state["down_times"] = []
        state["blocked"] = False
        if not keep_edges:
            state["last_direction"] = 0
            state["last_taunt_held"] = False

    def _emit(self, kind: str, slot: str, sample: dict) -> None:
        self._command_sequence += 1
        command = MissionMenuCommand(
            sequence=self._command_sequence,
            kind=str(kind),
            teamtag=self._teamtag(slot),
            slot=str(slot),
            base=int(sample.get("base", 0) or 0),
            char_id=int(sample.get("char_id", 0) or 0),
            sample_ns=int(sample.get("sample_ns", 0) or time.monotonic_ns()),
        )
        self._commands.append(command)
        del self._commands[:-self._command_limit]
        count = self._visual_count_by_slot.get(slot, 0)
        if kind == MISSION_MENU_NEXT and count:
            self._visual_index_by_slot[slot] = (self._visual_index_by_slot.get(slot, 0) + 1) % count
        elif kind == MISSION_MENU_PREVIOUS and count:
            self._visual_index_by_slot[slot] = (self._visual_index_by_slot.get(slot, 0) - 1) % count
        self._publish_visual(slot, kind not in (MISSION_MENU_CLOSE, MISSION_MENU_SELECT))

    def _set_point_slot(self, teamtag: str, slot: str, sample: dict) -> None:
        previous = self._point_slot_by_team.get(teamtag)
        if previous == slot:
            return
        if previous:
            self._reset_gesture(previous, keep_edges=False)
        self._point_slot_by_team[teamtag] = slot
        self._reset_gesture(slot, keep_edges=False)
        if self._menu_open_by_team.get(teamtag, False):
            old_menu_slot = self._menu_slot_by_team.get(teamtag) or previous or slot
            self._emit(MISSION_MENU_CLOSE, old_menu_slot, sample)
            self._menu_open_by_team[teamtag] = False
            self._menu_slot_by_team.pop(teamtag, None)

    def on_sample(self, slot_label: str, sample: dict) -> None:
        """Consume one realtime sampler packet while Mission Mode is enabled."""
        if not isinstance(sample, dict):
            return
        with self._lock:
            if not self._enabled:
                return
        slot = str(slot_label or sample.get("slot") or "")
        if not slot:
            return
        teamtag = self._teamtag(slot)
        point_active = bool(
            sample.get(
                "point_active",
                sample.get("damage_point_active", sample.get("damage_is_point", False)),
            )
        )

        with self._lock:
            if point_active:
                self._set_point_slot(teamtag, slot, sample)
            if self._point_slot_by_team.get(teamtag) != slot or not point_active:
                return

            state = self._slot_state(slot)
            held = int(sample.get("held", 0) or 0) & 0xFFFF
            pressed = int(sample.get("pressed", 0) or 0) & 0xFFFF
            direction = held & MISSION_MENU_DIRECTION_MASK
            previous_direction = int(state.get("last_direction", 0) or 0)
            taunt_held = (
                held & MISSION_MENU_BUTTON_TAUNT
            ) == MISSION_MENU_BUTTON_TAUNT
            taunt_rising = bool(
                (pressed & MISSION_MENU_BUTTON_TAUNT) == MISSION_MENU_BUTTON_TAUNT
                or (taunt_held and not bool(state.get("last_taunt_held", False)))
            )
            down_rising = bool(
                direction in MISSION_MENU_DOWN_DIRECTIONS
                and previous_direction not in MISSION_MENU_DOWN_DIRECTIONS
            )
            up_rising = bool(
                direction in MISSION_MENU_UP_DIRECTIONS
                and previous_direction not in MISSION_MENU_UP_DIRECTIONS
            )
            right_rising = direction == MISSION_MENU_RIGHT_DIRECTION and previous_direction != MISSION_MENU_RIGHT_DIRECTION
            left_rising = direction == MISSION_MENU_LEFT_DIRECTION and previous_direction != MISSION_MENU_LEFT_DIRECTION
            attack_pressed = bool(
                pressed
                & (
                    MISSION_MENU_BUTTON_A
                    | MISSION_MENU_BUTTON_B
                    | MISSION_MENU_BUTTON_C
                    | MISSION_MENU_BUTTON_PARTNER
                )
            )
            sample_time = self._sample_time_seconds(sample)

            if self._menu_open_by_team.get(teamtag, False):
                if down_rising:
                    self._emit(MISSION_MENU_NEXT, slot, sample)
                if up_rising:
                    self._emit(MISSION_MENU_PREVIOUS, slot, sample)
                if right_rising:
                    self._emit(MISSION_MENU_LOCK, slot, sample)
                if left_rising:
                    self._emit(MISSION_MENU_UNLOCK, slot, sample)
                if taunt_rising:
                    self._emit(MISSION_MENU_SELECT, slot, sample)
                    self._menu_open_by_team[teamtag] = False
                    self._menu_slot_by_team.pop(teamtag, None)
                    self._reset_gesture(slot, keep_edges=True)
            else:
                if attack_pressed:
                    self._reset_gesture(slot, keep_edges=True)
                    state["blocked"] = True

                if down_rising and not attack_pressed:
                    down_times = state.setdefault("down_times", [])
                    if down_times and sample_time - float(down_times[-1]) > self._repeat_window_sec:
                        down_times[:] = []
                    down_times.append(sample_time)
                    del down_times[:-2]
                    state["blocked"] = False

                if taunt_rising:
                    down_times = list(state.get("down_times") or [])
                    valid = bool(
                        not state.get("blocked", False)
                        and len(down_times) >= 2
                        and float(down_times[-1]) - float(down_times[-2])
                        <= self._repeat_window_sec
                        and sample_time - float(down_times[-1])
                        <= self._repeat_window_sec
                    )
                    self._reset_gesture(slot, keep_edges=True)
                    if valid:
                        self._emit(MISSION_MENU_OPEN, slot, sample)
                        self._menu_open_by_team[teamtag] = True
                        self._menu_slot_by_team[teamtag] = slot

            state["last_direction"] = direction
            state["last_taunt_held"] = taunt_held

    def set_enabled(self, enabled: bool) -> None:
        """Enable the Mission Select gesture only while Mission Mode is active.

        Disabling clears all partial gestures and menu ownership so a Down, Down
        sequence performed during normal play cannot be completed later by a
        Taunt after Mission Mode is enabled.
        """
        with self._lock:
            enabled = bool(enabled)
            if self._enabled == enabled:
                return
            self._enabled = enabled
            if not enabled:
                for slot in self._menu_slot_by_team.values():
                    self._publish_visual(slot, False)
            self._commands.clear()
            self._menu_open_by_team.clear()
            self._menu_slot_by_team.clear()
            for slot in list(self._state_by_slot):
                self._reset_gesture(slot, keep_edges=False)
            if not enabled:
                self._point_slot_by_team.clear()

    @property
    def enabled(self) -> bool:
        with self._lock:
            return bool(self._enabled)

    def drain_commands(self) -> list[MissionMenuCommand]:
        with self._lock:
            commands = list(self._commands)
            self._commands.clear()
        return commands

    def close_menu(self, teamtag: str | None = None) -> None:
        """Synchronize an external close/timeout without changing held edges."""
        with self._lock:
            teams = [str(teamtag)] if teamtag else list(self._menu_open_by_team)
            for team in teams:
                self._menu_open_by_team[team] = False
                slot = self._menu_slot_by_team.pop(team, None) or self._point_slot_by_team.get(team)
                if slot:
                    self._reset_gesture(slot, keep_edges=True)
                    self._publish_visual(slot, False)

    def set_menu_open(self, teamtag: str, slot: str) -> None:
        """Synchronize a menu opened by another surface such as the mouse."""
        with self._lock:
            team = str(teamtag or self._teamtag(slot))
            self._menu_open_by_team[team] = True
            self._menu_slot_by_team[team] = str(slot)
            self._reset_gesture(str(slot), keep_edges=True)
            self._publish_visual(str(slot), True)

    def point_slot(self, teamtag: str = "P1") -> str | None:
        with self._lock:
            return self._point_slot_by_team.get(str(teamtag))

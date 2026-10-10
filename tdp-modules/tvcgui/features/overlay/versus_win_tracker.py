"""Read-only Versus match result tracker for the original standalone Continuo.

This never modifies Dolphin's battle logic.  Results are counted only after a
live match was observed, using team-KO / native-result / native winner-pose
proof.  Fighter-pointer gaps in NEW HERO / transitions MUST NOT throw away an
armed, uncredited match.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

LIVE_BATTLE_STATE = 4
RESULT_BATTLE_STATES = frozenset((9, 10, 11, 12))
# (base, hp, ko_action, winner_pose), or (base, hp, ko_action) for callers
# that do not provide winner-pose metadata.
Slot = tuple[int, int, bool] | tuple[int, int, bool, bool] | None


@dataclass
class VersusWinTracker:
    armed: bool = False
    counted: bool = False
    ever_armed: bool = False
    live_samples: int = 0
    missing_samples: int = 0
    intermission_seen: bool = False
    result_winner: str = ""
    result_samples: int = 0
    roster: dict[str, set[int]] = field(default_factory=lambda: {"P1": set(), "P2": set()})
    dead_bases: set[int] = field(default_factory=set)
    last_reason: str = ""
    last_phase: int | None = None
    pending_result: bool = False
    last_candidate: str = ""
    unresolved_transition: bool = False

    def _clear_round(self) -> None:
        self.armed = False
        self.counted = False
        self.live_samples = 0
        self.missing_samples = 0
        self.intermission_seen = False
        self.result_winner = ""
        self.result_samples = 0
        self.roster = {"P1": set(), "P2": set()}
        self.dead_bases.clear()
        self.last_reason = ""
        self.pending_result = False
        self.last_candidate = ""
        self.unresolved_transition = False

    def observe(self, battle_state: int | None, slots: Mapping[str, Slot]) -> str | None:
        """Return 'P1' / 'P2' once after sufficiently confirmed match victory."""
        self.last_phase = battle_state
        members: dict[str, dict[int, bool]] = {"P1": {}, "P2": {}}
        win_pose_teams: set[str] = set()
        for slotname, record in (slots or {}).items():
            team = str(slotname).split("-", 1)[0].upper()
            if team not in members or record is None:
                continue
            base, hp, ko_action = record[:3]
            win_pose = bool(record[3]) if len(record) > 3 else False
            if int(base) <= 0:
                continue
            dead = int(hp) <= 0 or bool(ko_action)
            members[team][int(base)] = dead
            if dead and self.armed:
                self.dead_bases.add(int(base))
            if not dead and win_pose:
                win_pose_teams.add(team)

        both_loaded = bool(members["P1"] and members["P2"])
        both_alive = both_loaded and not any(members["P1"].values()) and not any(members["P2"].values())
        is_live = battle_state in (None, LIVE_BATTLE_STATE)
        fresh_live = bool(is_live and both_alive and not win_pose_teams)

        if battle_state in RESULT_BATTLE_STATES and self.armed:
            self.pending_result = True

        if fresh_live:
            self.live_samples += 1
            self.missing_samples = 0
            if self.live_samples >= 2 and (not self.armed or self.counted or self.intermission_seen):
                # New match, or one that returned from NEW HERO before the
                # winner could be verified. Never award an unproven win.
                had_unresolved = bool(self.armed and not self.counted and self.intermission_seen)
                self._clear_round()
                self.armed = True
                self.ever_armed = True
                self.roster = {"P1": set(members["P1"]), "P2": set(members["P2"])}
                if had_unresolved:
                    self.unresolved_transition = True
                    self.last_reason = "unresolved_previous_match_rearmed"
            elif self.armed:
                for team in ("P1", "P2"):
                    self.roster[team].update(members[team])
            return None

        self.live_samples = 0
        if not any(members.values()):
            self.missing_samples += 1
            if self.missing_samples >= 4 and self.armed:
                # A transition can drop ALL fighter pointers before the result
                # is readable. Keep the previous roster + KO evidence; do not
                # clear the match until the NEXT verified live battle.
                self.intermission_seen = True
                self.last_reason = "fighter_pointers_unloaded_pending_result"
            return None
        self.missing_samples = 0
        if not self.armed or self.counted:
            return None

        def team_fully_dead(team: str) -> bool:
            expected = self.roster[team]
            if not expected:
                return False
            for base in expected:
                dead = members[team].get(base)
                if dead is False or (dead is None and base not in self.dead_bases):
                    return False
                if dead is not True and base not in self.dead_bases:
                    return False
            return True

        p1_dead, p2_dead = team_fully_dead("P1"), team_fully_dead("P2")
        winner, reason, required = "", "", 999999
        in_result = battle_state in RESULT_BATTLE_STATES

        if p1_dead != p2_dead:
            winner = "P2" if p1_dead else "P1"
            reason = "complete_team_ko"
            required = 1 if (in_result or is_live) else 999999
            # The final fighter may unload in the very next frame; requiring
            # a second full-team-KO snapshot previously missed valid wins.
        elif len(win_pose_teams) == 1:
            candidate = next(iter(win_pose_teams))
            opponent = "P2" if candidate == "P1" else "P1"
            if self.roster[candidate] and self.roster[opponent] and any(not d for d in members[candidate].values()):
                if in_result or self.pending_result:
                    winner, reason, required = candidate, "native_winner_pose", 1
                elif not members[opponent] and is_live and (self.intermission_seen or any(base in self.dead_bases for base in self.roster[opponent])):
                    winner, reason, required = candidate, "winner_pose_unloaded_opponent", 2
        elif in_result and both_loaded:
            # A result phase is the authority that this was the *match* KO,
            # not a single-character tag/KO. A defeated fighter can already
            # have unloaded before another fighter's KO is captured.
            p1_observed_dead = any(members["P1"].values()) or bool(self.dead_bases & self.roster["P1"])
            p2_observed_dead = any(members["P2"].values()) or bool(self.dead_bases & self.roster["P2"])
            p1_living = any(not d for d in members["P1"].values())
            p2_living = any(not d for d in members["P2"].values())
            if p1_observed_dead and not p2_observed_dead and p2_living:
                # Native result phase confirms that an earlier one-character
                # KO is now a completed match, even if a stale reserve pointer
                # still looks alive during the transition.
                winner, reason, required = "P2", "result_defeated_side", 3
            elif p2_observed_dead and not p1_observed_dead and p1_living:
                winner, reason, required = "P1", "result_defeated_side", 3
        elif in_result:
            # One team has already unloaded; the other still has at least one
            # live fighter. Require three consecutive native-result frames.
            present = [team for team in ("P1", "P2") if members[team]]
            if len(present) == 1:
                candidate = present[0]
                opponent = "P2" if candidate == "P1" else "P1"
                if self.roster[candidate] and self.roster[opponent] and any(not d for d in members[candidate].values()):
                    winner, reason, required = candidate, "result_survivor_only", 3

        if not winner:
            self.result_winner = ""
            self.result_samples = 0
            if in_result:
                self.last_reason = "result_without_winner_proof"
            return None

        # Changing evidence types in the same result screen must not reset
        # accumulated proof of the SAME winning side.
        if winner != self.result_winner:
            self.result_winner = winner
            self.result_samples = 0
        self.last_reason = reason
        self.last_candidate = winner
        self.result_samples += 1
        if self.result_samples < required:
            return None
        self.counted = True
        self.result_winner = ""
        self.last_reason = f"credited_{reason}"
        return winner

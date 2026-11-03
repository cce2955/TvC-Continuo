"""Single source of truth for Mission Mode defender continuity.

These are the verified native action IDs that the original Mission Mode used
as its explicit "do not reset" policy.  Keep the policy here so the realtime
event producer, V2 evaluator, and legacy adapter cannot silently drift apart.
"""
from __future__ import annotations

# Historical verified mission keep-alive states.  These are intentionally kept
# verbatim from MissionManager's pre-V2 continuity logic.
MISSION_CROUCH_REACTION_STATES = frozenset({51, 67})
MISSION_REACTION_STATES = frozenset({
    48, 49, 50, 52, 53, 60, 61, 62, 64, 65, 66, 73, 74, 75, 76, 79, 80,
    81, 82, 83, 88, 89, 90, 91, 92, 94, 95, 96, 97, 98, 101, 102, 105,
    106, 142, 448, 449,
    4608, 4609, 4610, 4611, 4613, 4614, 4615, 4616, 4617, 4618, 4619,
    4620, 4621, 4622, 4623, 4625,
    4562, 4565, 4568, 4571, 4573, 4631,
}) | MISSION_CROUCH_REACTION_STATES
MISSION_MEGACRASH_STATES = frozenset({448})
MISSION_COMBO_KEEPALIVE_STATES = frozenset(
    set(MISSION_REACTION_STATES) | set(MISSION_MEGACRASH_STATES)
)
MISSION_BLOCKSTUN_STATES = frozenset({48, 49, 50, 51, 52, 53})

# Additional reaction IDs learned by the reaction-state profiler after the old
# table was built.  They supplement the historical table; they never replace it.
MISSION_EXTENDED_REACTION_STATES = frozenset({
    55, 56, 57, 58, 59, 70, 77, 84, 85, 86, 87, 93, 99, 108, 109,
    124, 128, 129, 130, 132, 161,
})
MISSION_REACTION_ACTIONS = frozenset(
    set(MISSION_COMBO_KEEPALIVE_STATES) | set(MISSION_EXTENDED_REACTION_STATES)
)

# Vulnerable knockdown phases. These remain continuity even when scalar stun
# clocks briefly read zero.
MISSION_KNOCKDOWN_ACTIONS = frozenset({
    70, 73, 74, 75, 76, 77, 80, 81, 82, 83, 89, 90, 91, 92,
    93, 95, 96, 98, 102, 104, 105, 106, 108, 109, 132, 142, 161,
})

# Genuine recovery boundaries.  These are the states that are allowed to end a
# live mission route (or begin authored grace).
MISSION_WAKEUP_ACTIONS = frozenset({103, 113, 115, 116, 119, 133})
MISSION_AIR_RECOVERY_ACTIONS = frozenset({126, 160, 165})
MISSION_IDLE_ACTIONS = frozenset({1})
MISSION_RECOVERY_BOUNDARY_ACTIONS = frozenset(
    set(MISSION_WAKEUP_ACTIONS)
    | set(MISSION_AIR_RECOVERY_ACTIONS)
    | set(MISSION_IDLE_ACTIONS)
)

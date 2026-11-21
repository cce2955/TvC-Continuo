# Feature checklist

Analysis window first pass (2026-11-02): opt-in capture, manual round markers,
local save/open, attempt notes and side-by-side timing comparison, conversion
groups, resource durations, move starts, damage totals, sampled positions,
possible assist entries, personal bests, and recent activity are wired up.
The Matchups window now has opt-in, winner-verified match records with a P1/P2
perspective and cross-session archive rollups for team, character, starting
role, observed point time, and move damage. Match statistics now track in the
background by default, with a persisted off switch and a nondestructive reset.
Items below remain open until their full validation and presentation are done.

## Combo Lab

- [ ] Attempt recorder: capture input and recognized action timestamps, confirmed contacts, combo state, damage, meter, and fighter position on one game-frame timeline. Preserve raw input and recognized action separately.
- [ ] Route Optimizer: compare two or more recorded attempts by confirmed hits, damage, meter gained and spent, hit count, end state, and where the routes first diverge.
- [ ] Drop Investigator: compare a dropped attempt with a successful attempt of the same route. Show the first divergent input/action/contact and its timing relative to the previous confirmed hit; report observed hitstun and spacing when available.
- [ ] Meter Planner: show actual meter gained and spent along a recorded route, the ending balance, and whether the route reaches a user-selected meter target.
- [ ] Conversion Library: save successful attempts by starter and context, then browse and compare routes that began from the same kind of hit.

Start with manual recording and side-by-side attempt comparison. Add automatic route grouping and repeatability statistics once the captured timeline is reliable.

## Match Analysis

- [ ] Matchup Report Card: aggregate verified wins and losses by team composition, character pair, and opponent starting point or partner role. Keep point exposure, damage taken, and move counts separate from match outcomes; never infer causation from a low sample count. Support P1/P2 perspective and saved-session history.

- [ ] Round Review: summarize damage dealt and taken, meter gained and spent, frequent hit sources, and notable events after each round. Link summary items to their underlying recorded events when available.
- [ ] Damage Source Breakdown: group damage taken and dealt by opposing fighter and move across a round or session. Keep assists and repeated hits identifiable, and mark unknown sources rather than guessing.
- [ ] Move Usage Map: show each fighter's recognized move attempts, contacts, blocks, and whiffs over a session. Separate move recognition from confirmed outcomes so incomplete telemetry does not create misleading rates.
- [ ] Position Report: group hits dealt and taken by position (corner, midscreen, side switch) and identify the moves involved.
- [ ] Assist Value Report: count assist calls, contacts, damage that followed, and times the partner was punished. Keep direct assist damage distinct from follow-up damage.
- [ ] Resource Decisions: track meter gained and spent, time spent at each bar level (0, 1, 2, 3, etc.), Megacrash uses per match, peak Baroque value, and how long that peak was held. Show the resource state at significant hits and decisions.
- [ ] Personal Bests: save best damage, meter gain, and completion consistency for a route or starter, with enough context to compare like-for-like attempts.

These analysis views should be opt in and built from a shared event history so their counts agree.


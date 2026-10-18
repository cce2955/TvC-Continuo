## Table of Contents

<details open>
<summary><strong>Open table of contents</strong></summary>

- [Downloads](#downloads)
- [TvC Continuo](#tvc-continuo)
- [Overview](#overview)
- [High-Level Overview](#high-level-overview)
- [Getting Started](#getting-started)
- [Using Continuo](#using-continuo)
  - [Main Window at a Glance](#main-window-at-a-glance)
  - [HUD Section](#hud-section)
  - [Input Monitor and Charge Tracking](#input-monitor-and-charge-tracking)
  - [Damage Scaling](#damage-scaling)
  - [Visuals Section](#visuals-section)
  - [Character Panel Buttons](#character-panel-buttons)
  - [Understanding Frame Data](#understanding-frame-data)
  - [Quick Assist Buttons](#quick-assist-buttons)
  - [Normals and Advantage Workspace](#normals-and-advantage-workspace)
  - [Bottom Workspace Tabs](#bottom-workspace-tabs)
  - [Training Tools](#training-tools)
  - [Megacrash Trainer](#megacrash-trainer)
  - [Lab and Setup Tools](#lab-and-setup-tools)
- [Features](#features)
- [Mission Mode](#mission-mode)
- [HUD Controls Quick Reference](#hud-controls-quick-reference)
- [Installation](#installation)
- [Running the HUD](#running-the-hud)
- [Settings and Session Replay](#settings-and-session-replay)
- [Frame Data Scanning](#frame-data-scanning)
- [Memory Reference](#memory-reference-us-build)
- [Offsets Reference](#tvc-continuo--offsets-reference)
- [Troubleshooting](#troubleshooting)
- [Developer Notes](#developer-notes)
- [License](#license)
- [Open Source and Legal Notice](#open-source-and-legal-notice)
- [Special Thanks](#special-thanks)

</details>

---
# Downloads

The Download for Continuo is through the release page

- https://github.com/cce2955/TvC-Continuo/releases

Download it through here, this page is just for developers who want to fork or PR


# TvC Continuo

A Python-based live memory overlay for Tatsunoko vs. Capcom: Ultimate All-Stars (Wii), running on Dolphin Emulator.
This suite connects directly to Dolphin's RAM and provides:

- A real-time training HUD with health, meter, baroque tracking, and frame advantage
- Live hitbox overlay with per-slot filtering
- Automated move-table and frame-data scanning
- Memory tools for debugging and reverse-engineering fighter structs

---

## Overview

TvC Continuo visualizes live match data pulled directly from Dolphin's memory.
It supports all four character slots (P1-C1, P1-C2, P2-C1, P2-C2) with dynamic pointer resolution to track state across tags, swaps, and giant normalization logic.

The HUD runs at a fixed 60 FPS and is designed to remain stable even during pointer churn, character swaps, assists, and match transitions.

---
## High-Level Overview

TvC Continuo is an all-in-one live training and reverse-engineering toolkit for Tatsunoko vs. Capcom running through Dolphin Emulator.

It reads game memory in real time to provide tools that do not exist in-game, including:

- Live HUD with health, meter, move states, and frame advantage
- Hitbox and projectile visualization overlays
- Mission mode with combo validation and progress tracking
- Automated frame-data extraction
- Debug flag editing and training mode enhancements
- Memory scanning and research utilities
- Character/move mapping systems for custom tools

---

## Getting Started

### 1. Requirements

- Windows
- TvC Continuo from the Release Page
- Dolphin Emulator
- Tatsunoko vs. Capcom (US build recommended)


### 2. Launch Dolphin

Start the game and enter training mode or a match.

### 3. Run Continuo

```bash
TvCGUI.exe
```

### 4. Use the HUD

Once hooked:

- View live fighter data
- Toggle overlays
- Open frame data tools
- Run mission mode
- Use debug tools

## Using Continuo

This section is the nontechnical guide to the main Continuo interface. It explains what each visible control does and when you would normally use it.

Continuo separates the **main control window** from the **in-game transparent overlay**. A button in the main window may toggle an in-game layer, change a training option, or open a dedicated tool window. Turning one layer off does not automatically disable the others.

### Main Window at a Glance

The main window is organized into four working areas:

1. **HUD** controls what information appears in the in-game team overlay.
2. **VISUALS** controls hitboxes, hurtboxes, and reach rulers.
3. **Character panels** show the current fighters and provide Frame Data, Profile Monitor, Mission Mode, and quick-assist controls.
4. **Bottom workspace** provides Normals, Advantage, Events, Debug, and Activity views.

Click **Tools** in the HUD section to expand the dedicated **TRAINING** and **LAB / SETUP** sections.

### HUD Section

These controls affect the informational layers shown over the game.

| Control | What it does |
|---|---|
| **Overlay: ON/OFF** | Shows or hides the core team HUD. This does **not** turn off hitboxes, hurtboxes, rulers, Mission Mode, or trainer prompts. Those layers are independent. |
| **Hit / Block** | Shows or hides the live interaction ribbon used for hit and block feedback. |
| **Combo** | Shows or hides the live combo ledger and route information. |
| **Set: Core / Research / Full** | Cycles through HUD information presets. **Core** enables Meter and Red HP. **Research** adds the damage-scaling display. **Full** additionally enables Hit / Block, Combo, and Tag. Changing an individual HUD toggle changes the preset label to **Custom**. |
| **Dmg: ON/OFF** | Shows or hides the damage-scaling gauges beneath the character health information. |
| **Meter** | Shows or hides the extra meter gain/spend information beside the normal meter display. |
| **Red HP** | Shows or hides recoverable-health information, including the recoverable portion of the health bar and its amount. |
| **Research** | Opens the standalone **read-only Attack Research** window. This is separate from the **Set: Research** HUD preset and does not add another card to the match overlay. |
| **Tag** | Shows or hides the incoming tag resource card. |
| **Clear** | Hides the optional HUD cards in one click while leaving the core overlay available. It does not erase saved data, clear scans, or shut Continuo down. |
| **Tools** | Expands or collapses the TRAINING and LAB / SETUP sections. |

Open **Settings & Sessions → Customize overlay** for a visual preview and independent switches for health, hitstun, damage scaling, inputs, move timing, and the meter rail. Choose separate P1/P2 accent colors and panel opacity. Enable **Edit positions** there, drag either team panel on the game screen, then turn Edit positions off. Changes save immediately; **Reset positions** returns the panels to their corners. **Use dock setting** returns damage scaling to the normal dock control.


### Input Monitor and Charge Tracking

The in-game HUD includes a live input history for the active point fighter. It is meant to answer simple questions such as "Did the game actually see my input?", "How long did I hold that direction?", and "Was my button early or late?"

| Readout | What it means |
|---|---|
| **INPUTS** | Shows the most recent direction and button states using fighting-game numpad notation. The newest input is shown first. A, B, C, Partner, and Taunt inputs are included. |
| **FRAMES** | Shows how many frames each displayed input state lasted. The newest input continues counting until the input changes, then its final count is frozen. |
| **HOLD** | Appears when a button or charge direction has been held for at least 26 frames. While the hold is active, the chip says **HELD** and its frame counter continues increasing. After release, the completed hold remains briefly so you can see the final charge time. |

Same-frame direction and button inputs are grouped together when appropriate, while real repeated taps remain separate entries. This makes motions and confirms easier to read without hiding double taps or repeated button presses.

The HOLD row can track A, B, C, Partner, Taunt, and the eight non-neutral directions. This is especially useful for charge-based actions. For example, you can hold a charge input for Zero or Joe the Condor and see the exact number of frames held before release.

Continuo also includes a deeper **Input Monitor** view for inspecting a selected fighter slot. When that window is open, it shows the live held input, newly pressed and released buttons, recent input stream, current action, and the rule associated with that action. The monitor is **read-only**. It does not inject controller inputs, enable Dolphin debugging, install codes, or change Dolphin configuration.

### Damage Scaling

Turn on **Dmg** or use the **Research** HUD preset to show the live damage-scaling rows beneath the team HUD.

The percentage is a live outgoing damage modifier, not the move's listed base damage:

- **100%** means the fighter is currently at the normal base modifier.
- **Below 100%** means the current hit would be reduced by scaling or another damage-reduction factor.
- **Above 100%** means a damage bonus is currently active.
- A trailing **`*`** means at least one part of the displayed modifier is marked approximate by Continuo rather than fully resolved.

The C1 and C2 rows let you see the current modifier for both members of the team even when only one character is on point. The factor text explains why the percentage changed. Depending on the current match state, you may see labels such as:

| Label | Meaning |
|---|---|
| **TEAM SCALE** or **SCALE** | Current combo-proration multiplier. |
| **BAROQUE** | Damage adjustment currently associated with the attacker's active Baroque state. |
| **GUTS** | Low-health damage reduction on the defender. |
| **DANGER** | Low-health damage bonus on the attacker. |
| **LAST** | Last-character damage bonus when the teammate has been KO'd. |
| **ROLL / PUDDLES** | Roll-specific power or puddle bonuses. |
| **HEIGHT** | Height-based modifier currently being applied. |
| **VAR / DHC / START / TEAM** | Team or sequence correction currently affecting damage. |
| **SCRIPT ?** | Continuo detected an additional script-side modifier but cannot yet resolve its exact contribution. |
| **BASE** | No additional live modifier is currently being displayed. |

In per-hit breakdowns, Continuo may also show **POINT TRACK 5% -> 35%** or **ASSIST TRACK 3% -> 43%**. The first number is the loss applied as that proration lane advances, and the second number is the floor for that lane. The label tells you which scaling track Continuo is using for the breakdown.

Damage scaling is useful for comparing the same route under different conditions. It lets you see when a combo is losing damage to proration, when Guts is reducing the result, or when Baroque and character-specific bonuses are changing the expected damage.

### Visuals Section

The visual controls are independent from the core HUD.

| Control | What it does |
|---|---|
| **Hitboxes: ON/OFF** | Shows or hides attack and projectile hitbox drawings for all currently enabled hitbox slots. |
| **Hitbox 1 / 2 / 3 / 4** | Enables or disables hitbox drawing for one fighter slot without changing the others. |
| **Hurtboxes: ON/OFF** | Shows or hides body and defender hurtbox drawings for all currently enabled hurtbox slots. |
| **Hurtbox 1 / 2 / 3 / 4** | Enables or disables hurtbox drawing for one fighter slot. |
| **Ruler: ON/OFF** | Shows or hides saved active-frame reach guides. |
| **Horizontal** | Shows the furthest forward edge of the saved active hitbox reach. |
| **Vertical** | Shows the full vertical active-hitbox envelope. Horizontal and Vertical can be enabled together. |
| **Ruler 1 / 2 / 3 / 4** | Chooses which fighter slots are allowed to contribute saved ruler guides. |

The numbered visual slots map to the four fighter slots as follows:

| Visual slot | Fighter slot |
|---|---|
| **1** | P1-C1 |
| **2** | P2-C1 |
| **3** | P1-C2 |
| **4** | P2-C2 |

### Character Panel Buttons

Each live fighter panel has its own controls. These buttons always apply to the fighter shown in that panel.

| Control | What it does |
|---|---|
| **Frame Data** | Opens the detailed Frame Data workbench for that fighter. If the rich profile is not ready yet, Continuo opens a loading window and prepares it automatically. |
| **Profile Monitor** | Opens that fighter slot's projectile profile monitor. It lists the character's known possible projectiles and automatically selects the matching live projectile when one appears in-game. |
| **Mission Mode** | Turns Mission Mode on or off for that fighter slot. Mission Mode is its own overlay layer and does not switch the normal HUD or collision overlays on or off. |

**Right-click a fighter panel** to copy that fighter's current base address to the clipboard. This is mainly useful for research and development.

### Understanding Frame Data

The **Frame Data** workbench contains the familiar timing values, but it also exposes several TvC-specific protection and cancel fields that are easy to misread. Select a move to populate the inspector and frame timeline.

#### Basic timing and advantage

| Entry | What it means |
|---|---|
| **Startup** | Frames before the move becomes active. |
| **Active** | Main active hitbox window. |
| **Active 2** | A second separated active window when the move has another active phase. |
| **Hitstop** | Freeze frames caused by the hit. |
| **Hitstun** | Time the opponent remains in hitstun after the hit. |
| **Blockstun** | Time the opponent remains in blockstun. |
| **Observed Block Adv** | Block advantage measured from actual gameplay captures. When available, this is the preferred value for matchup and punish decisions. |
| **Derived Block Adv** | Block advantage calculated from the frame-data fields Continuo has decoded. It is useful when no observed value exists, but it may be less reliable when a move has an unusual recovery or cancel structure. |

#### Protection and cancel entries

| Entry | What it means |
|---|---|
| **Invuln** | Frames where the move is excluded from normal collision. This is invulnerability, not armor. |
| **Counter** | A native protection path detected on the move. This label does **not** mean "counter hit." It is tracked separately from guard points and armor. |
| **Guard Point** | Frames where the move's native protection can guard the listed attack heights. It is not the same thing as full invulnerability. |
| **Armor** | Frames where armor is active. When runtime-confirmed, this is the protection path where the fighter can take damage without entering ordinary hitstun. |
| **Protection** | The combined workbench column that summarizes detected Counter, Guard Point, and Armor information for the move. |
| **Baroque Window** | Frames where that move's native Baroque-cancel gate is open. This does **not** mean the character currently has red life or can Baroque for resource reasons. It only tells you when the move itself permits the cancel. |
| **Cancel windows ?** | Continuo's focused decoder for native cancel-window records found for the move. A move can have more than one window. |
| **Custom Cancel** | A user-editable Continuo window used for lab and balance-patch work. It is **not** the game's decoded native cancel window. |

Where a protection or cancel entry includes **`[R]`**, the listed frame range was observed directly at runtime. Some static invulnerability entries can instead carry confidence markers such as **`[C]`**, **`[H]`**, **`[M]`**, or **`[L]`**. Those markers describe the confidence of the static identification rather than a live-captured frame range.

#### Other useful entries

| Entry | What it means |
|---|---|
| **Attack Property** | Human-readable decoding of important attack-property flags attached to the hit. |
| **Hit Reaction** | What the hit does to the opponent, such as knockback, knockdown, launcher, stagger, crumple, wallbounce, or another special reaction. |
| **Hit Result** | Additional result flags associated with the hit packet. |
| **KB Style / Extra Launch / Launch Adjust / KB X / Arc** | Knockback and launch behavior used to describe where the opponent is sent. |
| **Speed Mod** | A move-local timing modifier that can change how the scripted action progresses. |
| **SuperBG** | The move's super-background flag when that record is available. |
| **Address** | The underlying move-record address. This is mainly useful for research or editing work. |

The **FRAME TIMELINE** in the inspector draws startup, active, invulnerability, Counter, Guard Point, Armor, and Baroque-cancel ranges together so you can see how the windows overlap. Clicking the timeline edits the **Custom Cancel** window only. It does not rewrite the native Baroque or protection windows.

### Quick Assist Buttons

When assist profiles are available, a fighter panel also shows an **Assist** row with up to four quick buttons.

Clicking a quick-assist button selects that configured assist profile for the fighter in that panel. The currently selected assist is highlighted. Use **Assist Setup** when you need to inspect or change the underlying assist choices.

### Normals and Advantage Workspace

The **NORMALS** tab compares the normal attacks for all four currently loaded fighter slots. Its highlight controls are designed to answer common matchup questions without opening the full Frame Data workbench.

| Control | What it does |
|---|---|
| **Fastest** | Highlights each character's fastest valid grounded normal and fastest valid aerial normal. |
| **Damage** | Highlights the highest listed primary-hit damage normal for the grounded and aerial groups. |
| **Block** | Highlights the normal with the best block advantage in each grounded and aerial group. Observed block advantage is preferred when available. |
| **Matchup** | After you select a move row, highlights the equivalent normal notation on the opposing team so the same button/command can be compared directly. |
| **Advanced** | Expands or collapses the extra Safe and Unsafe highlight filters. |
| **Safe** | Highlights each character's best non-negative block-advantage normal for grounded and aerial groups. |
| **Unsafe** | Highlights the most punishable listed normal in each grounded and aerial group. |
| **Punish** | Select a move row to find the opposing team's fastest legal normal punish and highest-damage legal normal punish within that move's block disadvantage. |
| **Live Punish** | Watches the point character's currently executing normal and automatically performs the same punish lookup. If the move is safe, the status line says so instead. |

Click an active highlight button again to clear it. If you click a move row while no metric filter is active, Continuo automatically treats the selection as a **Punish** query.

Clicking the **ADVANTAGE** tab opens the separate **Advantage Matrix** window. The matrix is intended for broader character-to-character punish comparison using the available observed frame data. Negative entries can be inspected for punish options.

### Bottom Workspace Tabs

| Tab | What it does |
|---|---|
| **NORMALS** | Opens the four-character normal-move comparison and highlight tools described above. |
| **ADVANTAGE** | Opens the Advantage Matrix in a separate window. It does not replace the current bottom tab. |
| **EVENTS** | Shows the recent live combat event feed. |
| **DEBUG** | Shows the advanced training/debug flag panel. This panel is write-capable and is intended for users who know which training flags they want to change. Right-clicking a debug row copies its address and performs that row's toggle or cycle action, so use it intentionally. |
| **ACTIVITY** | Shows the current or most recent compact activity/advantage readout. |

### Training Tools

Open these controls with **Tools**.

| Tool | What it does |
|---|---|
| **Cancel Mapper** | Opens a read-only map of the selected character's known cancel relationships. Choose a source move to see targets marked **Allowed**, **Eligible**, **Blocked**, or **Unknown**. Use this when you want to inspect what should cancel into what without running a live cancel test. |
| **Cancel Lab** | Opens the live cancel-testing tool. Choose a fighter, source action, one or more target actions, and a frame window, then test or arm that route. Manual mode waits for the real target input before routing the requested action. |
| **Action Recorder** | Records clean action transitions for a selected fighter slot. Rejected command attempts are kept separately, and captures can be copied or saved as CSV. Useful when you want to see exactly what actions the game recognized. |
| **Timing Monitor** | Shows live hitstop, hitstun, blockstun, and observed advantage. You can arm a Whiff, Hit, or Block capture, then copy or save the captured timing data. |
| **Punish Trainer** | Lets you choose a fighter slot and move, then repeatedly executes that move after a fixed or random countdown. Use it to practice reactions, defense, or punishes against a repeatable setup. |
| **Megacrash Trainer** | Opens the configurable Megacrash reaction trainer. Choose the watched character, move label, matching hit occurrence, trigger chance, delay, and cooldown, then arm the route. See [Megacrash Trainer](#megacrash-trainer) below for the full control guide. |

### Megacrash Trainer

**Megacrash Trainer** is a write-capable practice tool that forces Megacrash under conditions you choose. It is designed for practicing reactions and defensive situations that would be tedious to recreate manually.

The trainer follows one route: a matching hit occurs, the requested occurrence is reached, the chance roll succeeds, the selected delay passes, and then Megacrash is forced.

| Setting | What it does |
|---|---|
| **Character** | Chooses which live point fighter the trainer watches. **Any active point** allows either active point fighter to qualify. |
| **Label** | Chooses the move that must hit. **Any label** allows any qualifying move. The list is grouped into normals, specials, supers, projectiles, and other actions for easier browsing. |
| **Occurrence** | Chooses which matching hit in the current combo should be used, such as the 1st, 2nd, or 3rd occurrence of the selected move. |
| **Chance** | Sets the percentage chance that Megacrash will actually trigger after the selected occurrence is reached. Use 100% for a completely repeatable drill or a lower value for reaction practice. |
| **Crash delay** | Adds a delay in frames after a successful chance roll before Megacrash occurs. |
| **Cooldown** | Minimum time after a forced Megacrash before the trainer can trigger another one. |
| **ARM / OFF** | Turns the configured trainer route on or off. Opening the window does not by itself mean the trainer is armed. |

The live status area shows the current route, the game's combo count when available, and how many matching hits have been seen toward the requested occurrence.

For example, if you choose **P1-C1**, **5B**, **2nd occurrence**, **50% chance**, and **+5f delay**, the trainer waits for the second matching 5B hit in the combo. It then makes a 50% roll. If that roll succeeds, Megacrash is forced five frames later, followed by the configured cooldown.

**Reset defaults** restores **Any active point**, **Any label**, **1st occurrence**, **100% chance**, **+5f delay**, and **3s cooldown**. Resetting the settings does not unexpectedly change the current armed/off state.

### Lab and Setup Tools

These controls are also opened with **Tools**. Several of them intentionally change live game state, so use them when you are setting up training or research rather than during normal play.

| Tool | What it does |
|---|---|
| **Assist Setup** | Opens the shared-character assist picker. It shows the assist profile for each loaded fighter, lets you choose a fighter's assist, refresh the cached routes, and control automatic assist triggering for duplicate characters. |
| **Stage Control** | Opens direct stage-carousel control. While the in-game stage carousel is open, **Grab live ID** reads the current stage ID and **Previous**, **Apply ID**, and **Next** move or set the selected ID. |
| **Extra Characters: ON/OFF** | Activates Yami 1, Yami 2, and Yami 3 on the character select screen. It also exposes a solo character slot. |
| **KO Control: ON/OFF** | Enables or restores Continuo's post-KO control path so the surviving fighter can remain controllable for post-KO lab work.  |
| **Dump Memory** | Creates a zipped snapshot of Dolphin's MEM1 and MEM2 regions for research, comparison, or bug reports. The file is written under `memory_dumps/`. |
| **Win Score** | Opens the live HUD score editor. It can inspect or change visible win counts, score, stage digit, and timer display, and can hold displayed win counts through `NEW HERO` resets. It is turned on by default. |
| **Tool Status** | Opens the runtime status and safety panel. It shows the state of major Continuo tools and exposes maintenance controls such as Refresh, Dump, Safe Restore, and Hard Reset. |

---
## Features

### Real-Time HUD

- 4-panel live display for both teams (HP, meter, position, current move)
- Color-coded health bars with pooled HP (red-life style)
- True 32-bit baroque detection using live HP32 vs Pool32 comparison
- Baroque readiness and activation tracking
- Real-time frame advantage computation based on live hits
- Event feed for hits and inferred attacker/victim pairs
- Correct giant-solo detection (only when C1 and C2 share the same base)
- Per-slot assist phase tracking (fly-in / attack / recover inference)
- Live input history with per-input frame timing and charge/hold tracking
- Dynamic character metadata caching (true struct ID + CSV correction)
- Panel slide and fade animations
- Clipboard integration (right-click fighter panel to copy base address)
- Scrollable debug overlay
---

### HUD Overlay Evolution

The real time HUD now works in game, so you can see everything without hopping to an alt screen

### Visual Upgrades

- Fully redesigned row layout with dynamic width scaling
- Metallic neo-futurist panel styling
- Team-colored borders and accent rails
- Animated scanline energy sweeps across active players
- Fade-in / fade-out transitions for appearing slots
- Smooth value interpolation for meters and UI elements
- Responsive scaling for different Dolphin window sizes
- Cleaner spacing, typography, and alignment
- Improved contrast for instant readability
- Multi-layer alpha effects and glow treatments

### Live Combat Feedback

The overlay now reacts to gameplay events in real time instead of only showing static values.

New popup systems include:

- Damage popups on hit
- Meter gain popups
- Baroque gain / loss popups
- Live frame advantage popups
- Move history timeline
- Highlighted newest route step
- Animated current-state emphasis

Each event fades, animates, and stacks cleanly during fast gameplay. 

### Enhanced Data Display

Every slot can now show richer live combat information including:

- Current HP and max HP
- Animated health bars
- Meter pips plus raw meter values
- Current move labels
- Passive vs active state dimming
- Baroque readiness state
- Stored red-life percentage
- Frame advantage outcomes
- Assist state awareness
- Active character detection
- Team slot identity (C1 / C2)

### Smart Match Logic

The overlay now makes decisions based on match state rather than blindly printing memory values.

Examples include:

- Detecting active point character vs assist
- Cross-team frame advantage pairing
- Ignoring passive states
- Tracking combo gaps correctly
- Resetting stale interactions
- Preserving popup history briefly after events
- Handling tag scenarios more cleanly
- Stable rendering during pointer churn

### Move History System

A major quality-of-life upgrade is the route history display.

Recent actions are shown in sequence, such as:

```text
5A > 5B > 2B > 5C > Baroque Cancel > j.B
```

The newest action is highlighted, older steps fade over time, and Baroque actions receive custom animated styling.

This turns the HUD into a real combo-learning tool rather than just a stat panel.


---

### Hitbox Overlay

- Launch a live hitbox visualizer directly from the HUD with one click
- Per-slot color-coded filter checkboxes (P1/P2/P3/P4)
- Runtime slot filter persisted via hitbox_filter.json
- Overlay runs as an independent subprocess
- Automatic overlay shutdown when the HUD exits
- Live process monitoring (overlay state reflects actual subprocess state)

---

### Frame Data Scanner

- Deep MEM2 analysis via scan_normals_all.py
- Extracts:
  - Startup
  - Active
  - Recovery
  - Hitstun
  - Blockstun
  - Observed and derived block advantage
  - Invulnerability and protection windows
  - Counter, guard point, and armor detection
  - Native Baroque cancel windows
  - Hit reaction and attack-property summaries
  - Damage
  - Knockback
  - Meter values
- Computes estimated frame advantage on hit and block
- Interactive move-table window per character slot
- Background scan worker (non-blocking)
- Auto-scan on character change
- F5 refreshes the currently visible fields inside the Frame Data workbench
- Synchronous fallback scan if worker is unavailable
- Slide-in animation when new scan data arrives

---
## Mission Mode

TvC Continuo now includes a full in-game combo trial and training mission system built directly into the live HUD.

Mission Mode transforms the toolkit from a viewer into an active training platform by validating routes in real time, tracking progress, and celebrating successful clears.

### Core Features

- Per-character mission packs
- Live combo step validation
- Real-time progress tracking
- Auto-reset on dropped routes
- Mission completion save data
- Dynamic mission selection overlay
- Completion celebration effects
- Integrated with the main HUD
- Works directly from live memory state
- No emulator mods required

### How It Works

Each mission contains a sequence of expected actions such as:

```text
5A -> 5B -> 2B -> 5C -> 6C -> Issen
```

The system watches live animation IDs, move labels, hit states, damage confirms, and combo state transitions.

When the correct next action occurs, progress advances instantly.

If the combo drops before completion, progress resets and the player can retry immediately.

### Validation Logic

Mission validation is deeper than simple input matching.

The engine can detect:

- Correct move order
- Fresh move instances
- Repeated moves counted separately
- Damage-confirmed actions
- Baroque cancels
- Whiff-confirmed utility moves
- Combo state continuity
- Hitstun / reaction state transitions
- Route completion
- Route failure and reset

This allows missions to behave like real combo trials instead of basic macro checklists.

### Mission Selection

Players can change missions without leaving training mode.

Open the selector with:

```text
Crouch, Crouch, Taunt
```

Then choose a mission directly from the overlay.

### Progress Saving

Completed missions are stored automatically.

The system tracks:

- Cleared missions
- Current selected mission
- Character mission progress

So players can return later without losing progress.

### Mission Examples

Examples of supported mission styles:

- Basic confirms
- Launcher routes
- Baroque extensions
- Metered enders
- Rejump combos
- Character-specific challenge routes
- Repetition tests
- Execution trials
- Advanced conversions

### Why It Matters

Most fighting games require external guides or combo videos.

Mission Mode brings structured practice directly into Tatsunoko vs. Capcom through Dolphin, with live validation and instant feedback.

That means players can learn routes faster, practice consistently, and build execution without guessing whether they did the combo correctly.

### Future Expansion Ideas

- Difficulty tiers
- Community mission packs
- Custom mission creator
- Time attack clears
- Combo score grading
- Character completion percentage
- Replay verification
- Advanced punish drills
- Defensive challenges
- Matchup-specific training missions
---
### Assist Phase Tracking

Each slot maintains a lightweight assist state machine:

```
None -> flyin -> attack -> recover -> None
```

Assist phases are inferred from animation IDs using:

```
ASSIST_FLYIN_IDS
ASSIST_ATTACK_IDS
```

Snapshots are augmented with:

```
snap["assist_phase"]
snap["is_assist"]
```

This system is conservative and animation-driven.
Future refinement can replace animation ID inference with explicit assist struct mapping.

---

### True 32-bit Baroque Detection

In addition to pool-byte tracking, TvC Continuo reads:

```
+0x28  HP32
+0x2C  Pool32
```

Baroque readiness is determined by:

```
hp32 != pool32
```

The HUD computes:

- baroque_local_hp32
- baroque_local_pool32
- baroque_ready_local
- baroque_red_amt
- baroque_red_pct_max

This avoids inaccuracies from 8-bit pool tracking and reflects actual red-life state.

---

### Memory Tools

- redscan.py / global_redscan.py - detect HP-correlated bytes (red-life / mystery bytes)
- memscan.py - scans MEM1 and MEM2 for ASCII strings and backreferences
- resolver.py - automatically resolves and validates fighter base pointers
- tvc_fill_bacluster.py - read, fill, and restore fighter float clusters (BA40-BA9F)

---

### Interactive Debug Flags

The debug panel supports direct memory writes:

- Toggle flags (PauseOverlay, FreeBaroque, CameraLock, etc.)
- Cycle values (CpuAction, CpuGuard, DummyMeter, CpuDifficulty)
- Momentary triggers (HypeTrigger, SpecialPopup)
- Coupled logic (P2Pause auto-syncs TrPause)

Momentary writes are automatically restored after a short delay to prevent unintended state corruption.

All debug writes are performed safely via wd8() and are guarded against failure.

---

## HUD Controls Quick Reference

This is the compact control reference. For plain-language descriptions and examples, see [Using Continuo](#using-continuo).

| Control | Action |
|---|---|
| **Overlay: ON/OFF** | Toggle only the core in-game team HUD. |
| **Hit / Block** | Toggle the live hit/block interaction ribbon. |
| **Combo** | Toggle the live combo ledger. |
| **Set: Core / Research / Full** | Cycle HUD information presets. Individual HUD toggles switch the preset label to Custom. |
| **Dmg** | Toggle damage-scaling gauges. |
| **Meter** | Toggle meter gain/spend annotations. |
| **Red HP** | Toggle recoverable-health display. |
| **Research** | Open the standalone read-only Attack Research window. |
| **Tag** | Toggle the incoming tag card. |
| **Clear** | Hide optional HUD cards without disabling the core HUD or clearing saved data. |
| **Tools** | Expand or collapse Training and Lab / Setup controls. |
| **Hitboxes / Hurtboxes** | Toggle those collision layers for every currently enabled visual slot. |
| **Visual slot 1 / 2 / 3 / 4** | Toggle an individual fighter source for Hitboxes, Hurtboxes, or Ruler. |
| **Ruler** | Toggle saved active-frame reach guides. |
| **Horizontal / Vertical** | Toggle the forward-reach and vertical-envelope ruler axes independently. |
| **Frame Data** | Open the selected fighter's detailed frame-data workbench. |
| **Profile Monitor** | Open the selected fighter's projectile profile monitor. |
| **Mission Mode** | Toggle Mission Mode for the selected fighter slot. |
| **Assist quick button** | Apply that panel's configured assist selection. |
| **NORMALS** | Show the four-character normal comparison workspace. |
| **ADVANTAGE** | Open the Advantage Matrix in a separate window. |
| **EVENTS** | Show the recent combat event feed. |
| **DEBUG** | Show advanced training/debug flags. |
| **ACTIVITY** | Show the compact latest activity/advantage readout. |
| **Right-click fighter panel** | Copy the fighter base address to the clipboard. |
| **Right-click debug row** | Copy that row's address and perform that row's toggle or cycle action. The DEBUG panel is write-capable. |
| **Mouse wheel in DEBUG** | Scroll the debug flag list. |
| **F5 inside Frame Data workbench** | Refresh the currently visible workbench fields. |

---

## Installation

### Requirements

- Python 3.10+
- Dolphin Emulator (US build recommended)
- Pygame

---

### Quick Setup (Windows)

Full HUD plus all tools:

```
run.bat
```

Hitbox overlay only:

```
hitbox.bat
```

Manual setup:

```
python -m venv .venv
.venv\Scripts\activate
pip install pygame
```

Ensure Dolphin is running TvC and memory read and write is enabled.
If your revision differs from the US build, update addresses in constants.py.

---

## Running the HUD

```
python main.py
```

Wait until the console shows Dolphin is hooked.
When connected, four fighter panels and the event log appear automatically.

In **Custom Music**, choose **Fighter** as the source and **Per tag** for fighter
changes. **Start with** selects the opening P1 or P2 point fighter. **Tag music
follows** can then use both sides (the default) or that side alone; in both
sides mode, whichever team tags in most recently owns the music. Fighters
without custom songs use the current stage playlist. **Stage** source ignores
tag changes.

For stage and fighter songs, **Song ends** defaults to **Repeat until next
match**: the selected track repeats, and the next match picks the next track
in the chosen order. **Next song** advances through that stage or fighter's
folder when a track finishes. Per-track loop points apply in repeat mode;
playlist mode plays each file through once. A **Per tag** fighter change can
still switch songs during a match.

The **Rules** tab has optional low health music, off by default. When one
fighter on a team is KO and the survivor is at or below the chosen health
percentage (30% by default), Continuo looks in
`low_health/characters/<fighter>` for Fighter source or
`low_health/stages/<stage>` for Stage source. If that folder has no song, the
normal music choice stays in effect. These songs also appear in **Loops**.

The same tab can ignore fighter music changes caused by Tag out, DHC, VAR, or
Cross counter independently. Action IDs must be observed for an ignore rule to
apply; unidentified point changes retain the usual behavior. A cooldown from
0 to 300 seconds can prevent another fighter music change shortly after one
plays; its default is 0 seconds.

The **Loops** tab groups songs under expandable **Stages**, **Characters**,
**Menus**, **Extras**, **Low health**, and **Legacy scenes** folders. Select any custom song to
set its loop start and optional end. Enter seconds or
`mm:ss.s`. **First play from** can be **Song beginning** (play the intro once)
or **Loop start** (skip the intro); later repeats always return to the loop start.
Leave End blank to repeat at the file's end. Click **Save loop** above the song
list, or press Enter in either time field. Loop settings are saved as
`loop_points.json` in the selected music folder. WAV songs can loop from the
beginning; use OGG or MP3 to seek to a later loop start. MP3 seek times may be
approximate.

Selecting a song shows its length in the Loops tab. **Use song end** copies
that timestamp into Loop end so you can adjust it before saving or previewing.

Click **Preview loop** to hear about three seconds before the entered End and
three seconds after it jumps to Start. Preview uses the current fields, so you
can listen before saving. **Stop preview** ends it early. Live custom music is
lowered while the preview plays and returns to its prior volume afterward.

If preview plays but the game keeps native music, open **Custom Music** during
the affected match. Check **Detected game state** for the stage and playlist,
then read **Native BGM** at the bottom. **AX BGM pair not verified** means the
game's music voices could not be identified, so Continuo leaves native audio
untouched and holds custom playback. **Inspect BGM** opens a read-only report
of the current AX voices; capture it while native music is audible. The report
helps identify a different game or Dolphin audio layout without guessing at
which voices to mute.

---

## Settings and Session Replay

Click **Settings** in the top HUD dock to export or import a ZIP of preferences
and editable profiles. The ZIP does not contain music tracks, memory dumps,
logs, or generated exports. Restart Continuo after importing settings.

The same window can record the latest minute of HUD fighter snapshots and a
bounded list of music events. Click **Start recording**, reproduce a music
transition, then click **Stop recording** and **Save session**. The JSON file's
`events` list includes raw scene signals, accepted scene changes, native phase
and AX gate changes, and music play/stop events. The read-only replay shows fighter IDs,
health, meter, action, and the music scene/stage sampled by the app. It does
not write to Dolphin.

---

## Frame Data Scanning

```
python -m scan_normals_all
```

Or click **Frame Data** on a fighter panel. Continuo loads or prepares the detailed profile automatically.
Displays move labels, startup, active frames, hitstun, blockstun, and computed frame advantage.

---

## Memory Reference (US Build)

### Slot Pointers

```
803C9FCC  PTR_P1_CHAR1
803C9FDC  PTR_P1_CHAR2
803C9FD4  PTR_P2_CHAR1
803C9FE4  PTR_P2_CHAR2
```

### Fighter Struct (relative to resolved base)

```
+0x14  Character ID (u32)
+0x24  Max HP (s32)
+0x28  Current HP (s32)
+0x2C  Aux HP / Red-life (s32)
+0x40  Last damage chunk (s32)
+0x4C  Super meter primary (s32)
+0xF0  Position X (f32)
+0xF4  Position Y (auto-picked by variance)
```

### Valid Ranges

```
MEM1: 0x80000000-0x817FFFFF
MEM2: 0x90000000-0x93FFFFFF
BAD_PTRS: {0x00000000, 0x80520000}
```

---

## Character IDs (known)

```
1   Ken the Eagle
2   Casshan
3   Tekkaman
4   Polimar
5   Yatterman-1
6   Doronjo
7   Ippatsuman
8   Jun the Swan
9   Unknown (most likely Hakushon)
10  Karas
11  PTX-40A
12  Ryu
13  Chun-Li
14  Batsu
15  Morrigan
16  Alex
17  Viewtiful Joe
18  Volnutt
19  Roll
20  Saki
21  Soki
22  Gold Lightan
23  Yami
24  Yami
25  Yami
26  Tekkaman Blade
27  Joe the Condor
28  Yatterman-2
29  Zero
30  Frank West
```

Extend the list in constants.py under CHAR_NAMES.

---

## Attacker Detection Logic

When a victim's HP drops or a hit state is detected, the system:

1. Logs a hit for that fighter
2. Finds the nearest opponent (distance squared heuristic)
3. Associates that attacker to compute live frame advantage

This method is consistent during training mode and normal gameplay.

---

## File Overview

| File | Purpose |
|------|---------|
| main.py | Main Pygame HUD loop |
| hitboxesscaling.py | Standalone hitbox overlay |
| fighter.py | Reads fighter structs |
| advantage.py | Frame advantage tracker |
| hud_draw.py | Visual HUD rendering |
| layout.py | Panel layout and giant normalization |
| resolver.py | Slot pointer resolution |
| meter.py | Meter state reading |
| moves.py | Move label mapping |
| move_id_map.py | Move ID to name lookup |
| scan_normals_all.py | Full move table scanner |
| scan_worker.py | Background scan thread |
| frame_data_window.py | Interactive frame data GUI |
| debug_panel.py | Debug overlay rendering |
| training_flags.py | Training flag reader |
| redscan.py / global_redscan.py | HP correlation scanners |
| memscan.py | ASCII and pointer reference search |
| tvc_fill_bacluster.py | Fighter float cluster tool |
| events.py | Hit and advantage logging |
| constants.py | Offsets and IDs |
| config.py | Screen and color config |
| portraits.py | Portrait loading |

> **Developer / reverse-engineering reference:** The sections beginning with **TvC Continuo - Offsets Reference** are primarily for contributors, modders, and researchers. Normal users do not need the address and structure reference to use the HUD or training tools.

# TvC Continuo - Offsets Reference

---

## Slot Pointers

```
803C9FCC  PTR_P1_CHAR1
803C9FDC  PTR_P1_CHAR2
803C9FD4  PTR_P2_CHAR1
803C9FE4  PTR_P2_CHAR2
```

---

## Fighter Struct Offsets (relative to resolved base)

```
+0x14  Character ID (u32)
+0x24  Max HP (s32)
+0x28  Current HP (s32)
+0x2C  Aux HP / Red-life (s32)
+0x40  Last damage chunk (s32)
+0x4C  Super meter primary (s32)
+0xF0  Position X (f32)
+0xF4  Position Y (auto-picked by variance)
```

---

## Baroque Detection Offsets (relative to resolved base)

```
+0x28  HP32
+0x2C  Pool32
```

---

## Fighter Struct - Additional Fields (from fighter.py)

```
+0x02A  HP Pool Byte (u8) - experimental pooled health byte
+0x02B  Mystery Byte (u8) - unknown, tracked alongside pool
```

---

## Control / State Offsets (from fighter.py / constants.py)

```
CTRL_WORD_OFF   Control word (u32)
FLAG_062        State flag byte at +0x062
FLAG_063        State flag byte at +0x063
FLAG_064        State flag byte at +0x064
FLAG_072        State flag byte at +0x072
```

---

## Position Offsets (from resolver.py / fighter.py)

```
POSX_OFF        Position X (f32)              - defined in constants.py
POSY_CANDS      Y-offset candidates list       - sampled and selected by variance
0xF4            Fallback Y offset (f32)        - used if variance picker fails
```

---

## Move Block Pattern Offsets (from fd_patterns.py)

```
Phase Record Header:   04 01 02 3F  [u32 phase]  [u16 anim_id]
  - Anim ID at:        +8 from record header start

Legacy Anim Header:    01 ?? 01 3C
  - Anim ID at:        +1 from header start

Speed Mod Pattern:     20 3F 00 00 00 [XX] 04 17
  - Value byte (XX):   +5 from anchor start

SuperBG Pattern:       04 [XX] 60  (after anim anchor)
  - Toggle byte (XX):  +1 from 0x04 marker
  - ON  = 0x04
  - OFF = 0x01

Combo KB Modifier:     01 AC 3D 00 00 00 [XX]
                       01 AC 3F 00 00 00 [XX]
  - Value byte (XX):   +6 from pattern start
  - Scan range:        first 0x200 bytes of move block

Fallback Hitbox Offset: 0x21C
Hitbox Scan Max:        0x600
```

---

## Projectile Pattern (from fd_utils.py)

```
Suffix anchor:   00 00 00 0C FF FF FF FF
Damage word:     4 bytes immediately before suffix - format: 00 00 XX YY
  - Damage:      low 16 bits (XX YY, big-endian)
  - proj_tpl:    absolute address of the 00 00 XX YY word

Strength Slice Anchor:  A6 F0
  - slice[0] = L
  - slice[1] = M
  - slice[2] = H / C
```

---

## Scan Regions (from fd_window.py / fd_utils.py)

```
Default scan start:   0x92477400
Default scan end:     0x94477500
Default region size:  0x1400  (per-move block scan)
Max region clamp:     0x6000
```

---

## Hit Reaction Values (from fd_format.py)

```
0x000000  Stay on ground
0x000001  Ground/Air > KB
0x000002  Ground/Air > KD
0x000003  Ground/Air > Spiral KD
0x000004  Sweep
0x000008  Stagger
0x000010  Ground > Stay Ground, Air > KB
0x000040  Ground > Stay Ground, Air > KB, OTG > Stay OTG
0x000041  Ground/Air > KB, OTG > Stay OTG
0x000042  Ground/Air > KD, OTG > Stay OTG
0x000080  Ground > Stay Ground, Air > KB
0x000082  Ground/Air > KD
0x000083  Ground/Air > Spiral KD
0x000400  Launcher
0x000800  Ground > Stay Ground, Air > Soft KD
0x000848  Ground > Stagger, Air > Soft KD
0x002010  Ground > Stay Ground, Air > KB
0x003010  Ground > Stay Ground, Air > KB
0x004200  Ground/Air > KD
0x800002  Ground/Air > KD, Wall > Wallbounce
0x800008  Alex Flash Chop
0x800020  Snap Back
0x800080  Ground > Crumple, Air > KB
0x800082  Ground/Air > KD, Wall > Wallbounce
0x001001  Wonky: Friender/Zombies grab if KD near ground
0x001003  Wonky variant
```

---

## Knockback Trajectory Values (from fd_format.py)

```
0xBC  Up KB (Spiral)
0xBD  Up Forward KB
0xBE  Down Forward KB
0xC4  Up Pop (j.L / j.M)
```

---

## Stun Value Encoding (from fd_format.py)

```
Display  Raw Byte
10       0x0C
15       0x0F
17       0x11
21       0x15
```

---

## Float Cluster Range

```
BA40-BA9F  Fighter float cluster (read/fill/restore via tvc_fill_bacluster.py)
```

---

## Hitbox Overlay - Slot Base Addresses (MEM2, US Build)

```
P1: 0x9246B9C0
P2: 0x92B6BA00
P3: 0x927EB9E0
P4: 0x92EEBA20
```

---

## Hitbox Struct Layout (relative to slot_base + struct_shift)

```
struct_shift: +0x4C0       (hitbox struct starts here, relative to slot base)

Block offsets within struct:
  Block 0: +0x64
  Block 1: +0xA4
  Block 2: +0xE4

Per-block field offsets:
  +0x00  X position (f32)
  +0x04  Y position (f32)
  +0x18  Radius (f32)
  +0xC3  Active flag (u8)  - 0x53 = active hitbox
```

---

## Hitbox Motion / Camera Struct (static base)

```
Base: 0x8053CB20
  +0x00  X (f32)
  +0x04  Y (f32)
  +0x08  Z (f32)
  +0x0C  W (f32)
```

---

## gui_hitbox_probe.py - Table Probe Constants

```
TAIL_PATTERN:        00 00 00 38 01 33 00 00
ANCHOR_BYTES:        FF FF FF FE
ANCHOR_REL_FROM_TAIL: -0xB0   (anchor is 0xB0 bytes before tail)
SAMPLE_TAIL_REL:     0x10     (capture starts 0x10 before tail)
SCAN_MATCH_LEN:      0x40     (bytes matched when identifying character table)
CAPTURE_SIZE:        0x2000   (8KB capture window)
```

---

## memscan.py - Memory Region Definitions

```
MEM1: 0x80000000 - 0x81800000  (~24 MB)
MEM2: 0x90000000 - 0x94000000  (up to 64 MB)
Chunk size: 0x10000 (64KB reads)
Local scan radius: ±0x4000 around fighter base
```

---

## Valid Memory Ranges

```
MEM1: 0x80000000 - 0x817FFFFF
MEM2: 0x90000000 - 0x93FFFFFF
```
---
## Global Table Pointer
```
Global Table Pointer 0x803AA4C0
```
---

## Bad Pointers

```
0x00000000
0x80520000
```

---

## Meter Offsets (from meter.py / constants.py)

```
METER_OFF_PRIMARY    Super meter primary read address   (relative to fighter base)
METER_OFF_SECONDARY  Super meter secondary/mirror bank  (relative to fighter base)
  - Valid meter range: 0 .. 200,000
  - Known full-meter sentinel: 50,000 (0xC350)
```

---

## Attack / Move ID Offsets (from moves.py / constants.py)

```
ATT_ID_OFF_PRIMARY   Primary attack/state ID (u32, relative to fighter base)
ATT_ID_OFF_SECOND    Secondary attack ID     (u32, relative to fighter base)
```

---

## Normal Move Anim ID Map (from scan_normals_all.py)

```
0x00  5A       0x01  5B       0x02  5C
0x03  2A       0x04  2B       0x05  2C
0x06  6C       0x08  3C
0x09  j.A      0x0A  j.B      0x0B  j.C
0x0E  6B
```

---

## Flag 0x62 State Decode Table (from moves.py)

```
Value   State
0       ATTACK_ACTIVE
8       STUN_LOCK
16      THROW
32      MOVEMENT
40      IMPACTED
48      THROW_TECH
64      ``` (throw knockdown only)
128     ATK_REC
136     ATK_END
160     IDLE_BASE
168     ENGAGED
```

---

## Flag 0x63 State Decode Table (from moves.py)

```
Value   State
0       STARTUP
1       NEUTRAL
4       HITSTUN_PUSH
6       HIT_COMMIT
16      BLOCK_PUSH
17      ATKR_READY
32      STARTUP
34      CHAIN_BUFFER
36      HIT_RESOLVE
37/5    RECOVERY
64      AIR_ASCEND_ATK
65      AIR_CANCEL
68      AIR_IMPACT
70      AIR_PREHIT
96      AIR_CHAIN_BUF1
168     DEF_READY
192     AIR_DESC_ATK
193     FALLING
194     AIR_CHAIN_END
196     KB_VERTICAL
197     KB_GROUNDED
198     KB_VERTICAL_PEAK
224     AIR_CHAIN_BUF2
230     AIR_CHAIN_BUF3
```

---

## scan_normals_all.py - Scanner Constants

```
CLUSTER_GAP:         0x4000   (max gap between blocks treated as same cluster)
CLUSTER_PAD_BACK:    0x400    (padding behind cluster start for region read)
LOOKAHEAD_AFTER_HDR: 0x80     (bytes scanned after anim header for frame data)
PAIR_RANGE:          0x600    (search range when pairing move blocks)
INLINE_ACTIVE_OFF:   0xB0     (offset within move block where inline active frames live)
SLOT_SCAN_BEFORE:    0x2000   (bytes before slot base to begin scan)
SLOT_SCAN_LENS:      0x30000, 0x50000, 0x80000  (192KB / 320KB / 512KB scan windows)
```

---

## scan_normals_all.py - Pattern Headers

```
DAMAGE_HDR:          35 10 20 3F 00
HITREACTION_CODE_OFF: +28 from HITREACTION_HDR match start
HITBOX_OFF_X:        +0x40 from hitbox block base
HITBOX_OFF_Y:        +0x48 from hitbox block base
INLINE_ACTIVE_LEN:   17 bytes
```

---

## Projectile Instance Struct Offsets (from projectile.py)

```
+0x68  Collider pointer (u32)
+0x70  Owner fighter_base pointer (u32)
+0x94  Projectile life counter (u32)
```

---

## Projectile Definition / Template Block (from projectile.py / projectiles.py)

```
Segment header:        00 00 00 04  (block starts here)
Delimiter markers:     FF FF FF FF FF FF FF FF  (FF*8)
                       FF FF FF FF              (FF*4)
Hitbox marker set:     35 0D 20 3F
                       33 0D 20 3F
                       37 0D 20 3F
  - Hitbox radius:     +0x44 from marker
Behavior triple:       ?? [family] [variant]
  - Default family:    0xA5
  - Default variant:   0xA0
Physics cluster:       [f32 speed] [f32 accel] [u32 0x00000000] [f32 cap]
  - Dominant cluster gap: ~0x40 .. 0x800
DEF_REGION (scan target): 0x90800000 .. 0x90A80000
```

---

## redscan.py - Fighter Struct Scan Range

```
SCAN_START: +0x000  (relative to fighter base)
SCAN_END:   +0x100  (exclusive; scans offsets 0x000..0x0FF)
```

---

## special_runtime_finder.py - Special Animation Scan

```
Pattern:    01 [XX] 01 3C   where XX in [0x01 .. 0x1E]
Region:     MEM2 full scan (MEM2_LO .. MEM2_HI)
```
---
## Troubleshooting

If the HUD says "waiting for Dolphin":

- Ensure Dolphin is running TvC
- Close duplicate Dolphin instances
- Relaunch both Dolphin and the HUD

---

## Developer Notes

- Bulk fighter struct reads use rbytes() for performance
- Character metadata cache refreshes automatically on ID change
- Safe wrappers prevent pointer churn from crashing the render loop
- RedScan requires multiple snapshots
- Frame advantage auto-corrects using active frames
- CSV output logged under HIT_CSV
- Compatible with modern Dolphin memory APIs (dolphin_io.py)

---

## License

MIT License - free for community use and research.
Not affiliated with Capcom, Tatsunoko, or the Dolphin project.

---
## Open Source and Legal Notice

This repository contains original tooling, documentation, schemas, tests, and independently researched factual mappings.

It does not include or grant rights to:

- Game executables, disc images, or ROM data
- Memory dumps or save states
- Extracted textures, portraits, audio, or other game assets
- Proprietary source code or decompiled source distributions
- Generated recompilation output containing copyrighted game logic
- Nintendo, Capcom, Eighting, or Dolphin project trademarks and assets

Users must provide their own legally obtained game files and emulator installation.

Research notes such as memory addresses, structure offsets, field descriptions, and behavioral observations are provided for interoperability, debugging, preservation, and educational research.

This project is not affiliated with, endorsed by, or sponsored by Nintendo, Capcom, Eighting, Dolphin Emulator, or any related rights holder.

Unless otherwise noted, original code in this repository is licensed under the MIT License. Third-party components remain subject to their respective licenses. See `LICENSE`, `NOTICE.txt`, `THIRD_PARTY_NOTICES.txt`, and `SOURCE_BOUNDARY.md` for details.

---
## Special Thanks

- Jaaaames - for his amazing foundational work
- The TvC community led by Dr. Science
- Capcom and their amazing work over the years
- Brian Transeau
- This fish sandwich with homemade coleslaw and spicy mayo sitting in front of me, man I wish I had some cajun fries.

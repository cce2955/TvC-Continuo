from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from tvcgui.core.tk_host import tk_call


_WIN: tk.Toplevel | None = None


HELP_TOPICS: list[tuple[str, str]] = [
    (
        "Getting Started",
        """CONTINUO HELP

1. Start Dolphin and launch Tatsunoko vs. Capcom.
2. Enter Training Mode or a match.
3. Start Continuo and wait for the status rail to say Dolphin hooked.
4. Use the controls in the top dock to choose what appears in the HUD.

The main Continuo window is a control surface. Some buttons toggle layers over the game, while other buttons open dedicated tool windows. Turning the core Overlay off does not disable the other visual or training layers.

A good first setup is HUD Settings > Core, then enable Hitboxes or Hurtboxes only when you need them. Open Tools when you want the dedicated training and setup utilities.""",
    ),
    (
        "HUD Controls",
        """HUD CONTROLS

HUD Settings
Opens the display switches and built-in Core, Research, and Full sets. Save the current switches and overlay appearance as a named preset, then load it later. Appearance and positions opens the visual customizer.

Overlay
Shows or hides the in-game overlay. HUD information switches live in HUD Settings.

Hit / Block
Shows the live interaction ribbon for recent hit and block results.

Combo
Shows the live combo ledger and recent route information.

Dmg
Shows the live damage-scaling gauges. See the Damage Scaling topic for how to read them.

HS Scale
Shows a per-hit air-recovery expiry clock. The fill advances toward the scaled recovery point; the red end is the portion removed by combo deterioration. A new hit replaces the old clock immediately. See the Hitstun Scaling topic for details.

Meter
Adds meter gain and spend information beside the normal meter display.

Red HP
Shows recoverable health and its amount.

Attack Research
Opens the standalone read-only Attack Research window from HUD Settings. It is separate from the Research HUD preset.

Tag
Shows or hides the incoming tag resource card.

Clear
Hides optional HUD cards without clearing saved data or shutting Continuo down.

Tools
Expands the Training and Lab / Setup sections.

Help
Opens this guide from Settings.""",
    ),
    (
        "Visuals",
        """VISUAL CONTROLS

Hitboxes
Shows attack and projectile hitbox drawings for the currently enabled fighter slots. The numbered buttons let you include or exclude individual slots.

Hurtboxes
Shows body and defender hurtbox drawings. The numbered buttons work independently from the hitbox filters.

Ruler
Shows saved active-frame reach guides. It does not require hitboxes or hurtboxes to remain visible.

Horizontal
Shows the furthest forward edge of saved active hitbox reach.

Vertical
Shows the full vertical active-hitbox envelope. Horizontal and Vertical can be enabled together.

Envelope
When Horizontal and Vertical are both enabled, Envelope adds the sampled 2D threat shape and its time heatmap. Turn Envelope off if you want both simple ruler axes without the filled 2D coverage shape.

Ruler Lock
Opens a move picker for the four fighter slots. Choose a saved move and Lock Selected to keep that move's ruler visible while you reposition the characters. Unlock Slot or Unlock All returns to the normal automatic move/posture behavior.

Visual slot mapping
1 = P1-C1
2 = P2-C1
3 = P1-C2
4 = P2-C2""",
    ),
    (
        "Character Panels",
        """CHARACTER PANEL CONTROLS

Frame Data
Opens the detailed Frame Data workbench for the fighter shown in that panel. If the rich profile is not ready, Continuo prepares it automatically.

Profile Monitor
Opens that fighter slot's projectile profile monitor. It lists known projectile definitions and follows the matching live projectile when one appears.

Mission Mode
Turns Mission Mode on or off for that fighter slot. Mission Mode is independent from the normal HUD and collision overlays.

Assist buttons
Select the configured quick-assist profile for that fighter. Use Assist Setup when you need to inspect or change the underlying choices.

Right-click a fighter panel
Copies that fighter's current base address to the clipboard. This is mainly useful for research and development.""",
    ),
    (
        "Input Monitor and Charge",
        """INPUT MONITOR AND CHARGE TRACKING

The input monitor shows the recent control history being read from the game. It is intended to help verify execution, timing, and charge behavior without guessing what the game recognized.

Input rows
Recent presses and directional states are shown with their live frame duration. A direction or button that remains held continues counting instead of appearing as repeated new presses.

HOLD row
Long holds are promoted into the HOLD row so charge inputs are easy to spot. Continuo begins treating a continuous hold as charge-relevant after 26 frames. The frame count continues updating while the input remains held.

Release behavior
When the held input is released, the completed hold remains visible briefly so you can read the final duration instead of losing it immediately.

This display is read-only. It reports what Continuo sees and does not inject an input.""",
    ),
    (
        "Damage Scaling",
        """DAMAGE SCALING

The Dmg HUD shows the live damage multiplier being applied to attacks. Think of the percentage as how much of the move's normal damage is currently being retained after the game's active modifiers.

100%
The move is currently using its normal damage value before any additional modifier shown by the gauge.

Below 100%
Damage is being reduced. Combo proration is the most common reason, but low-health effects and other game states can also contribute.

Above 100%
A positive damage modifier is active.

Labels
The HUD may identify contributors such as combo scaling, Baroque, Guts, Danger, team or DHC modifiers, Roll-specific modifiers, height-related effects, or another decoded modifier. The label tells you why the displayed multiplier changed rather than making you infer it from the final damage number alone.

Approximation marker
An asterisk means the displayed modifier is an inferred or approximate interpretation rather than a fully proven direct field. Treat it as useful live guidance, not a guaranteed internal formula.

This gauge is independent from hitstun scaling. Use the HS Scale button when you want to see air-recovery deterioration.""",
    ),
    (
        "Hitstun Scaling",
        """HITSTUN SCALING

TvC deteriorates air-combo recovery lockout separately from ordinary grounded hitstun and separately from damage scaling. The HS Scale button shows that native system as a per-hit expiry clock.

Blue segment
Elapsed frames on the current hit. The fill starts at zero and advances one step whenever the native air-recovery timer decrements.

Gray segment
The runway still available before the scaled recovery endpoint.

Red segment
The part of the raw lockout that is no longer reachable because combo deterioration moved recovery earlier.

Stop marker
The defender's effective recovery endpoint for this hit. The fill stops here rather than filling the raw bar.

DECAY
The exact deterioration step from the native rule. The game removes one frame for every four qualifying count units.

Elapsed / target
The live recovery clock for every hit. It starts full and drains toward zero. Native +0x1220 air-recovery lockout is used when present; ordinary +0x1210 hitstun is the fallback so the clock is visible before decay begins. For example, a raw approximately 24F window with DECAY -4F can produce a 21F effective window that drains 21 through 0. The raw reconstruction is approximate because the HUD may observe the native timer after initialization.

New hit
A new combo hit immediately discards the previous clock and starts a fresh bar, even when the replacement lockout is shorter than the old timer had remaining.

NO TECH
The native CANTUKEMI state is active. Recovery is being blocked independently of the normal untech countdown.

FLOOR 4F
The deterioration branch never initializes the air-recovery lockout below four frames.

This is not ordinary DSTIFF hitstun. The normal resolved hitstun timer is a separate game field.""",
    ),
    (
        "Frame Data Fields",
        """HOW TO READ FRAME DATA

Startup
Frames before the move becomes active.

Active
Frames where the attack can connect. Multi-part active ranges may be shown when a move turns active more than once.

Recovery
Frames after the active portion before the move finishes recovering.

Hitstop
The freeze applied when an attack connects.

Hitstun / Blockstun
How long the defender remains unable to act after being hit or blocking.

Observed Block Advantage
A live measured result from actual gameplay timing. When Continuo has a trustworthy observation, this is preferred over a derived estimate.

Derived Block Advantage
A calculated estimate from the available frame fields. It is useful when no live observation has been recorded yet.

Invuln
Frames where the fighter is not vulnerable to the relevant attack interaction.

Armor
A protection state that can absorb or resist a hit without behaving like ordinary invulnerability.

Counter
Marks a counter-style defensive or reactive property. It is not the same thing as armor or a guard point.

Guard Point
Frames where the move supplies a guarding state during the action.

Baroque Window
Frames where the move permits a Baroque cancel. This field does not mean the character currently has red health available for Baroque.

Protection
A broader decoded protection property used by the move. If the field includes [R], that entry is backed by a runtime observation or runtime-derived result.

Cancel Windows
The portions of the move where ordinary cancel transitions are permitted.

Custom Cancel
A move-specific cancel rule that does not fit the normal shared cancel categories.

Attack Property
Special attack behavior decoded from the move data.

Hit Reaction / Hit Result
What happens to the opponent on connection, such as knockback, knockdown, stagger, launcher, wallbounce, or another reaction.

Knockback / Launch fields
Describe the direction or trajectory applied by the hit.

Speed Mod
A move-specific playback or timing modifier when one is present.

SuperBG
Indicates the move's super-background behavior.

Address
The underlying memory location for the decoded move data. Normal users can ignore this field.

Frame Timeline
A visual summary of the move's startup, active, recovery, and other known timing regions.""",
    ),
    (
        "Normals and Advantage",
        """NORMALS AND ADVANTAGE

Fastest
Highlights each character's fastest valid grounded and aerial normal.

Damage
Highlights the highest listed primary-hit damage normal in each group.

Block
Highlights the normal with the best block advantage. Observed values are preferred when available.

Matchup
After selecting a move, highlights the equivalent notation on the opposing team for direct comparison.

Safe / Unsafe
Safe highlights the best non-negative block-advantage normal. Unsafe highlights the most punishable listed normal.

Punish
Select a move to find the opponent's fastest legal normal punish and highest-damage legal normal punish within that move's disadvantage.

Live Punish
Watches the point character's current normal and performs the same punish lookup automatically.

Advantage tab
Opens the separate Advantage Matrix for broader character-to-character punish comparison.""",
    ),
    (
        "Training Tools",
        """TRAINING TOOLS

Cancel Mapper
Read-only map of known cancel relationships. Choose a source move to inspect Allowed, Eligible, Blocked, or Unknown targets.

Cancel Lab
Live cancel-testing tool. Choose a fighter, source action, target actions, and timing window to test a route.

Action Recorder
Records recognized action transitions. Rejected command attempts are kept separately, and captures can be copied or saved as CSV.

Timing Monitor
Shows live hitstop, hitstun, blockstun, and observed advantage. Arm Whiff, Hit, or Block when you want a focused timing capture.

Punish Trainer
Repeatedly performs a selected move after a fixed or random countdown so you can practice reactions, defense, or punishes.

Megacrash Trainer
Automates Megacrash practice. See the dedicated Megacrash Trainer topic for the settings.""",
    ),
    (
        "Megacrash Trainer",
        """MEGACRASH TRAINER

The Megacrash Trainer creates repeatable defensive practice without requiring you to manually trigger Megacrash every time.

Attacker / character target
Chooses which attacking fighter or scope the trainer watches.

Move label
Limits the trigger to the selected attack when a specific move is chosen.

Occurrence
Chooses which matching hit in the sequence can trigger the trainer. This is useful for multi-hit moves or repeated contacts.

Chance
Sets the probability that an otherwise valid trigger actually activates Megacrash. Lower values make the response less predictable.

Delay
Adds a frame delay between the matching hit and the Megacrash activation.

Cooldown
Prevents another automatic activation until the configured cooldown has expired.

Arm / Off
Arm enables the configured trainer. Off stops automatic Megacrash activation.

Trigger flow
While armed, Continuo watches for the selected attacker, move, and hit occurrence. When the conditions match, it rolls the configured chance, waits the selected delay, activates Megacrash, then observes the cooldown before another trigger is allowed.

Reset
Resets the trainer's current trigger/cooldown state. It does not erase unrelated Continuo settings.""",
    ),
    (
        "Lab and Setup",
        """LAB / SETUP TOOLS

Assist Setup
Inspect and change the assist profile used by loaded fighters and quick-assist buttons.

Extra Stages
Reads or changes the current stage selection while the in-game stage carousel is available.

Extra Characters
Enables the additional character-select entries exposed by Continuo and the solo character slot.

KO Control
Keeps the surviving fighter controllable for post-KO lab work, then restores the normal path when disabled.

Dump Memory
Creates a zipped MEM1/MEM2 snapshot under memory_dumps for research, comparison, or bug reports.

Win Score
Opens the visible score editor for win counts, score, stage digit, and timer display.

Tool Status
Shows the runtime state of major tools and exposes maintenance actions such as Refresh, Dump, Safe Restore, and Hard Reset.""",
    ),
    (
        "Mission Mode",
        """MISSION MODE

Mission Mode turns Continuo into an in-game combo-trial system. It watches the live route and advances only when the expected actions and validation conditions are satisfied.

Starting a mission
Click Mission Mode on the fighter panel you want to train.

Changing missions
With Mission Mode enabled, use Crouch, Crouch, Taunt in-game to open the mission selector, then choose a mission from the overlay.

Progress
Correct steps advance immediately. If the route drops before completion, the mission can reset so you can retry.

Validation can include
Move order, repeated move instances, damage-confirmed actions, Baroque cancels, whiff requirements, combo continuity, hitstun or reaction states, and route completion.

Completed missions are saved automatically.""",
    ),
    (
        "Bottom Tabs",
        """BOTTOM WORKSPACE TABS

NORMALS
Four-character normal-move comparison and highlight tools.

ADVANTAGE
Opens the separate Advantage Matrix window.

EVENTS
Shows the recent live combat event feed.

DEBUG
Advanced training/debug flags. This panel is write-capable. Right-clicking a row copies its address and also performs that row's toggle or cycle action, so use it intentionally.

ACTIVITY
Shows the current or most recent compact activity and advantage readout.""",
    ),
]


def _build_window(root: tk.Tk) -> None:
    global _WIN

    if _WIN is not None:
        try:
            if _WIN.winfo_exists():
                _WIN.deiconify()
                _WIN.lift()
                _WIN.focus_force()
                return
        except Exception:
            _WIN = None

    win = tk.Toplevel(root)
    _WIN = win
    win.title("TvC Continuo Help")
    win.geometry("920x680")
    win.minsize(720, 500)
    win.configure(bg="#08111d")

    def _close() -> None:
        global _WIN
        try:
            win.destroy()
        finally:
            _WIN = None

    win.protocol("WM_DELETE_WINDOW", _close)

    style = ttk.Style(win)
    try:
        style.theme_use("clam")
    except Exception:
        pass
    style.configure(
        "ContinuoHelp.Vertical.TScrollbar",
        background="#18283a",
        troughcolor="#08111d",
        bordercolor="#08111d",
        arrowcolor="#d7e6f5",
    )

    header = tk.Frame(win, bg="#0d1b2a", height=64)
    header.pack(fill="x")
    header.pack_propagate(False)

    tk.Label(
        header,
        text="TvC Continuo Help",
        bg="#0d1b2a",
        fg="#f4f8fc",
        font=("Segoe UI", 18, "bold"),
        anchor="w",
    ).pack(side="left", padx=(18, 8), pady=(10, 0))

    tk.Label(
        header,
        text="What each control does and how to use it",
        bg="#0d1b2a",
        fg="#8fa9c2",
        font=("Segoe UI", 10),
        anchor="w",
    ).pack(side="left", padx=(0, 14), pady=(18, 0))

    body = tk.Frame(win, bg="#08111d")
    body.pack(fill="both", expand=True, padx=12, pady=12)

    nav_frame = tk.Frame(
        body,
        bg="#0b1725",
        width=220,
        highlightthickness=1,
        highlightbackground="#263b51",
    )
    nav_frame.pack(side="left", fill="y", padx=(0, 10))
    nav_frame.pack_propagate(False)

    tk.Label(
        nav_frame,
        text="TOPICS",
        bg="#0b1725",
        fg="#6fb9e6",
        font=("Segoe UI", 9, "bold"),
        anchor="w",
    ).pack(fill="x", padx=12, pady=(12, 6))

    topic_list = tk.Listbox(
        nav_frame,
        bg="#0b1725",
        fg="#dbe8f5",
        selectbackground="#214c69",
        selectforeground="#ffffff",
        highlightthickness=0,
        bd=0,
        relief="flat",
        font=("Segoe UI", 10),
        exportselection=False,
    )
    topic_list.pack(fill="both", expand=True, padx=6, pady=(0, 8))

    for title, _content in HELP_TOPICS:
        topic_list.insert("end", title)

    content_frame = tk.Frame(
        body,
        bg="#0b1725",
        highlightthickness=1,
        highlightbackground="#263b51",
    )
    content_frame.pack(side="left", fill="both", expand=True)

    text_scroll = ttk.Scrollbar(
        content_frame,
        orient="vertical",
        style="ContinuoHelp.Vertical.TScrollbar",
    )
    text_scroll.pack(side="right", fill="y")

    text = tk.Text(
        content_frame,
        wrap="word",
        yscrollcommand=text_scroll.set,
        bg="#0b1725",
        fg="#dbe8f5",
        insertbackground="#ffffff",
        selectbackground="#214c69",
        selectforeground="#ffffff",
        bd=0,
        relief="flat",
        highlightthickness=0,
        padx=22,
        pady=18,
        font=("Segoe UI", 10),
        spacing1=1,
        spacing3=3,
    )
    text.pack(side="left", fill="both", expand=True)
    text_scroll.config(command=text.yview)

    text.tag_configure(
        "title",
        foreground="#78c7f2",
        font=("Segoe UI", 16, "bold"),
        spacing3=12,
    )
    text.tag_configure(
        "heading",
        foreground="#ffffff",
        font=("Segoe UI", 10, "bold"),
        spacing1=8,
        spacing3=2,
    )
    text.tag_configure(
        "body",
        foreground="#cbd9e7",
        font=("Segoe UI", 10),
        spacing3=4,
    )

    def _render(index: int) -> None:
        if not (0 <= index < len(HELP_TOPICS)):
            return
        _title, content = HELP_TOPICS[index]
        text.config(state="normal")
        text.delete("1.0", "end")
        lines = content.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                text.insert("end", "\n", "body")
                continue
            if i == 0:
                text.insert("end", stripped + "\n", "title")
                continue
            next_nonempty = ""
            for j in range(i + 1, len(lines)):
                if lines[j].strip():
                    next_nonempty = lines[j].strip()
                    break
            if len(stripped) <= 34 and next_nonempty and not stripped.endswith("."):
                text.insert("end", stripped + "\n", "heading")
            else:
                text.insert("end", stripped + "\n", "body")
        text.config(state="disabled")
        text.yview_moveto(0.0)

    def _on_topic(_event=None) -> None:
        sel = topic_list.curselection()
        if sel:
            _render(int(sel[0]))

    topic_list.bind("<<ListboxSelect>>", _on_topic)
    topic_list.selection_set(0)
    topic_list.activate(0)
    _render(0)

    footer = tk.Frame(win, bg="#08111d", height=36)
    footer.pack(fill="x", padx=12, pady=(0, 10))
    footer.pack_propagate(False)
    tk.Label(
        footer,
        text="Tip: Hovering controls in the main dock also shows a short description when available.",
        bg="#08111d",
        fg="#6f879d",
        font=("Segoe UI", 9),
        anchor="w",
    ).pack(side="left", fill="x", expand=True, padx=(4, 8))
    tk.Button(
        footer,
        text="Close",
        command=_close,
        bg="#19354b",
        fg="#ffffff",
        activebackground="#24516e",
        activeforeground="#ffffff",
        relief="flat",
        bd=0,
        padx=18,
        pady=5,
        cursor="hand2",
        font=("Segoe UI", 9, "bold"),
    ).pack(side="right")

    win.lift()
    win.focus_force()


def open_help_window() -> None:
    """Open or focus the single Continuo help window on the shared Tk host."""
    tk_call(_build_window)

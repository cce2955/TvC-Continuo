# fd_widgets.py
#
# Small reusable Tk widgets/dialogs to keep fd_window slim.

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, simpledialog, messagebox
from pathlib import Path
import os
import sys

from tvcgui.core.paths import resource_path


class Tooltip:
    def __init__(self, widget, text: str):
        self.widget = widget
        self.text = text or ""
        self.tip = None
        widget.bind("<Enter>", self._show, add=True)
        widget.bind("<Leave>", self._hide, add=True)

    def _show(self, _evt=None):
        if self.tip or not self.text:
            return
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.geometry(f"+{x}+{y}")
        frame = tk.Frame(self.tip, bg="#1e1e1e", bd=1, relief="solid")
        frame.pack(fill="both", expand=True)
        lbl = tk.Label(
            frame,
            text=self.text,
            bg="#1e1e1e",
            fg="#e8e8e8",
            justify="left",
            font=("Segoe UI", 9),
            padx=8,
            pady=6,
        )
        lbl.pack()

    def _hide(self, _evt=None):
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


_APP_ICON_IMAGE = None

def _candidate_icon_paths() -> list[Path]:
    paths: list[Path] = []
    try:
        here = Path(__file__).resolve().parent
    except Exception:
        here = Path.cwd()
    try:
        resource_root = Path(resource_path())
    except Exception:
        resource_root = here
    try:
        if getattr(sys, "frozen", False):
            root = Path(sys.executable).resolve().parent
        else:
            root = resource_root
    except Exception:
        root = resource_root

    # Preserve the old source/frozen search behavior after this module moved:
    # persistent assets beside the executable first, then bundled resources.
    for p in [
        root / "assets" / "icon.png",
        resource_root / "assets" / "icon.png",
        here / "assets" / "icon.png",
        root / "assets" / "portraits" / "Placeholder.png",
        resource_root / "assets" / "portraits" / "Placeholder.png",
        here / "assets" / "portraits" / "Placeholder.png",
        here / "app_title_icon.png",
        resource_root / "app_title_icon.png",
        root / "app_title_icon.png",
    ]:
        if p not in paths:
            paths.append(p)
    return paths


def apply_titlebar_icon(win, parent=None):
    """Apply the preferred bundled app icon/logo to window title bars."""
    global _APP_ICON_IMAGE
    try:
        if _APP_ICON_IMAGE is None:
            for icon_path in _candidate_icon_paths():
                try:
                    if icon_path.exists():
                        _APP_ICON_IMAGE = tk.PhotoImage(file=str(icon_path))
                        break
                except Exception:
                    continue
        if _APP_ICON_IMAGE is not None:
            win.iconphoto(True, _APP_ICON_IMAGE)
    except Exception:
        try:
            if parent is not None:
                img = getattr(parent, "_app_title_icon_image", None)
                if img is not None:
                    win.iconphoto(True, img)
        except Exception:
            pass
    try:
        if _APP_ICON_IMAGE is not None:
            setattr(win, "_app_title_icon_image", _APP_ICON_IMAGE)
    except Exception:
        pass


def configure_light_dialog(dlg, parent=None, *, width: int | None = None, height: int | None = None, resizable=(False, False)):
    apply_titlebar_icon(dlg, parent)
    try:
        dlg.configure(bg="#F3F4F7")
    except Exception:
        pass
    try:
        dlg.resizable(bool(resizable[0]), bool(resizable[1]))
    except Exception:
        pass
    # Do not force a fixed geometry before the dialog's widgets exist; that can
    # clip OK/Cancel rows on systems with different DPI/text metrics.  We keep
    # the desired size as a minimum and finalize after layout instead.
    try:
        if width or height:
            dlg.minsize(int(width or 0), int(height or 0))
    except Exception:
        pass
    try:
        dlg.transient(parent)
    except Exception:
        pass
    try:
        dlg.grab_set()
    except Exception:
        pass
    return dlg




def finalize_dialog_geometry(dlg, min_w: int = 0, min_h: int = 0, pad_w: int = 18, pad_h: int = 18):
    """Resize light dialogs after widgets are packed so buttons never end up clipped."""
    try:
        dlg.update_idletasks()
        req_w = int(dlg.winfo_reqwidth()) + int(max(0, pad_w))
        req_h = int(dlg.winfo_reqheight()) + int(max(0, pad_h))
        final_w = max(int(min_w or 0), req_w)
        final_h = max(int(min_h or 0), req_h)
        if final_w > 0 and final_h > 0:
            dlg.geometry(f"{final_w}x{final_h}")
            try:
                dlg.minsize(final_w, final_h)
            except Exception:
                pass
    except Exception:
        pass


_NATIVE_MESSAGEBOX_FUNCS = {
    "showerror": messagebox.showerror,
    "showwarning": messagebox.showwarning,
    "showinfo": messagebox.showinfo,
    "askyesno": messagebox.askyesno,
    "askokcancel": messagebox.askokcancel,
    "askquestion": messagebox.askquestion,
}


def _resolve_dialog_parent(parent=None):
    try:
        if parent is not None and bool(parent.winfo_exists()):
            return parent
    except Exception:
        pass
    try:
        root = tk._default_root
        if root is not None and bool(root.winfo_exists()):
            return root
    except Exception:
        pass
    return None


def _styled_notice(title, message, *, parent=None, kind="info", buttons=("OK",), default=None):
    """Modal app-styled substitute for Tk's plain native message boxes."""
    host = _resolve_dialog_parent(parent)
    try:
        dlg = tk.Toplevel(host) if host is not None else tk.Toplevel()
    except Exception:
        # Keep the app functional if a modal cannot be constructed during shutdown.
        native = _NATIVE_MESSAGEBOX_FUNCS.get("showerror" if kind == "error" else "showwarning" if kind == "warning" else "showinfo")
        return native(title, message, parent=host) if native else None
    dlg.title(str(title or "TvC Mapping"))
    apply_titlebar_icon(dlg, host)
    dlg.configure(bg="#F3F4F7")
    dlg.resizable(False, False)
    if host is not None:
        try:
            dlg.transient(host)
        except Exception:
            pass

    result = {"value": None}
    palette = {
        "error": ("#A13E4A", "#FFF2F3", "Error"),
        "warning": ("#A56E20", "#FFF7E4", "Warning"),
        "info": ("#356A9C", "#EEF7FF", "Notice"),
        "question": ("#4A668A", "#F0F6FF", "Confirm"),
    }
    rail, rail_text, eyebrow = palette.get(str(kind), palette["info"])

    outer = tk.Frame(dlg, bg="#F3F4F7")
    outer.pack(fill="both", expand=True)
    tk.Frame(outer, bg=rail, height=5).pack(fill="x")
    body = tk.Frame(outer, bg="#F3F4F7")
    body.pack(fill="both", expand=True, padx=18, pady=14)

    tk.Label(body, text=eyebrow.upper(), bg="#F3F4F7", fg=rail, font=("Segoe UI Semibold", 8)).pack(anchor="w")
    tk.Label(body, text=str(title or "TvC Mapping"), bg="#F3F4F7", fg="#111722", font=("Segoe UI Semibold", 13)).pack(anchor="w", pady=(2, 8))
    tk.Label(body, text=str(message or ""), bg="#F3F4F7", fg="#4B5666", font=("Segoe UI", 10), wraplength=520, justify="left").pack(anchor="w")

    btn_row = tk.Frame(body, bg="#F3F4F7")
    btn_row.pack(fill="x", pady=(16, 0))

    def close_with(value):
        result["value"] = value
        try:
            dlg.destroy()
        except Exception:
            pass

    button_values = tuple(buttons or ("OK",))
    for label in reversed(button_values):
        is_primary = label in {"OK", "Yes"}
        btn = ttk.Button(btn_row, text=label, command=lambda v=label: close_with(v))
        btn.pack(side="right", padx=(6, 0))
        if is_primary:
            try:
                btn.focus_set()
            except Exception:
                pass
    try:
        dlg.protocol("WM_DELETE_WINDOW", lambda: close_with("No" if "No" in button_values else "Cancel" if "Cancel" in button_values else "OK"))
        dlg.bind("<Escape>", lambda _e: close_with("No" if "No" in button_values else "Cancel" if "Cancel" in button_values else "OK"))
        if "Yes" in button_values:
            dlg.bind("<Return>", lambda _e: close_with("Yes"))
        else:
            dlg.bind("<Return>", lambda _e: close_with("OK"))
    except Exception:
        pass
    finalize_dialog_geometry(dlg, 420, 180, pad_w=28, pad_h=24)
    try:
        dlg.grab_set()
        dlg.wait_window()
    except Exception:
        pass
    return result["value"]


def _styled_showerror(title=None, message=None, **kwargs):
    _styled_notice(title, message, parent=kwargs.get("parent"), kind="error")
    return "ok"


def _styled_showwarning(title=None, message=None, **kwargs):
    _styled_notice(title, message, parent=kwargs.get("parent"), kind="warning")
    return "ok"


def _styled_showinfo(title=None, message=None, **kwargs):
    _styled_notice(title, message, parent=kwargs.get("parent"), kind="info")
    return "ok"


def _styled_askyesno(title=None, message=None, **kwargs):
    return _styled_notice(title, message, parent=kwargs.get("parent"), kind="question", buttons=("Yes", "No")) == "Yes"


def _styled_askokcancel(title=None, message=None, **kwargs):
    return _styled_notice(title, message, parent=kwargs.get("parent"), kind="question", buttons=("OK", "Cancel")) == "OK"


def _styled_askquestion(title=None, message=None, **kwargs):
    return "yes" if _styled_askyesno(title, message, **kwargs) else "no"


def install_styled_messageboxes():
    """Patch tkinter.messagebox once; all existing module imports share this object."""
    if getattr(messagebox, "_tvc_mapping_styled", False):
        return
    try:
        messagebox.showerror = _styled_showerror
        messagebox.showwarning = _styled_showwarning
        messagebox.showinfo = _styled_showinfo
        messagebox.askyesno = _styled_askyesno
        messagebox.askokcancel = _styled_askokcancel
        messagebox.askquestion = _styled_askquestion
        messagebox._tvc_mapping_styled = True
    except Exception:
        pass


install_styled_messageboxes()

def build_dialog_shell(dlg, *, title_text: str, help_text: str = "", current_text: str = "", address: int | None = None, wrap: int = 500):
    outer = tk.Frame(dlg, bg="#F3F4F7")
    outer.pack(fill="both", expand=True, padx=12, pady=10)
    tk.Label(outer, text=title_text, bg="#F3F4F7", fg="#111111", font=("Segoe UI", 13, "bold")).pack(pady=(0, 8))
    if help_text:
        tk.Label(outer, text=help_text, bg="#F3F4F7", fg="#6E747E", wraplength=wrap, justify="left").pack(anchor="w", pady=(0, 6))
    if current_text:
        tk.Label(outer, text=current_text, bg="#F3F4F7", fg="#1E5EFF", font=("Segoe UI", 10)).pack(pady=(0, 8))
    if address is not None:
        tk.Label(outer, text=f"Address: 0x{int(address):08X}", bg="#F3F4F7", fg="#6E747E", font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))
    return outer


def make_list_picker(parent, *, items: list[str], height: int = 12):
    frame = tk.Frame(parent, bg="#F3F4F7")
    frame.pack(fill="both", expand=True, pady=(6, 8))
    list_frame = tk.Frame(frame, bg="#FFFFFF", bd=1, relief="solid")
    list_frame.pack(fill="both", expand=True)
    sb = tk.Scrollbar(list_frame)
    sb.pack(side="right", fill="y")
    lb = tk.Listbox(list_frame, yscrollcommand=sb.set, height=height, activestyle="none", bd=0, highlightthickness=0, font=("Segoe UI", 10))
    lb.pack(side="left", fill="both", expand=True)
    sb.config(command=lb.yview)
    for line in items:
        lb.insert("end", line)
    return frame, lb


FIELD_HELP = {
    "move": (
        "Move / animation ID. Double-click to replace the animation or open manual animation ID tools. "
        "This changes which move record or animation the row points at; it is separate from damage, stun, and physics values."
    ),
    "kind": "Scanner classification for this row. It is display-only and helps separate normals, specials, supers, and unknown rows.",
    "hits": "Detected per-hit bundles for this move. Expand a multi-hit row to view and edit each hit separately.",
    "link": "Display-only family link. Shows when separate table sections appear to be pieces of one player-facing move, such as Tatsu Start / Spin / End.",
    "damage": "Base damage dealt by this move or hit. Higher values increase raw damage; it does not change hitstun or knockback by itself.",
    "meter": "Meter gain/base meter value attached to the hit. This changes resource behavior, not damage or hit reaction.",
    "startup": "First active frame. Lower values make the hitbox become active sooner; this also rewrites the start of the active range.",
    "active": "Main active frame range, shown as start-end. Extending the end keeps the hit active longer; moving the start changes startup.",
    "active2": "Second active window when the move has one. Use this for moves with a later/reappearing hitbox instead of changing the main active range.",
    "active_windows": "Every decoded active window owned by the move, preserved in order. Read-only so third, fourth, and later active phases are not hidden behind only Active and Active 2.",
    "recovery": "Frames after the final active frame until the animation/action tail finishes. Static values are MOT-derived unless the row reports a runtime-observed source; exposed read-only.",
    "animation_total_frames": "Total frames in the mapped animation clip used by the static recovery calculation. Read-only animation metadata.",
    "adv_hit_derived": "Derived hit advantage from hitstun minus recovery. Positive means the attacker recovers first; negative means the defender recovers first.",
    "hitstun": "Frames the opponent stays in hit reaction after getting hit. More hitstun usually gives more combo time.",
    "blockstun": "Frames the opponent stays locked after blocking. More blockstun improves block advantage and pressure.",
    "hitstop": (
        "UNVERIFIED LABEL: this field is currently called Hitstop, but that has not been confirmed in-game yet. "
        "It may control impact freeze/pause frames or another hit-timing behavior. Treat it as experimental: change one value at a time and verify the result in-game."
    ),
    "adv_block_derived": "Scanner-derived block advantage from blockstun minus recovery. Useful for audit, but not always 1:1 with match reality.",
    "adv_block_observed": "Observed wiki block advantage. The normals preview uses this value first when it exists.",
    "hit_spark": "Hit spark/effect number. User-tested on Ryu 5A: changing this can change the spark type and sometimes the spark location.",
    "stretch_part": "Which body/limb slot the reach stretch packet targets. Change this to test what part gets stretched.",
    "stretch_len": "Reach length scale. Higher values can create longer Dhalsim-style limb reach when the stretch packet is active.",
    "stretch_width": "Reach width/side scale for the stretch packet. Use this to test the second stretch axis.",
    "stretch_height": "Reach height/depth scale for the stretch packet. Use this to test the third stretch axis.",
    "stretch_time": "Stretch timing/slot value. This may change when or how the stretch packet applies.",
    "post_link": "Dangerous post-animation script link. Changing it can freeze the character after the animation; use small tests and restore if needed.",
    "kb_type": (
        "Knockback style number. 9 means normal knockback. Other values are experimental and may route to different knockback behavior."
    ),
    "launch_profile": (
        "Extra Launch. 0 means normal knockback. Any value above 0 turns on extra launch behavior and makes Launch Adjust matter."
    ),
    "kb_unknown": (
        "Launch Adjust. Mostly ignored when Extra Launch is 0. When Extra Launch is above 0, this can change launch speed, direction, curve, or how KB X / Arc are interpreted."
    ),
    "ground_kb_mode": "Raw mode word from the native 35/0C hit push/pull packet. Exposed read-only until every mode is independently named.",
    "ground_kb": "Native 35/0C +8 hit push/pull X float. Negative values on known moves push away; positive values can pull/vacuum inward. Editable when the exact packet is verified.",
    "ground_kb_y": "Native 35/0C +C auxiliary push/pull float. Kept separately from launch Arc because it belongs to the grounded push/pull packet.",
    "push_pull_packets": "Every owned 35/0C push/pull packet associated with this move/hit. Read-only summary so multi-packet moves do not hide secondary settings.",
    "kb_x": "Grounded or standing horizontal knockback. Larger positive values push/launch farther; zero removes most horizontal push.",
    "air_kb": "Airborne or vertical arc value. Higher values usually give a higher/longer relaunch arc; negative values can drive downward arcs.",
    "speed_mod": "Move speed byte. 100 / 0x64 is the normal baseline; lower/higher values can slow down or speed up the move behavior.",
    "invuln": "Native collision exclusion at fighter +0x1218. [R] is the exact action-frame range observed live. [C/H/M/L] is static script confidence. The field counts down by 0x100 per frame.",
    "invuln_kind": "Decoded invulnerability/protection family associated with this move. Read-only evidence field.",
    "invuln_frames": "Decoded or observed invulnerability frame count. Read-only; use the ranges field when multiple windows exist.",
    "invuln_ranges": "All decoded/observed invulnerability frame ranges retained for this move instead of collapsing them to one summary.",
    "invuln_confidence": "Evidence tier for the current invulnerability decode. Runtime observation outranks static heuristics.",
    "invuln_startup_active_limit": "Static boundary used by the invulnerability decoder when separating startup/active ownership. Research/provenance only.",
    "invuln_probe_count": "Number of raw invulnerability evidence packets retained for this move.",
    "invuln_probes": "Raw invulnerability evidence records behind the friendly Invuln summary. Read-only.",
    "counter": "Decoded counter behavior for this action when confirmed or observed.",
    "counter_mask": "Raw native height/category mask associated with the counter route. Kept visible so the friendly label does not hide the actual bits.",
    "counter_source": "Where the counter conclusion came from, such as confirmed reference data or runtime/static evidence.",
    "guard_point": "Decoded guard-point behavior. A guard point blocks qualifying incoming attacks without behaving like ordinary armor.",
    "guard_point_mask": "Native +0x244 guard-height acceptance mask used with the protection-enable route.",
    "guard_point_source": "Evidence source used for the guard-point conclusion.",
    "guard_point_probe_count": "Number of raw guard-point setup/evidence packets retained for this move.",
    "guard_point_probes": "Raw guard-point packet evidence, including setup addresses/masks where available.",
    "armor": "Decoded armor behavior for the move when confirmed or observed.",
    "armor_kind": "Internal decoded armor family. Read-only until separate native armor routes are fully named and safely writable.",
    "armor_mask": "Raw protection/stun-bypass mask associated with the decoded armor behavior.",
    "armor_source": "Evidence source used for the armor conclusion.",
    "armor_probe": "Protection decoder. Visible results require +0x58 bit 0x00400000 paired with a nonzero +0x244 height mask. 0x02000000 Mid, 0x04000000 High, 0x08000000 Low. Confirmed armor uses [C]; [R] includes exact observed frames.",
    "cancel_window_count": "Number of native/static cancel-window records retained for this move.",
    "cancel_windows": "All decoded cancel-window records for this move, preserved raw instead of reducing the move to the first window only.",
    "property_probe_count": "Number of experimental fighter-relative property probes owned by this move.",
    "property_probes": "Raw property-probe evidence used by cancel/Baroque/protection research. Read-only and intentionally not over-labeled.",
    "baroque_research_probe": "Raw/experimental Baroque-related evidence retained by the scanner. The friendly Baroque Window field should be used when the native gate is proven.",
    "attack_property": "Attack property byte. Use the known-value dropdown for confirmed properties; manual entry is for testing unknown bytes.",
    "hit_reaction": "The reaction state/animation chosen on hit, such as standing hitstun, low hitstun, overhead hitstun, knockdown, crumple, or special reactions.",
    "hit_result_flags": "OTG toggle written to fighter +0x240 after the 0x80042F00 clear mask. User-verified: 0x00000000 = OTG off, 0x00004000 = OTG on. Values 0x00004100 and above enter reaction/knockdown families and are kept as manual/custom testing values.",
    "superbg": "Super background flag. Toggles whether the super-style background effect is enabled for this move when the signature is found.",
    "abs": "Absolute move-table address for this row. This is for lookup/debugging and address tools; it is not directly edited as frame data.",
    "combo_kb_mod": "Combo knockback modifier byte when the signature is found. Useful for testing combo scaling/knockback behavior.",
    "combo_kb_mod_addr": "Exact native address of the combo knockback modifier byte. Read-only provenance for the editable Combo KB Mod value.",
    "proj_dmg": "Projectile damage value when a linked projectile template is found. Use this for projectile hits instead of the main damage cell.",
    "proj_tpl": "Exact native u32 address backing Linked Proj Damage. The writer preserves the upper halfword and changes the projectile damage payload only.",
    "proj_marker": "Companion marker address used as evidence when resolving the linked projectile damage template. Read-only research/provenance field.",
    "proj_ps_card_type": "Compact projectile-super card type. 0x0023 and 0x0123 are the card families found on Volnutt, Morrigan, Tekkaman, and Casshan projectile supers. Display-only.",
    "proj_ps_lifetime": "Projectile-super card life/active time. Higher values keep this card active longer.",
    "proj_ps_hit_count": "Projectile-super card hit count or emission limit. This can cap how many hits this card emits.",
    "proj_ps_mode": "Projectile-super card mode/style word. Case-by-case; useful for farming unknown card behavior.",
    "proj_ps_emit_count": "Secondary emit/count value for compact projectile-super cards. Often controls how many pulses or pieces are emitted.",
    "proj_ps_interval": "Hit interval/spacing for compact projectile-super cards when present.",
    "proj_ps_offset_x": "Spawn or motion X value for compact projectile-super cards. On known cards this behaves like source offset or travel component.",
    "proj_ps_offset_y": "Spawn or motion Y value for compact projectile-super cards. On known cards this behaves like source offset or travel component.",
    "proj_ps_scale": "Scale/radius-style value for compact projectile-super cards, often 1.0 on stock cards.",
    "proj_ps_particle_fx": "Particle/effect id for compact projectile-super cards. Farm nearby values to build friendly dropdown names.",
    "proj_ps_projectile_id": "Projectile/object id for compact projectile-super cards.",
    "proj_ps_spawn_bone": "Spawn bone/source selector for compact projectile-super cards when present.",
    "hb_main": "Primary hitbox radius. Bigger values increase reach/coverage; smaller values shrink the detected hit area.",
    "hb": "All detected hitbox radius candidates for the row. r0 is usually the main candidate, but some moves expose several radii.",
    "hb_x": "Raw float stored at the move root +0x40. Exposed read-only because its exact geometry role can vary by move; useful when comparing native hitbox layout.",
    "hb_y": "Raw float stored at the move root +0x48. Exposed read-only because its exact geometry role can vary by move; useful when comparing native hitbox layout.",
    "native_definition_status": "Read-only status from the native Property A/B script decoder. Refresh the selected row to decode older cached profiles.",
    "native_property_a": "Raw native Property A word resolved for this hit definition. This is the source value; the decoded text beside it is only an interpretation.",
    "native_property_a_text": "Friendly decode of proven Property A guard-height/strength/result bits. Raw Property A remains visible so unknown flags are never hidden.",
    "native_property_a_addr": "Native script address associated with the resolved Property A definition/write source.",
    "native_property_b": "Raw native Property B contact-routing word resolved for this hit definition.",
    "native_property_b_text": "Friendly decode of confirmed/observed Property B route bits. Any unrecognized bits are explicitly preserved as unknown.",
    "native_property_b_unknown_mask": "Bits currently present in Property B that have no confirmed/observed/correlated label yet.",
    "native_property_b_addr": "Native script address associated with the resolved Property B definition/write source.",
    "native_definition_scan_size": "Number of bytes inspected for the selected move's native Property A/B definition.",
    "native_property_command_count": "Number of native Property A/B field operations found in this action script.",
    "native_property_phase_count": "Number of distinct Property A/B phases reconstructed from the action script.",
    "native_property_a_initial": "Raw Property A value at the beginning of the first decoded hit-definition phase.",
    "native_property_a_final": "Raw Property A value after all decoded operations in the first hit-definition phase.",
    "native_property_a_initial_unknown_mask": "Bits present in Property A at phase start that do not yet have a confirmed gameplay name. Kept raw on purpose.",
    "native_property_a_final_unknown_mask": "Bits present in final Property A that do not yet have a confirmed gameplay name. Kept raw on purpose.",
    "native_property_a_all_or_mask": "Combined raw bit mask ORed into Property A during the phase.",
    "native_property_a_all_clear_mask": "Combined raw bit mask cleared from Property A during the phase.",
    "native_property_a_post_result_or_mask": "Raw Property A OR mask applied after the native hit-result mutation sequence.",
    "native_property_b_initial": "Raw Property B contact-routing word at the beginning of the first decoded phase.",
    "native_property_b_final": "Raw Property B contact-routing word after the first decoded phase finishes.",
    "native_property_a_modifier_count": "How many SET/OR/CLEAR operations touched native Property A in the first decoded phase.",
    "native_property_b_modifier_count": "How many SET/OR/CLEAR operations touched native Property B in the first decoded phase.",
    "native_result_clear_mask": "Raw mask used by the native hit-result rewrite idiom before the replacement result is ORed in.",
    "native_hit_result_raw": "Raw hit-result word written by the native Property A script sequence. This is exposed without guessing unknown bits.",
    "native_hit_result_code": "Result/reaction code extracted from the raw native hit-result write (raw >> 8). Read-only.",
    "native_hit_result_addr": "Exact script address of the decoded native hit-result write when one is present.",
    "native_phase_map": "Human-readable phase-by-phase reconstruction of Property A, Property B, and hit-result mutations. Read-only.",
    "native_phases": "Raw decoded phase-record list exactly as produced by the native Property A/B parser. Exposed alongside the friendly Phase Chain so parser fields are not hidden.",
    "native_operation_map": "Every decoded Property A/B SET, OR, and CLEAR operation in script order, including its packet address.",
    "native_operations": "Raw decoded Property A/B operation-record list. This preserves parser metadata that the friendly operation map may not print.",
    "native_field_op_count": "Count of structurally valid fighter-relative 04/xx field-operation packets found in this move, including fields whose gameplay meaning is not named yet.",
    "native_field_op_map": "Raw read-only inventory of fighter-relative field operations: target offset, command form, type word, value, and packet address. This is the catch-all exposure layer for not-yet-decoded fields.",
    "native_command_count": "Number of recognized native script command packets found inside this move entry during the selected-row refresh.",
    "native_command_map": "Read-only inventory of every command family currently recognized in this move script, with command id, current label, and packet address. A label means the family is recognized; it does not mean every byte in the packet has been decoded.",
    "native_command_bytes_map": "Raw bytes following every recognized command-family occurrence. This intentionally exposes undecoded payload bytes instead of hiding them behind the current command label.",
    "native_root_word_map": "Every 32-bit word in the first 0x80 bytes of the native move root. Each word stays visible as raw hex; plausible finite float interpretations are shown beside it for research only.",
    "native_raw_span_size": "Exact number of native bytes captured for this selected move entry. The read is bounded by the next known move root when possible.",
    "native_raw_span_hex": "Full captured native byte span for this selected move. This is the raw escape hatch: bytes remain visible even when no decoder or command-family name exists yet.",
    "native_script_head_hex": "First 0x100 bytes of the selected move script, exposed as raw hex for quick comparison/research. The Full Native Span field and Raw Move Data window expose the complete selected entry.",
    "active_start_addr": "Exact resolved native write/source address for the active-window start value.",
    "active_end_addr": "Exact resolved native write/source address for the active-window end value.",
    "active_write_verified": "Scanner verification state for the active-window write target. Read-only provenance, not a gameplay value.",
    "damage_value_addr": "Exact value address inside the resolved damage packet.",
    "damage_write_verified": "Scanner verification state for the damage write target.",
    "stun_addr": "Resolved native stun packet/source address used by this row.",
    "stun_source_addr": "Exact native source address used by the stun resolver when one is known.",
    "stun_write_verified": "Scanner verification state for the stun write target.",
    "hitstun_addr": "Exact native hitstun value/write address when the move authors hitstun directly.",
    "blockstun_addr": "Exact native blockstun value/write address when the move authors blockstun directly.",
    "hitstop_addr": "Exact native hitstop packet/value address.",
    "invuln_addr": "Resolved native invulnerability/collision-exclusion source address for this row.",
    "hit_reaction_addr": "Exact native hit-reaction/result mutation address.",
    "ground_kb_packet_addr": "Address of the native 35/0C push/pull packet selected for this move.",
    "ground_kb_addr": "Exact native X push/pull scalar address.",
    "ground_kb_y_addr": "Exact native auxiliary push/pull scalar address. Gameplay meaning is still intentionally not over-named.",
    "generic_write_owner_start": "Start of the move-owned script interval used when proving generic write targets belong to this move.",
    "generic_write_owner_end": "End of the move-owned script interval used when proving generic write targets belong to this move.",
    "generic_write_verified": "Whether generic resolved write targets passed the move-ownership verification checks.",
    "kb0": "Legacy/raw knockback alias retained by the scanner. Currently mirrors the resolved launch-profile value where that decoder applies.",
    "kb1": "Legacy/raw knockback alias retained by the scanner. Currently mirrors the resolved knockback-style value where that decoder applies.",
    "kb_traj": "Legacy/raw trajectory slot retained by the scanner. It remains exposed even where the current decoder does not assign a semantic value.",
    "move_name_source": "Where the friendly move name came from, such as direct lookup or animation mapping.",
    "table_rel": "Move root offset relative to the live character table. Useful for matching cached profiles back to native table entries.",
    "atkprop_addr": "Legacy/native alias for an attack-property source address retained by the scanner.",
    "source": "How the native move anchor was discovered, such as the character action table, animation header, AIR header, or fallback scan.",
    "air_owner_addr": "Native AIR/header owner address associated with this action anchor when the scanner linked one.",
    "family_role": "Structural role assigned by move-family grouping, such as entry or linked section. Display provenance, not a gameplay property.",
    "linked_owner_abs": "Native owner/root address used when a helper or linked hit section was attached to a parent move family.",
    "action_name": "Action name carried by live timing observations when one was captured. This is provenance for the observation layer.",
    "animation_duration_seconds": "Exact MOT clip duration from the animation database for this action.",
    "animation_char_key": "Character key used to resolve this action in the bundled MOT animation database.",
    "animation_motion": "MOT file used as the animation-duration source for this action, normally 0000.mot.",
    "animation_clip_offset": "Clip offset stored by the MOT animation database for this action.",
    "animation_duration_field_offset": "Source-field offset inside the MOT record used to obtain the duration/total-frame value.",
    "recovery_formula": "Text provenance for how Recovery was produced. Current static MOT recovery is total_animation_frames - final_active_frame.",
    "runtime_observed_recovery": "A previously observed runtime recovery retained when the MOT-derived value becomes the main displayed Recovery field.",
    "attack_property_sig": "Raw signature/context retained by the attack-property pattern resolver for the selected source packet.",
    "combo_kb_sig": "Raw signature/context retained by the combo-knockback modifier resolver.",
    "hb_off": "Relative offset of the hitbox-radius candidate selected as the primary hitbox by the legacy radius scout.",
    "proj_radius_addr": "Exact native backing address for the projectile radius/scale value when one is writable.",
    "custom_cancel_window_data": "Raw serialized custom-cancel-window payload used by the GUI/runtime patch layer. This is tool data, not a decoded native TvC cancel packet.",
    "dispatch_fmt": "Internal/native dispatch record format classification retained by the super integration layer.",
    "dispatch_key": "Stable integration key used to associate the displayed super row with its native dispatch record.",
    "proj_key": "Stable projectile integration key used to associate a displayed row with its scanner record.",
    "proj_move": "Projectile move/link metadata retained by the integration layer.",
    "proj_slice": "Selected projectile payload slice metadata retained by the integration layer.",
    "proj_slices": "Raw projectile payload-slice collection retained by the integration layer.",
    "super_dispatch_row": "Raw super-dispatch record attached to this integrated row. Exposed for audit rather than reduced to the friendly fields only.",
    "damage_flag": "Raw damage/hit-level flag used by the default stun resolver on actions that use the engine's -2/-2 stun sentinel family.",
    "stun_default_level": "Resolved default stun level selected by the action's damage flag when the move uses engine-default hit/blockstun.",
    "runtime_profile_eligible": "Whether this row belongs to the exact default-stun family that is eligible for reverse runtime profiling.",
    "stun_source": "Provenance for the displayed stun values: direct authored packet, engine default resolver, runtime profile, etc.",
    "hitstun_source": "Specific provenance for hitstun when tracked separately by the profile.",
    "blockstun_source": "Specific provenance for blockstun when tracked separately by the profile.",
    "hitstop_source": "Specific provenance for hitstop when tracked separately by the profile.",
    "recovery_source": "Provenance for recovery. Recovery is commonly derived from MOT animation length minus the final active frame rather than stored as a single writable byte.",
    "normal_confirmed": "Scanner classification/evidence that this action is a confirmed normal. Structural metadata, not a gameplay property.",
    "parent_id": "Parent action id for a child/multi-hit segment row when applicable.",
    "parent_abs": "Parent action address for a child/multi-hit segment row when applicable.",
    "char_id": "Character id attached to this discovered payload/dispatch row. Read-only provenance.",
    "discovery_proof": "Read-only evidence string describing how the projectile/super scanner discovered this record.",
    "ownership_proof": "Read-only evidence used to prove that this payload belongs to the selected character/action instead of a nearby unrelated script.",
    "payload_field_summary": "Compact raw summary of known fields found in the child/payload script attached to this super.",
    "owned_damage": "Raw damage value recovered from a child script after dispatch ownership was proven.",
    "dmg_write_addr": "Exact native damage write address used by this projectile/emitter record.",
    "emitter_count": "Raw grouped-emitter count retained by the projectile integration layer.",
    "proj_aliases": "Other projectile/template identities grouped with this emitter record.",
    "dispatch_index": "Zero-based position of this row inside its adjacent native 00/23 dispatch group.",
    "dispatch_count": "Number of rows in this native 00/23 dispatch group.",
    "dispatch_range_start": "Native address at the beginning of the resolved dispatch-row group.",
    "dispatch_range_end": "Native address at the end of the resolved dispatch-row group.",
    "selector_set": "Raw set of action selectors observed across the resolved dispatch group.",
    "map_move_name": "Friendly move-name candidates recovered from the action map for this super/payload association.",
    "super_name": "Resolved friendly super name attached to this dispatch/payload record.",
    "owned_damage_addr": "Exact native child-script address backing the owned damage value after dispatch ownership was proven.",
    "super_entry_count": "Number of nearby native super-entry records associated with this dispatch row.",
    "super_entry_addr": "Address of the primary native super-entry record associated with this dispatch row.",
    "super_entry_post_link": "Raw post-link value carried by the primary associated super-entry record.",
    "payload_only_summary": "Summary of payload candidates found near the child script that were not promoted into the main owned-field set.",
    "payload_only_candidates": "Raw list of payload-only candidate records. Exposed so unpromoted scanner evidence is never discarded from the GUI.",
}


def get_field_help(field: str, fallback: str = "") -> str:
    return FIELD_HELP.get(field, fallback)


HIT_RESULT_FLAG_PRESETS = [
    ("Manual / keep typed value", None),
    ("OTG off / stock simple hit (0x00000000)", 0x00000000),
    ("OTG on / clean toggle (0x00004000)", 0x00004000),
]


def _parse_u32_text(raw: str) -> int:
    raw = (raw or "").strip().replace("_", "")
    if not raw:
        return 0
    return int(raw, 16) if raw.lower().startswith("0x") else int(raw, 10)


def ask_hit_result_flags_with_presets(
    parent,
    *,
    title: str = "Edit Hit Result Flags",
    help_text: str = "",
    initialvalue: int = 0,
    address: int | None = None,
) -> int | None:
    """Hit-result flag editor with a clickable list on top and manual entry below."""
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    configure_light_dialog(dlg, parent, width=560, height=470)

    result = {"value": None}
    outer = build_dialog_shell(
        dlg,
        title_text="Hit Result Flags",
        help_text=help_text or "Choose a known OTG / reaction flag from the list, or type a manual hex / decimal value below.",
        current_text=f"Current: 0x{int(initialvalue) & 0xFFFFFFFF:08X}",
        address=address,
        wrap=520,
    )

    tk.Label(outer, text="Common values:", bg="#F3F4F7", fg="#111111", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 4))
    picker_frame, listbox = make_list_picker(outer, items=[f"0x{int(v or 0) & 0xFFFFFFFF:08X}: {label}" for label, v in HIT_RESULT_FLAG_PRESETS if v is not None], height=6)
    value_var = tk.StringVar(master=dlg, value=f"0x{int(initialvalue) & 0xFFFFFFFF:08X}")

    def set_manual(v: int):
        value_var.set(f"0x{int(v) & 0xFFFFFFFF:08X}")

    known_vals = [v for _label, v in HIT_RESULT_FLAG_PRESETS if v is not None]

    def on_select(_evt=None):
        sel = listbox.curselection()
        if sel:
            set_manual(known_vals[int(sel[0])])

    listbox.bind("<<ListboxSelect>>", on_select)

    tk.Label(outer, text="Manual entry:", bg="#F3F4F7", fg="#111111", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(6, 2))
    entry_row = tk.Frame(outer, bg="#F3F4F7")
    entry_row.pack(anchor="w", fill="x")
    ent = ttk.Entry(entry_row, textvariable=value_var, width=18)
    ent.pack(side="left")
    ttk.Button(entry_row, text="OTG off", command=lambda: set_manual(0x00000000)).pack(side="left", padx=(8, 0))
    ttk.Button(entry_row, text="OTG on", command=lambda: set_manual(0x00004000)).pack(side="left", padx=(6, 0))
    ttk.Button(entry_row, text="Current", command=lambda: set_manual(int(initialvalue) & 0xFFFFFFFF)).pack(side="left", padx=(6, 0))

    btns = tk.Frame(outer, bg="#F3F4F7")
    btns.pack(fill="x", pady=(12, 0))

    def apply_value():
        try:
            val = _parse_u32_text(value_var.get()) & 0xFFFFFFFF
        except Exception:
            messagebox.showerror(title, "Invalid integer value. Use decimal or hex like 0x4000.", parent=dlg)
            return
        result["value"] = val
        dlg.destroy()

    ttk.Button(btns, text="OK", command=apply_value).pack(side="right", padx=(6, 0))
    ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")
    finalize_dialog_geometry(dlg, 560, 470)
    finalize_dialog_geometry(dlg, 500, 260)
    finalize_dialog_geometry(dlg, 500, 240)
    ent.focus_set(); ent.selection_range(0, "end")
    dlg.bind("<Return>", lambda _e: apply_value())
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    try:
        parent.wait_window(dlg)
    except Exception:
        pass
    return result["value"]


def ask_integer_with_help(
    parent,
    *,
    title: str,
    prompt: str,
    help_text: str,
    initialvalue: int = 0,
    minvalue: int | None = None,
    maxvalue: int | None = None,
    address: int | None = None,
) -> int | None:
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    configure_light_dialog(dlg, parent, width=500, height=260)
    result = {"value": None}
    outer = build_dialog_shell(
        dlg,
        title_text=prompt,
        help_text=help_text,
        current_text=f"Current: {int(initialvalue)}",
        address=address,
        wrap=450,
    )
    tk.Label(outer, text="Manual entry:", bg="#F3F4F7", fg="#111111", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
    row = tk.Frame(outer, bg="#F3F4F7")
    row.pack(anchor="w", fill="x")
    var = tk.StringVar(master=dlg, value=str(int(initialvalue)))
    ent = ttk.Entry(row, textvariable=var, width=16)
    ent.pack(side="left")
    bounds = []
    if minvalue is not None:
        bounds.append(f"min {minvalue}")
    if maxvalue is not None:
        bounds.append(f"max {maxvalue}")
    if bounds:
        tk.Label(outer, text="Allowed: " + ", ".join(bounds), bg="#F3F4F7", fg="#6E747E", font=("Segoe UI", 9)).pack(anchor="w", pady=(6, 0))
    btns = tk.Frame(outer, bg="#F3F4F7")
    btns.pack(fill="x", pady=(14, 0))
    def apply_value():
        try:
            raw = (var.get() or "").strip()
            val = int(raw, 16) if raw.lower().startswith("0x") else int(raw, 10)
        except Exception:
            messagebox.showerror(title, "Invalid integer value.", parent=dlg)
            return
        if minvalue is not None and val < minvalue:
            messagebox.showerror(title, f"Value must be at least {minvalue}.", parent=dlg); return
        if maxvalue is not None and val > maxvalue:
            messagebox.showerror(title, f"Value must be no more than {maxvalue}.", parent=dlg); return
        result["value"] = val
        dlg.destroy()
    ttk.Button(btns, text="OK", command=apply_value).pack(side="right", padx=(6, 0))
    ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")
    ent.focus_set(); ent.selection_range(0, "end")
    dlg.bind("<Return>", lambda _e: apply_value())
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    try:
        parent.wait_window(dlg)
    except Exception:
        pass
    return result["value"]


def ask_float_with_help(
    parent,
    *,
    title: str,
    prompt: str,
    help_text: str,
    initialvalue: float = 0.0,
    minvalue: float | None = None,
    maxvalue: float | None = None,
) -> float | None:
    dlg = tk.Toplevel(parent)
    dlg.title(title)
    configure_light_dialog(dlg, parent, width=500, height=240)
    result = {"value": None}
    outer = build_dialog_shell(
        dlg,
        title_text=prompt,
        help_text=help_text,
        current_text=f"Current: {float(initialvalue):.6g}",
        wrap=450,
    )
    tk.Label(outer, text="Manual entry:", bg="#F3F4F7", fg="#111111", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
    row = tk.Frame(outer, bg="#F3F4F7")
    row.pack(anchor="w", fill="x")
    var = tk.StringVar(master=dlg, value=f"{float(initialvalue):.6g}")
    ent = ttk.Entry(row, textvariable=var, width=16)
    ent.pack(side="left")
    btns = tk.Frame(outer, bg="#F3F4F7")
    btns.pack(fill="x", pady=(14, 0))
    def apply_value():
        try:
            val = float((var.get() or "").strip())
        except Exception:
            messagebox.showerror(title, "Invalid number value.", parent=dlg); return
        if minvalue is not None and val < minvalue:
            messagebox.showerror(title, f"Value must be at least {minvalue}.", parent=dlg); return
        if maxvalue is not None and val > maxvalue:
            messagebox.showerror(title, f"Value must be no more than {maxvalue}.", parent=dlg); return
        result["value"] = val
        dlg.destroy()
    ttk.Button(btns, text="OK", command=apply_value).pack(side="right", padx=(6, 0))
    ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right")
    ent.focus_set(); ent.selection_range(0, "end")
    dlg.bind("<Return>", lambda _e: apply_value())
    dlg.bind("<Escape>", lambda _e: dlg.destroy())
    try:
        parent.wait_window(dlg)
    except Exception:
        pass
    return result["value"]


class ManualAnimIDDialog(simpledialog.Dialog):
    def __init__(self, parent, cur_hi=None, cur_lo=None):
        self.cur_hi = cur_hi
        self.cur_lo = cur_lo
        self.result = None
        self.parent = parent
        super().__init__(parent, title="Manual Anim ID (HI / LO)")

    def body(self, master):
        try:
            configure_light_dialog(self, self.parent, width=360, height=210)
        except Exception:
            pass
        master.configure(bg="#F3F4F7")
        ttk.Label(master, text="High byte (HI):").grid(row=0, column=0, sticky="e", padx=6, pady=4)
        ttk.Label(master, text="Low byte (LO):").grid(row=1, column=0, sticky="e", padx=6, pady=4)

        self.hi_var = tk.StringVar(value=f"{self.cur_hi:02X}" if self.cur_hi is not None else "")
        self.lo_var = tk.StringVar(value=f"{self.cur_lo:02X}" if self.cur_lo is not None else "")

        self.hi_entry = ttk.Entry(master, width=6, textvariable=self.hi_var)
        self.lo_entry = ttk.Entry(master, width=6, textvariable=self.lo_var)

        self.hi_entry.grid(row=0, column=1, padx=6, pady=4)
        self.lo_entry.grid(row=1, column=1, padx=6, pady=4)

        ttk.Label(master, text="Hex (00-FF)").grid(row=0, column=2, rowspan=2, padx=6)

        return self.hi_entry

    def validate(self):
        try:
            hi = int(self.hi_var.get(), 16)
            lo = int(self.lo_var.get(), 16)
            if not (0 <= hi <= 0xFF and 0 <= lo <= 0xFF):
                raise ValueError
            self.result = (hi, lo)
            return True
        except Exception:
            messagebox.showerror("Invalid Input", "HI and LO must be hex bytes (00-FF).")
            return False


    def buttonbox(self):
        box = tk.Frame(self, bg="#F3F4F7")
        ttk.Button(box, text="OK", width=8, command=self.ok, default="active").pack(side="right", padx=6, pady=8)
        ttk.Button(box, text="Cancel", width=8, command=self.cancel).pack(side="right", padx=6, pady=8)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)
        box.pack(fill="x")
        finalize_dialog_geometry(self, 360, 210)

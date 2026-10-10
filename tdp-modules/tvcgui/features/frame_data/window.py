# frame_data_window.py

import tkinter as tk
from tvcgui.core.tk_host import tk_call
from .timing_observations import apply_observations_to_scan_data

try:
    from .workbench_v3 import open_editable_frame_data_window as _open_new_editor
    HAVE_NEW_EDITOR = True
except Exception as e:
    HAVE_NEW_EDITOR = False
    _open_new_editor = None
    print(f"WARNING: fd_window editor not available ({e!r})")

# Legacy fallback stays inside this file
def _fmt_stun(v):
    if v is None:
        return ""
    if v == 0:
        return "0"
    return str(v)


def _fmt_adv(v):
    if v is None or v == "":
        return ""
    try:
        return f"{int(v):+d}"
    except Exception:
        return str(v)


def _fmt_move_label(mv):
    aid = mv.get("id")
    name = mv.get("move_name")

    if aid is None:
        return "anim_----"

    if not name or name.strip() == "" or name.startswith("anim_--"):
        name = f"anim_{aid:04X}"

    return f"{name} [0x{aid:04X}]"


def _open_legacy_viewer(slot_label, target_slot):
    try:
        from tkinter import ttk
    except Exception:
        print("tkinter not available")
        return

    cname = target_slot.get("char_name", ",")
    root = tk.Tk()
    root.title(f"Frame data: {slot_label} ({cname})")

    cols = (
        "move", "kind", "damage", "meter",
        "startup", "active", "hitstun", "blockstun", "hitstop",
        "advH", "advBD", "advBO",
        "hb",
        "abs",
    )

    frame = ttk.Frame(root)
    frame.pack(fill="both", expand=True)

    tree = ttk.Treeview(frame, columns=cols, show="headings", height=30)
    vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")

    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)

    headers = [
        ("move", "Move"),
        ("kind", "Kind"),
        ("damage", "Dmg"),
        ("meter", "Meter Base"),
        ("startup", "Start"),
        ("active", "Active"),
        ("hitstun", "HS"),
        ("blockstun", "BS"),
        ("hitstop", "Stop"),
        ("advH", "Adv H"),
        ("advBD", "Derived B"),
        ("advBO", "Observed B"),
        ("hb", "HB"),
        ("abs", "ABS"),
    ]
    for col_id, txt in headers:
        tree.heading(col_id, text=txt)

    tree.column("move", width=260, anchor="w")
    tree.column("kind", width=70, anchor="w")
    tree.column("damage", width=70, anchor="center")
    tree.column("meter", width=70, anchor="center")
    tree.column("startup", width=70, anchor="center")
    tree.column("active", width=90, anchor="center")
    tree.column("hitstun", width=55, anchor="center")
    tree.column("blockstun", width=55, anchor="center")
    tree.column("hitstop", width=70, anchor="center")
    tree.column("advH", width=70, anchor="center")
    tree.column("advBD", width=86, anchor="center")
    tree.column("advBO", width=92, anchor="center")
    tree.column("hb", width=110, anchor="center")
    tree.column("abs", width=120, anchor="center")

    moves = target_slot.get("moves", [])
    moves_sorted = sorted(moves, key=lambda mv: (mv.get("id") is None, mv.get("id") or 0))

    for mv in moves_sorted:
        move_display = _fmt_move_label(mv)

        a_s = mv.get("startup")
        a_e = mv.get("active_end")
        if a_s is not None and a_e is not None:
            active_txt = f"{a_s}-{a_e}"
        elif a_e is not None:
            active_txt = str(a_e)
        else:
            active_txt = ""

        hb_x = mv.get("hb_x")
        hb_y = mv.get("hb_y")
        if hb_x is not None or hb_y is not None:
            if hb_x is None:
                hb_txt = f"-x{hb_y:.1f}"
            elif hb_y is None:
                hb_txt = f"{hb_x:.1f}x-"
            else:
                hb_txt = f"{hb_x:.1f}x{hb_y:.1f}"
        else:
            hb_txt = ""

        adv_hit = mv.get("adv_hit")
        adv_block_derived = mv.get("adv_block_derived")
        if adv_block_derived is None:
            adv_block_derived = mv.get("adv_block")
        adv_block_observed = mv.get("adv_block_observed")
        if adv_block_observed is None:
            adv_block_observed = mv.get("observed_adv_block")

        tree.insert(
            "",
            "end",
            values=(
                move_display,
                mv.get("kind", ""),
                "" if mv.get("damage") is None else str(mv.get("damage")),
                "" if mv.get("meter") is None else str(mv.get("meter")),
                "" if a_s is None else str(a_s),
                active_txt,
                _fmt_stun(mv.get("hitstun")),
                _fmt_stun(mv.get("blockstun")),
                "" if mv.get("hitstop") is None else str(mv.get("hitstop")),
                "" if adv_hit is None else f"{adv_hit:+d}",
                _fmt_adv(adv_block_derived),
                _fmt_adv(adv_block_observed),
                hb_txt,
                f"0x{mv.get('abs', 0):08X}" if mv.get("abs") else "",
            ),
        )

    root.mainloop()


def open_frame_data_window(slot_label, scan_data):
    """
    Public entry point used by main.py.
    Prefers the NEW fd_window editor; falls back to legacy viewer.
    """
    if not scan_data:
        return
    # Prefer the lean modular editor. Timing observations are merged only for
    # the requested fighter inside the editor entry point.
    if HAVE_NEW_EDITOR and _open_new_editor is not None:
        _open_new_editor(slot_label, scan_data)
        return

    # Legacy fallback
    target = None
    for s in scan_data:
        if s.get("slot_label") == slot_label:
            target = s
            break
    if not target:
        return

    _open_legacy_viewer(slot_label, target)



# ---------------------------------------------------------------------------
# Immediate non-blocking Frame Data launch shell
# ---------------------------------------------------------------------------
# The always-on HUD deliberately uses a compact preview snapshot with no live
# write addresses.  This shell appears right away while the worker loads the
# *editable* profile cache, then main.py swaps it for the real workbench.
_FD_LOADING_WINDOWS = {}


def open_frame_data_loading_window(slot_label, char_name=""):
    """Show a lightweight native loading window immediately.

    The worker-owned full profile cache is intentionally loaded off the pygame
    click path.  The shell prevents the old "click again once warm" behavior
    while preserving a responsive HUD.
    """
    label = str(slot_label or "Frame Data")
    cname = str(char_name or "").strip()

    def create(master_root):
        old = _FD_LOADING_WINDOWS.pop(label, None)
        try:
            if old is not None and bool(old.winfo_exists()):
                old.destroy()
        except Exception:
            pass
        win = tk.Toplevel(master_root)
        _FD_LOADING_WINDOWS[label] = win
        win.title(f"Frame Data Editor: {label}{(' (' + cname + ')') if cname else ''}")
        try:
            win.geometry("610x135")
            win.minsize(560, 125)
        except Exception:
            pass
        frame = tk.Frame(win, padx=20, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Opening editable frame data…", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(
            frame,
            text="Loading live, writable move packets for this fighter.",
            justify="left",
            wraplength=550,
        ).pack(anchor="w", pady=(9, 0))
        win.protocol("WM_DELETE_WINDOW", lambda: _close_loading_on_tk(label))

    tk_call(create)


def _close_loading_on_tk(slot_label):
    label = str(slot_label or "Frame Data")
    win = _FD_LOADING_WINDOWS.pop(label, None)
    try:
        if win is not None and bool(win.winfo_exists()):
            win.destroy()
    except Exception:
        pass


def close_frame_data_loading_window(slot_label):
    """Close the transient launcher after a writable workbench is ready."""
    tk_call(lambda _root: _close_loading_on_tk(slot_label))


# ---------------------------------------------------------------------------
# Main-GUI Cancel Mapper launcher
# ---------------------------------------------------------------------------
_CANCEL_MAPPER_LOADING_WINDOW = None


def open_cancel_mapper_window(scan_data, initial_slot="P1-C1"):
    """Open the standalone Cancel Mapper from rich scan rows."""
    rows = [row for row in (scan_data or []) if isinstance(row, dict) and row.get("moves")]
    if not rows:
        return False

    def create(master_root):
        close_cancel_mapper_loading_window()
        from .cancel_mapper import open_standalone_cancel_mapper

        open_standalone_cancel_mapper(
            parent=master_root,
            slot_rows=rows,
            initial_slot=str(initial_slot or "P1-C1"),
            status_callback=lambda text: print(f"[cancel mapper] {text}", flush=True),
        )

    tk_call(create)
    return True


def open_cancel_mapper_loading_window():
    """Show immediate feedback while the rich frame-data profile is prepared."""
    def create(master_root):
        global _CANCEL_MAPPER_LOADING_WINDOW
        old = _CANCEL_MAPPER_LOADING_WINDOW
        try:
            if old is not None and bool(old.winfo_exists()):
                old.destroy()
        except Exception:
            pass
        win = tk.Toplevel(master_root)
        _CANCEL_MAPPER_LOADING_WINDOW = win
        win.title("Cancel Mapper")
        try:
            win.geometry("620x145")
            win.minsize(560, 130)
        except Exception:
            pass
        frame = tk.Frame(win, padx=20, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Opening Cancel Mapper...", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(
            frame,
            text="Loading the rich move profile so normals, specials, supers, addresses, and route evidence are available.",
            justify="left",
            wraplength=570,
        ).pack(anchor="w", pady=(9, 0))
        win.protocol("WM_DELETE_WINDOW", close_cancel_mapper_loading_window)

    tk_call(create)


def close_cancel_mapper_loading_window():
    def close(_root=None):
        global _CANCEL_MAPPER_LOADING_WINDOW
        win = _CANCEL_MAPPER_LOADING_WINDOW
        _CANCEL_MAPPER_LOADING_WINDOW = None
        try:
            if win is not None and bool(win.winfo_exists()):
                win.destroy()
        except Exception:
            pass

    tk_call(close)


# ---------------------------------------------------------------------------
# Main-GUI Cancel Lab launcher
# ---------------------------------------------------------------------------
_CANCEL_LAB_LOADING_WINDOW = None
_CANCEL_LAB_PICKER_WINDOW = None


def open_cancel_lab_window(scan_data, initial_slot="P1-C1"):
    """Open Live Cancel Lab immediately on the first fighter slot.

    The lab owns its fighter-slot selector, so the main GUI never opens a
    separate picker. P1-C1 is preferred whenever its rich profile is ready;
    otherwise the first available profile is used.
    """
    rows = [row for row in (scan_data or []) if isinstance(row, dict) and row.get("moves")]
    if not rows:
        return False

    def create(master_root):
        close_cancel_lab_loading_window()
        from .cancel_lab import open_cancel_lab

        def slot_name(row):
            return str(row.get("slot_label") or row.get("slot") or "P1-C1").strip().upper()

        # The main-GUI button always starts on the first fighter slot. Slot
        # changes happen inside Live Cancel Lab itself.
        selected_row = next((row for row in rows if slot_name(row) == "P1-C1"), rows[0])
        open_cancel_lab(
            parent=master_root,
            slot_label=slot_name(selected_row),
            target_slot=selected_row,
            moves=list(selected_row.get("moves") or []),
            profiles=rows,
            status_callback=lambda text: print(f"[cancel lab] {text}", flush=True),
        )

    tk_call(create)
    return True

def open_cancel_lab_loading_window():
    """Show immediate feedback while the rich profile for Cancel Lab is prepared."""
    def create(master_root):
        global _CANCEL_LAB_LOADING_WINDOW
        old = _CANCEL_LAB_LOADING_WINDOW
        try:
            if old is not None and bool(old.winfo_exists()):
                old.destroy()
        except Exception:
            pass
        win = tk.Toplevel(master_root)
        _CANCEL_LAB_LOADING_WINDOW = win
        win.title("Cancel Lab")
        try:
            win.geometry("620x145")
            win.minsize(560, 130)
        except Exception:
            pass
        frame = tk.Frame(win, padx=20, pady=18)
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text="Opening Cancel Lab...", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        tk.Label(
            frame,
            text="Loading the rich move profile so the live source and target action lists are available.",
            justify="left",
            wraplength=570,
        ).pack(anchor="w", pady=(9, 0))
        win.protocol("WM_DELETE_WINDOW", close_cancel_lab_loading_window)

    tk_call(create)


def close_cancel_lab_loading_window():
    def close(_root=None):
        global _CANCEL_LAB_LOADING_WINDOW
        win = _CANCEL_LAB_LOADING_WINDOW
        _CANCEL_LAB_LOADING_WINDOW = None
        try:
            if win is not None and bool(win.winfo_exists()):
                win.destroy()
        except Exception:
            pass

    tk_call(close)

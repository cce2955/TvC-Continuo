from __future__ import annotations

import time
import tkinter as tk
from typing import Any, Callable

try:
    from tvcgui.core.tk_host import tk_call
except Exception:  # pragma: no cover
    tk_call = None

try:
    from tvcgui.platform.dolphin import rd8, rd32, wd8, wd32
except Exception:  # pragma: no cover
    rd8 = None
    rd32 = None
    wd8 = None
    wd32 = None

try:
    import tvcgui.platform.patch_manager as _rpm
except Exception:  # pragma: no cover
    _rpm = None

try:
    from tvcgui.features.training.win_counter_gate import (
        is_win_counter_runtime_active,
        win_counter_block_message,
    )
except Exception:  # pragma: no cover
    def is_win_counter_runtime_active() -> bool:
        return False

    def win_counter_block_message() -> str:
        return "Win Counter writes are currently unavailable."

_OPEN_WINDOW: tk.Toplevel | None = None

_BG = "#0d1018"
_PANEL = "#171b27"
_PANEL_2 = "#202638"
_BORDER = "#31384d"
_TEXT = "#e9edf7"
_MUTED = "#aeb8cc"
_ACCENT = "#89a7ff"
_ACTIVE = "#2a3553"
_FIELD = "#0f1320"
_BAD = "#ff8a8a"
_GOOD = "#95e6c8"

# Shared arcade HUD score/stage/win digit texture table.
# Values are written as big-endian u32 pointers into the loaded texture bank.
SCORE_DIGIT_TEXTURES: dict[int, tuple[int, int]] = {
    0: (0x921D13C0, 0x921D1300),
    1: (0x921D1660, 0x921D15C0),
    2: (0x921D1920, 0x921D1860),
    3: (0x921D1BE0, 0x921D1B20),
    4: (0x921D1EA0, 0x921D1DE0),
    5: (0x921D2160, 0x921D20A0),
    6: (0x921D2420, 0x921D2360),
    7: (0x921D26E0, 0x921D2620),
    8: (0x921D29A0, 0x921D28E0),
    9: (0x921D2C60, 0x921D2BA0),
}

# Timer uses its own Time_0.tpl through Time_9.tpl texture bank.
TIMER_DIGIT_TEXTURES: dict[int, tuple[int, int]] = {
    0: (0x921EEEE0, 0x921EEDE0),
    1: (0x921EF2A0, 0x921EF1A0),
    2: (0x921EF660, 0x921EF560),
    3: (0x921EFA20, 0x921EF920),
    4: (0x921EFDE0, 0x921EFCE0),
    5: (0x921F01A0, 0x921F00A0),
    6: (0x921F0560, 0x921F0460),
    7: (0x921F0920, 0x921F0820),
    8: (0x921F0CE0, 0x921F0BE0),
    9: (0x921F10A0, 0x921F0FA0),
}

_SCORE_TEXTURE_TO_DIGIT = {pair: digit for digit, pair in SCORE_DIGIT_TEXTURES.items()}
_TIMER_TEXTURE_TO_DIGIT = {pair: digit for digit, pair in TIMER_DIGIT_TEXTURES.items()}

# Digit positions are ones, tens, hundreds.
# VS = active in-fight top HUD copy (vs_1_* / vs_2_*).
# HUD = result/arcade win_* copy.
# SVM = SVM-prefixed result copy.
WIN_DIGIT_BANKS: dict[str, dict[str, tuple[tuple[int, int], tuple[int, int], tuple[int, int]]]] = {
    "P1": {
        "VS": (
            (0x80BEBB80, 0x80BEBB84),
            (0x80BEBDA0, 0x80BEBDA4),
            (0x80BEBFC0, 0x80BEBFC4),
        ),
        "HUD": (
            (0x80BE9BC0, 0x80BE9BC4),
            (0x80BE9DE0, 0x80BE9DE4),
            (0x80BEA000, 0x80BEA004),
        ),
        "SVM": (
            (0x80BF1000, 0x80BF1004),
            (0x80BF1220, 0x80BF1224),
            (0x80BF1440, 0x80BF1444),
        ),
    },
    "P2": {
        "VS": (
            (0x80BEC940, 0x80BEC944),
            (0x80BECB60, 0x80BECB64),
            (0x80BECD80, 0x80BECD84),
        ),
        "HUD": (
            (0x80BEADC0, 0x80BEADC4),
            (0x80BEAFE0, 0x80BEAFE4),
            (0x80BEB200, 0x80BEB204),
        ),
        "SVM": (
            (0x80BF1DC0, 0x80BF1DC4),
            (0x80BF1FE0, 0x80BF1FE4),
            (0x80BF2200, 0x80BF2204),
        ),
    },
}

# Pane enable byte directly before each pane name. The active VS HUD disables
# the tens/hundreds pane when the real count is below 10/100, so texture-only
# writes made values like 21 appear as only 1. These flags fix that.

# VS HUD mode parent toggles. When a side is at 0/new hero, the game disables
# the win group and enables NewHero_* instead. Texture writes still land, but
# they are hidden until the parent mode is switched back to win_vs*.
VS_WIN_MODE_ENABLES: dict[str, dict[str, int]] = {
    "P1": {"WIN": 0x80BEB85B, "NEWHERO": 0x80BEB63B},
    "P2": {"WIN": 0x80BEC61B, "NEWHERO": 0x80BEC3FB},
}

# The active in-fight HUD uses a separate tiny "S" pane for plural wins:
#   1 WIN  -> S pane off
#   2 WINS -> S pane on
# Zero defaults to NEW HERO unless 0 WINS is explicitly forced.
VS_WIN_PLURAL_S_ENABLES: dict[str, int] = {
    "P1": 0x80BEC0DB,  # vs_1_s
    "P2": 0x80BECE9B,  # vs_2_s
}

# Raw Versus win counters (two known mirrors per player). Monitor the
# maximum so later updates to the second mirror do not count the same win twice.
# The persisted HUD total is independent of both counters and their resets.
RAW_WIN_COUNTERS: dict[str, tuple[int, ...]] = {
    "P1": (0x803EB9FC, 0x803EBA08),
    "P2": (0x803EBA00, 0x803EBA04),
}

# Runtime state for HUD overrides that must survive closing the editor window.
# The window is only the control panel; main.py calls tick_hud_editor_state()
# so active holds/freezes keep writing while the window is closed.
_HUD_EDITOR_RUNTIME_STATE: dict[str, Any] = {
    # Default ON at 0, but keep the game's normal NEW HERO presentation.
    # Turn this on only when the operator explicitly wants visible 0 WINS text.
    "force_zero_as_win": False,
    "use_hud": True,
    "use_svm": False,
    "last_tick_time": 0.0,
    "last_force_time": 0.0,
    "status": "Raw-counter auto tracking armed; zero shows NEW HERO.",
    "holds": {
        "P1": {"enabled": True, "auto": True, "value": 0, "last_raw": None},
        "P2": {"enabled": True, "auto": True, "value": 0, "last_raw": None},
    },
}

WIN_PANE_ENABLES: dict[str, dict[str, tuple[int, int, int]]] = {
    "P1": {
        "VS": (0x80BEBA7B, 0x80BEBC9B, 0x80BEBEBB),
        "HUD": (0x80BE9ABB, 0x80BE9CDB, 0x80BE9EFB),
        "SVM": (0x80BF0EFF, 0x80BF111F, 0x80BF133F),
    },
    "P2": {
        "VS": (0x80BEC83B, 0x80BECA5B, 0x80BECC7B),
        "HUD": (0x80BEACBB, 0x80BEAEDB, 0x80BEB0FB),
        "SVM": (0x80BF1CBF, 0x80BF1EDF, 0x80BF20FF),
    },
}

# Arcade score display panes. Order is right-to-left so index 0 is the rightmost
# visible digit. The two special suffix panes are the fixed trailing zeroes.
SCORE_DIGIT_BANKS: dict[str, tuple[tuple[int, int], ...]] = {
    "P1": (
        (0x80BE53C0, 0x80BE53C4),  # 1_00
        (0x80BE55E0, 0x80BE55E4),  # 1_01
        (0x80BE5800, 0x80BE5804),  # 1_1
        (0x80BE5A20, 0x80BE5A24),  # 1_10
        (0x80BE5C40, 0x80BE5C44),  # 1_100
        (0x80BE5E60, 0x80BE5E64),  # 1_1000
        (0x80BE6080, 0x80BE6084),  # 1_10000
        (0x80BE62A0, 0x80BE62A4),  # 1_100000
        (0x80BE64C0, 0x80BE64C4),  # 1_1000000
        (0x80BE66E0, 0x80BE66E4),  # 1_10000000
        (0x80BE6900, 0x80BE6904),  # 1_100000000
        (0x80BE6B20, 0x80BE6B24),  # 1_1000000000
        (0x80BE6D40, 0x80BE6D44),  # 1_10000000000
        (0x80BE6F60, 0x80BE6F64),  # 1_100000000000
    ),
    "P2": (
        (0x80BE7280, 0x80BE7284),  # 2_00
        (0x80BE74A0, 0x80BE74A4),  # 2_01
        (0x80BE76C0, 0x80BE76C4),  # 2_1
        (0x80BE78E0, 0x80BE78E4),  # 2_10
        (0x80BE7B00, 0x80BE7B04),  # 2_100
        (0x80BE7D20, 0x80BE7D24),  # 2_1000
        (0x80BE7F40, 0x80BE7F44),  # 2_10000
        (0x80BE8160, 0x80BE8164),  # 2_100000
        (0x80BE8380, 0x80BE8384),  # 2_1000000
        (0x80BE85A0, 0x80BE85A4),  # 2_10000000
        (0x80BE87C0, 0x80BE87C4),  # 2_100000000
        (0x80BE89E0, 0x80BE89E4),  # 2_1000000000
        (0x80BE8C00, 0x80BE8C04),  # 2_10000000000
        (0x80BE8E20, 0x80BE8E24),  # 2_100000000000
    ),
}

STAGE_DIGIT_PAIR = (0x80BE9780, 0x80BE9784)
TIMER_TENS_PAIR = (0x80BE4E80, 0x80BE4E84)
TIMER_ONES_PAIR = (0x80BE50A0, 0x80BE50A4)
TIMER_RAW_ADDR = 0x809BDD10
ARCADE_SCORE_RAW_ADDR = 0x803EB904


def _clamp_int(value: Any, lo: int, hi: int, default: int = 0) -> int:
    try:
        n = int(round(float(str(value).strip())))
    except Exception:
        n = int(default)
    return max(int(lo), min(int(hi), n))


def _clean_digits(value: Any, max_len: int) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        digits = "0"
    return digits[-max_len:]


def _count_digits(value: Any) -> tuple[int, int, int]:
    n = _clamp_int(value, 0, 999)
    return (n % 10, (n // 10) % 10, (n // 100) % 10)


def _selected_banks(use_vs: bool, use_hud: bool, use_svm: bool) -> tuple[str, ...]:
    out: list[str] = []
    if use_vs:
        out.append("VS")
    if use_hud:
        out.append("HUD")
    if use_svm:
        out.append("SVM")
    if not out:
        out.append("VS")
    return tuple(out)


def _write_u8(addr: int, value: int) -> bool:
    addr_i = int(addr)
    value_i = int(value) & 0xFF
    if _rpm is not None:
        try:
            return bool(_rpm.write_u8(addr_i, value_i, key="hud:u8", dirty=True))
        except Exception:
            pass
    if wd8 is None:
        return False
    # Dirty write: most persistent HUD holds repeatedly request the same byte.
    # Skip the write when the live value already matches.
    if rd8 is not None:
        try:
            cur = rd8(addr_i)
            if cur is not None and int(cur) == value_i:
                return True
        except Exception:
            pass
    try:
        return bool(wd8(addr_i, value_i))
    except Exception:
        return False


def _read_u8(addr: int) -> int | None:
    if rd8 is None:
        return None
    try:
        return int(rd8(int(addr)))
    except Exception:
        return None


def _read_u32(addr: int) -> int | None:
    if rd32 is None:
        return None
    try:
        return int(rd32(int(addr)))
    except Exception:
        return None


def read_raw_win_count(player: str) -> int | None:
    player = str(player or "P1").upper()
    vals: list[int] = []
    for addr in RAW_WIN_COUNTERS.get(player, ()):
        value = _read_u32(addr)
        if value is None:
            continue
        if 0 <= value <= 999:
            vals.append(int(value))
    if not vals:
        return None
    return max(vals)


def _set_vs_win_mode(player: str, *, show_win: bool, show_plural_s: bool = False) -> None:
    player = str(player or "P1").upper()
    addrs = VS_WIN_MODE_ENABLES.get(player)
    if not addrs:
        return
    # Only the parent mode decides whether win digits or NEW HERO are visible.
    # The child digit panes can be enabled and still remain hidden under NewHero.
    _write_u8(addrs["WIN"], 1 if show_win else 0)
    _write_u8(addrs["NEWHERO"], 0 if show_win else 1)

    # Singular/plural text is independent of the numeric digits.  The base pane
    # is WIN; this tiny child pane supplies the trailing S for WINS.
    s_addr = VS_WIN_PLURAL_S_ENABLES.get(player)
    if s_addr is not None:
        _write_u8(s_addr, 1 if (show_win and show_plural_s) else 0)


def _write_pair(addr_a: int, addr_b: int, digit: int, table: dict[int, tuple[int, int]] = SCORE_DIGIT_TEXTURES) -> bool:
    addr_a_i = int(addr_a)
    addr_b_i = int(addr_b)
    a, b = table[int(digit)]
    try:
        if rd32 is not None:
            cur_a = rd32(addr_a_i)
            cur_b = rd32(addr_b_i)
            if cur_a is not None and cur_b is not None and int(cur_a) == int(a) and int(cur_b) == int(b):
                return True
    except Exception:
        pass
    if _rpm is not None:
        try:
            ok_a = bool(_rpm.write_u32(addr_a_i, int(a), key="hud:pair", dirty=False))
            ok_b = bool(_rpm.write_u32(addr_b_i, int(b), key="hud:pair", dirty=False))
            return bool(ok_a and ok_b)
        except Exception:
            pass
    if wd32 is None:
        return False
    try:
        ok_a = bool(wd32(addr_a_i, int(a)))
        ok_b = bool(wd32(addr_b_i, int(b)))
        return bool(ok_a and ok_b)
    except Exception:
        return False


def _read_digit_pair(addr_a: int, addr_b: int, texture_to_digit: dict[tuple[int, int], int]) -> int | None:
    if rd32 is None:
        return None
    try:
        pair = (int(rd32(int(addr_a))), int(rd32(int(addr_b))))
    except Exception:
        return None
    return texture_to_digit.get(pair)


def _set_win_pane_enables(player: str, bank_name: str, value: int, *, force_zero_as_win: bool = False) -> None:
    bank_key = str(bank_name or "").upper()
    value_i = _clamp_int(value, 0, 999)

    if bank_key == "VS":
        if value_i == 0 and not force_zero_as_win:
            # Normal game behavior: zero wins means NEW HERO, not 0 WIN(S).
            _set_vs_win_mode(player, show_win=False, show_plural_s=False)
        else:
            # 1 WIN, 2+ WINS, and optional 0 WINS.
            _set_vs_win_mode(player, show_win=True, show_plural_s=(value_i != 1))

    flags = WIN_PANE_ENABLES.get(player, {}).get(bank_name)
    if not flags:
        return

    if bank_key == "VS" and value_i == 0 and not force_zero_as_win:
        desired = (0, 0, 0)
    else:
        desired = (1, 1 if value_i >= 10 else 0, 1 if value_i >= 100 else 0)
    for addr, enabled in zip(flags, desired):
        _write_u8(addr, enabled)


def apply_win_count(player: str, value: Any, *, use_vs: bool = True, use_hud: bool = True, use_svm: bool = False, force_zero_as_win: bool = False) -> bool:
    if not is_win_counter_runtime_active():
        return False
    player = str(player or "P1").upper()
    if player not in WIN_DIGIT_BANKS:
        player = "P1"

    value_i = _clamp_int(value, 0, 999)
    digits = _count_digits(value_i)
    ok_any = False
    for bank_name in _selected_banks(use_vs, use_hud, use_svm):
        _set_win_pane_enables(player, bank_name, value_i, force_zero_as_win=force_zero_as_win)
        for idx, (addr_a, addr_b) in enumerate(WIN_DIGIT_BANKS[player].get(bank_name, ())) :
            if _write_pair(addr_a, addr_b, digits[idx], SCORE_DIGIT_TEXTURES):
                ok_any = True
    return ok_any


def read_win_count(player: str, *, bank_name: str = "VS") -> int | None:
    player = str(player or "P1").upper()
    bank_name = str(bank_name or "VS").upper()
    bank = WIN_DIGIT_BANKS.get(player, {}).get(bank_name)
    if not bank:
        return None

    digits: list[int] = []
    for addr_a, addr_b in bank:
        digit = _read_digit_pair(addr_a, addr_b, _SCORE_TEXTURE_TO_DIGIT)
        if digit is None:
            return None
        digits.append(int(digit))

    return digits[0] + digits[1] * 10 + digits[2] * 100


def read_visible_vs_win_count(player: str) -> int | None:
    'Return what the in-fight VS HUD is actually meant to be showing.\n\n    If the side is in NEW HERO mode, the digit panes may still contain stale\n    values from an earlier write, so treat the visible count as zero.  This is\n    the value used to prefill the HUD Editor on open so merely opening the menu\n    does not reset a selected current progress to 0.\n    '
    player = str(player or "P1").upper()
    mode = VS_WIN_MODE_ENABLES.get(player)
    if mode:
        win_enabled = _read_u8(mode.get("WIN", 0))
        newhero_enabled = _read_u8(mode.get("NEWHERO", 0))
        if newhero_enabled == 1 and win_enabled != 1:
            return 0

    value = read_win_count(player, bank_name="VS")
    if value is not None:
        return value
    return read_raw_win_count(player)


def _runtime_hold(player: str) -> dict[str, Any]:
    player = str(player or "P1").upper()
    if player not in ("P1", "P2"):
        player = "P1"
    return _HUD_EDITOR_RUNTIME_STATE.setdefault("holds", {}).setdefault(
        player,
        {"enabled": False, "auto": True, "value": 0, "last_raw": None},
    )


def get_hud_editor_runtime_state() -> dict[str, Any]:
    return _HUD_EDITOR_RUNTIME_STATE


def sync_hud_editor_runtime_options(*, force_zero_as_win: bool | None = None, use_hud: bool | None = None, use_svm: bool | None = None) -> None:
    if force_zero_as_win is not None:
        _HUD_EDITOR_RUNTIME_STATE["force_zero_as_win"] = bool(force_zero_as_win)
    if use_hud is not None:
        _HUD_EDITOR_RUNTIME_STATE["use_hud"] = bool(use_hud)
    if use_svm is not None:
        _HUD_EDITOR_RUNTIME_STATE["use_svm"] = bool(use_svm)


def sync_hud_editor_hold_from_controls(
    player: str,
    value: Any,
    *,
    enabled: bool,
    auto: bool,
    force_zero_as_win: bool | None = None,
    use_hud: bool | None = None,
    use_svm: bool | None = None,
) -> None:
    player = str(player or "P1").upper()
    if player not in ("P1", "P2"):
        player = "P1"
    sync_hud_editor_runtime_options(
        force_zero_as_win=force_zero_as_win,
        use_hud=use_hud,
        use_svm=use_svm,
    )
    hold = _runtime_hold(player)
    was_enabled = bool(hold.get("enabled", False))
    now_enabled = bool(enabled)
    hold["enabled"] = now_enabled
    hold["auto"] = bool(auto)
    hold["value"] = _clamp_int(value, 0, 999)
    if now_enabled and not was_enabled:
        hold["last_raw"] = read_raw_win_count(player)


def set_hud_editor_hold(
    player: str,
    value: Any,
    *,
    enabled: bool = True,
    auto: bool = True,
    force_zero_as_win: bool | None = None,
    use_hud: bool | None = None,
    use_svm: bool | None = None,
    apply_now: bool = True,
) -> int:
    player = str(player or "P1").upper()
    if player not in ("P1", "P2"):
        player = "P1"
    value_i = _clamp_int(value, 0, 999)
    sync_hud_editor_hold_from_controls(
        player,
        value_i,
        enabled=enabled,
        auto=auto,
        force_zero_as_win=force_zero_as_win,
        use_hud=use_hud,
        use_svm=use_svm,
    )
    if apply_now and enabled:
        apply_win_count(
            player,
            value_i,
            use_vs=True,
            use_hud=bool(_HUD_EDITOR_RUNTIME_STATE.get("use_hud", True)),
            use_svm=bool(_HUD_EDITOR_RUNTIME_STATE.get("use_svm", False)),
            force_zero_as_win=bool(_HUD_EDITOR_RUNTIME_STATE.get("force_zero_as_win", False)),
        )
    return value_i


def release_hud_editor_hold(player: str) -> None:
    player = str(player or "P1").upper()
    if player not in ("P1", "P2"):
        player = "P1"
    hold = _runtime_hold(player)
    hold["enabled"] = False
    hold["last_raw"] = read_raw_win_count(player)


def clear_hud_editor_hold(player: str, *, apply_zero: bool = True) -> None:
    player = str(player or "P1").upper()
    if player not in ("P1", "P2"):
        player = "P1"
    hold = _runtime_hold(player)
    hold["enabled"] = False
    hold["value"] = 0
    hold["last_raw"] = read_raw_win_count(player)
    if apply_zero:
        apply_win_count(
            player,
            0,
            use_vs=True,
            use_hud=bool(_HUD_EDITOR_RUNTIME_STATE.get("use_hud", True)),
            use_svm=bool(_HUD_EDITOR_RUNTIME_STATE.get("use_svm", False)),
            force_zero_as_win=bool(_HUD_EDITOR_RUNTIME_STATE.get("force_zero_as_win", False)),
        )



def reset_hud_editor_runtime_state(*, apply_zero: bool = False) -> dict[str, Any]:
    """Release HUD Editor persistent runtime state for the Overseer panel.

    Safe restore releases holds without forcing a visible zero. Hard reset may
    optionally apply zero/default if a caller asks for it.
    """
    before = {
        "P1": dict(_runtime_hold("P1")),
        "P2": dict(_runtime_hold("P2")),
    }
    for player in ("P1", "P2"):
        hold = _runtime_hold(player)
        hold["enabled"] = False
        hold["auto"] = True
        hold["value"] = 0
        hold["last_raw"] = read_raw_win_count(player)
        if apply_zero:
            try:
                apply_win_count(
                    player,
                    0,
                    use_vs=True,
                    use_hud=bool(_HUD_EDITOR_RUNTIME_STATE.get("use_hud", True)),
                    use_svm=bool(_HUD_EDITOR_RUNTIME_STATE.get("use_svm", False)),
                    force_zero_as_win=bool(_HUD_EDITOR_RUNTIME_STATE.get("force_zero_as_win", False)),
                )
            except Exception:
                pass
    _HUD_EDITOR_RUNTIME_STATE["last_tick_time"] = 0.0
    _HUD_EDITOR_RUNTIME_STATE["last_force_time"] = 0.0
    _HUD_EDITOR_RUNTIME_STATE["status"] = "HUD Editor runtime state reset by Overseer."
    return {"before": before, "after": {"P1": dict(_runtime_hold("P1")), "P2": dict(_runtime_hold("P2"))}}

def tick_hud_editor_state(
    *, now: float | None = None, force: bool = False,
    snaps: dict | None = None, character_select: bool = False, unsafe: bool = False,
) -> None:
    """Count increases in each game's raw win counter; hold the totals all session.

    Read P1/P2 even when the fighter snapshots disappear during NEW HERO, and
    before checking the texture-write gate.  KO, win pose, screen phase and
    character-select detection do not determine the score.

    Display writes are still protected by the existing match/Yami gate.
    """
    try:
        now_f = float(time.monotonic() if now is None else now)
    except Exception:
        now_f = time.monotonic()

    for player in ("P1", "P2"):
        hold = _runtime_hold(player)
        current = read_raw_win_count(player)
        previous = hold.get("last_raw")
        if current is None:
            # A failed memory read isn't a reset.  Do not corrupt the baseline.
            continue
        current = int(current)
        hold["last_raw"] = current
        if previous is None:
            # Attach-time sample is a baseline, never an artificial victory.
            print(f"[win score] {player}: raw counter baseline {current}; held {hold['value']}", flush=True)
            continue
        previous = int(previous)
        if current == previous:
            continue
        if current > previous:
            delta = current - previous
            if bool(hold.get("enabled", False)) and bool(hold.get("auto", True)):
                hold["value"] = _clamp_int(int(hold.get("value", 0)) + delta, 0, 999)
                _HUD_EDITOR_RUNTIME_STATE["status"] = (
                    f"{player} raw wins {previous}->{current}; held {hold['value']} (+{delta})."
                )
                print(f"[win score] {player} raw {previous}->{current}: +{delta}; held {hold['value']}", flush=True)
                force = True
            else:
                print(f"[win score] {player} raw {previous}->{current}: auto hold disabled", flush=True)
        else:
            # NEW HERO/rematches may reset the game's counter; our score won't.
            print(f"[win score] {player} raw reset {previous}->{current}; held {hold['value']}", flush=True)

    _HUD_EDITOR_RUNTIME_STATE["raw_counters"] = {
        p: _runtime_hold(p).get("last_raw") for p in ("P1", "P2")
    }
    _HUD_EDITOR_RUNTIME_STATE["held_counters"] = {
        p: _clamp_int(_runtime_hold(p).get("value", 0), 0, 999) for p in ("P1", "P2")
    }

    if not is_win_counter_runtime_active():
        _HUD_EDITOR_RUNTIME_STATE["last_runtime_active"] = False
        # Observing the counters never requires permission to write HUD memory.
        return
    if not _HUD_EDITOR_RUNTIME_STATE.get("last_runtime_active", False):
        force = True
    _HUD_EDITOR_RUNTIME_STATE["last_runtime_active"] = True

    if not force:
        # The game can hide our held digits under NEW HERO on any rematch.
        # Immediately restore the win-mode flag if a nonzero total is hidden.
        for player in ("P1", "P2"):
            hold = _runtime_hold(player)
            if not bool(hold.get("enabled", False)) or not int(hold.get("value", 0)):
                continue
            mode = VS_WIN_MODE_ENABLES.get(player) or {}
            if _read_u8(mode.get("NEWHERO", 0)) == 1 or _read_u8(mode.get("WIN", 0)) == 0:
                force = True
                break
        if not force:
            last_tick = float(_HUD_EDITOR_RUNTIME_STATE.get("last_tick_time", 0.0) or 0.0)
            if now_f - last_tick < 0.066:
                return

    _HUD_EDITOR_RUNTIME_STATE["last_tick_time"] = now_f
    use_hud = bool(_HUD_EDITOR_RUNTIME_STATE.get("use_hud", True))
    use_svm = bool(_HUD_EDITOR_RUNTIME_STATE.get("use_svm", False))
    force_zero = bool(_HUD_EDITOR_RUNTIME_STATE.get("force_zero_as_win", False))
    for player in ("P1", "P2"):
        hold = _runtime_hold(player)
        if bool(hold.get("enabled", False)):
            apply_win_count(
                player, _clamp_int(hold.get("value", 0), 0, 999),
                use_vs=True, use_hud=use_hud, use_svm=use_svm,
                force_zero_as_win=force_zero,
            )


def apply_score_display(player: str, score_text: Any) -> bool:
    player = str(player or "P1").upper()
    bank = SCORE_DIGIT_BANKS.get(player)
    if not bank:
        bank = SCORE_DIGIT_BANKS["P1"]
    digits = _clean_digits(score_text, len(bank))
    # Right-align into all available panes. Extra leading panes become zero.
    digits = digits.rjust(len(bank), "0")
    ok_any = False
    for (addr_a, addr_b), ch in zip(bank, reversed(digits)):
        if _write_pair(addr_a, addr_b, int(ch), SCORE_DIGIT_TEXTURES):
            ok_any = True
    return ok_any


def read_score_display(player: str) -> str | None:
    player = str(player or "P1").upper()
    bank = SCORE_DIGIT_BANKS.get(player)
    if not bank:
        return None
    digits: list[str] = []
    for addr_a, addr_b in reversed(bank):
        digit = _read_digit_pair(addr_a, addr_b, _SCORE_TEXTURE_TO_DIGIT)
        if digit is None:
            return None
        digits.append(str(digit))
    return "".join(digits).lstrip("0") or "0"


def apply_stage_display(value: Any) -> bool:
    digit = _clamp_int(value, 0, 9)
    return _write_pair(STAGE_DIGIT_PAIR[0], STAGE_DIGIT_PAIR[1], digit, SCORE_DIGIT_TEXTURES)


def read_stage_display() -> int | None:
    return _read_digit_pair(STAGE_DIGIT_PAIR[0], STAGE_DIGIT_PAIR[1], _SCORE_TEXTURE_TO_DIGIT)


def apply_timer_display(value: Any, *, write_raw: bool = False) -> bool:
    value_i = _clamp_int(value, 0, 99)
    tens = (value_i // 10) % 10
    ones = value_i % 10
    ok = False
    ok = _write_pair(TIMER_TENS_PAIR[0], TIMER_TENS_PAIR[1], tens, TIMER_DIGIT_TEXTURES) or ok
    ok = _write_pair(TIMER_ONES_PAIR[0], TIMER_ONES_PAIR[1], ones, TIMER_DIGIT_TEXTURES) or ok
    if write_raw:
        try:
            # Observed raw timer value is display - 1.
            raw_timer = max(0, value_i - 1)
            if _rpm is not None:
                _rpm.write_u32(TIMER_RAW_ADDR, raw_timer, key="hud:timer_raw", dirty=True)
            elif wd32 is not None:
                wd32(TIMER_RAW_ADDR, raw_timer)
        except Exception:
            pass
    return ok


def read_timer_display() -> int | None:
    tens = _read_digit_pair(TIMER_TENS_PAIR[0], TIMER_TENS_PAIR[1], _TIMER_TEXTURE_TO_DIGIT)
    ones = _read_digit_pair(TIMER_ONES_PAIR[0], TIMER_ONES_PAIR[1], _TIMER_TEXTURE_TO_DIGIT)
    if tens is None or ones is None:
        return None
    return tens * 10 + ones


def write_arcade_score_raw_from_display(score_text: Any) -> bool:
    digits = _clean_digits(score_text, 14)
    try:
        visible_value = int(digits)
        raw_value = max(0, min(0xFFFFFFFF, visible_value // 100))
        if _rpm is not None:
            return bool(_rpm.write_u32(ARCADE_SCORE_RAW_ADDR, raw_value, key="hud:score_raw", dirty=True))
        if wd32 is None:
            return False
        return bool(wd32(ARCADE_SCORE_RAW_ADDR, raw_value))
    except Exception:
        return False


def _label(
    parent: tk.Misc,
    text: str,
    *,
    muted: bool = False,
    bold: bool = False,
    size: int = 9,
    wraplength: int | None = None,
) -> tk.Label:
    return tk.Label(
        parent,
        text=text,
        bg=parent.cget("bg"),
        fg=_MUTED if muted else _TEXT,
        font=("Segoe UI", size, "bold" if bold else "normal"),
        anchor="w",
        justify="left",
        wraplength=wraplength or 0,
    )


def _button(parent: tk.Misc, text: str, command: Callable[[], None] | None = None, *, width: int | None = None) -> tk.Button:
    # Use a visibly different fill from the cards themselves.  Flat buttons on
    # Windows blended into the dark panel and read like plain text, especially
    # in the Hold Visible Wins action grid.
    return tk.Button(
        parent,
        text=text,
        command=command,
        width=width or 0,
        bg=_ACTIVE,
        fg=_TEXT,
        activebackground="#35466c",
        activeforeground=_TEXT,
        relief="raised",
        bd=1,
        padx=10,
        pady=5,
        font=("Segoe UI", 9),
        cursor="hand2",
        highlightthickness=1,
        highlightbackground=_BORDER,
        highlightcolor=_ACCENT,
    )


def _field(parent: tk.Misc, textvariable: tk.Variable, *, width: int = 8, max_value: int = 999):
    return tk.Spinbox(
        parent,
        textvariable=textvariable,
        from_=0,
        to=max_value,
        increment=1,
        width=width,
        bg=_FIELD,
        fg=_TEXT,
        insertbackground=_TEXT,
        relief="flat",
        bd=1,
        highlightthickness=1,
        highlightbackground=_BORDER,
        highlightcolor=_ACCENT,
        buttonbackground=_PANEL_2,
        font=("Segoe UI", 9),
    )


def _entry(parent: tk.Misc, textvariable: tk.Variable, *, width: int = 18):
    return tk.Entry(
        parent,
        textvariable=textvariable,
        width=width,
        bg=_FIELD,
        fg=_TEXT,
        insertbackground=_TEXT,
        relief="flat",
        bd=1,
        highlightthickness=1,
        highlightbackground=_BORDER,
        highlightcolor=_ACCENT,
        font=("Segoe UI", 9),
    )


def _check(parent: tk.Misc, text: str, var: tk.BooleanVar) -> tk.Checkbutton:
    return tk.Checkbutton(
        parent,
        text=text,
        variable=var,
        bg=parent.cget("bg"),
        fg=_TEXT,
        activebackground=parent.cget("bg"),
        activeforeground=_TEXT,
        selectcolor=_FIELD,
        font=("Segoe UI", 9),
        relief="flat",
        bd=0,
    )


def _card(parent: tk.Misc) -> tk.Frame:
    outer = tk.Frame(parent, bg=_BORDER, padx=1, pady=1)
    inner = tk.Frame(outer, bg=_PANEL, padx=12, pady=10)
    inner.pack(fill="both", expand=True)
    outer.inner = inner  # type: ignore[attr-defined]
    return outer


def open_hud_editor_window() -> None:
    """Open the live HUD texture override window."""

    def _show(root: tk.Tk) -> None:
        global _OPEN_WINDOW

        try:
            if _OPEN_WINDOW is not None and bool(_OPEN_WINDOW.winfo_exists()):
                _OPEN_WINDOW.lift()
                _OPEN_WINDOW.focus_force()
                return
        except Exception:
            _OPEN_WINDOW = None

        win = tk.Toplevel(root)
        _OPEN_WINDOW = win
        win.title("Win Score")
        win.geometry("1080x820")
        win.minsize(1040, 720)
        win.configure(bg=_BG)

        try:
            win.attributes("-topmost", True)
            win.after(300, lambda: win.attributes("-topmost", False))
        except Exception:
            pass

        p1_win_var = tk.StringVar(value="0")
        p2_win_var = tk.StringVar(value="0")
        p1_score_var = tk.StringVar(value="0")
        p2_score_var = tk.StringVar(value="0")
        stage_var = tk.StringVar(value="1")
        timer_var = tk.StringVar(value="99")
        p1_streak_var = tk.StringVar(value=str(_runtime_hold("P1").get("value", 0)))
        p2_streak_var = tk.StringVar(value=str(_runtime_hold("P2").get("value", 0)))

        vs_var = tk.BooleanVar(value=True)
        hud_var = tk.BooleanVar(value=bool(_HUD_EDITOR_RUNTIME_STATE.get("use_hud", True)))
        svm_var = tk.BooleanVar(value=bool(_HUD_EDITOR_RUNTIME_STATE.get("use_svm", False)))
        freeze_var = tk.BooleanVar(value=False)
        score_raw_var = tk.BooleanVar(value=False)
        timer_raw_var = tk.BooleanVar(value=False)
        p1_streak_enabled_var = tk.BooleanVar(value=bool(_runtime_hold("P1").get("enabled", False)))
        p2_streak_enabled_var = tk.BooleanVar(value=bool(_runtime_hold("P2").get("enabled", False)))
        p1_streak_auto_var = tk.BooleanVar(value=bool(_runtime_hold("P1").get("auto", True)))
        p2_streak_auto_var = tk.BooleanVar(value=bool(_runtime_hold("P2").get("auto", True)))
        zero_wins_as_text_var = tk.BooleanVar(value=bool(_HUD_EDITOR_RUNTIME_STATE.get("force_zero_as_win", False)))
        status_var = tk.StringVar(value="Ready. Win Score defaults ON at 0 for both sides, with NEW HERO kept as the normal zero display.")
        readout_var = tk.StringVar(value="Current: --")

        last_applied = {
            "P1_WIN": 0,
            "P2_WIN": 0,
            "P1_SCORE": "0",
            "P2_SCORE": "0",
            "STAGE": 1,
            "TIMER": 99,
        }
        freeze_kinds: set[str] = set()
        streak_last_raw = {
            "P1": read_raw_win_count("P1"),
            "P2": read_raw_win_count("P2"),
        }

        root_frame = tk.Frame(win, bg=_BG, padx=16, pady=14)
        root_frame.pack(fill="both", expand=True)

        header = tk.Frame(root_frame, bg=_BG)
        header.pack(fill="x")
        _label(header, "Win Score", bold=True, size=14).pack(side="left")
        _check(header, "Freeze writes", freeze_var).pack(side="right")

        desc = _label(
            root_frame,
            "Edits live HUD texture selectors. Zero wins normally shows NEW HERO; Hold Visible Wins preserves a side's displayed wins through NEW HERO resets.",
            muted=True,
            wraplength=980,
        )
        desc.pack(fill="x", pady=(4, 10))

        opts = tk.Frame(root_frame, bg=_BG)
        opts.pack(fill="x", pady=(0, 10))
        _check(opts, "Write VS/in-fight wins", vs_var).pack(side="left")
        _check(opts, "Write result HUD wins", hud_var).pack(side="left", padx=(18, 0))
        _check(opts, "Write SVM wins too", svm_var).pack(side="left", padx=(18, 0))
        _check(opts, "Raw score too", score_raw_var).pack(side="left", padx=(26, 0))
        _check(opts, "Raw timer too", timer_raw_var).pack(side="left", padx=(18, 0))
        _check(opts, "Show 0 WINS instead of NEW HERO", zero_wins_as_text_var).pack(side="left", padx=(26, 0))

        def update_readout() -> None:
            p1_vs = read_visible_vs_win_count("P1")
            p2_vs = read_visible_vs_win_count("P2")
            p1_hud = read_win_count("P1", bank_name="HUD")
            p2_hud = read_win_count("P2", bank_name="HUD")
            p1_score = read_score_display("P1")
            p2_score = read_score_display("P2")
            stage = read_stage_display()
            timer = read_timer_display()
            raw_p1 = read_raw_win_count("P1")
            raw_p2 = read_raw_win_count("P2")
            readout_var.set(
                f"VS wins P1 {p1_vs if p1_vs is not None else '--'} | P2 {p2_vs if p2_vs is not None else '--'}    "
                f"Raw P1 {raw_p1 if raw_p1 is not None else '--'} | P2 {raw_p2 if raw_p2 is not None else '--'}    "
                f"Result P1 {p1_hud if p1_hud is not None else '--'} | P2 {p2_hud if p2_hud is not None else '--'}    "
                f"Score P1 {p1_score or '--'} | P2 {p2_score or '--'}    "
                f"Stage {stage if stage is not None else '--'}    Timer {timer if timer is not None else '--'}"
            )

        def sample_current_hud_values() -> None:
            'Prefill controls from the current HUD without writing anything.\n\n            This prevents the window from defaulting to 0 and accidentally\n            overwriting current progress when the operator hits Apply/Hold.\n            '
            sampled: dict[str, int] = {}
            for player in ("P1", "P2"):
                current = read_visible_vs_win_count(player)
                if current is None:
                    current = read_raw_win_count(player)
                if current is None:
                    current = 0
                value_i = _clamp_int(current, 0, 999)
                sampled[player] = value_i

            p1_win_var.set(str(sampled["P1"]))
            p2_win_var.set(str(sampled["P2"]))
            if not bool(_runtime_hold("P1").get("enabled", False)):
                p1_streak_var.set(str(sampled["P1"]))
            else:
                p1_streak_var.set(str(_runtime_hold("P1").get("value", sampled["P1"])))
            if not bool(_runtime_hold("P2").get("enabled", False)):
                p2_streak_var.set(str(sampled["P2"]))
            else:
                p2_streak_var.set(str(_runtime_hold("P2").get("value", sampled["P2"])))
            p1_streak_enabled_var.set(bool(_runtime_hold("P1").get("enabled", False)))
            p2_streak_enabled_var.set(bool(_runtime_hold("P2").get("enabled", False)))
            p1_streak_auto_var.set(bool(_runtime_hold("P1").get("auto", True)))
            p2_streak_auto_var.set(bool(_runtime_hold("P2").get("auto", True)))
            last_applied["P1_WIN"] = sampled["P1"]
            last_applied["P2_WIN"] = sampled["P2"]

            p1_score = read_score_display("P1")
            p2_score = read_score_display("P2")
            stage = read_stage_display()
            timer = read_timer_display()
            if p1_score is not None:
                p1_score_var.set(str(p1_score))
                last_applied["P1_SCORE"] = str(p1_score)
            if p2_score is not None:
                p2_score_var.set(str(p2_score))
                last_applied["P2_SCORE"] = str(p2_score)
            if stage is not None:
                stage_var.set(str(stage))
                last_applied["STAGE"] = int(stage)
            if timer is not None:
                timer_var.set(str(timer))
                last_applied["TIMER"] = int(timer)

            status_var.set(
                f"Sampled current HUD on open: P1 {sampled['P1']} wins, P2 {sampled['P2']} wins. No values were written."
            )

        sample_current_hud_values()

        def write_win(player: str, value: Any, *, announce: bool = True) -> None:
            value_i = _clamp_int(value, 0, 999)
            # The default-on persistent hold must not undo manual Apply.
            hold = _runtime_hold(player)
            if bool(hold.get("enabled", False)):
                hold["value"] = value_i
                (p1_streak_var if player == "P1" else p2_streak_var).set(str(value_i))
            ok = apply_win_count(player, value_i, use_vs=bool(vs_var.get()), use_hud=bool(hud_var.get()), use_svm=bool(svm_var.get()), force_zero_as_win=bool(zero_wins_as_text_var.get()))
            last_applied[f"{player}_WIN"] = value_i
            freeze_kinds.add(f"{player}_WIN")
            if announce:
                status_var.set(f"{player} wins set to {value_i}" if ok else win_counter_block_message())
                update_readout()

        def write_score(player: str, value: Any, *, announce: bool = True) -> None:
            value_s = _clean_digits(value, len(SCORE_DIGIT_BANKS.get(player, SCORE_DIGIT_BANKS["P1"])))
            ok = apply_score_display(player, value_s)
            if bool(score_raw_var.get()):
                ok = write_arcade_score_raw_from_display(value_s) or ok
            last_applied[f"{player}_SCORE"] = value_s
            freeze_kinds.add(f"{player}_SCORE")
            if announce:
                status_var.set(f"{player} score display set to {value_s}" if ok else f"{player} score write failed; confirm Dolphin is hooked")
                update_readout()

        def write_stage(value: Any, *, announce: bool = True) -> None:
            value_i = _clamp_int(value, 0, 9)
            ok = apply_stage_display(value_i)
            last_applied["STAGE"] = value_i
            freeze_kinds.add("STAGE")
            if announce:
                status_var.set(f"Stage digit set to {value_i}" if ok else "Stage write failed; confirm Dolphin is hooked")
                update_readout()

        def write_timer(value: Any, *, announce: bool = True) -> None:
            value_i = _clamp_int(value, 0, 99)
            ok = apply_timer_display(value_i, write_raw=bool(timer_raw_var.get()))
            last_applied["TIMER"] = value_i
            freeze_kinds.add("TIMER")
            if announce:
                status_var.set(f"Timer display set to {value_i:02d}" if ok else "Timer write failed; confirm Dolphin is hooked")
                update_readout()


        def _streak_controls_for(player: str):
            player = str(player or "P1").upper()
            if player == "P2":
                return p2_streak_var, p2_streak_enabled_var, p2_streak_auto_var
            return p1_streak_var, p1_streak_enabled_var, p1_streak_auto_var

        def _sync_runtime_options_from_ui() -> None:
            sync_hud_editor_runtime_options(
                force_zero_as_win=bool(zero_wins_as_text_var.get()),
                use_hud=bool(hud_var.get()),
                use_svm=bool(svm_var.get()),
            )

        def _sync_hold_control_to_runtime(player: str) -> None:
            player = str(player or "P1").upper()
            var, enabled_var, auto_var = _streak_controls_for(player)
            sync_hud_editor_runtime_options(
                force_zero_as_win=bool(zero_wins_as_text_var.get()),
                use_hud=bool(hud_var.get()),
                use_svm=bool(svm_var.get()),
            )
            hold = _runtime_hold(player)
            enabled = bool(enabled_var.get())
            auto = bool(auto_var.get())
            if enabled != bool(hold.get("enabled", False)):
                # Import a typed value only on a genuine enable/disable action.
                # Importing it on every 150ms refresh erased automatic wins.
                sync_hud_editor_hold_from_controls(player, var.get(), enabled=enabled, auto=auto)
            else:
                hold["auto"] = auto

        def capture_streak_from_vs(player: str) -> None:
            player = str(player or "P1").upper()
            var, _enabled_var, _auto_var = _streak_controls_for(player)
            current = read_win_count(player, bank_name="VS")
            if current is None:
                current = read_raw_win_count(player)
            if current is None:
                current = _clamp_int(var.get(), 0, 999)
            var.set(str(_clamp_int(current, 0, 999)))
            last_applied[f"{player}_WIN"] = _clamp_int(var.get(), 0, 999)
            streak_last_raw[player] = read_raw_win_count(player)
            status_var.set(f"Captured {player} visible wins as {var.get()}.")
            update_readout()

        def enable_streak(player: str) -> None:
            player = str(player or "P1").upper()
            var, enabled_var, auto_var = _streak_controls_for(player)
            value_i = _clamp_int(var.get(), 0, 999)
            var.set(str(value_i))
            last_applied[f"{player}_WIN"] = value_i
            freeze_kinds.add(f"{player}_WIN")
            enabled_var.set(True)
            streak_last_raw[player] = read_raw_win_count(player)
            set_hud_editor_hold(
                player,
                value_i,
                enabled=True,
                auto=bool(auto_var.get()),
                force_zero_as_win=bool(zero_wins_as_text_var.get()),
                use_hud=bool(hud_var.get()),
                use_svm=bool(svm_var.get()),
            )
            status_var.set(f"{player} visible wins held at {value_i}. This keeps running even if the HUD Editor window is closed.")
            update_readout()

        def bump_streak(player: str, delta: int) -> None:
            player = str(player or "P1").upper()
            var, enabled_var, auto_var = _streak_controls_for(player)
            value_i = _clamp_int(var.get(), 0, 999)
            value_i = max(0, min(999, value_i + int(delta)))
            var.set(str(value_i))
            last_applied[f"{player}_WIN"] = value_i
            freeze_kinds.add(f"{player}_WIN")
            if bool(enabled_var.get()):
                set_hud_editor_hold(
                    player,
                    value_i,
                    enabled=True,
                    auto=bool(auto_var.get()),
                    force_zero_as_win=bool(zero_wins_as_text_var.get()),
                    use_hud=bool(hud_var.get()),
                    use_svm=bool(svm_var.get()),
                )
            else:
                apply_win_count(player, value_i, use_vs=True, use_hud=bool(hud_var.get()), use_svm=bool(svm_var.get()), force_zero_as_win=bool(zero_wins_as_text_var.get()))
            status_var.set(f"{player} held wins adjusted to {value_i}.")
            update_readout()

        def disable_streak(player: str) -> None:
            player = str(player or "P1").upper()
            _var, enabled_var, _auto_var = _streak_controls_for(player)
            enabled_var.set(False)
            release_hud_editor_hold(player)
            status_var.set(f"{player} Win Score hold released. The game can show NEW HERO normally again after the next HUD refresh.")
            update_readout()

        def enable_both_streaks() -> None:
            _sync_runtime_options_from_ui()
            for player in ("P1", "P2"):
                var, enabled_var, auto_var = _streak_controls_for(player)
                value_i = _clamp_int(var.get(), 0, 999)
                var.set(str(value_i))
                enabled_var.set(True)
                last_applied[f"{player}_WIN"] = value_i
                freeze_kinds.add(f"{player}_WIN")
                streak_last_raw[player] = read_raw_win_count(player)
                set_hud_editor_hold(
                    player,
                    value_i,
                    enabled=True,
                    auto=bool(auto_var.get()),
                    force_zero_as_win=bool(zero_wins_as_text_var.get()),
                    use_hud=bool(hud_var.get()),
                    use_svm=bool(svm_var.get()),
                )
            status_var.set("Win Score enabled for P1 and P2.")
            update_readout()

        def disable_both_streaks() -> None:
            for player in ("P1", "P2"):
                _var, enabled_var, _auto_var = _streak_controls_for(player)
                enabled_var.set(False)
                release_hud_editor_hold(player)
            status_var.set("Win Score disabled for P1 and P2. The game can show NEW HERO normally after the next HUD refresh.")
            update_readout()

        def clear_visible_wins(player: str) -> None:
            player = str(player or "P1").upper()
            var, enabled_var, _auto_var = _streak_controls_for(player)
            var.set("0")
            enabled_var.set(False)
            if player == "P1":
                p1_win_var.set("0")
            else:
                p2_win_var.set("0")
            last_applied[f"{player}_WIN"] = 0
            freeze_kinds.discard(f"{player}_WIN")
            sync_hud_editor_runtime_options(
                force_zero_as_win=bool(zero_wins_as_text_var.get()),
                use_hud=bool(hud_var.get()),
                use_svm=bool(svm_var.get()),
            )
            clear_hud_editor_hold(player, apply_zero=True)
            if bool(zero_wins_as_text_var.get()):
                status_var.set(f"{player} cleared to visible 0 WINS.")
            else:
                status_var.set(f"{player} cleared to NEW HERO/default zero state.")
            update_readout()

        def apply_stored_streak(player: str) -> None:
            player = str(player or "P1").upper()
            var, enabled_var, auto_var = _streak_controls_for(player)
            value_i = _clamp_int(var.get(), 0, 999)
            var.set(str(value_i))
            last_applied[f"{player}_WIN"] = value_i
            freeze_kinds.add(f"{player}_WIN")
            if bool(enabled_var.get()):
                set_hud_editor_hold(
                    player,
                    value_i,
                    enabled=True,
                    auto=bool(auto_var.get()),
                    force_zero_as_win=bool(zero_wins_as_text_var.get()),
                    use_hud=bool(hud_var.get()),
                    use_svm=bool(svm_var.get()),
                )
            else:
                apply_win_count(player, value_i, use_vs=True, use_hud=bool(hud_var.get()), use_svm=bool(svm_var.get()), force_zero_as_win=bool(zero_wins_as_text_var.get()))
            status_var.set(f"Held {player} wins {value_i} written.")
            update_readout()

        def streak_tick_once() -> None:
            try:
                _sync_hold_control_to_runtime("P1")
                _sync_hold_control_to_runtime("P2")
                for player, var in (("P1", p1_streak_var), ("P2", p2_streak_var)):
                    hold = _runtime_hold(player)
                    if bool(hold.get("enabled", False)):
                        var.set(str(_clamp_int(hold.get("value", 0), 0, 999)))
                        last_applied[f"{player}_WIN"] = _clamp_int(hold.get("value", 0), 0, 999)
                msg = str(_HUD_EDITOR_RUNTIME_STATE.get("status") or "")
                if msg:
                    status_var.set(msg)
            except Exception as e:
                status_var.set(f"Hold Visible Wins tick failed: {e!r}")

        def make_win_card(parent: tk.Misc, player: str, var: tk.StringVar) -> tk.Frame:
            card = _card(parent)
            inner = card.inner  # type: ignore[attr-defined]
            _label(inner, f"{player} WINS", bold=True, size=11).grid(row=0, column=0, sticky="w", columnspan=8)
            _label(inner, "0-999. 0 normally shows NEW HERO; 1 shows WIN; 2+ shows WINS.", muted=True).grid(row=1, column=0, sticky="w", columnspan=8, pady=(2, 8))
            _field(inner, var, width=7, max_value=999).grid(row=2, column=0, sticky="w")
            _button(inner, "Apply", lambda: write_win(player, var.get()), width=8).grid(row=2, column=1, padx=(8, 0), sticky="w")
            _button(inner, "Clear", lambda: clear_visible_wins(player), width=7).grid(row=2, column=2, padx=(8, 0), sticky="w")
            presets = (0, 1, 2, 3, 5, 10, 21, 99)
            for idx, preset in enumerate(presets):
                def _cmd(v=preset) -> None:
                    var.set(str(v))
                    write_win(player, v)
                _button(inner, str(preset), _cmd, width=4).grid(row=3, column=idx, padx=(0 if idx == 0 else 5, 0), pady=(10, 0), sticky="w")
            return card

        streak_card = _card(root_frame)
        streak_card.pack(fill="x", pady=(0, 10))
        streak_inner = streak_card.inner  # type: ignore[attr-defined]
        streak_inner.grid_columnconfigure(0, weight=1)
        streak_inner.grid_columnconfigure(1, weight=1)
        _label(streak_inner, "HOLD VISIBLE WINS", bold=True, size=11).grid(row=0, column=0, sticky="w", columnspan=2)
        _label(
            streak_inner,
            "Capture a side's current visible wins and retain that value through NEW HERO resets. 0 defaults to NEW HERO. Enable 0 WINS only when zero must be shown as text. 1 shows WIN; 2+ shows WINS.",
            muted=True,
            wraplength=980,
        ).grid(row=1, column=0, sticky="ew", columnspan=2, pady=(2, 8))

        both_row = tk.Frame(streak_inner, bg=_PANEL)
        both_row.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        _button(both_row, "Enable both", enable_both_streaks, width=12).pack(side="left")
        _button(both_row, "Disable both", disable_both_streaks, width=12).pack(side="left", padx=(8, 0))
        _label(both_row, "Bulk toggle for P1/P2 Win Score holds.", muted=True).pack(side="left", padx=(12, 0))

        def make_hold_card(parent: tk.Misc, col: int, player: str, var: tk.StringVar, enabled_var: tk.BooleanVar, auto_var: tk.BooleanVar) -> None:
            side_outer = tk.Frame(parent, bg=_BORDER, padx=1, pady=1)
            side = tk.Frame(side_outer, bg=_PANEL_2, padx=10, pady=8)
            side.pack(fill="both", expand=True)
            for grid_col in range(4):
                side.grid_columnconfigure(grid_col, weight=1, uniform="hold_actions")
            side_outer.grid(row=3, column=col, sticky="ew", padx=(0, 6) if col == 0 else (6, 0))

            _label(side, f"{player} held wins", bold=True, size=10).grid(row=0, column=0, sticky="w", columnspan=2)

            held_box = tk.Frame(side, bg=_PANEL_2)
            held_box.grid(row=0, column=2, columnspan=2, sticky="e")
            _label(held_box, "Held", muted=True).pack(side="left", padx=(0, 5))
            _field(held_box, var, width=6, max_value=999).pack(side="left")

            options = tk.Frame(side, bg=_PANEL_2)
            options.grid(row=1, column=0, columnspan=4, sticky="ew", pady=(6, 0))
            _check(options, "Hold this side", enabled_var).pack(side="left")
            _check(options, "Auto add real wins", auto_var).pack(side="left", padx=(14, 0))

            # Keep the action buttons in a contained 4-column grid.  The old
            # single packed row overflowed badly on 1080px-wide windows.
            actions = tk.Frame(side, bg=_PANEL_2)
            actions.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
            for grid_col in range(4):
                actions.grid_columnconfigure(grid_col, weight=1, uniform="hold_btns")

            action_specs = (
                ("Capture", lambda p=player: capture_streak_from_vs(p), 0, 0, 1),
                ("Hold", lambda p=player: enable_streak(p), 0, 1, 1),
                ("Release", lambda p=player: disable_streak(p), 0, 2, 1),
                ("Clear", lambda p=player: clear_visible_wins(p), 0, 3, 1),
                ("-1", lambda p=player: bump_streak(p, -1), 1, 0, 1),
                ("+1", lambda p=player: bump_streak(p, 1), 1, 1, 1),
                ("Apply held", lambda p=player: apply_stored_streak(p), 1, 2, 2),
            )
            for text, cmd, row, grid_col, span in action_specs:
                _button(actions, text, cmd).grid(
                    row=row,
                    column=grid_col,
                    columnspan=span,
                    sticky="ew",
                    padx=(0 if grid_col == 0 else 5, 0),
                    pady=(0 if row == 0 else 6, 0),
                )

        make_hold_card(streak_inner, 0, "P1", p1_streak_var, p1_streak_enabled_var, p1_streak_auto_var)
        make_hold_card(streak_inner, 1, "P2", p2_streak_var, p2_streak_enabled_var, p2_streak_auto_var)

        win_cards = tk.Frame(root_frame, bg=_BG)
        win_cards.pack(fill="x", pady=(0, 10))
        win_cards.grid_columnconfigure(0, weight=1)
        win_cards.grid_columnconfigure(1, weight=1)
        make_win_card(win_cards, "P1", p1_win_var).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        make_win_card(win_cards, "P2", p2_win_var).grid(row=0, column=1, sticky="ew", padx=(6, 0))

        score_cards = tk.Frame(root_frame, bg=_BG)
        score_cards.pack(fill="x", pady=(0, 10))
        score_cards.grid_columnconfigure(0, weight=1)
        score_cards.grid_columnconfigure(1, weight=1)
        score_cards.grid_columnconfigure(2, weight=1)

        score_card = _card(score_cards)
        score_inner = score_card.inner  # type: ignore[attr-defined]
        _label(score_inner, "ARCADE SCORE", bold=True, size=11).grid(row=0, column=0, sticky="w", columnspan=8)
        _label(score_inner, "Texture-only display score. Right-aligned into score digit panes.", muted=True).grid(row=1, column=0, sticky="w", columnspan=8, pady=(2, 8))
        _label(score_inner, "P1", muted=True).grid(row=2, column=0, sticky="w")
        _entry(score_inner, p1_score_var, width=16).grid(row=2, column=1, sticky="w", padx=(6, 0))
        _button(score_inner, "Apply", lambda: write_score("P1", p1_score_var.get()), width=8).grid(row=2, column=2, sticky="w", padx=(8, 0))
        _label(score_inner, "P2", muted=True).grid(row=3, column=0, sticky="w", pady=(8, 0))
        _entry(score_inner, p2_score_var, width=16).grid(row=3, column=1, sticky="w", padx=(6, 0), pady=(8, 0))
        _button(score_inner, "Apply", lambda: write_score("P2", p2_score_var.get()), width=8).grid(row=3, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
        _button(score_inner, "Apply both", lambda: (write_score("P1", p1_score_var.get(), announce=False), write_score("P2", p2_score_var.get(), announce=False), status_var.set("P1/P2 score display written"), update_readout()), width=12).grid(row=4, column=0, sticky="w", pady=(10, 0), columnspan=2)
        score_card.grid(row=0, column=0, columnspan=2, sticky="ew", padx=(0, 6))

        misc_card = _card(score_cards)
        misc_inner = misc_card.inner  # type: ignore[attr-defined]
        _label(misc_inner, "STAGE / TIMER", bold=True, size=11).grid(row=0, column=0, sticky="w", columnspan=4)
        _label(misc_inner, "Stage uses Score digit bank. Timer uses Time digit bank.", muted=True).grid(row=1, column=0, sticky="w", columnspan=4, pady=(2, 8))
        _label(misc_inner, "Stage", muted=True).grid(row=2, column=0, sticky="w")
        _field(misc_inner, stage_var, width=5, max_value=9).grid(row=2, column=1, sticky="w", padx=(6, 0))
        _button(misc_inner, "Apply", lambda: write_stage(stage_var.get()), width=8).grid(row=2, column=2, sticky="w", padx=(8, 0))
        _label(misc_inner, "Timer", muted=True).grid(row=3, column=0, sticky="w", pady=(8, 0))
        _field(misc_inner, timer_var, width=5, max_value=99).grid(row=3, column=1, sticky="w", padx=(6, 0), pady=(8, 0))
        _button(misc_inner, "Apply", lambda: write_timer(timer_var.get()), width=8).grid(row=3, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
        for idx, val in enumerate((56, 60, 90, 99)):
            def _cmd(v=val) -> None:
                timer_var.set(str(v))
                write_timer(v)
            _button(misc_inner, str(val), _cmd, width=4).grid(row=4, column=idx, sticky="w", pady=(10, 0), padx=(0 if idx == 0 else 5, 0))
        misc_card.grid(row=0, column=2, sticky="nsew", padx=(6, 0))

        mid = tk.Frame(root_frame, bg=_BG)
        mid.pack(fill="x", pady=(0, 10))
        _button(mid, "Apply win both", lambda: (write_win("P1", p1_win_var.get(), announce=False), write_win("P2", p2_win_var.get(), announce=False), status_var.set(f"P1 wins={_clamp_int(p1_win_var.get(), 0, 999)}, P2 wins={_clamp_int(p2_win_var.get(), 0, 999)} written"), update_readout()), width=14).pack(side="left")
        _button(mid, "Refresh readout", update_readout, width=14).pack(side="left", padx=(8, 0))
        _label(mid, "Freeze repeats only fields applied from this window, about 10 times per second.", muted=True).pack(side="left", padx=(12, 0))

        readout = tk.Label(root_frame, textvariable=readout_var, bg=_BG, fg=_GOOD, anchor="w", justify="left", font=("Segoe UI", 9, "bold"))
        readout.pack(fill="x", pady=(2, 4))

        status = tk.Label(root_frame, textvariable=status_var, bg=_BG, fg=_MUTED, anchor="w", justify="left", wraplength=980, font=("Segoe UI", 9))
        status.pack(fill="x", pady=(2, 0))

        addr_card = _card(root_frame)
        addr_card.pack(fill="x", pady=(10, 0))
        addr_inner = addr_card.inner  # type: ignore[attr-defined]
        _label(addr_inner, "Confirmed live HUD layers", bold=True, size=10).pack(fill="x")
        _label(
            addr_inner,
            "VS wins P1: 0x80BEBB80/BDA0/BFC0, P2: 0x80BEC940/CB60/CD80. Score P1 starts 0x80BE53C0, P2 starts 0x80BE7280. Stage 0x80BE9780. Timer 0x80BE4E80/0x80BE50A0.",
            muted=True,
            wraplength=980,
        ).pack(fill="x", pady=(4, 0))

        def freeze_tick() -> None:
            try:
                if not is_win_counter_runtime_active():
                    if bool(p1_streak_enabled_var.get()) or bool(p2_streak_enabled_var.get()) or freeze_var.get():
                        status_var.set(win_counter_block_message())
                else:
                    if bool(p1_streak_enabled_var.get()) or bool(p2_streak_enabled_var.get()):
                        streak_tick_once()
                    if freeze_var.get():
                        if "P1_WIN" in freeze_kinds and not bool(p1_streak_enabled_var.get()):
                            apply_win_count("P1", last_applied.get("P1_WIN", _clamp_int(p1_win_var.get(), 0, 999)), use_vs=bool(vs_var.get()), use_hud=bool(hud_var.get()), use_svm=bool(svm_var.get()), force_zero_as_win=bool(zero_wins_as_text_var.get()))
                        if "P2_WIN" in freeze_kinds and not bool(p2_streak_enabled_var.get()):
                            apply_win_count("P2", last_applied.get("P2_WIN", _clamp_int(p2_win_var.get(), 0, 999)), use_vs=bool(vs_var.get()), use_hud=bool(hud_var.get()), use_svm=bool(svm_var.get()), force_zero_as_win=bool(zero_wins_as_text_var.get()))
                        if "P1_SCORE" in freeze_kinds:
                            apply_score_display("P1", last_applied.get("P1_SCORE", p1_score_var.get()))
                        if "P2_SCORE" in freeze_kinds:
                            apply_score_display("P2", last_applied.get("P2_SCORE", p2_score_var.get()))
                        if "STAGE" in freeze_kinds:
                            apply_stage_display(last_applied.get("STAGE", _clamp_int(stage_var.get(), 0, 9)))
                        if "TIMER" in freeze_kinds:
                            apply_timer_display(last_applied.get("TIMER", _clamp_int(timer_var.get(), 0, 99)), write_raw=bool(timer_raw_var.get()))
            except Exception as e:
                status_var.set(f"Freeze write failed: {e!r}")
            try:
                if bool(win.winfo_exists()):
                    win.after(150, freeze_tick)
            except Exception:
                pass

        def on_close() -> None:
            global _OPEN_WINDOW
            # Do not disable active Hold Visible Wins here.  The hold state now
            # lives at module scope and main.py keeps ticking it after this
            # control window closes.  Closing the window should not drop a
            # preserved streak mid-run.
            try:
                _sync_hold_control_to_runtime("P1")
                _sync_hold_control_to_runtime("P2")
                tick_hud_editor_state(force=True)
            except Exception:
                pass
            _OPEN_WINDOW = None
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        update_readout()
        freeze_tick()

    if tk_call is not None:
        tk_call(_show)
        return

    root = tk.Tk()
    root.withdraw()
    _show(root)
    root.mainloop()

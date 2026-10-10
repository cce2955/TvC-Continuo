# dolphin_io.py
#
# Thin wrapper around dolphin_memory_engine so the module can safely
# read AND WRITE Dolphin's emulated Wii memory (MEM1 + MEM2).
#
# CHANGE (MEM2 latch):
#   Dolphin can have multiple MEM2-sized committed regions. DME can
#   attach to the wrong one. The module "latch" the correct MEM2 host base.
#
#   IMPORTANT: the latch used to depend on a fixed fighter-base sentinel
#   (0x9246B9C0). That only exists during certain already-started matches,
#   so the GUI would fail to detect matches if it was launched from menus.
#   The latch is now dynamic: it polls the stable MEM1 fighter pointer slots
#   and uses whichever live fighter base appears first as the MEM2 proof.
#
# Exposes:
#   hook()               - hook Dolphin immediately; MEM2 latches lazily
#   addr_in_ram(a)       - True if address in MEM1 or MEM2
#   rbytes(addr, size)   - bulk read, returns bytes
#   rd8(addr)            - read 8-bit unsigned, returns int 0..255 or None
#   rd32(addr)           - read 32-bit BE unsigned
#   rdf32(addr)          - read 32-bit BE float
#   wd8(addr, val)       - write 8-bit unsigned
#   wd32(addr, val)      - write 32-bit BE unsigned
#   wdf32(addr, val)     - write 32-bit BE float
#   wbytes(addr, data)   - write bytes

import time
import math
import struct
import os
import sys
import json
import threading
import ctypes
from ctypes import wintypes

import dolphin_memory_engine as dme
from tvcgui.core.constants import MEM1_LO, MEM1_HI, MEM2_LO, MEM2_HI
from tvcgui.core.paths import user_data_path

# Character-select write quarantine is controlled by main.py. It blocks tool-side
# emulated-memory writes while the select scene is active and records attempts.
_WRITE_QUARANTINE_LOCK = threading.RLock()
_WRITE_QUARANTINE_ACTIVE = False
_WRITE_QUARANTINE_REASON = ""
_WRITE_QUARANTINE_TRACE_PATH = user_data_path("runtime", "chrsel_write_trace.jsonl")
_WRITE_QUARANTINE_LAST: dict[tuple[int, bytes, str], float] = {}
_WRITE_QUARANTINE_TRACE_INTERVAL_SEC = 0.50


def set_emulated_write_quarantine(active: bool, *, reason: str = "", trace_path: str | None = None) -> None:
    """Enable or disable the select-scene emulated-memory write barrier."""
    global _WRITE_QUARANTINE_ACTIVE, _WRITE_QUARANTINE_REASON, _WRITE_QUARANTINE_TRACE_PATH
    with _WRITE_QUARANTINE_LOCK:
        _WRITE_QUARANTINE_ACTIVE = bool(active)
        _WRITE_QUARANTINE_REASON = str(reason or "")
        if trace_path:
            _WRITE_QUARANTINE_TRACE_PATH = str(trace_path)


def get_emulated_write_quarantine_state() -> dict:
    with _WRITE_QUARANTINE_LOCK:
        return {
            "active": bool(_WRITE_QUARANTINE_ACTIVE),
            "reason": str(_WRITE_QUARANTINE_REASON),
            "trace_path": str(_WRITE_QUARANTINE_TRACE_PATH),
        }


def _quarantine_caller() -> str:
    try:
        frame = sys._getframe(2)
    except Exception:
        return "unknown"
    for _ in range(10):
        if frame is None:
            break
        filename = os.path.basename(str(frame.f_code.co_filename))
        if filename not in {"dolphin.py", "patch_manager.py", "dolphin_io.py", "runtime_patch_manager.py"}:
            return f"{filename}:{frame.f_code.co_name}"
        frame = frame.f_back
    return "unknown"


def _trace_quarantined_write(addr: int, payload: bytes) -> None:
    now = time.monotonic()
    caller = _quarantine_caller()
    with _WRITE_QUARANTINE_LOCK:
        reason = str(_WRITE_QUARANTINE_REASON)
        path = str(_WRITE_QUARANTINE_TRACE_PATH)
        key = (int(addr), bytes(payload), caller)
        prior = float(_WRITE_QUARANTINE_LAST.get(key, 0.0) or 0.0)
        if now - prior < _WRITE_QUARANTINE_TRACE_INTERVAL_SEC:
            return
        _WRITE_QUARANTINE_LAST[key] = now
    record = {
        "monotonic": round(now, 6),
        "addr": f"0x{int(addr):08X}",
        "size": len(payload),
        "data_hex": bytes(payload).hex(),
        "caller": caller,
        "reason": reason,
    }
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as trace_file:
            trace_file.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        pass

# ============================================================
# MEM2 LATCH CONFIG
# ============================================================


# Stable MEM1 addresses that point to live fighter structs once a match exists.
# These are safe to poll from menus; they simply read as null/invalid until
# the match allocator installs fighter bases.
DYNAMIC_SLOT_PTRS = (
    0x803C9FCC,  # P1-C1
    0x803C9FDC,  # P1-C2
    0x803C9FD4,  # P2-C1
    0x803C9FE4,  # P2-C2
)

# Character Select exposes the loaded chrsel.seq base through two stable MEM1
# globals. This is the safe menu-time MEM2 anchor. Unlike the retired fighter
# sentinel, the signature is long, nonzero, and unique to the active scene.
CHRSEL_SEQ_PTR_ADDRS = (
    0x809BCE38,
    0x80796364,
)
CHRSEL_SEQ_SIGNATURE_OFFSET = 0x40
CHRSEL_SEQ_SIGNATURE = b"fpack/menu/001/0000.fpk\x00"
CHRSEL_SEQ_HEADER = bytes.fromhex(
    "00 00 00 20 00 00 01 00 00 00 00 40 80 79 63 08"
)

# Legacy fallback only. Do not rely on this to prime MEM2 from menus.
EXPECT_EA = 0x9246B9C0
EXPECT_BYTES = bytes.fromhex(
    "00 00 80 0F 00 00 00 00 00 00 00 00 00 00 00 00 "
    "00 00 00 00 00 00 00 0C 00 00 00 00 00 00 00 00"
)

# Accept the observed toggle variant if the target behavior requires (0x80 -> 0x00 at byte[2]).
# If the operator don't want this tolerance, set EXPECT_BYTES_ALT = None.
EXPECT_BYTES_ALT = bytearray(EXPECT_BYTES)
EXPECT_BYTES_ALT[2] = 0x00
EXPECT_BYTES_ALT = bytes(EXPECT_BYTES_ALT)

MEM2_SIZE = MEM2_HI - MEM2_LO  # usually 0x04000000 (64MB)

# Process name to target
DOLPHIN_EXE = "Dolphin.exe"

# How often the module retry latching if the module can't find it yet. This is also the
# non-blocking throttle used by rbytes/wbytes while no match is active.
LATCH_RETRY_SLEEP = 0.25

# ============================================================
# WINDOWS PROCESS MEMORY HELPERS (ctypes)
# ============================================================

_IS_WINDOWS = (os.name == "nt")

if _IS_WINDOWS:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    PROCESS_VM_WRITE = 0x0020
    PROCESS_VM_OPERATION = 0x0008

    MEM_COMMIT = 0x1000

    PAGE_NOACCESS = 0x01
    PAGE_READONLY = 0x02
    PAGE_READWRITE = 0x04
    PAGE_WRITECOPY = 0x08
    PAGE_EXECUTE = 0x10
    PAGE_EXECUTE_READ = 0x20
    PAGE_EXECUTE_READWRITE = 0x40
    PAGE_EXECUTE_WRITECOPY = 0x80
    PAGE_GUARD = 0x100
    PAGE_NOCACHE = 0x200
    PAGE_WRITECOMBINE = 0x400

    READABLE_PROTECT = {
        PAGE_READONLY,
        PAGE_READWRITE,
        PAGE_WRITECOPY,
        PAGE_EXECUTE_READ,
        PAGE_EXECUTE_READWRITE,
        PAGE_EXECUTE_WRITECOPY,
    }

    WRITABLE_PROTECT = {
        PAGE_READWRITE,
        PAGE_WRITECOPY,
        PAGE_EXECUTE_READWRITE,
        PAGE_EXECUTE_WRITECOPY,
    }

    class MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", wintypes.LPVOID),
            ("AllocationBase", wintypes.LPVOID),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        wintypes.LPVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.ReadProcessMemory.restype = wintypes.BOOL

    kernel32.WriteProcessMemory.argtypes = [
        wintypes.HANDLE,
        wintypes.LPVOID,
        wintypes.LPCVOID,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.WriteProcessMemory.restype = wintypes.BOOL

    kernel32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE,
        wintypes.LPCVOID,
        ctypes.POINTER(MEMORY_BASIC_INFORMATION),
        ctypes.c_size_t,
    ]
    kernel32.VirtualQueryEx.restype = ctypes.c_size_t


# ============================================================
# MEM2 LATCH STATE
# ============================================================

_mem2_proc_handle = None
_mem2_pid = None
_mem2_host_base = None
_mem2_host_size = None
_mem2_latch_mode = None
_mem2_latch_note = "unlatched"
_mem2_last_latch_attempt = 0.0


def _reset_mem2_latch():
    global _mem2_proc_handle, _mem2_pid, _mem2_host_base, _mem2_host_size
    global _mem2_latch_mode, _mem2_latch_note
    if _mem2_proc_handle is not None and _IS_WINDOWS:
        try:
            kernel32.CloseHandle(_mem2_proc_handle)
        except Exception:
            pass
    _mem2_proc_handle = None
    _mem2_pid = None
    _mem2_host_base = None
    _mem2_host_size = None
    _mem2_latch_mode = None
    _mem2_latch_note = "unlatched"


def _find_pid_by_name(exe_name: str):
    """
    Find a PID for exe_name on Windows. Tries psutil first, then tasklist.
    Returns int PID or None.
    """
    if not _IS_WINDOWS:
        return None

    # Try psutil if present
    try:
        import psutil  # type: ignore
        for p in psutil.process_iter(["pid", "name"]):
            nm = p.info.get("name")
            if nm and nm.lower() == exe_name.lower():
                return int(p.info["pid"])
    except Exception:
        pass

    # Fallback: tasklist parsing
    try:
        import subprocess
        out = subprocess.check_output(
            ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/FO", "CSV"],
            universal_newlines=True,
            stderr=subprocess.DEVNULL,
        )
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        if len(lines) >= 2:
            # "Image Name","PID",...
            cols = [c.strip().strip('"') for c in lines[1].split(",")]
            if len(cols) >= 2 and cols[0].lower() == exe_name.lower():
                return int(cols[1])
    except Exception:
        pass

    return None


def _open_process(pid: int):
    if not _IS_WINDOWS:
        return None
    access = (
        PROCESS_QUERY_INFORMATION
        | PROCESS_VM_READ
        | PROCESS_VM_WRITE
        | PROCESS_VM_OPERATION
    )
    h = kernel32.OpenProcess(access, False, pid)
    if not h:
        return None
    return h


def _rpm(hproc, addr: int, size: int) -> bytes:
    if not _IS_WINDOWS or not hproc or size <= 0:
        return b""
    buf = (ctypes.c_ubyte * size)()
    nread = ctypes.c_size_t(0)
    ok = kernel32.ReadProcessMemory(
        hproc,
        ctypes.c_void_p(addr),
        buf,
        size,
        ctypes.byref(nread),
    )
    if not ok or nread.value != size:
        return b""
    return bytes(buf)


def _wpm(hproc, addr: int, data: bytes) -> bool:
    if not _IS_WINDOWS or not hproc or not data:
        return False
    size = len(data)
    cbuf = (ctypes.c_ubyte * size).from_buffer_copy(data)
    nwritten = ctypes.c_size_t(0)
    ok = kernel32.WriteProcessMemory(
        hproc,
        ctypes.c_void_p(addr),
        cbuf,
        size,
        ctypes.byref(nwritten),
    )
    return bool(ok and nwritten.value == size)


def _iter_regions(hproc):
    """
    Yield (base, size, protect, state) for all regions in the process.
    """
    if not _IS_WINDOWS or not hproc:
        return
    mbi = MEMORY_BASIC_INFORMATION()
    addr = 0
    max_addr = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1

    while addr < max_addr:
        ret = kernel32.VirtualQueryEx(
            hproc, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi)
        )
        if not ret:
            break

        base = int(ctypes.cast(mbi.BaseAddress, ctypes.c_void_p).value or 0)
        size = int(mbi.RegionSize)
        protect = int(mbi.Protect)
        state = int(mbi.State)

        yield base, size, protect, state

        # Advance
        next_addr = base + size
        if next_addr <= addr:
            break
        addr = next_addr


def _score_match(buf: bytes) -> int:
    if not buf:
        return 0
    score = 0
    for i in range(min(len(buf), len(EXPECT_BYTES))):
        if buf[i] == EXPECT_BYTES[i] or (EXPECT_BYTES_ALT and buf[i] == EXPECT_BYTES_ALT[i]):
            score += 1
    return score


def _u32be_from(buf: bytes, off: int):
    if off < 0 or off + 4 > len(buf):
        return None
    return struct.unpack_from(">I", buf, off)[0]


def _f32be_from(buf: bytes, off: int):
    if off < 0 or off + 4 > len(buf):
        return None
    try:
        val = struct.unpack_from(">f", buf, off)[0]
    except Exception:
        return None
    return val if math.isfinite(val) else None


def _dme_rd32_be(addr: int):
    """Read a MEM1 u32 directly through DME, bypassing MEM2 latch logic."""
    try:
        data = dme.read_bytes(int(addr), 4)
    except Exception:
        return None
    if not data or len(data) != 4:
        return None
    return struct.unpack(">I", data)[0]


def _dynamic_slot_pointer_candidates():
    """Return unique MEM2-looking live fighter EAs from the stable MEM1 slots."""
    out = []
    seen = set()
    for ptr_addr in DYNAMIC_SLOT_PTRS:
        ea = _dme_rd32_be(ptr_addr)
        if ea is None:
            continue
        if not (MEM2_LO <= int(ea) < MEM2_HI):
            continue
        # Fighter structs are word-aligned. This also filters garbage reads.
        if int(ea) & 0x3:
            continue
        if int(ea) in seen:
            continue
        seen.add(int(ea))
        out.append((ptr_addr, int(ea)))
    return out


def _chrsel_sequence_pointer_candidates():
    """Return unique loaded chrsel.seq EAs reported by stable MEM1 globals."""
    out = []
    seen = set()
    for ptr_addr in CHRSEL_SEQ_PTR_ADDRS:
        ea = _dme_rd32_be(ptr_addr)
        if ea is None:
            continue
        ea = int(ea) & 0xFFFFFFFF
        if not (MEM2_LO <= ea < MEM2_HI):
            continue
        if ea & 0x3:
            continue
        if ea in seen:
            continue
        seen.add(ea)
        out.append((int(ptr_addr), ea))
    return out


def _latch_mem2_from_chrsel_sequence() -> bool:
    """Latch MEM2 during Character Select from the loaded script signature."""
    global _mem2_host_base, _mem2_host_size, _mem2_latch_mode, _mem2_latch_note

    candidates = _chrsel_sequence_pointer_candidates()
    if not candidates:
        _mem2_latch_note = "waiting for fighter or chrsel.seq pointer"
        return False

    for base, size, protect, state in _iter_regions(_mem2_proc_handle):
        if state != MEM_COMMIT:
            continue
        prot = protect & 0xFF
        if prot not in READABLE_PROTECT:
            continue
        if size < MEM2_SIZE:
            continue

        for ptr_addr, seq_ea in candidates:
            seq_off = seq_ea - MEM2_LO
            sig_off = seq_off + CHRSEL_SEQ_SIGNATURE_OFFSET
            if seq_off < 0 or sig_off + len(CHRSEL_SEQ_SIGNATURE) > size:
                continue
            header = _rpm(_mem2_proc_handle, base + seq_off, len(CHRSEL_SEQ_HEADER))
            if header != CHRSEL_SEQ_HEADER:
                continue
            signature = _rpm(
                _mem2_proc_handle,
                base + sig_off,
                len(CHRSEL_SEQ_SIGNATURE),
            )
            if signature != CHRSEL_SEQ_SIGNATURE:
                continue

            _mem2_host_base = int(base)
            _mem2_host_size = int(size)
            _mem2_latch_mode = "chrsel_sequence"
            _mem2_latch_note = f"chrsel ptr 0x{ptr_addr:08X}->0x{seq_ea:08X}"
            print(
                f"[MEM2 latch] chrsel.seq 0x{ptr_addr:08X}->0x{seq_ea:08X}; "
                f"host=0x{base:X}"
            )
            return True

    ptrs = ", ".join(f"0x{ea:08X}" for _ptr, ea in candidates)
    _mem2_latch_note = f"chrsel.seq pointer present but MEM2 map not found: {ptrs}"
    return False


def _fighter_block_score(buf: bytes) -> int:
    "\n    Score a candidate fighter struct block read from a possible MEM2 host map.\n\n    This intentionally stays permissive enough for load/round transitions, but\n    strict enough to reject random committed regions. The candidate EA already\n    came from one of the game's live fighter pointer slots, so only need to\n    prove the host base is the right MEM2 mirror.\n    "
    if not buf or len(buf) < 0x130:
        return 0

    score = 0
    first = _u32be_from(buf, 0x00)
    cid = _u32be_from(buf, 0x14)
    max_hp = _u32be_from(buf, 0x24)
    cur_hp = _u32be_from(buf, 0x28)
    aux_hp = _u32be_from(buf, 0x2C)
    pos_x = _f32be_from(buf, 0xF0)

    # Common live fighter header observed as 0x0000800F, with one known toggle
    # variant 0x0000000F. Keep this as a strong clue, not a hard requirement.
    if first in (0x0000800F, 0x0000000F):
        score += 4
    elif first is not None and (first & 0xFFFF) in (0x800F, 0x000F):
        score += 2

    # TvC retail/Yami IDs stay small. 0 can appear for null/helper slots, so
    # treat it as a weak clue instead of rejecting it outright.
    if cid is not None:
        if 1 <= cid <= 0x40:
            score += 3
        elif cid == 0:
            score += 1

    # HP values vary, but real fighters are positive and not huge.
    if max_hp is not None:
        if 1000 <= max_hp <= 300000:
            score += 3
        elif 1 <= max_hp <= 1000000:
            score += 1

    if cur_hp is not None and max_hp is not None and max_hp > 0:
        if 0 <= cur_hp <= max_hp * 2:
            score += 2

    if aux_hp is not None and max_hp is not None and max_hp > 0:
        if 0 <= aux_hp <= max_hp * 2:
            score += 1

    if pos_x is not None and abs(pos_x) < 100000.0:
        score += 1

    # Reject all-zero and all-FF pages even if a few weak fields matched.
    if buf[:0x80] == b"\x00" * 0x80 or buf[:0x80] == b"\xFF" * 0x80:
        return 0

    return score


def _latch_mem2_from_dynamic_slots() -> bool:
    """Latch MEM2 by validating live fighter pointer slots instead of a fixed EA."""
    global _mem2_host_base, _mem2_host_size, _mem2_latch_mode, _mem2_latch_note

    candidates = _dynamic_slot_pointer_candidates()
    if not candidates:
        _mem2_latch_note = "waiting for live fighter slot pointer"
        return False

    best = None  # (score, region_base, region_size, ptr_addr, fighter_ea)

    for base, size, protect, state in _iter_regions(_mem2_proc_handle):
        if state != MEM_COMMIT:
            continue
        prot = protect & 0xFF  # ignore guard/nocache flags
        if prot not in READABLE_PROTECT:
            continue
        if size < MEM2_SIZE:
            continue

        for ptr_addr, fighter_ea in candidates:
            off = fighter_ea - MEM2_LO
            if off < 0 or off + 0x130 > size:
                continue
            block = _rpm(_mem2_proc_handle, base + off, 0x130)
            if not block:
                continue
            score = _fighter_block_score(block)
            if best is None or score > best[0]:
                best = (score, base, size, ptr_addr, fighter_ea)
                if score >= 10:
                    break
        if best is not None and best[0] >= 10:
            break

    if not best or best[0] < 7:
        cand_s = ", ".join(f"0x{ea:08X}" for _ptr, ea in candidates[:4])
        _mem2_latch_note = f"slot candidates present but no validated MEM2 map: {cand_s}"
        return False

    score, base, size, ptr_addr, fighter_ea = best
    _mem2_host_base = int(base)
    _mem2_host_size = int(size)
    _mem2_latch_mode = "dynamic_slot"
    _mem2_latch_note = f"slot 0x{ptr_addr:08X}->0x{fighter_ea:08X} score={score}"
    print(f"[MEM2 latch] dynamic slot 0x{ptr_addr:08X}->0x{fighter_ea:08X}; host=0x{base:X}; score={score}")
    return True


def _latch_mem2_from_legacy_sentinel() -> bool:
    """Disabled legacy fixed-EA latch.

    The old sentinel at 0x9246B9C0 is mostly zero bytes.  From menus, many
    committed Dolphin regions read as zeros at the same offset, so the legacy
    score can falsely pass and permanently latch the wrong MEM2 host before a
    match exists.  That is the exact startup-from-main-menu failure: once a
    bad legacy latch is marked valid, the dynamic slot latch never gets a
    chance when the real fighter pointers appear.

    Keep this function stubbed so older callers do not break, but do not use
    it for automatic priming.
    """
    global _mem2_latch_note
    _mem2_latch_note = "legacy sentinel disabled; waiting for dynamic fighter slot"
    return False


def _latch_mem2_from_legacy_sentinel_DISABLED_OLD() -> bool:
    """Old fixed-EA latch retained only for reference; do not call automatically."""
    global _mem2_host_base, _mem2_host_size, _mem2_latch_mode, _mem2_latch_note

    # Compute EA offset into MEM2
    if not (MEM2_LO <= EXPECT_EA < MEM2_HI):
        _mem2_latch_note = "legacy sentinel outside MEM2"
        return False
    ea_off = EXPECT_EA - MEM2_LO

    best = None  # (score, base, size)
    for base, size, protect, state in _iter_regions(_mem2_proc_handle):
        if state != MEM_COMMIT:
            continue

        prot = protect & 0xFF  # ignore guard/nocache flags
        if prot not in READABLE_PROTECT:
            continue
        if size < MEM2_SIZE:
            continue

        host_addr = base + ea_off
        probe = _rpm(_mem2_proc_handle, host_addr, len(EXPECT_BYTES))
        if not probe:
            continue

        score = _score_match(probe)
        if best is None or score > best[0]:
            best = (score, base, size)
            if score >= len(EXPECT_BYTES):
                break

    if not best:
        _mem2_latch_note = "legacy sentinel not found"
        return False

    score, base, size = best
    if score < 16:
        _mem2_latch_note = f"legacy sentinel weak score={score}"
        return False

    _mem2_host_base = int(base)
    _mem2_host_size = int(size)
    _mem2_latch_mode = "legacy_sentinel"
    _mem2_latch_note = f"legacy EXPECT_EA=0x{EXPECT_EA:08X} score={score}"
    print(f"[MEM2 latch] legacy sentinel; host=0x{base:X}; score={score}")
    return True


def _latch_mem2():
    """
    Find the correct MEM2 host mapping.

    Primary path: dynamic live fighter slot pointers. This works when the GUI is
    launched from menus, because it simply stays unlatched until a match starts.
    Fallback path: the old fixed sentinel for already-active/legacy sessions.
    """
    global _mem2_proc_handle, _mem2_pid, _mem2_host_base, _mem2_host_size
    global _mem2_latch_note

    if not _IS_WINDOWS:
        return False

    pid = _find_pid_by_name(DOLPHIN_EXE)
    if pid is None:
        _reset_mem2_latch()
        _mem2_latch_note = "Dolphin.exe not found"
        return False

    # Reopen handle if PID changed
    if _mem2_pid != pid or _mem2_proc_handle is None:
        _reset_mem2_latch()
        h = _open_process(pid)
        if h is None:
            _mem2_latch_note = f"OpenProcess failed for pid={pid}"
            return False
        _mem2_proc_handle = h
        _mem2_pid = pid

    # Do not clear a good latch unless the module is actively replacing process handles.
    _mem2_host_base = None
    _mem2_host_size = None

    if _latch_mem2_from_dynamic_slots():
        return True

    if _latch_mem2_from_chrsel_sequence():
        return True

    # Do not fall back to the old fixed 0x9246B9C0 sentinel. In menu dumps that
    # address is all-zero and can identify the wrong 64 MB region.
    return False


def _mem2_is_latched() -> bool:
    return _mem2_proc_handle is not None and _mem2_host_base is not None


def _mem2_host_addr(ea: int) -> int:
    # EA must be in MEM2 range
    return int(_mem2_host_base + (ea - MEM2_LO))


def _ensure_mem2_latched(max_attempts: int = 1, sleep: float = 0.0, force: bool = False):
    """
    Ensure MEM2 latch exists without blocking the GUI.

    Old behavior retried for ~10 seconds and depended on a fixed fighter struct.
    New behavior is lazy: while no match exists it returns False quickly, then
    automatically latches as soon as the live slot pointers become valid.
    """
    global _mem2_last_latch_attempt

    if not _IS_WINDOWS:
        return True  # non-windows: fall back to DME behavior
    if _mem2_is_latched():
        # Defensive: older builds could create a bad legacy_sentinel latch from
        # the main menu.  If this module is hot-reloaded or reused, clear that
        # latch so the dynamic slot path can take over once a match starts.
        if _mem2_latch_mode == "legacy_sentinel":
            _reset_mem2_latch()
        else:
            return True

    now = time.time()
    if not force and (now - float(_mem2_last_latch_attempt or 0.0)) < LATCH_RETRY_SLEEP:
        return False

    attempts = max(1, int(max_attempts or 1))
    for i in range(attempts):
        _mem2_last_latch_attempt = time.time()
        if _latch_mem2():
            return True
        if sleep and i + 1 < attempts:
            time.sleep(float(sleep))
    return False


def prime_mem2_latch() -> bool:
    """Public non-blocking primer for the main loop/debug tools."""
    return _ensure_mem2_latched(max_attempts=1, sleep=0.0, force=False)


# ============================================================
# PUBLIC API
# ============================================================

def hook():
    """
    Hook Dolphin. MEM2 latches lazily when a live match slot appears.

    This intentionally does not block on MEM2. Starting the GUI from the main
    menu/character select should leave it armed in "MEM2 pending" state; the
    first match frame with valid fighter slot pointers will prime the latch.
    """
    while not dme.is_hooked():
        try:
            dme.hook()
        except Exception:
            pass
        time.sleep(0.2)

    # One quick dynamic attempt is useful if a match is already running, but
    # failure is normal from menus and must not stop future detection.  The old
    # fixed sentinel path is disabled, so this cannot accidentally latch a zero
    # menu page as MEM2.
    _ensure_mem2_latched(max_attempts=1, sleep=0.0, force=True)


def addr_in_ram(a):
    if a is None:
        return False
    return (MEM1_LO <= a < MEM1_HI) or (MEM2_LO <= a < MEM2_HI)


def _clamp_read_range(addr, size):
    if size <= 0:
        return False, addr, 0

    # MEM1
    if MEM1_LO <= addr < MEM1_HI:
        hi_allowed = min(addr + size, MEM1_HI)
        return True, addr, hi_allowed - addr

    # MEM2
    if MEM2_LO <= addr < MEM2_HI:
        hi_allowed = min(addr + size, MEM2_HI)
        return True, addr, hi_allowed - addr

    return False, addr, 0


# ============================================================
# READ FUNCTIONS
# ============================================================

def rbytes(addr, size):
    ok, base, span = _clamp_read_range(addr, size)
    if not ok or span <= 0:
        return b""

    # MEM2: use latched host mapping if possible
    if _IS_WINDOWS and (MEM2_LO <= base < MEM2_HI):
        if _ensure_mem2_latched():
            host = _mem2_host_addr(base)
            data = _rpm(_mem2_proc_handle, host, span)
            if data:
                return data

        # If latch fails or RPM fails, fall back to DME (may be wrong)
        try:
            data = dme.read_bytes(base, span)
            return data if data else b""
        except Exception:
            return b""

    # MEM1 or non-windows: use DME
    try:
        data = dme.read_bytes(base, span)
        return data if data else b""
    except Exception:
        return b""


def rd8(addr):
    if not addr_in_ram(addr):
        return None
    b = rbytes(addr, 1)
    if not b or len(b) != 1:
        return None
    return b[0]


def rd32(addr):
    if not addr_in_ram(addr):
        return None
    b = rbytes(addr, 4)
    if not b or len(b) != 4:
        return None
    return struct.unpack(">I", b)[0]


def rdf32(addr):
    if not addr_in_ram(addr):
        return None
    b = rbytes(addr, 4)
    if not b or len(b) != 4:
        return None

    raw_u32 = struct.unpack(">I", b)[0]
    f = struct.unpack(">f", struct.pack(">I", raw_u32))[0]
    if not math.isfinite(f) or abs(f) > 1e8:
        return None
    return f


# ============================================================
# WRITE FUNCTIONS
# ============================================================

def wbytes(addr, data, *, bypass_quarantine: bool = False):
    if not addr_in_ram(addr):
        return False
    if not data:
        return False

    payload = bytes(data)
    with _WRITE_QUARANTINE_LOCK:
        quarantined = bool(_WRITE_QUARANTINE_ACTIVE)
    if quarantined and not bool(bypass_quarantine):
        _trace_quarantined_write(int(addr), payload)
        return False

    # MEM2: latched write if possible
    if _IS_WINDOWS and (MEM2_LO <= addr < MEM2_HI):
        if _ensure_mem2_latched():
            host = _mem2_host_addr(addr)
            if _wpm(_mem2_proc_handle, host, bytes(data)):
                return True

        # fallback
        try:
            dme.write_bytes(addr, data)
            return True
        except Exception as e:
            print(f"wbytes failed at {addr:08X}: {e}")
            return False

    # MEM1 or non-windows
    try:
        dme.write_bytes(addr, data)
        return True
    except Exception as e:
        print(f"wbytes failed at {addr:08X}: {e}")
        return False


def wd8(addr, value, *, bypass_quarantine: bool = False):
    try:
        val = int(value) & 0xFF
    except Exception:
        return False
    return wbytes(addr, bytes([val]), bypass_quarantine=bypass_quarantine)


def wd32(addr, value, *, bypass_quarantine: bool = False):
    try:
        val = int(value) & 0xFFFFFFFF
    except Exception:
        return False
    return wbytes(addr, struct.pack(">I", val), bypass_quarantine=bypass_quarantine)


def wdf32(addr, value, *, bypass_quarantine: bool = False):
    try:
        f = float(value)
        if not math.isfinite(f):
            return False
    except Exception:
        return False
    raw = struct.unpack(">I", struct.pack(">f", f))[0]
    return wbytes(addr, struct.pack(">I", raw), bypass_quarantine=bypass_quarantine)


# Optional: expose latch state for debugging
def mem2_latch_info():
    return {
        "pid": _mem2_pid,
        "host_base": _mem2_host_base,
        "host_size": _mem2_host_size,
        "latched": _mem2_is_latched(),
        "mode": _mem2_latch_mode,
        "note": _mem2_latch_note,
        "last_attempt": _mem2_last_latch_attempt,
        "expect_ea": EXPECT_EA,
        "dynamic_slot_ptrs": DYNAMIC_SLOT_PTRS,
    }

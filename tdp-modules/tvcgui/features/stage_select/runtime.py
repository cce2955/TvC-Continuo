"""Direct live stage-code controls and the Workshop-to-Yami arena route."""
from __future__ import annotations

import threading
import time
from typing import Any

from tvcgui.runtime.session_recording import recorder as session_recorder

try:
    from tvcgui.platform.dolphin import rd32, wd32
except Exception:  # pragma: no cover
    rd32 = None
    wd32 = None

_LOCK = threading.RLock()
_WORKER_LOCK = threading.Lock()

# This static mirror is the raw cursor value used by the working iterator.
# It is the one stable address the GUI can always grab without trying to
# identify a loaded menu object.
STAGE_GLOBAL_SELECTION = 0x809BCEE0

# The game exposes the matching UI/runtime cursor through a live object while
# the stage carousel is present.  The root slot can legitimately be stale
# outside that scene, so it is an optional second write, never a precondition.
STAGE_RUNTIME_PTR = 0x809BCE28
RUNTIME_ENTRY_COUNT_OFF = 0x04
RUNTIME_SELF_PTR_OFF = 0x4C
RUNTIME_SELECTION_OFF = 0x6C
MEM2_LO = 0x90000000
MEM2_HI = 0x94000000

TEST_CODE_MIN = 0
TEST_CODE_MAX = 0xFFFFFFFF
RAW_CODE_DEFAULT = 0

# The verified Workshop handoff uses cursor 2 and carries arena ID 15 through
# character selection and match loading.
YAMI_REPLACEMENT_SLOT = 2
STAGE_RESULT_ID = 0x803EB8F4
YAMI_STAGE_ASSET_ID = 0x0000000F
YAMI_SLOT_ARM_TIMEOUT_SEC = 45.0
YAMI_CHARACTER_SELECT_TIMEOUT_SEC = 600.0
YAMI_MATCH_SETTLE_SEC = 2.0
YAMI_SLOT_POLL_SEC = 0.03
NATIVE_BATTLE_PTR = 0x803EFB4C
SELECT_SCENE_GLOBAL = 0x809BCECC
CHARACTER_UI_STATE_OFF = 0x70

_STATE: dict[str, Any] = {
    "last_action": "Ready. Grab or apply a raw stage ID.",
    "last_error": "",
    "last_write": None,
    "last_read": None,
    "worker_busy": False,
    "runtime_hint": None,
    "runtime_hint_at": 0.0,
    "last_resolved_stage_id": None,
    "slot2_yami": {
        "active": False,
        "source_code": YAMI_REPLACEMENT_SLOT,
        "phase": "idle",
        "baseline": None,
        "runtime": None,
        "writes": 0,
        "write_failures": 0,
        "last_write_failure": "",
        "global_overridden": False,
        "battle_phase": None,
        "detail": "",
    },
}

_SLOT2_YAMI_LOCK = threading.RLock()
_SLOT2_YAMI_CANCEL: threading.Event | None = None


def _u32(addr: int | None) -> int | None:
    if rd32 is None or addr is None:
        return None
    try:
        value = rd32(int(addr))
        return int(value) if value is not None else None
    except Exception:
        return None


def _fmt(value: int | None) -> str:
    return "--" if value is None else f"0x{int(value) & 0xFFFFFFFF:08X}"


def _in_mem2(value: int | None) -> bool:
    return value is not None and MEM2_LO <= int(value) < MEM2_HI


def _live_runtime() -> int | None:
    """Return the runtime only when the root currently names a stage object.

    This is deliberately light: it does not care about the old sequence
    header/table assumptions that caused the previous window to refuse every
    action after a reset.  It only prevents a second write into a recycled
    heap allocation.
    """
    root = _u32(STAGE_RUNTIME_PTR)
    if _in_mem2(root):
        runtime = int(root)
        if _u32(runtime + RUNTIME_ENTRY_COUNT_OFF) == 15 and _u32(runtime + RUNTIME_SELF_PTR_OFF) == runtime:
            with _LOCK:
                _STATE["runtime_hint"] = runtime
                _STATE["runtime_hint_at"] = time.time()
            return runtime

    # A cached runtime is usable only while it is still self-consistent.
    with _LOCK:
        cached = _STATE.get("runtime_hint")
    if _in_mem2(cached):
        runtime = int(cached)
        if _u32(runtime + RUNTIME_ENTRY_COUNT_OFF) == 15 and _u32(runtime + RUNTIME_SELF_PTR_OFF) == runtime:
            return runtime
    return None


def _set_slot2_yami_state(**updates: Any) -> None:
    with _LOCK:
        record = dict(_STATE.get("slot2_yami") or {})
        record.update(updates)
        _STATE["slot2_yami"] = record


def _slot2_yami_snapshot() -> dict[str, Any]:
    with _LOCK:
        return dict(_STATE.get("slot2_yami") or {})


def _finish_slot2_yami(*, detail: str, error: str = "") -> None:
    source_code = int(_slot2_yami_snapshot().get("source_code") or YAMI_REPLACEMENT_SLOT)
    _set_slot2_yami_state(active=False, phase="idle", detail=str(detail))
    with _LOCK:
        if error:
            _STATE["last_error"] = str(error)
            _STATE["last_action"] = f"ID {source_code} Yami route stopped."
        else:
            _STATE["last_error"] = ""
            _STATE["last_action"] = str(detail)


def _restore_slot2_baseline(baseline: int | None) -> bool:
    """Undo the pre-arm while the player is still browsing or backs out."""
    if baseline is None or wd32 is None:
        return False
    try:
        # The game may already have resolved the newly highlighted stage.
        # Only undo the value this route actually owns.
        if _u32(STAGE_RESULT_ID) != YAMI_STAGE_ASSET_ID:
            return False
        tile_source = int(_slot2_yami_snapshot().get("source_code") or YAMI_REPLACEMENT_SLOT) == 15
        return _stage_write32(STAGE_RESULT_ID, int(baseline), tile_source=tile_source) and _u32(STAGE_RESULT_ID) == int(baseline)
    except Exception:
        return False


def _native_battle_phase() -> int | None:
    ptr = _u32(NATIVE_BATTLE_PTR)
    if ptr is None or not (0x80000000 <= ptr < 0x81800000 or _in_mem2(ptr)):
        return None
    phase = _u32(ptr + 12)
    return phase if phase is not None and 0 <= phase <= 12 else None


def _stage_write32(address: int, value: int, *, tile_source: bool = False) -> bool:
    if wd32 is None:
        return False
    if tile_source:
        try:
            return bool(wd32(address, value, bypass_quarantine=True))
        except TypeError:  # test memory fakes
            pass
    return bool(wd32(address, value))


def _write_verified(address: int, value: int, *, tile_source: bool = False) -> bool:
    if wd32 is None:
        return False
    try:
        return _stage_write32(address, value, tile_source=tile_source) and _u32(address) == value
    except Exception:
        return False


def _restore_pending_yami(baseline: int | None, *, global_overridden: bool) -> None:
    if global_overridden and _u32(STAGE_GLOBAL_SELECTION) == YAMI_STAGE_ASSET_ID:
        source_code = int(_slot2_yami_snapshot().get("source_code") or YAMI_REPLACEMENT_SLOT)
        _write_verified(STAGE_GLOBAL_SELECTION, source_code, tile_source=source_code == 15)
    _restore_slot2_baseline(baseline)


def _record_stage_route(*, observed_global: int | None, observed_result: int | None,
                        carousel_live: bool | None = None) -> None:
    """Preserve the handoff evidence in Settings > Recorded session."""
    state = _slot2_yami_snapshot()
    with _LOCK:
        last_error = str(_STATE.get("last_error") or "")
    root = _u32(STAGE_RUNTIME_PTR)
    session_recorder.capture_event(time.monotonic(), "stage_route", {
        "phase": state.get("phase"),
        "detail": state.get("detail"),
        "writes": state.get("writes"),
        "write_failures": state.get("write_failures"),
        "last_write_failure": state.get("last_write_failure"),
        "last_error": last_error,
        "observed_global": observed_global,
        "observed_result": observed_result,
        "after_global": _u32(STAGE_GLOBAL_SELECTION),
        "after_result": _u32(STAGE_RESULT_ID),
        "carousel_root": root,
        "carousel_cursor": _u32(root + RUNTIME_SELECTION_OFF) if _in_mem2(root) else None,
        "scene_state": _u32(SELECT_SCENE_GLOBAL),
        "ui_state": _u32(root + CHARACTER_UI_STATE_OFF) if _in_mem2(root) else None,
        "carousel_live": carousel_live,
        "battle_phase": _native_battle_phase(),
    })


def _run_slot2_yami_override(
    *,
    starting_runtime: int,
    baseline_value: int | None,
    cancel_event: threading.Event,
    source_code: int = YAMI_REPLACEMENT_SLOT,
) -> None:
    """Carry a selected Yami route into the resolved arena at load.

    Stage selection precedes character selection. Keep the chosen arena in
    both selection and result fields after the carousel closes, until the
    native battle object reports a live match. Writes occur only on drift.
    """
    global _SLOT2_YAMI_CANCEL
    started = time.monotonic()
    select_deadline = started + YAMI_SLOT_ARM_TIMEOUT_SEC
    match_deadline: float | None = None
    live_since: float | None = None
    writes = 1  # verified pre-arm write
    write_failures = 0
    last_write_failure = ""
    global_overridden = False
    phase = "waiting_for_confirm"
    last_sample: tuple | None = None
    _set_slot2_yami_state(
        active=True,
        source_code=source_code,
        phase=phase,
        baseline=baseline_value,
        runtime=starting_runtime,
        writes=writes,
        write_failures=0,
        last_write_failure="",
        global_overridden=False,
        battle_phase=None,
        detail=f"ID {source_code} selected. Yami stage route is armed for the next normal confirm.",
    )
    with _LOCK:
        _STATE["last_error"] = ""
        _STATE["last_action"] = f"Applied raw stage ID {source_code}. Yami stage route is armed for the next confirm."

    try:
        while not cancel_event.is_set():
            now = time.monotonic()
            raw_code = _u32(STAGE_GLOBAL_SELECTION)
            root = _u32(STAGE_RUNTIME_PTR)
            carousel_live = (
                root == starting_runtime
                and _u32(starting_runtime + RUNTIME_ENTRY_COUNT_OFF) == 15
                and _u32(starting_runtime + RUNTIME_SELF_PTR_OFF) == starting_runtime
                and _u32(SELECT_SCENE_GLOBAL) == 0
                and _u32(starting_runtime + CHARACTER_UI_STATE_OFF) == 0
            )
            current_result = _u32(STAGE_RESULT_ID)
            battle_phase = _native_battle_phase()
            with _LOCK:
                _STATE["last_resolved_stage_id"] = current_result
                _STATE["slot2_yami"]["battle_phase"] = battle_phase

            if phase == "waiting_for_confirm":
                if carousel_live and raw_code != source_code:
                    _restore_pending_yami(baseline_value, global_overridden=False)
                    _finish_slot2_yami(detail=f"ID {source_code} Yami route cancelled: stage cursor moved away.")
                    return
                if not carousel_live or (current_result is not None and current_result != YAMI_STAGE_ASSET_ID):
                    phase = "awaiting_match"
                    match_deadline = now + YAMI_CHARACTER_SELECT_TIMEOUT_SEC
                    _set_slot2_yami_state(phase=phase, detail="Stage confirmed; waiting through character select for match load.")
                elif now >= select_deadline:
                    _restore_pending_yami(baseline_value, global_overridden=False)
                    _finish_slot2_yami(detail=f"ID {source_code} Yami route timed out at stage select.")
                    return

            if phase in {"awaiting_match", "match_live"}:
                if phase == "awaiting_match" and carousel_live and raw_code != source_code:
                    _restore_pending_yami(baseline_value, global_overridden=global_overridden)
                    _finish_slot2_yami(detail=f"Yami route cancelled: returned to stage select and moved off ID {source_code}.")
                    return
                if battle_phase == 4:
                    if live_since is None:
                        live_since = now
                        phase = "match_live"
                        _set_slot2_yami_state(phase=phase, detail="Live match detected; settling Yami stage route.")
                elif phase == "match_live":
                    live_since = None
                    phase = "awaiting_match"
                    _set_slot2_yami_state(phase=phase, detail="Match load still changing; waiting for live battle.")

                # The selected tile needs its raw cursor until the stage
                # menu closes. Afterward, the game may copy that cursor into
                # the result during character select or the later loader.
                if not carousel_live and raw_code != YAMI_STAGE_ASSET_ID:
                    if not _write_verified(STAGE_GLOBAL_SELECTION, YAMI_STAGE_ASSET_ID,
                                           tile_source=source_code == 15):
                        write_failures += 1
                        last_write_failure = f"Selection ID 15 did not hold; read {_fmt(_u32(STAGE_GLOBAL_SELECTION))}."
                    else:
                        writes += 1
                        global_overridden = True
                if current_result != YAMI_STAGE_ASSET_ID:
                    if not _write_verified(STAGE_RESULT_ID, YAMI_STAGE_ASSET_ID,
                                           tile_source=source_code == 15):
                        write_failures += 1
                        last_write_failure = f"Result ID 15 did not hold; read {_fmt(_u32(STAGE_RESULT_ID))}."
                    else:
                        writes += 1
                        with _LOCK:
                            _STATE["last_resolved_stage_id"] = YAMI_STAGE_ASSET_ID
                _set_slot2_yami_state(writes=writes, write_failures=write_failures,
                                      last_write_failure=last_write_failure,
                                      global_overridden=global_overridden)

                if live_since is not None and now - live_since >= YAMI_MATCH_SETTLE_SEC:
                    final_result = _u32(STAGE_RESULT_ID)
                    _finish_slot2_yami(
                        detail=(f"Match became live with result ID {_fmt(final_result)}; "
                                f"{writes} verified writes, {write_failures} failed attempts. Confirm arena visually."),
                        error=("Yami result ID 15 was not held at live match."
                               if final_result != YAMI_STAGE_ASSET_ID else ""),
                    )
                    return
                if match_deadline is not None and now >= match_deadline:
                    _restore_pending_yami(baseline_value, global_overridden=global_overridden)
                    _finish_slot2_yami(detail="Yami route timed out before a live match; previous stage result restored.")
                    return

            sample = (phase, raw_code, current_result, _u32(STAGE_GLOBAL_SELECTION),
                      _u32(STAGE_RESULT_ID), carousel_live, battle_phase, writes,
                      last_write_failure)
            if sample != last_sample:
                _record_stage_route(observed_global=raw_code,
                                    observed_result=current_result,
                                    carousel_live=carousel_live)
                last_sample = sample
            time.sleep(YAMI_SLOT_POLL_SEC)
    except Exception as exc:  # pragma: no cover
        _finish_slot2_yami(
            detail=f"ID {source_code} Yami route stopped.",
            error=f"ID {source_code} Yami route failed: {exc!r}",
        )
    finally:
        _record_stage_route(observed_global=_u32(STAGE_GLOBAL_SELECTION),
                            observed_result=_u32(STAGE_RESULT_ID))
        with _SLOT2_YAMI_LOCK:
            if _SLOT2_YAMI_CANCEL is cancel_event:
                _SLOT2_YAMI_CANCEL = None


def _arm_slot2_yami_override(runtime: int | None, *, source_code: int = YAMI_REPLACEMENT_SLOT) -> tuple[bool, str]:
    """Pre-arm the Yami load substitution for a verified stage cursor."""
    global _SLOT2_YAMI_CANCEL
    if runtime is None:
        return False, f"ID {source_code} was selected, but the live carousel is not present so no Yami route was armed."
    if _u32(SELECT_SCENE_GLOBAL) != 0 or _u32(runtime + CHARACTER_UI_STATE_OFF) != 0:
        return False, "The Yami route must be armed while the stage carousel is visible."
    if wd32 is None:
        return False, f"ID {source_code} was selected, but Dolphin write support is unavailable for the Yami route."

    with _SLOT2_YAMI_LOCK:
        existing = _SLOT2_YAMI_CANCEL
        existing_state = _slot2_yami_snapshot()
        if existing is not None and not existing.is_set():
            if existing_state.get("runtime") == int(runtime) and int(existing_state.get("source_code") or YAMI_REPLACEMENT_SLOT) == source_code:
                return True, f"ID {source_code} remains armed for Yami's stage."
            existing.set()

        baseline = _u32(STAGE_RESULT_ID)
        try:
            prearm_ok = _stage_write32(STAGE_RESULT_ID, YAMI_STAGE_ASSET_ID,
                                       tile_source=source_code == 15)
            prearm_value = _u32(STAGE_RESULT_ID)
        except Exception as exc:
            return False, f"ID {source_code} was selected, but Yami pre-arm failed: {exc!r}"
        if not prearm_ok or prearm_value != YAMI_STAGE_ASSET_ID:
            return False, f"ID {source_code} was selected, but the Yami stage-result pre-arm did not verify."
        with _LOCK:
            _STATE["last_resolved_stage_id"] = prearm_value

        cancel_event = threading.Event()
        _SLOT2_YAMI_CANCEL = cancel_event
        _set_slot2_yami_state(
            active=True,
            source_code=source_code,
            phase="starting",
            baseline=baseline,
            runtime=int(runtime),
            writes=1,
            detail=f"ID {source_code} selected. Starting Yami stage route.",
        )
        _record_stage_route(observed_global=_u32(STAGE_GLOBAL_SELECTION),
                            observed_result=baseline, carousel_live=True)
        threading.Thread(
            target=_run_slot2_yami_override,
            kwargs={
                "starting_runtime": int(runtime),
                "baseline_value": baseline,
                "cancel_event": cancel_event,
                "source_code": source_code,
            },
            name=f"StageId{source_code}YamiRoute",
            daemon=True,
        ).start()
    return True, f"ID {source_code} selected. Yami stage route is armed for the next confirm."


def _cancel_slot2_yami_if_active(*, restore: bool) -> bool:
    """Disarm a pending Yami route when another stage is selected."""
    global _SLOT2_YAMI_CANCEL
    with _SLOT2_YAMI_LOCK:
        event = _SLOT2_YAMI_CANCEL
        _SLOT2_YAMI_CANCEL = None
    if event is None:
        return False
    record = _slot2_yami_snapshot()
    event.set()
    pending = record.get("phase") in {"starting", "waiting_for_confirm", "awaiting_match"}
    if restore and pending:
        _restore_pending_yami(record.get("baseline"), global_overridden=bool(record.get("global_overridden")))
    restored = bool(restore and pending)
    _set_slot2_yami_state(
        active=False,
        phase="idle",
        detail=f"ID {int(record.get('source_code') or YAMI_REPLACEMENT_SLOT)} Yami route cancelled."
               + (" Pre-arm value restored." if restored else ""),
    )
    return True


def _snapshot() -> dict[str, Any]:
    global_code = _u32(STAGE_GLOBAL_SELECTION)
    root = _u32(STAGE_RUNTIME_PTR)
    runtime = _live_runtime()
    runtime_code = _u32(runtime + RUNTIME_SELECTION_OFF) if runtime is not None else None
    resolved_stage_id = _u32(STAGE_RESULT_ID)
    with _LOCK:
        _STATE["last_resolved_stage_id"] = resolved_stage_id
    return {
        "global_code": global_code,
        "runtime_code": runtime_code,
        "runtime": runtime,
        "root": root,
        "runtime_live": runtime is not None,
    }


def _run_worker(label: str, fn: Any) -> dict[str, Any]:
    with _WORKER_LOCK:
        with _LOCK:
            if _STATE.get("worker_busy"):
                _STATE["last_action"] = "Stage ID operation is already running."
                _STATE["last_error"] = ""
                return get_stage_probe_state()
            _STATE["worker_busy"] = True
            _STATE["last_action"] = f"{label}…"
            _STATE["last_error"] = ""

    def _worker() -> None:
        try:
            fn()
        except Exception as exc:  # pragma: no cover
            with _LOCK:
                _STATE["last_action"] = "Stage ID operation stopped."
                _STATE["last_error"] = f"{label} failed: {exc!r}"
        finally:
            with _LOCK:
                _STATE["worker_busy"] = False

    threading.Thread(target=_worker, name="StageIdWorker", daemon=True).start()
    return get_stage_probe_state()


def read_stage_selection() -> dict[str, Any]:
    snapshot = _snapshot()
    with _LOCK:
        _STATE["last_read"] = dict(snapshot)
        _STATE["last_error"] = ""
        if snapshot["runtime_live"]:
            _STATE["last_action"] = (
                f"Grabbed raw stage ID {snapshot['global_code']} from the global mirror and "
                f"{snapshot['runtime_code']} from the live carousel mirror."
            )
        else:
            _STATE["last_action"] = (
                f"Grabbed raw stage ID {snapshot['global_code']} from the global mirror. "
                "The stage carousel runtime is not live yet."
            )
    return get_stage_probe_state()


def request_stage_probe() -> dict[str, Any]:
    return _run_worker("Grabbing the live stage ID", read_stage_selection)


def set_stage_selection(code: int, *, label: str | None = None, arm_yami: bool = False) -> dict[str, Any]:
    try:
        target = int(code)
    except Exception:
        target = -1
    if not TEST_CODE_MIN <= target <= TEST_CODE_MAX:
        with _LOCK:
            _STATE["last_error"] = "Stage ID must be an unsigned 32-bit value."
            _STATE["last_action"] = "No stage ID write was sent."
        return get_stage_probe_state()
    if wd32 is None:
        with _LOCK:
            _STATE["last_error"] = "Dolphin write support is unavailable."
            _STATE["last_action"] = "No stage ID write was sent."
        return get_stage_probe_state()

    # Keep the direct control exactly one-shot for every other ID.  Only an
    # actual ID-2 request is allowed to carry a Yami load-route worker.
    if target != YAMI_REPLACEMENT_SLOT:
        _cancel_slot2_yami_if_active(restore=True)

    before = _snapshot()
    global_ok = False
    runtime_ok: bool | None = None
    runtime_addr: int | None = None
    write_error = ""
    try:
        # Always write the stable raw-ID mirror.  This is the path that must
        # remain usable even when a reset has invalidated the old heap object.
        global_ok = bool(wd32(STAGE_GLOBAL_SELECTION, target))

        # When the actual carousel is live, mirror the same raw code into the
        # current runtime object.  It is optional, so a stale root cannot veto
        # the global write or turn the UI into a refusal screen.
        runtime = _live_runtime()
        if runtime is not None:
            runtime_addr = runtime + RUNTIME_SELECTION_OFF
            runtime_ok = bool(wd32(runtime_addr, target))
    except Exception as exc:  # pragma: no cover
        write_error = repr(exc)

    # Let the menu's next update consume the static mirror before sampling it.
    time.sleep(0.015)
    after = _snapshot()
    global_verified = after["global_code"] == target
    runtime_verified = (
        runtime_ok is None
        or (after["runtime_live"] and after["runtime_code"] == target)
    )
    verified = bool(global_ok and global_verified and runtime_verified)
    record = {
        "target": target,
        "label": str(label or f"raw stage ID {target}"),
        "before_global": before["global_code"],
        "before_runtime": before["runtime_code"],
        "after_global": after["global_code"],
        "after_runtime": after["runtime_code"],
        "global_write_ok": global_ok,
        "runtime_write_ok": runtime_ok,
        "runtime_address": runtime_addr,
        "verified": verified,
    }
    with _LOCK:
        _STATE["last_write"] = record
        _STATE["last_read"] = dict(after)
        if verified:
            _STATE["last_error"] = ""
            if runtime_ok is None:
                _STATE["last_action"] = (
                    f"Applied raw stage ID {target} to the stable cursor mirror. "
                    "Open the stage carousel and press Apply again to mirror it into the live UI."
                )
            else:
                _STATE["last_action"] = f"Applied raw stage ID {target}."
        else:
            detail = write_error or "the write did not remain in the raw cursor mirror"
            _STATE["last_error"] = f"Raw stage ID write did not verify: {detail}."
            _STATE["last_action"] = "No ongoing write loop was left running."

    # Stage Control itself is intentionally unchanged.  This is the sole
    # conditional add-on: applying a verified ID 2 on a live carousel arms the
    # known Yami result ID for that selection's normal confirm.
    if verified and target == YAMI_REPLACEMENT_SLOT and arm_yami:
        armed, detail = _arm_slot2_yami_override(after.get("runtime"))
        with _LOCK:
            if armed:
                _STATE["last_error"] = ""
                _STATE["last_action"] = detail
            else:
                _STATE["last_error"] = detail
                _STATE["last_action"] = "Applied raw stage ID 2, but Yami routing was not armed."
    return get_stage_probe_state()


def request_stage_selection(code: int, *, label: str | None = None, arm_yami: bool = False) -> dict[str, Any]:
    return _run_worker(
        f"Applying raw stage ID {int(code) if isinstance(code, int) else code}",
        lambda: set_stage_selection(code, label=label, arm_yami=arm_yami),
    )


def get_stage_probe_state(*, refresh: bool = False) -> dict[str, Any]:
    if refresh:
        read_stage_selection()
    with _LOCK:
        read = dict(_STATE.get("last_read") or {})
        return {
            "mode": "direct_raw_stage_id",
            "last_action": str(_STATE.get("last_action") or ""),
            "last_error": str(_STATE.get("last_error") or ""),
            "last_write": dict(_STATE.get("last_write") or {}),
            "last_read": read,
            "worker_busy": bool(_STATE.get("worker_busy")),
            "addresses": {
                "global_selection": _fmt(STAGE_GLOBAL_SELECTION),
                "runtime_root": _fmt(STAGE_RUNTIME_PTR),
                "runtime_selection": _fmt((int(read["runtime"]) + RUNTIME_SELECTION_OFF) if read.get("runtime") is not None else None),
            },
            "test_code_min": TEST_CODE_MIN,
            "test_code_max": TEST_CODE_MAX,
            "slot2_yami": dict(_STATE.get("slot2_yami") or {}),
            "resolved_stage_id": _STATE.get("last_resolved_stage_id"),
        }


def request_arm_yami_stage() -> dict[str, Any]:
    return request_stage_selection(YAMI_REPLACEMENT_SLOT, label="Yami handoff test", arm_yami=True)


def cancel_stage_result_override() -> dict[str, Any]:
    if not _cancel_slot2_yami_if_active(restore=True):
        with _LOCK:
            _STATE["last_action"] = "No Yami arena route is armed."
            _STATE["last_error"] = ""
    return get_stage_probe_state()


def select_stage_code_then_arm_yami(code: int, *, label: str | None = None) -> dict[str, Any]:
    return set_stage_selection(code, label=label)


def select_yami_stage_slot() -> dict[str, Any]:
    return set_stage_selection(YAMI_REPLACEMENT_SLOT, label="Yami handoff test", arm_yami=True)


def move_to_stage_code(code: int) -> dict[str, Any]:
    return set_stage_selection(code, label=f"raw stage ID {int(code)}")


def tick_stage_probe() -> None:
    from . import yami_tile
    yami_tile.ensure_running()


def tick_stage_test() -> None:
    return None


def get_stage_patch_state() -> dict[str, Any]:
    return get_stage_probe_state()


def queue_stage_capture(label: str) -> dict[str, Any]:
    with _LOCK:
        _STATE["last_error"] = "Capture mode is not part of the direct raw-ID control."
        _STATE["last_action"] = "No file was created."
    return get_stage_probe_state()


def queue_stage_clear() -> dict[str, Any]:
    return get_stage_probe_state()

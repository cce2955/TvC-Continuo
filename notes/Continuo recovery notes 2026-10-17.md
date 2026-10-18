# Continuo recovery notes — 2026-10-17

These are project notes for recovering the working behavior after a refactor or a bad build. They are not agent instructions.

## Known working baseline

- KO Control was confirmed by the user in live Dolphin play after the post-KO winner input route was restored. The historical reference the user identified as working is commit `f971adbfc2f2f1e03f848e9d2d568768fb5ebd8b`. The current implementation lives in `tdp-modules/tvcgui/runtime/ko_control.py`.
- During normal play KO Control stays SAFE. After a full team KO it switches to FULL, releases the living winning point fighter, and bridges result-screen input. When both teams are live again it returns to SAFE. OFF restores the original DOL instructions. The two result input gates at `0x80076820` and `0x80076A80` must be restored outside FULL mode.
- The top dock should show ARMED while SAFE is waiting and ACTIVE during the full KO result window. A successful memory write or ACTIVE label alone does not prove the fighter can move; live movement is the final check.
- Music, normal preview, live punish, and overlay changes in the October 17 worktree are not represented by the repository HEAD (`29bf135`). Preserve the current source and tests before resetting or checking out older commits.

## Recovery checks

From `tdp-modules`, run:

```powershell
..\.venv\Scripts\python.exe -m pytest -q tests/test_music_rules.py tests/test_music_result_persistence.py tests/test_music_loop_preview.py tests/test_music_track_loops.py tests/test_music_library_location.py tests/test_ax_bgm_gate_native_restore.py tests/test_preview_ko_runtime_fixes.py tests/test_overlay_customization.py tests/test_settings_sessions_and_fighter_frames.py tests/test_compact_telemetry_v3_mainhud_contract.py tests/test_compact_telemetry_v4_meter_rail_contract.py tests/test_top_corner_hud_contract.py -p no:cacheprovider
```

This focused set passed **111 tests** on October 17. It covers music rules and native restore, loop behavior, preview/punish logic, KO patch bytes and mode transitions, settings, and overlay customization. It is an automated regression check, not a replacement for the Dolphin movement and audio checks.

The full legacy `tests` collection was already red on October 17: it reached 20 failures and 102 passes before stopping at `--maxfail=20`. Early failures include attack property expectations and source-string contracts for older GUI layouts. Do not interpret a full-suite failure as a new KO or music regression without checking the focused set and the actual behavior.

## Where settings and evidence live

- Overlay visual settings: `tdp-modules/data/overlay/overlay_customization.json` at runtime, with defaults and validation in `tvcgui/features/overlay/customization.py`. The editor is at **Settings & Sessions → Customize overlay**. Edit positions is an explicit mode; drag P1/P2 on the game screen, then turn it off. The dock's older switches remain in `master_overlay_control.json`.
- Music settings and files: `tdp-modules/data/continuo/`. Keep personal music and settings during cleanup. The music dock must avoid a full controller status query every GUI frame; `dock_active()` is its cheap state read.
- KO recordings: `TvCContinuo-session-20261017-143932.json` and `...144410.json` under `tdp-modules` document the stuck period. Other `TvCContinuo-session-*.json` files may contain useful state transitions. Do not delete them as build artifacts.
- The tester's `image.webp` and `image (1).webp` are evidence for the AX BGM pairing issue. Preview playback worked while live stage audio reported an unverified AX BGM pair. The AX gate diagnostics and alternate paired-address handling are in `tvcgui/features/audio/ax_bgm_gate.py`.
- `image (2).webp` adds a live AX inspection: stage 17 and one custom track were detected, but the pair remained 0/2. Its two structural candidates use combined control `0x0C63000B` on the canonical stream ranges. The gate now recognizes that exact form only, with the existing unique-pair and readback checks. This source fix still needs tester validation in a rebuilt EXE.
- `TvC_Recomp Package.zip` is reference material and is intentionally retained.

## Performance baseline and changes

The recorded GUI sessions showed median frame work around 250–310 ms, mostly draw time. Dock status polling, repeated outlined text composition, and per-frame portrait scaling were reduced in `ui/components.py` and `main.py`. The change was verified by tests and syntax checks, but a new live frame-time capture is still needed before claiming a 60 FPS result.

## Builds

PyInstaller outputs are generated artifacts. `build_onefile.bat` uses `tdp-modules/TvCGUI_onefile.spec`. Source changes made after an EXE was built are absent from that EXE. Keep one current tested distribution and remove superseded build files only after verifying the new build. Do not remove `data`, `assets`, `missions`, session recordings, screenshots, or the recomp archive during build cleanup.

Important cleanup finding: `tdp-modules/dist-release/data` and `tdp-modules/dist-axcompat/data` contain nearly 2 GB of runtime files, including personal music and profiles. Treat those `data` folders as user data, even though they sit under directories named `dist-*`. Never delete those trees as disposable PyInstaller outputs.

The current one-file EXE is `tdp-modules/dist-current/TvCGUI.exe` (built October 17, SHA-256 `EF6323533153D17974836D37452C4D9C02045FE693D625A453DA1F7DB2C03597`). PyInstaller's analysis included the Tk runtime and overlay customizer. The old `build`, `build-release`, `build-current`, `dist`, `dist 18S`, and `dist-diagnostic` trees were removed after the new build. Only the obsolete EXE files were removed from `dist-release` and `dist-axcompat`; their `data` folders remain. The older `.gitignore.pre-cleanup.bak` was also removed.

For a tester whose music lives beside an older EXE, replace **only** that EXE with the current one. Keep their `data` folder and music files in place. The combined-control AX fix has automated tests but has not yet been heard in that tester's Dolphin session.

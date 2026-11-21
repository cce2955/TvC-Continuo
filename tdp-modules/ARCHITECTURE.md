# Continuo architecture and testing

Continuo runs from `main.py`, which coordinates the HUD loop, fighter snapshots,
feature updates, and the existing Tk-hosted windows. The importable application
package is `tvcgui` in this directory.

## Package layout

- `tvcgui/core`: shared constants, configuration, paths, and Tk coordination.
- `tvcgui/platform/dolphin.py`: Dolphin connection and guest-memory access.
- `tvcgui/runtime`: per-frame coordination, session recording, and optional
  training behavior such as the sequence trainer.
- `tvcgui/features`: feature-specific interfaces and logic, including combat,
  frame data, assists, audio, character and stage selection, and overlays.
- `tvcgui/tools/scanners`: fighter resolution and memory-backed scanners.
- `tvcgui/ui`: common presentation components and windows.
- `tests`: automated tests. The repository also has standalone research notes
  and scripts under `../research/agent_mapping` when present locally.

The feature directories contain both read-only diagnostics and explicitly
controlled training features. Check the module's contract before reusing a
memory operation; a read-only caller must not invoke a write or hook merely to
obtain data. Address constants belong to a particular game build and memory
space. Resolve and validate fighter identity after pointer changes, and mark
missing or unverified values as unavailable.

## Feature boundaries

- `features/assists/backend.py` owns assist profile selection, caches, and
  guarded runtime routing. Diagnostic access to its cached state must stay
  independent of writes and duplicate-character safeguards.
- `features/combat/projectile_scanner.py` hosts the projectile/profile monitor,
  including the Research tab. `features/combat/timing_engine.py` shares sampled
  hitstop and stun observations with the HUD and frame-data views.
- `features/frame_data` holds scanning integrations, workbenches, cancel models,
  and spreadsheet export. A modeled cancel or exported timing value is evidence
  to validate, not automatically a verified move property.
- `features/character_select` and `features/stage_select` contain selection
  logic and opt-in modifications. Their pointers and resource offsets are
  scene-specific and must be revalidated after loading or leaving a menu.
- `runtime/ko_control.py` and `runtime/punish_training.py` include optional
  gameplay writes. Their lifetimes and cleanup must remain separate from
  read-only recorders and background observers.

Several of these modules combine UI, state, and native-memory handling. When
changing one, extract a coherent responsibility with its existing tests and
public entry points intact; broad formatting or moving files is not a substitute
for checking pointer ownership, thread boundaries, and cleanup behavior.

## Capture and threading

The Research tab uses `features/combat/research_capture.py` for sampling,
buffering, and export, and `research_capture_panel.py` for Tk controls. A
worker reads slots sequentially; samples in a batch are not frame-atomic. The
optional damage mailbox reader consumes an existing probe and does not install
one. UI updates stay on the Tk thread, and export works from a locked snapshot
of the bounded buffers.

## Tests

From the repository root, the standard-library launcher runs tests discoverable
by `unittest`:

```powershell
.\.venv\Scripts\python.exe tdp-modules\run_unit_tests.py
```

If pytest is already available in the virtual environment, it also discovers
pytest-style functions and fixtures:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tdp-modules\tests -q
```

For a focused Research capture check:

```powershell
.\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider tdp-modules\tests\test_research_fighter_capture.py -q
```

Neither offline tests nor static address checks substitute for a live Dolphin
check of pointer lifetime, game timing, or visual behavior.

"""Portable export/import of user preferences and editable profiles.

The allowlist deliberately excludes music files, memory dumps, logs and
generated frame-data exports. Imported settings take effect after restart.
"""
from __future__ import annotations

from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

from tvcgui.core.paths import user_data_path


FORMAT = "tvc-continuo-settings"
VERSION = 1
PENDING_IMPORT = ".pending_settings_import.zip"
MAX_FILE_BYTES = 2_000_000
MAX_TOTAL_BYTES = 20_000_000
SETTINGS_PATTERNS = (
    "assists/quick_assists.json",
    "character_select/visual_overrides.json",
    "character_select/yami_name_overlay.json",
    "continuo/music_location.json",
    "continuo/music_settings.json",
    "frame_data/custom_cancel_windows.json",
    "frame_data/frame_data_custom_columns.json",
    "frame_data/frame_data_label_overrides.json",
    "frame_data/frame_data_ui_prefs.json",
    "frame_data/frame_data_profiles/*.json",
    "hitboxes/hitbox_filter.json",
    "hitboxes/hitbox_range_profiles.json",
    "overlay/master_overlay_control.json",
    "overlay/overlay_customization.json",
    "overlay/hud_presets.json",
    "training/megacrash_trainer.json",
    "training/mission_progress.json",
    "training/punish_trainer.json",
    "training/punish_training.json",
)


def _allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or "\\" in name:
        return False
    return any(fnmatchcase(name, pattern) for pattern in SETTINGS_PATTERNS)


def _data_root(root: Path | None) -> Path:
    return Path(root) if root is not None else Path(user_data_path())


def _target(root: Path, name: str) -> Path:
    target = root.joinpath(*PurePosixPath(name).parts)
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Settings destination escapes data folder: {name}")
    return target


def export_settings(destination: Path, *, data_root: Path | None = None) -> int:
    """Write an archive of known settings; return its file count."""
    root = _data_root(data_root)
    files = []
    total = 0
    if root.is_dir():
        for path in root.rglob("*.json"):
            name = path.relative_to(root).as_posix()
            if not _allowed(name) or path.is_symlink() or not path.is_file():
                continue
            size = path.stat().st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(f"Setting is too large to export: {name}")
            total += size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Settings exceed the archive size limit")
            payload = path.read_bytes()
            json.loads(payload)  # reject a damaged setting before exporting it
            files.append((name, payload))
    files.sort(key=lambda item: item[0])
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "files": {name: hashlib.sha256(payload).hexdigest() for name, payload in files},
    }
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
        for name, payload in files:
            archive.writestr(name, payload)
    return len(files)


def _read_archive(source: Path) -> dict[str, bytes]:
    """Validate every member before any setting is written."""
    with zipfile.ZipFile(source) as archive:
        infos = archive.infolist()
        names = [item.filename for item in infos]
        if len(names) != len(set(names)) or "manifest.json" not in names:
            raise ValueError("Invalid or duplicate settings archive entries")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
            raise ValueError("Unsupported settings archive")
        expected = manifest.get("files")
        if not isinstance(expected, dict) or set(names) != set(expected) | {"manifest.json"}:
            raise ValueError("Settings archive manifest does not match its files")
        total = 0
        payloads = {}
        for info in infos:
            name = info.filename
            if name == "manifest.json":
                continue
            if not _allowed(name) or info.is_dir() or info.file_size > MAX_FILE_BYTES:
                raise ValueError(f"Unsupported settings file: {name}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("Settings archive exceeds the size limit")
            payload = archive.read(info)
            if hashlib.sha256(payload).hexdigest() != expected[name]:
                raise ValueError(f"Settings checksum mismatch: {name}")
            json.loads(payload)
            payloads[name] = payload
    return payloads


def import_settings(source: Path, *, data_root: Path | None = None) -> int:
    """Replace settings with rollback on error. Call before app modules load."""
    root = _data_root(data_root)
    payloads = _read_archive(source)

    previous = {}
    changed = []
    try:
        for name, payload in payloads.items():
            target = _target(root, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink():
                raise ValueError(f"Settings destination is a link: {name}")
            previous[name] = target.read_bytes() if target.exists() else None
            _replace_bytes(target, payload)
            changed.append(name)
    except Exception:
        for name in reversed(changed):
            target = _target(root, name)
            old = previous[name]
            if old is None:
                target.unlink(missing_ok=True)
            else:
                _replace_bytes(target, old)
        raise
    return len(changed)


def schedule_settings_import(source: Path, *, data_root: Path | None = None) -> int:
    """Validate now, then install on next startup before settings are loaded."""
    root = _data_root(data_root)
    payloads = _read_archive(source)
    root.mkdir(parents=True, exist_ok=True)
    _replace_bytes(root / PENDING_IMPORT, Path(source).read_bytes())
    return len(payloads)


def apply_pending_settings(*, data_root: Path | None = None) -> int:
    """Apply a queued import at startup; retain it if the write fails."""
    root = _data_root(data_root)
    pending = root / PENDING_IMPORT
    if not pending.is_file():
        return 0
    count = import_settings(pending, data_root=root)
    pending.unlink()
    return count


def _replace_bytes(target: Path, payload: bytes) -> None:
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".setting-", delete=False) as file:
        temporary = Path(file.name)
        file.write(payload)
    try:
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)

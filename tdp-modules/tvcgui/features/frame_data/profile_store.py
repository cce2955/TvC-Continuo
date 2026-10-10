"""Per-character frame-data profile storage.

The original cache stored every fighter in one JSON document. That made a
single file exceed GitHub's 100 MiB limit and forced every profile save to
rewrite the whole roster. This module stores one complete profile per fighter
while retaining read compatibility with the legacy monolithic document.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional

from tvcgui.core.paths import data_path, user_data_path

PROFILE_DIRECTORY_NAME = "frame_data_profiles"
LEGACY_PROFILE_FILENAME = "frame_data_profiles.json"
PROFILE_FILE_SUFFIX = ".json"

_IO_LOCK = threading.RLock()
_LEGACY_CACHE: dict[str, tuple[int, int, Dict[str, Any]]] = {}
_MIGRATION_CHECKED: set[tuple[str, str]] = set()
_SAFE_KEY_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


def _safe_key(key: Any) -> str:
    value = _SAFE_KEY_RE.sub("_", str(key or "unknown")).strip("._")
    return value or "unknown"


def default_bundled_directory() -> str:
    override = str(os.environ.get("TVC_FD_PROFILE_BUNDLED_DIR", "") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return data_path("frame_data", PROFILE_DIRECTORY_NAME)


def default_writable_directory() -> str:
    override = str(os.environ.get("TVC_FD_PROFILE_CACHE_DIR", "") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return user_data_path("frame_data", PROFILE_DIRECTORY_NAME)


def default_bundled_legacy_file() -> str:
    override = str(os.environ.get("TVC_FD_PROFILE_BUNDLED_FILE", "") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return data_path("frame_data", LEGACY_PROFILE_FILENAME)


def default_writable_legacy_file() -> str:
    override = str(os.environ.get("TVC_FD_PROFILE_CACHE_FILE", "") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return user_data_path("frame_data", LEGACY_PROFILE_FILENAME)


def profile_file_path(directory: str | os.PathLike[str], key: Any) -> str:
    return os.path.join(os.fspath(directory), _safe_key(key) + PROFILE_FILE_SUFFIX)


def _read_json_file(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _read_legacy_document(path: str) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    try:
        stat = os.stat(path)
        stamp = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return None

    cached = _LEGACY_CACHE.get(os.path.abspath(path))
    if cached and cached[:2] == stamp:
        return cached[2]

    raw = _read_json_file(path)
    if not isinstance(raw, dict) or not isinstance(raw.get("profiles"), dict):
        return None
    _LEGACY_CACHE[os.path.abspath(path)] = (stamp[0], stamp[1], raw)
    return raw


def _valid_profile(profile: Any, expected_version: Optional[int]) -> bool:
    if not isinstance(profile, dict):
        return False
    if expected_version is None:
        return True
    try:
        return int(profile.get("version") or 0) == int(expected_version)
    except Exception:
        return False


def _read_shard(path: str, expected_version: Optional[int]) -> Optional[Dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return None
    raw = _read_json_file(path)
    if isinstance(raw, dict) and isinstance(raw.get("profile"), dict):
        raw = raw["profile"]
    if not _valid_profile(raw, expected_version):
        return None
    return raw


def _candidate_paths(
    key: str,
    *,
    bundled_directory: Optional[str],
    writable_directory: Optional[str],
    bundled_legacy_file: Optional[str],
    writable_legacy_file: Optional[str],
) -> list[tuple[str, str]]:
    """Return sources in precedence order, lowest to highest."""
    candidates = [
        ("legacy", bundled_legacy_file or ""),
        ("shard", profile_file_path(bundled_directory, key) if bundled_directory else ""),
        ("legacy", writable_legacy_file or ""),
        ("shard", profile_file_path(writable_directory, key) if writable_directory else ""),
    ]
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, path in candidates:
        if not path:
            continue
        normalized = os.path.abspath(path)
        identity = (kind, normalized)
        if identity in seen:
            continue
        seen.add(identity)
        out.append((kind, normalized))
    return out




def _acquire_file_lock(lock_path: str, timeout: float = 2.0, stale_after: float = 30.0) -> Optional[int]:
    deadline = time.time() + max(0.05, float(timeout))
    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} time={time.time():.6f}\n".encode("ascii", "replace"))
            return fd
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_path) > stale_after:
                    os.unlink(lock_path)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                return None
            time.sleep(0.025)
        except OSError:
            return None


def _release_file_lock(fd: Optional[int], lock_path: str) -> None:
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        os.unlink(lock_path)
    except OSError:
        pass


def migrate_writable_legacy_if_needed(
    *,
    writable_legacy_file: Optional[str] = None,
    writable_directory: Optional[str] = None,
    expected_version: Optional[int] = None,
    scanner_build: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Migrate an old writable roster file once, preserving newer shards."""
    source_value = default_writable_legacy_file() if writable_legacy_file is None else writable_legacy_file
    directory_value = default_writable_directory() if writable_directory is None else writable_directory
    if not source_value or not directory_value:
        return None
    source = os.path.abspath(source_value)
    destination = os.path.abspath(directory_value)
    identity = (source, destination)
    with _IO_LOCK:
        if identity in _MIGRATION_CHECKED:
            return None
        _MIGRATION_CHECKED.add(identity)
        if not os.path.isfile(source):
            return None

        migration_lock = source + ".migration.lock"
        migration_fd = _acquire_file_lock(migration_lock)
        if migration_fd is None:
            return None
        try:
            if not os.path.isfile(source):
                return None
            report = split_legacy_profile_file(
                source,
                destination,
                expected_version=expected_version,
                scanner_build=scanner_build,
                overwrite=False,
            )
            if report["failed"]:
                return report

            archive = source + ".migrated.bak"
            try:
                if os.path.exists(archive):
                    archive = source + f".{int(time.time())}.migrated.bak"
                os.replace(source, archive)
                report["archive"] = archive
                _LEGACY_CACHE.pop(source, None)
            except OSError:
                report["archive"] = None
            return report
        finally:
            _release_file_lock(migration_fd, migration_lock)


def load_frame_data_profile(
    key: Any,
    *,
    expected_version: Optional[int] = None,
    copy_result: bool = True,
    bundled_directory: Optional[str] = None,
    writable_directory: Optional[str] = None,
    bundled_legacy_file: Optional[str] = None,
    writable_legacy_file: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Load one profile, preferring writable data over bundled seed data."""
    safe_key = _safe_key(key)
    if bundled_directory is None:
        bundled_directory = default_bundled_directory()
    if writable_directory is None:
        writable_directory = default_writable_directory()
    if bundled_legacy_file is None:
        bundled_legacy_file = default_bundled_legacy_file()
    if writable_legacy_file is None:
        writable_legacy_file = default_writable_legacy_file()

    if writable_legacy_file:
        migrate_writable_legacy_if_needed(
            writable_legacy_file=writable_legacy_file,
            writable_directory=writable_directory,
            expected_version=expected_version,
        )

    selected: Optional[Dict[str, Any]] = None
    with _IO_LOCK:
        for kind, path in _candidate_paths(
            safe_key,
            bundled_directory=bundled_directory,
            writable_directory=writable_directory,
            bundled_legacy_file=bundled_legacy_file,
            writable_legacy_file=writable_legacy_file,
        ):
            if kind == "shard":
                candidate = _read_shard(path, expected_version)
            else:
                doc = _read_legacy_document(path)
                candidate = None
                if isinstance(doc, dict):
                    profile = (doc.get("profiles") or {}).get(safe_key)
                    if _valid_profile(profile, expected_version):
                        candidate = profile
            if isinstance(candidate, dict):
                selected = candidate
    if not isinstance(selected, dict):
        return None
    # Most callers historically received an isolated deep copy. Keep that
    # behavior by default, but let the Frame Data profile cache retain the
    # freshly-loaded JSON object directly. Its rebase path creates fresh
    # dict/list structures, so another whole-profile deepcopy just adds
    # startup cost without protecting anything.
    return copy.deepcopy(selected) if copy_result else selected


def _atomic_write_json(path: str, payload: Dict[str, Any]) -> bool:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(temporary, path)
        return True
    except Exception:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except OSError:
            pass
        return False


def save_frame_data_profile(
    key: Any,
    profile: Dict[str, Any],
    *,
    writable_directory: Optional[str] = None,
    scanner_build: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> bool:
    """Atomically save one complete fighter profile."""
    if not isinstance(profile, dict):
        return False
    safe_key = _safe_key(key)
    if writable_directory is None:
        writable_directory = default_writable_directory()
    target = profile_file_path(writable_directory, safe_key)

    with _IO_LOCK:
        lock_path = target + ".lock"
        lock_fd = _acquire_file_lock(lock_path)
        if lock_fd is None:
            return False
        try:
            existing = _read_shard(target, expected_version=None) or {}
            output = copy.deepcopy(existing)
            output.update(copy.deepcopy(profile))
            output["key"] = safe_key
            if expected_version is not None:
                output["version"] = int(expected_version)
            if scanner_build:
                output["scanner_build"] = str(scanner_build)
            output["updated_at"] = str(
                output.get("updated_at")
                or time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
            )
            return _atomic_write_json(target, output)
        finally:
            _release_file_lock(lock_fd, lock_path)


def _iter_shard_profiles(
    directory: Optional[str],
    expected_version: Optional[int],
) -> Iterator[tuple[str, Dict[str, Any]]]:
    if not directory or not os.path.isdir(directory):
        return
    for path in sorted(Path(directory).glob(f"*{PROFILE_FILE_SUFFIX}")):
        if path.name.startswith("_"):
            continue
        profile = _read_shard(str(path), expected_version)
        if not isinstance(profile, dict):
            continue
        key = _safe_key(profile.get("key") or path.stem)
        yield key, profile


def iter_frame_data_profiles(
    *,
    expected_version: Optional[int] = None,
    bundled_directory: Optional[str] = None,
    writable_directory: Optional[str] = None,
    bundled_legacy_file: Optional[str] = None,
    writable_legacy_file: Optional[str] = None,
) -> Iterable[Dict[str, Any]]:
    """Return all profiles with the same precedence rules as single loads."""
    if bundled_directory is None:
        bundled_directory = default_bundled_directory()
    if writable_directory is None:
        writable_directory = default_writable_directory()
    if bundled_legacy_file is None:
        bundled_legacy_file = default_bundled_legacy_file()
    if writable_legacy_file is None:
        writable_legacy_file = default_writable_legacy_file()

    if writable_legacy_file:
        migrate_writable_legacy_if_needed(
            writable_legacy_file=writable_legacy_file,
            writable_directory=writable_directory,
            expected_version=expected_version,
        )

    merged: dict[str, Dict[str, Any]] = {}
    with _IO_LOCK:
        for legacy_path in (bundled_legacy_file,):
            doc = _read_legacy_document(legacy_path or "")
            if isinstance(doc, dict):
                for key, profile in (doc.get("profiles") or {}).items():
                    if _valid_profile(profile, expected_version):
                        merged[_safe_key(key)] = profile
        for key, profile in _iter_shard_profiles(bundled_directory, expected_version):
            merged[key] = profile
        for legacy_path in (writable_legacy_file,):
            if legacy_path and bundled_legacy_file and os.path.abspath(legacy_path) == os.path.abspath(bundled_legacy_file):
                continue
            doc = _read_legacy_document(legacy_path or "")
            if isinstance(doc, dict):
                for key, profile in (doc.get("profiles") or {}).items():
                    if _valid_profile(profile, expected_version):
                        merged[_safe_key(key)] = profile
        writable_abs = os.path.abspath(writable_directory) if writable_directory else ""
        bundled_abs = os.path.abspath(bundled_directory) if bundled_directory else ""
        if writable_abs != bundled_abs:
            for key, profile in _iter_shard_profiles(writable_directory, expected_version):
                merged[key] = profile
    return [copy.deepcopy(profile) for _, profile in sorted(merged.items())]


def split_legacy_profile_file(
    source_file: str,
    output_directory: str,
    *,
    expected_version: Optional[int] = None,
    scanner_build: Optional[str] = None,
    overwrite: bool = True,
) -> dict[str, Any]:
    """Split a legacy roster document into one JSON file per character."""
    source = os.path.abspath(os.path.expanduser(source_file))
    destination = os.path.abspath(os.path.expanduser(output_directory))
    doc = _read_legacy_document(source)
    if not isinstance(doc, dict):
        raise ValueError(f"Not a legacy frame-data profile document: {source}")

    written: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    profiles = doc.get("profiles") or {}
    for raw_key, raw_profile in sorted(profiles.items()):
        key = _safe_key(raw_key)
        if not _valid_profile(raw_profile, expected_version):
            skipped.append(key)
            continue
        target = profile_file_path(destination, key)
        if os.path.exists(target) and not overwrite:
            existing = _read_shard(target, expected_version)
            if isinstance(existing, dict):
                skipped.append(key)
                continue
        profile = copy.deepcopy(raw_profile)
        profile["key"] = key
        if expected_version is not None:
            profile["version"] = int(expected_version)
        if scanner_build:
            profile["scanner_build"] = str(scanner_build)
        if _atomic_write_json(target, profile):
            written.append(target)
        else:
            failed.append(target)

    return {
        "source": source,
        "output_directory": destination,
        "profile_count": len(profiles),
        "written": written,
        "skipped": skipped,
        "failed": failed,
    }


__all__ = [
    "PROFILE_DIRECTORY_NAME",
    "LEGACY_PROFILE_FILENAME",
    "default_bundled_directory",
    "default_writable_directory",
    "default_bundled_legacy_file",
    "default_writable_legacy_file",
    "profile_file_path",
    "load_frame_data_profile",
    "save_frame_data_profile",
    "iter_frame_data_profiles",
    "split_legacy_profile_file",
    "migrate_writable_legacy_if_needed",
]

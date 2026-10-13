"""Local PCM player with reversible TvC AX music routing for stock Dolphin.

The Wii AX mixer gate is host-local presentation behavior, not a proven netplay-
safe modification. We never alter AX linked-list pointers or the menu BGM float.
"""
from __future__ import annotations

import atexit
import json
import math
import os
from pathlib import Path
import random
import struct
import threading
import time
from typing import Any

from tvcgui.core.paths import user_data_path
from tvcgui.core.constants import SLOTS, OFF_MAX_HP, OFF_CHAR_ID, CHAR_NAMES
from tvcgui.features.audio.ax_bgm_gate import AxBgmGate
from tvcgui.features.audio.music_catalog import (
    RESOURCES, BY_KEY, AUTO_SCENES, STAGE_ID_RESOURCE, resource_label,
)

STAGE_RESULT = 0x803EB8F4  # candidate game loader result, not stage carousel cursor
STAGE_MENU_PTR = 0x809BCE28
STAGE_GLOBAL_CURSOR = 0x809BCEE0
NATIVE_BATTLE_PTR = 0x803EFB4C  # Kuro's TvcMusicTools.cpp / TvcCharacterSelect.cpp
VALID_EXT = {".ogg", ".mp3", ".wav"}
# Ultimate All-Stars stage selection IDs. These are game IDs, not file names.
# Named folders are the public library interface; legacy numeric directories
# remain readable so existing users do not lose their playlists.
# Source: historical STKE08 Stage Select Gecko code list (Skiller).
STAGE_NAMES = {
    1: 'Wasteland',
    2: "Yatterman's Workshop",
    3: 'Washinkyo, Amehon',
    4: 'Galactor Base',
    5: 'Burning Wasteland',
    6: 'Les Vagas',
    7: 'Daigo Temple (Cherry Blossoms)',
    8: 'Gesellschaft (Clear Skies)',
    9: 'Aensland Castle',
    10: 'Uncharted Region of China',
    11: 'Daigo Temple (Moonlight)',
    12: 'Gesellschaft (Storm)',
    13: 'Orbital Ring Systems Cargo Bay',
    14: 'Willamette Parkview Mall',
    15: 'Training Stage',
}


def stage_label(stage_id: int | None) -> str:
    if stage_id is None:
        return 'Unknown stage'
    return STAGE_NAMES.get(int(stage_id), f'Unmapped stage {int(stage_id):02d}')


def stage_id_from_label(label: str) -> int | None:
    if label in ('Auto', 'auto', ''):
        return None
    for key, name in STAGE_NAMES.items():
        if name == label:
            return key
    return None

POINT_FLAG_OFFSET = 0x44A0
ACTION_A_OFFSET = 0x1E8  # match dump: point neutral=1; reserve=430
POINT_IDLE_ACTION = 1
SAMPLE_INTERVAL = 0.30
AUDIO_INTERVAL = 0.06  # smooth host-side fades without blocking the UI
FADE_OUT_SECONDS = 0.30
BGM_VERIFY_INTERVAL = 0.40  # throttle DME reads/writes on the GUI thread
STABLE_SCENE_SAMPLES = 2
CHRSEL_ROOTS = (0x809BCE38, 0x80796364)
CHRSEL_SIGNATURE = b"fpack/menu/001/0000.fpk\x00"


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _valid_float(raw: bytes | None) -> float | None:
    if not raw or len(raw) != 4:
        return None
    value = struct.unpack(">f", raw)[0]
    return value if math.isfinite(value) and 0 <= value <= 1.01 else None


def _read_environment(io: Any) -> dict:
    """Read only validated stage/slot hints; menu identity still partly unknown."""
    def u32(addr):
        try:
            n = io.rd32(addr)
            return int(n) if n is not None else None
        except Exception:
            return None

    # The existing Continuo app owns MEM2 discovery. Do not force extra
    # expensive full-region latch scans just because music is polling menus.
    mem2_ready = True
    try:
        info_fn = getattr(io, 'mem2_latch_info', None)
        if info_fn is not None:
            mem2_ready = bool(info_fn().get('latched'))
    except Exception:
        mem2_ready = False

    # Kuro's native music boundary uses *(0x803EFB4C) and object+0x0c,
    # not the presence of stale fighter allocations. Phase 4 = live battle.
    battle_ptr = u32(NATIVE_BATTLE_PTR)
    battle_phase = None
    if battle_ptr is not None and (0x80000000 <= battle_ptr < 0x81800000 or
                                   0x90000000 <= battle_ptr < 0x94000000):
        candidate = u32(battle_ptr + 12)
        if candidate is not None and 0 <= candidate <= 12:
            battle_phase = candidate

    root = u32(STAGE_MENU_PTR)
    stage_menu = False
    stage_cursor_live = False
    if mem2_ready and root is not None and 0x90000000 <= root < 0x94000000:
        stage_menu = u32(root + 4) == 15 and u32(root + 0x4c) == root
        if stage_menu:
            # A recycled stage object can pass the above self-pointer check.
            # Require its live carousel cursor to agree with the global cursor
            # before preferring it over a simultaneously cached select asset.
            local_cursor = u32(root + 0x6c)
            global_cursor = u32(STAGE_GLOBAL_CURSOR)
            stage_cursor_live = (local_cursor is not None and
                                 global_cursor is not None and
                                 local_cursor == global_cursor and
                                 0 <= local_cursor <= 16)

    char_select = False
    char_root = None
    if mem2_ready:
        for ptr_addr in CHRSEL_ROOTS:
            seq = u32(ptr_addr)
            if seq is None or not (0x90000000 <= seq < 0x94000000):
                continue
            try:
                if io.rbytes(seq + 0x40, len(CHRSEL_SIGNATURE)) == CHRSEL_SIGNATURE:
                    char_select = True
                    char_root = seq
                    break
            except Exception:
                pass
    chars = {}
    actions = {}
    point_candidates = {"P1": [], "P2": []}
    for label, pointer, _team in SLOTS:
        base = u32(pointer)
        if base is None or not (0x80000000 <= base < 0x81800000 or 0x90000000 <= base < 0x94000000):
            continue
        if not mem2_ready and 0x90000000 <= base < 0x94000000:
            continue
        max_hp = u32(base + OFF_MAX_HP)
        cid = u32(base + OFF_CHAR_ID)
        if max_hp is not None and 0 < max_hp <= 80000 and cid in CHAR_NAMES:
            chars[label] = cid
            actions[label] = u32(base + ACTION_A_OFFSET)
            point_flag = u32(base + POINT_FLAG_OFFSET)
            if point_flag:  # Kuro treats every nonzero point flag as active
                point_candidates[label[:2]].append(label)
    # Presence of cached fighters isn't proof that battle is active. Use the
    # verified native phase when available; retain fighter fallback only if the
    # game's battle object isn't readable (older Continuo versions, early boot).
    fighters_loaded = any(k.startswith('P1') for k in chars) and any(k.startswith('P2') for k in chars)
    stage = u32(STAGE_RESULT)
    if stage is None or not 0 <= stage <= 255:
        stage = None
    # Phase 4 is Kuro's only confirmed live-battle phase. The prematch
    # entrance animation can briefly mark multiple point fighters active;
    # phases 5-12 aren't proof that the FIGHT opening has happened.
    if battle_phase == 4:
        scene = 'battle'
    elif stage_menu and (stage_cursor_live or not char_select):
        scene = 'stage_select'
    elif char_select:
        scene = 'character_select'
    elif battle_phase is not None and fighters_loaded:
        scene = 'loading'
    elif battle_phase is None and fighters_loaded:
        # Legacy fallback when the native battle object is unavailable;
        # still require a unique point fighter before selecting theme.
        scene = 'battle'
    elif stage_menu:
        scene = 'stage_select'
    else:
        scene = 'idle'
    points = {team: options[0] for team, options in point_candidates.items() if len(options) == 1}
    # A live phase is not sufficient to arm tags: the prematch entrance can
    # leave both point flags active. Observe *both* unique point fighters idle
    # before we accept the point/anchor baseline. Do not write guest memory.
    both_points_idle = (battle_phase == 4 and all(
        team in points and actions.get(points[team]) == POINT_IDLE_ACTION
        for team in ('P1', 'P2')))
    evidence = {
        'both_points_idle': both_points_idle,
        'native_phase': battle_phase,
        'stage_object': bool(stage_menu),
        'stage_cursor': bool(stage_cursor_live),
        'select_asset': bool(char_select),
        'ambiguous': bool(stage_menu and char_select),
    }
    return {'scene': scene, 'stage': stage, 'fighters': chars, 'points': points,
            'actions': actions, 'evidence': evidence}


class MusicController:
    def __init__(self, *, io=None, mixer=None, root=None, clock=None, start_sampler=True):
        self._io = io
        self._mixer = mixer
        # Persistent config and music live outside the PyInstaller _MEIPASS tree.
        # If an EXE is under a protected directory, use LocalAppData instead.
        self._explicit_root = root is not None
        self._default_root = Path(user_data_path("continuo", "music"))
        if not self._explicit_root:
            try:
                base = self._default_root.parent
                base.mkdir(parents=True, exist_ok=True)
                test = base / '.music-write-test'
                test.write_bytes(b'ok')
                test.unlink()
            except OSError:
                appdata = os.environ.get('LOCALAPPDATA') or str(Path.home() / '.local' / 'share')
                self._default_root = Path(appdata) / 'TvCContinuo' / 'music'
        self._config_root = self._default_root.parent
        self._location_file = None if self._explicit_root else self._config_root / 'music_location.json'
        requested = None
        if self._location_file and self._location_file.is_file():
            try:
                requested = json.loads(self._location_file.read_text(encoding='utf8')).get('path')
            except (OSError, ValueError, AttributeError):
                pass
        self.root = Path(root) if self._explicit_root else Path(requested or self._default_root)
        self.clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None
        self._allow_sampler = start_sampler
        self.enabled = False
        self._desired_enabled = False
        self._startup_last_attempt = float('-inf')
        self._native_fallback = False
        self._ax_gate = AxBgmGate()
        self._requested_mute = False
        self.original_volume: bytes | None = None
        self._owns_mute = False
        self._native_zero_confirmed = False
        self._mute_status = "Off"
        self._last_mute_check = float('-inf')
        self._mute_retries = 0
        self._pending_restore: bytes | None = None
        self._transition_ready = False
        self._entered_at: tuple | None = None
        self._seen_raw: tuple | None = None
        self._stable_count = 0
        self._observed: dict = {"scene": "idle", "stage": None, "fighters": {}, "points": {}, "evidence": {}}
        self._has_initial_observation = False
        self._last_audio = 0.0
        self._battle_music_scan_at = float('-inf')
        self._battle_music_scan_result = False
        self._current_owner: str | None = None
        self._current_file: str | None = None
        self._message = "Off. Put tracks into music folders before enabling."
        self._mode = "auto"  # auto, menu, character_select, stage_select, battle
        self._output = "custom"  # custom or no_bgm; OFF restores native BGM
        self._slot = "P1-point"  # follow verified active fighter on player 1
        self._source = "fighter"  # fighter or stage, explicit music source
        self._stage_order = "sequential"
        self._fighter_order = "sequential"
        self._fighter_switch = "tag"  # 'match' locks first point until battle ends
        self._locked_fighter: int | None = None
        self._in_battle = False
        self._last_point: dict[str, int] = {}
        # Kuro-style event ownership: follow side chooses opening music, then
        # each team's *verified* point transition can take over the soundtrack.
        self._opening_character: int | None = None
        self._tag_armed = False
        self._idle_ready_since: float | None = None
        self._tag_team_slots: dict[str, str] = {}
        # Remember the opening roster before using any unreliable prematch
        # point flags. The first *verified idle* point may already reflect a
        # real tag if it happened while the sampler was waiting to arm.
        self._opening_team_slots: dict[str, str] = {}
        self._tag_selected_character: int | None = None
        self._tag_selected_team: str | None = None
        self._tag_event_serial = 0
        self._played_event_serial = 0
        # Nonblocking host PCM fade: pygame.mixer.music.fadeout() can block.
        self._fade_started: float | None = None
        self._fade_level = 0.0
        self._pending_scene_fade = False
        self._stage_override: int | None = None
        self._char_override: int | None = None
        self._resource_override: str | None = None  # Exact known SSD, manual fallback
        self._shuffle = False
        self._volume = 0.65
        self._last_by_owner: dict[str, str] = {}
        self._settings_file = (self.root / 'settings.json' if self._explicit_root
                               else self._config_root / 'music_settings.json')
        self._history_file = (self.root / 'history.json' if self._explicit_root
                              else self._config_root / 'music_history.json')
        self._load()

    def _backend(self):
        if self._io is None:
            from tvcgui.platform import dolphin
            self._io = dolphin
        return self._io

    def _audio(self):
        if self._mixer is None:
            import pygame
            self._mixer = pygame.mixer
        return self._mixer

    def _save_json(self, dest: Path, value: dict) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, dest)

    def _load(self):
        try:
            legacy = self.root / 'settings.json'
            config = self._settings_file if self._settings_file.is_file() else legacy
            d = json.loads(config.read_text(encoding="utf-8"))
            self._mode = d.get("scene_mode", "auto")
            self._output = d.get("output", "custom") if d.get("output", "custom") in ("custom", "no_bgm") else "custom"
            if self._mode not in ("auto", "menu", "character_select", "stage_select", "battle"):
                self._mode = "auto"
            self._slot = d.get("character_slot", "P1-point")
            if self._slot not in ("P1-point", "P2-point", "P1-C1", "P1-C2", "P2-C1", "P2-C2", "stage_only"):
                self._slot = "P1-point"
            self._source = d.get('music_source', 'stage' if self._slot == 'stage_only' else 'fighter')
            if self._source not in ('stage', 'fighter'):
                self._source = 'fighter'
            old_order = 'random' if d.get('shuffle', False) else 'sequential'
            self._stage_order = d.get('stage_order', old_order)
            self._fighter_order = d.get('fighter_order', old_order)
            if self._stage_order not in ('random', 'sequential'):
                self._stage_order = 'sequential'
            if self._fighter_order not in ('random', 'sequential'):
                self._fighter_order = 'sequential'
            self._fighter_switch = d.get('fighter_switch', 'tag')
            if self._fighter_switch not in ('match', 'tag'):
                self._fighter_switch = 'tag'
            self._shuffle = bool(d.get("shuffle", False))
            self._volume = min(1., max(0., float(d.get("volume", 0.65))))
            self._stage_override = self._id_or_none(d.get("stage_override"))
            self._char_override = self._id_or_none(d.get("character_override"))
            selected = d.get("resource_override")
            self._resource_override = selected if selected in BY_KEY else None
            self._desired_enabled = bool(d.get('enabled_on_launch', False))
        except (OSError, ValueError, TypeError):
            pass
        try:
            legacy = self.root / 'history.json'
            hist_path = self._history_file if self._history_file.is_file() else legacy
            hist = json.loads(hist_path.read_text(encoding="utf-8"))
            self._last_by_owner = {str(k): str(v) for k, v in hist.get("last_by_owner", {}).items()}
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    @staticmethod
    def _id_or_none(v):
        if v in ('', None, 'auto'):
            return None
        try:
            text = str(v).strip()
            n = int(text, 16 if text.lower().startswith('0x') else 10)
            return n if 0 <= n <= 255 else None
        except (TypeError, ValueError):
            return None

    def configure(self, *, mode=None, output=None, slot=None, shuffle=None, volume=None,
                  stage=None, character=None, source=None, stage_order=None,
                  fighter_order=None, fighter_switch=None, resource=None):
        with self._lock:
            former = (self._mode, self._output, self._slot, self._stage_override, self._char_override,
                      self._source, self._fighter_switch, self._resource_override)
            if output in ("custom", "no_bgm"):
                self._output = output
            if mode is not None and mode in ("auto", "menu", "character_select", "stage_select", "battle"):
                self._mode = mode
            if slot is not None and slot in ("P1-point", "P2-point", "P1-C1", "P1-C2", "P2-C1", "P2-C2", "stage_only"):
                self._slot = slot
            if shuffle is not None:
                self._shuffle = bool(shuffle)
                self._stage_order = self._fighter_order = 'random' if shuffle else 'sequential'
            if source in ('stage', 'fighter'):
                self._source = source
            if stage_order in ('sequential', 'random'):
                self._stage_order = stage_order
            if fighter_order in ('sequential', 'random'):
                self._fighter_order = fighter_order
            if fighter_switch in ('match', 'tag'):
                if fighter_switch != self._fighter_switch:
                    self._locked_fighter = None
                self._fighter_switch = fighter_switch
            if volume is not None:
                self._volume = max(0., min(1., float(volume)))
            if stage is not None:
                self._stage_override = self._id_or_none(stage)
            if character is not None:
                self._char_override = self._id_or_none(character)
            if resource is not None:
                self._resource_override = resource if resource in BY_KEY else None
            self._save_settings()
            # Only restart a track if its owner or playlist binding changes.
            if former != (self._mode, self._output, self._slot, self._stage_override, self._char_override,
                          self._source, self._fighter_switch, self._resource_override):
                self._current_owner = None

    def _save_settings(self) -> None:
        self._save_json(self._settings_file, {
            'scene_mode': self._mode, 'output': self._output,
            'character_slot': self._slot, 'music_source': self._source,
            'stage_order': self._stage_order, 'fighter_order': self._fighter_order,
            'fighter_switch': self._fighter_switch, 'volume': self._volume,
            'stage_override': self._stage_override,
            'character_override': self._char_override,
            'resource_override': self._resource_override,
            'enabled_on_launch': self._desired_enabled,
        })

    def set_output_mode(self, selection: str) -> bool:
        """User selection (including the HUD control), persisted immediately."""
        if selection not in ('native', 'custom', 'no_bgm'):
            raise ValueError(selection)
        with self._lock:
            self._desired_enabled = selection != 'native'
            if selection != 'native':
                self._output = selection
            self._save_settings()
            if selection == 'native':
                self.stop()
                return True
            if self.enabled:
                if selection == 'no_bgm':
                    self._stop_audio()
                    self._current_owner = None
                    if self._native_fallback:
                        self._choose_native_fallback(False)
                    self._requested_mute = True
                    self._maintain_bgm(force=True)
                return True
            return self.start()

    def ensure_library(self) -> Path:
        """Create understandable folder names, non-destructively migrate old IDs.

        All existing music files are preserved. A legacy numeric folder is
        renamed only when the human-named target does not exist; otherwise the
        old folder remains readable as a backwards-compatible fallback.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        stages = self._dir_for('stages')
        stages.mkdir(parents=True, exist_ok=True)
        for stage_id, readable in STAGE_NAMES.items():
            target = stages / readable
            old = stages / f'{stage_id:02d}'
            if old.is_dir() and not target.exists():
                try:
                    old.rename(target)
                except OSError:
                    pass  # target created below; old path remains readable
            target.mkdir(parents=True, exist_ok=True)
        # ID 00 is not in the verified normal stage list. Keep any existing
        # tracks in an explicitly unverified folder rather than mislabeling it.
        old_zero = stages / '00'
        unknown = stages / 'Unmapped Stage (legacy 00)'
        if old_zero.is_dir() and not unknown.exists():
            try:
                if any(old_zero.iterdir()):
                    old_zero.rename(unknown)
                else:
                    old_zero.rmdir()
            except OSError:
                pass
        # Complete catalog of identified stock SSD streams. These folders are
        # human-readable; SSD filenames and DSP IDs remain internal metadata.
        # Never move or delete previous songs. Existing short scene folders
        # remain valid aliases in _context() below.
        for item in RESOURCES:
            self._dir_for(item.folder).mkdir(parents=True, exist_ok=True)
        for name in ['menu', 'character_select', 'stage_select', 'stages/default']:
            self.root.joinpath(*name.split('/')).mkdir(parents=True, exist_ok=True)
        for cid, name in CHAR_NAMES.items():
            self._dir_for(f'characters/{cid:02d}_{_slug(name)}').mkdir(parents=True, exist_ok=True)
        return self.root

    def set_music_root(self, selected: str | Path) -> Path:
        """Switch music library. The original folder and songs are left intact."""
        selected = Path(selected).expanduser().resolve()
        if self.enabled:
            raise RuntimeError('Disable custom music before changing the library folder.')
        selected.mkdir(parents=True, exist_ok=True)
        test = selected / '.continuo-write-test'
        try:
            test.write_bytes(b'ok')
        finally:
            test.unlink(missing_ok=True)
        with self._lock:
            self.root = selected
            if self._explicit_root:
                self._settings_file = selected / 'settings.json'
                self._history_file = selected / 'history.json'
            if self._location_file:
                self._save_json(self._location_file, {'path': str(selected)})
            self.ensure_library()
            self._current_owner = None
            self._current_file = None
            self._battle_music_scan_at = float('-inf')
            self._message = f'Music folder: {self.root}'
        return selected

    @property
    def default_music_root(self) -> Path:
        return self._default_root

    def _dir_for(self, owner: str) -> Path:
        return self.root.joinpath(*owner.split('/'))

    def _candidate_dirs(self, kind: str, value: int | None) -> list[Path]:
        if kind == 'stages':
            if value is None:
                return [self._dir_for('stages/default')]
            # Human-readable name is authoritative. Legacy ID-based directories
            # are retained as a read-only lookup for existing installations.
            names = ([f'stages/{STAGE_NAMES[value]}'] if value in STAGE_NAMES else [])
            if value == 0:
                names.append('stages/Unmapped Stage (legacy 00)')
            names += [f'stages/{value:02d}', f'stages/{value}',
                      f'stages/stage_{value:02d}', 'stages/default']
            return [self._dir_for(name) for name in dict.fromkeys(names)]
        if kind == 'characters' and value in CHAR_NAMES:
            return [self._dir_for(f'characters/{value:02d}_{_slug(CHAR_NAMES[value])}'),
                    self._dir_for(f'characters/{value}'),
                    self._dir_for(f'characters/{_slug(CHAR_NAMES[value])}')]
        return []

    def _ssd_candidate_dirs(self, key: str, *legacy: str) -> list[Path]:
        """Look up human-named SSD folder first, then existing scene aliases.

        Song files are not copied, migrated, renamed or combined, and the
        first nonempty playlist wins for backward compatibility.
        """
        item = BY_KEY.get(key)
        names = ([item.folder] if item else []) + list(legacy)
        return [self._dir_for(path) for path in dict.fromkeys(names)]

    @staticmethod
    def _songs(paths: list[Path]) -> list[Path]:
        for folder in paths:
            if folder.is_dir():
                found = sorted([p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VALID_EXT],
                               key=lambda p: p.name.lower())
                if found:
                    return found
        return []

    def _opening_fighter(self, observed: dict) -> int | None:
        """One character chosen by Follow before FIGHT, never from intro point flags.

        C1 is the roster's entering fighter (or a selected explicit slot).
        The live point flags become authoritative only after the two-idle gate.
        """
        if self._char_override in CHAR_NAMES:
            return self._char_override
        fighters = observed.get('fighters', {})
        slot = self._slot
        if slot in ('P1-point', 'P2-point'):
            team = slot[:2]
            # If Continuo attaches after the intro, a verified live idle point
            # takes priority over C1. Intro point flags are deliberately ignored.
            if (observed.get('scene') == 'battle' and
                    observed.get('evidence', {}).get('both_points_idle')):
                active = observed.get('points', {}).get(team)
                cid = fighters.get(active)
                if cid in CHAR_NAMES:
                    return cid
            slot = team + '-C1'
        candidate = fighters.get(slot)
        return candidate if candidate in CHAR_NAMES else None

    def _point_fighter(self, observed: dict) -> int | None:
        """Resolve a point fighter without changing emulated game state."""
        if self._char_override is not None:
            return self._char_override
        fighters = observed.get('fighters', {})
        if self._slot in ('P1-point', 'P2-point'):
            if self._fighter_switch == 'tag' and self._in_battle:
                # Crucial: never guess C1 while both prematch point flags are
                # active. Wait for a confirmed opening point after FIGHT.
                return self._tag_selected_character
            team = self._slot[:2]
            slot = observed.get('points', {}).get(team)
            if slot in fighters:
                cid = fighters[slot]
                self._last_point[team] = cid
                return cid
            if self._in_battle:
                return None
            return self._last_point.get(team) or fighters.get(f'{team}-C1')
        return fighters.get(self._slot)

    def _update_tag_owner(self, observed: dict, *, entered_battle=False) -> None:
        """Mirror Kuro's both-team point-event behavior, without guest writes.

        The sampler has already required two identical observations. Missing or
        ambiguous point flags are not events; both simultaneous changes use P1.
        P1-point / P2-point pick the opening fighter only. Fixed C1/C2 options
        remain fixed for users who explicitly selected them.
        """
        if self._fighter_switch != 'tag' or self._slot not in ('P1-point', 'P2-point'):
            return
        fighters = observed.get('fighters', {})
        points = observed.get('points', {})
        valid = {team: slot for team, slot in points.items()
                 if team in ('P1', 'P2') and slot in fighters and fighters[slot] in CHAR_NAMES}
        if entered_battle:
            self._tag_team_slots.clear()
            # C1 enters as point. During the intro both point flags can be
            # asserted, so capture roster identity instead of flag state.
            self._opening_team_slots = {
                team: f'{team}-C1' for team in ('P1', 'P2')
                if f'{team}-C1' in fighters
            }
            self._tag_armed = False
            self._idle_ready_since = None
            # Keep the prematch Follow theme playing uninterrupted at FIGHT.
            self._tag_selected_character = self._opening_character
            self._tag_selected_team = self._slot[:2]
        if not self._tag_armed:
            # Require actual action-1 idle points on both teams, not just phase
            # 4 or the existence of a point flag. Two sampler intervals prevent
            # accepting an intermediate tag/entrance animation as the baseline.
            idle = bool(observed.get('evidence', {}).get('both_points_idle'))
            if not idle or set(valid) != {'P1', 'P2'}:
                self._idle_ready_since = None
                return
            now = self.clock()
            if self._idle_ready_since is None:
                self._idle_ready_since = now
                return
            if now - self._idle_ready_since < SAMPLE_INTERVAL:
                return
            self._tag_team_slots = dict(valid)
            self._tag_armed = True
            # Do NOT silently consume the first real tag as the baseline.
            # If Ken/C1 was the opening point but Ryu/C2 is the first stable
            # idle point, the tag happened before we finished arming. Treat
            # this as a genuine entrance now, after the live-idle gate.
            early_events = [team for team in ('P1', 'P2')
                            if team in self._opening_team_slots
                            and team in valid
                            and valid[team] != self._opening_team_slots[team]]
            if early_events:
                team = early_events[0]  # same P1 precedence as normal tags
                self._tag_selected_team = team
                self._tag_selected_character = fighters[valid[team]]
                self._tag_event_serial += 1
            return
        events = [team for team in ('P1', 'P2')
                  if team in valid and team in self._tag_team_slots
                  and valid[team] != self._tag_team_slots[team]]
        for team in ('P1', 'P2'):
            if team in valid:
                self._tag_team_slots[team] = valid[team]
        # Kuro gives P1 priority for simultaneous entrance events.
        if events:
            team = events[0]
            self._tag_selected_team = team
            self._tag_selected_character = fighters[valid[team]]
            self._tag_event_serial += 1

    def _begin_fade(self) -> None:
        """Start a nonblocking local fade; no mutation to emulated TvC state."""
        if self._current_file and self._fade_started is None:
            self._fade_started = self.clock()
            self._fade_level = self._volume

    def _service_fade(self, now: float) -> bool:
        """Advance fading volume. Return True while the old track is active."""
        if self._fade_started is None:
            return False
        fraction = min(1., max(0., (now - self._fade_started) / FADE_OUT_SECONDS))
        if fraction >= 1.:
            self._stop_audio()
            self._current_owner = None
            self._fade_started = None
            return False
        try:
            self._audio().music.set_volume(max(0., self._fade_level * (1. - fraction)))
        except Exception:
            pass
        return True

    def _expected_ssd(self, observed: dict) -> str | None:
        """Catalog expectation, not proof of a live DVD/AX file read."""
        if self._resource_override in BY_KEY:
            return self._resource_override
        scene = self._mode if self._mode != 'auto' else observed.get('scene', 'idle')
        if scene in ('battle', 'loading'):
            sid = self._stage_override if self._stage_override is not None else observed.get('stage')
            return STAGE_ID_RESOURCE.get(sid)
        return AUTO_SCENES.get('menu' if scene == 'idle' else scene)

    def _context(self, observed: dict) -> tuple[str, list[Path]]:
        # Explicit manual selection is useful for Wi-Fi, endings, Yami forms,
        # and other screens whose live state is not yet verified. Do not
        # silently claim to have detected their SSD resource in Wii RAM.
        if self._resource_override in BY_KEY:
            key = self._resource_override
            return f'ssd/{key}', self._songs(self._ssd_candidate_dirs(key))
        mode = self._mode if self._mode != 'auto' else observed.get('scene', 'idle')
        if mode == 'idle':
            mode = 'menu'
        if mode in ('loading', 'battle'):
            # The prematch entry begins the Follow fighter's music. No point
            # mutations are consumed until both live point fighters are idle.
            if self._opening_character is None:
                self._opening_character = self._opening_fighter(observed)
            if mode == 'loading' and self._source == 'fighter' and self._slot != 'stage_only':
                opener = self._opening_character
                if opener in CHAR_NAMES:
                    tracks = self._songs(self._candidate_dirs('characters', opener))
                    if tracks:
                        return f'characters/{opener}', tracks
                sid = self._stage_override if self._stage_override is not None else observed.get('stage')
                return f'stages/{sid if sid is not None else "default"}', self._songs(self._candidate_dirs('stages', sid))
            if mode == 'loading':
                sid = self._stage_override if self._stage_override is not None else observed.get('stage')
                return f'stages/{sid if sid is not None else "default"}', self._songs(self._candidate_dirs('stages', sid))
            sid = self._stage_override if self._stage_override is not None else observed.get('stage')
            stage_owner = f"stages/{sid if sid is not None else 'default'}"
            stage_tracks = self._songs(self._candidate_dirs('stages', sid))
            if self._source == 'stage' or self._slot == 'stage_only':
                return stage_owner, stage_tracks
            fighter = self._point_fighter(observed)
            if self._fighter_switch == 'match':
                if self._locked_fighter is None:
                    self._locked_fighter = (self._opening_character
                                            if self._opening_character in CHAR_NAMES else fighter)
                fighter = self._locked_fighter
            if fighter in CHAR_NAMES:
                tracks = self._songs(self._candidate_dirs('characters', fighter))
                if tracks:
                    return f'characters/{fighter}', tracks
            return stage_owner, stage_tracks
        key = mode if mode in ('menu', 'character_select', 'stage_select') else 'menu'
        resource = AUTO_SCENES.get(key)
        if resource:
            # Preserve the menu/character_select/stage_select playlists which
            # have been used since the first Continuo music integration.
            return key, self._songs(self._ssd_candidate_dirs(resource, key))
        return key, self._songs([self._dir_for(key)])

    def _match_has_custom_music(self, observed: dict) -> bool:
        """Only music for *this lineup or stage*, not every installed theme.

        A song for an unrelated fighter must not suppress native audio when
        the current match has no configured character or stage soundtrack.
        Pre-battle snapshots can lack fighter IDs; in that case never assume
        the entire library belongs to the forthcoming match.
        """
        if self._resource_override in BY_KEY:
            return bool(self._songs(self._ssd_candidate_dirs(self._resource_override)))
        sid = self._stage_override if self._stage_override is not None else observed.get('stage')
        if self._songs(self._candidate_dirs('stages', sid)):
            return True
        if self._source != 'fighter' or self._slot == 'stage_only':
            return False
        if self._char_override in CHAR_NAMES:
            return bool(self._songs(self._candidate_dirs('characters', self._char_override)))
        return any(bool(self._songs(self._candidate_dirs('characters', cid)))
                   for cid in set(observed.get('fighters', {}).values()) if cid in CHAR_NAMES)

    def _next_scene_has_music(self, observed: dict) -> bool:
        """Keep BGM pre-armed through the forward select route.

        If the only custom tracks are in stage_select, dropping the mute in
        menu/character_select guarantees a late restore because character-select
        quarantine prevents writes. Native BGM may remain silent in intervening
        screens without custom songs: stock Dolphin cannot atomically switch
        the guest's audio volume at already-started tracks.
        """
        if self._resource_override in BY_KEY:
            return bool(self._songs(self._ssd_candidate_dirs(self._resource_override)))
        if self._mode != 'auto':
            return False
        scene = observed.get('scene')
        if scene in ('menu', 'idle'):
            return (any(self._songs([self._dir_for(folder)])
                        for folder in ('character_select', 'stage_select')) or
                    self._match_has_custom_music(observed))
        if scene == 'character_select':
            return bool(self._songs([self._dir_for('stage_select')])) or self._match_has_custom_music(observed)
        if scene in ('stage_select', 'loading'):
            return self._match_has_custom_music(observed)
        return False

    def _any_battle_music(self) -> bool:
        """Cache directory discovery; do not traverse a music library per GUI frame."""
        now = self.clock()
        if now - self._battle_music_scan_at < 2.0:
            return self._battle_music_scan_result
        self._battle_music_scan_at = now
        self._battle_music_scan_result = False
        for area in ('stages', 'characters'):
            folder = self.root / area
            if folder.is_dir() and any(p.is_file() and p.suffix.lower() in VALID_EXT
                                       for p in folder.rglob('*')):
                self._battle_music_scan_result = True
                break
        return self._battle_music_scan_result

    def _pick(self, owner: str, tracks: list[Path]) -> Path:
        last = self._last_by_owner.get(owner)
        order = self._fighter_order if owner.startswith('characters/') else self._stage_order
        if order == 'random' and len(tracks) > 1:
            choices = [p for p in tracks if p.name != last]
            return random.choice(choices or tracks)
        previous = next((i for i, p in enumerate(tracks) if p.name == last), -1)
        return tracks[(previous + 1) % len(tracks)]

    def _stop_audio(self):
        self._fade_started = None
        self._pending_scene_fade = False
        try:
            self._audio().music.stop()
        except Exception:
            pass
        self._current_file = None

    def _choose_native_fallback(self, native: bool) -> None:
        """Select the *local* AX output route without changing game options."""
        if native == self._native_fallback:
            return
        self._native_fallback = native
        if native and self._current_file:
            self._begin_fade()
        self._mute_status = ('Native audio requested (AX route)' if native else
                             'Custom audio requested (AX route)')
        self._last_mute_check = float('-inf')

    def _write_protected(self) -> bool:
        """Never bypass Continuo's character-select write quarantine."""
        try:
            state = getattr(self._backend(), 'get_emulated_write_quarantine_state', None)
            return bool(state and state().get('active'))
        except Exception:
            return True

    def _maintain_bgm(self, *, force: bool = False) -> None:
        """Target only the two verified AX BGM voices, never the global float."""
        now = self.clock()
        if not force and now - self._last_mute_check < 0.15:
            return
        self._last_mute_check = now
        mute = bool(self.enabled and self._requested_mute and not self._native_fallback)
        # Do not perform any writes if BGM is not requested and we own none.
        if not mute and not self._ax_gate.needs_restore:
            self._native_zero_confirmed = False
            self._transition_ready = True
            self._mute_status = 'Native BGM unchanged (AX gate idle)'
            return
        result = self._ax_gate.apply(self._backend(), mute=mute,
                                     write_protected=self._write_protected())
        self._native_zero_confirmed = result.muted if mute else False
        self._transition_ready = result.muted if mute else result.ready
        self._mute_status = result.text
        if mute and not result.muted and self._current_file:
            # Don't layer Continuo over a native soundtrack until both
            # routes have been readback-verified as disconnected.
            self._stop_audio()
            self._current_owner = None

    def retry_mute(self) -> str:
        with self._lock:
            if not self.enabled:
                return 'Enable Custom or No BGM first.'
            self._maintain_bgm(force=True)
            return self._mute_status

    def _start_sampler(self):
        if not self._allow_sampler:
            return
        # New event per enable. A previous session's thread can still be
        # winding down after a rapid OFF->ON toggle.
        stop_event = threading.Event()
        self._stop = stop_event
        self._sampler = threading.Thread(target=self._sample_loop, args=(stop_event,),
                                         daemon=True, name="ContinuoMusicSampler")
        self._sampler.start()

    def _sample_loop(self, stop_event):
        while not stop_event.wait(SAMPLE_INTERVAL):
            try:
                reading = _read_environment(self._backend())
            except Exception:
                continue
            with self._lock:
                if not self.enabled:
                    continue
                raw = (reading['scene'], reading['stage'],
                       tuple(sorted(reading.get('fighters', {}).items())),
                       tuple(sorted(reading.get('points', {}).items())))
                if raw == self._seen_raw:
                    self._stable_count += 1
                else:
                    self._seen_raw, self._stable_count = raw, 1
                if self._stable_count >= STABLE_SCENE_SAMPLES:
                    before = self._observed
                    self._observed = reading
                    new_scene = (before['scene'], before.get('stage')) != (reading['scene'], reading.get('stage'))
                    if new_scene and self._current_file is not None:
                        # GUI tick fades the old track; do not call SDL_mixer
                        # from the background DME sampling thread.
                        self._pending_scene_fade = True
                    self._has_initial_observation = True

    def start(self) -> bool:
        with self._lock:
            if self.enabled:
                self._maintain_bgm(force=True)
                return True
            # No music files? Do not silence native BGM in Custom mode.
            installed = any(p.is_file() and p.suffix.lower() in VALID_EXT
                            for p in self.root.rglob('*')) if self.root.is_dir() else False
            if self._output == "custom" and not installed:
                self._message = "No OGG/MP3/WAV tracks found. Add tracks before enabling."
                return False
            # AX routing is validated at playback time. Do not touch the
            # game's global BGM setting, even when legacy installs left it 0.
            try:
                mix = self._audio()
                if not mix.get_init():
                    mix.init(frequency=44100, size=-16, channels=2, buffer=1024)
                mix.music.set_volume(self._volume)
            except Exception as ex:
                self._message = f"Audio init failed: {ex!s}"
                return False
            self.original_volume = None
            self._pending_restore = None
            self._owns_mute = False
            self.enabled = True
            self._native_fallback = False
            self._requested_mute = (self._output == 'no_bgm')
            self._native_zero_confirmed = False
            self._mute_retries = 0
            self._last_mute_check = float('-inf')
            self._entered_at = None
            self._transition_ready = False
            self._seen_raw = None
            self._stable_count = 0
            self._observed = {"scene": "idle", "stage": None, "fighters": {}, "points": {}, "evidence": {}}
            self._has_initial_observation = False
            self._locked_fighter = None
            self._in_battle = False
            self._last_point = {}
            self._tag_team_slots.clear()
            self._opening_team_slots.clear()
            self._opening_character = None
            self._tag_armed = False
            self._idle_ready_since = None
            self._tag_selected_character = None
            self._tag_selected_team = None
            self._tag_event_serial = 0
            self._played_event_serial = 0
            self._fade_started = None
            self._pending_scene_fade = False
            self._message = "Custom music armed; AX voice routing will follow scene playlists."
            self._start_sampler()
            return True

    def stop(self):
        with self._lock:
            if not self.enabled and not self._ax_gate.needs_restore:
                return
            self.enabled = False
            self._native_fallback = False
            self._requested_mute = False
            self._stop.set()
            self._stop_audio()
            restore = None
            self._pending_restore = None
            self.original_volume = None
            self._owns_mute = False
            self._native_zero_confirmed = False
            self._transition_ready = False
            self._entered_at = None
            self._current_owner = None
            self._locked_fighter = None
            self._in_battle = False
            self._last_point.clear()
            self._tag_team_slots.clear()
            self._opening_team_slots.clear()
            self._opening_character = None
            self._tag_armed = False
            self._idle_ready_since = None
            self._tag_selected_character = None
            self._tag_selected_team = None
            self._last_mute_check = float('-inf')
            self._maintain_bgm(force=True)
            self._message = 'Custom music off; ' + self._mute_status

    def skip(self):
        with self._lock:
            if not self.enabled or not self._native_zero_confirmed:
                self._message = "Enable custom music and verify the AX music gate first."
                return
            self._current_file = None
            self._stop_audio()
            self._current_owner = None
            self._last_audio = 0.0

    def tick(self):
        """Host-side playback and bounded BGM verification, run by Pygame GUI."""
        with self._lock:
            now = self.clock()
            if self._desired_enabled and not self.enabled and now - self._startup_last_attempt >= 2.0:
                self._startup_last_attempt = now
                self.start()
            if not self.enabled and self._ax_gate.needs_restore:
                self._maintain_bgm()
            if now - self._last_audio < AUDIO_INTERVAL:
                return
            self._last_audio = now
            if not self.enabled:
                return

            battle = (self._mode == 'battle' or
                      (self._mode == 'auto' and self._observed.get('scene') == 'battle'))
            new_battle = battle and not self._in_battle
            if battle != self._in_battle:
                if battle and self._opening_character is None:
                    self._opening_character = self._opening_fighter(self._observed)
                self._locked_fighter = None
                self._last_point.clear()
                self._tag_team_slots.clear()
                if not battle:
                    self._opening_team_slots.clear()
                self._tag_selected_character = self._opening_character if battle else None
                self._tag_selected_team = self._slot[:2] if battle else None
                self._tag_armed = False
                self._idle_ready_since = None
                if not battle and self._observed.get('scene') not in ('loading',):
                    self._opening_character = None
                self._in_battle = battle
            if battle:
                self._update_tag_owner(self._observed, entered_battle=new_battle)
            owner, tracks = self._context(self._observed)
            if self._pending_scene_fade:
                self._pending_scene_fade = False
                # Loading -> FIGHT with the same opener is not a song change.
                # Keep the chosen opening song instead of advancing the queue.
                if not (new_battle and self._current_file and owner == self._current_owner):
                    self._begin_fade()
            # Native is the fallback whenever this actual scene/character
            # has no custom track. No song + no other fallback = native NOW.
            want_custom = self._output == 'custom' and bool(tracks)
            self._choose_native_fallback(self._output == 'custom' and not tracks)
            self._requested_mute = self._output == 'no_bgm' or want_custom
            self._maintain_bgm()
            if self._output == 'no_bgm':
                if self._current_file:
                    self._begin_fade()
                    self._service_fade(now)
                self._message = self._mute_status
                return
            if not tracks:
                if self._current_file:
                    self._begin_fade()
                    self._service_fade(now)
                self._message = f'No custom theme for {owner}; {self._mute_status}'
                return
            if not self._native_zero_confirmed:
                # Without verified routing, native audio may still be heard.
                self._message = f'Waiting for isolated AX BGM gate: {self._mute_status}'
                return

            event_changed = (self._source == 'fighter' and self._fighter_switch == 'tag'
                             and self._played_event_serial != self._tag_event_serial)
            changed = owner != self._current_owner or (event_changed and self._current_file is not None)
            if self._current_file and changed:
                self._begin_fade()
            if self._service_fade(now):
                return
            try:
                busy = bool(self._audio().music.get_busy())
            except Exception:
                busy = False
            if changed or not busy:
                try:
                    song = self._pick(owner, tracks)
                    self._audio().music.stop()
                    self._audio().music.load(str(song))
                    self._audio().music.set_volume(self._volume)
                    self._audio().music.play(loops=-1 if owner.startswith('characters/') else 0,
                                             fade_ms=160)
                    self._current_file = str(song)
                    self._current_owner = owner
                    self._played_event_serial = self._tag_event_serial
                    self._last_by_owner[owner] = song.name
                    try:
                        self._save_json(self._history_file, {'last_by_owner': self._last_by_owner})
                    except OSError:
                        pass
                    self._message = (f'{stage_label(int(owner.split("/")[1]))}: {song.name}'
                                     if owner.startswith('stages/') and owner.split('/')[1].isdigit()
                                     else f'{owner}: {song.name}')
                except Exception as ex:
                    self._current_owner = owner
                    self._current_file = None
                    self._message = f'Could not play {owner}: {ex!s}'
                    self._last_audio = now + 1.0
            else:
                try:
                    self._audio().music.set_volume(self._volume)
                except Exception:
                    pass

    def status(self) -> dict:
        with self._lock:
            # UI readout must not lock the next match's fighter/song merely
            # because the settings window happened to refresh first.
            saved_lock, saved_points = self._locked_fighter, dict(self._last_point)
            try:
                playlist, available = self._context(self._observed)
            finally:
                self._locked_fighter, self._last_point = saved_lock, saved_points
            return {
                "enabled": self.enabled,
                "desired_enabled": self._desired_enabled,
                "native_fallback": self._native_fallback,
                "message": self._message,
                "transition_ready": self._transition_ready,
                "bgm_verified": self._native_zero_confirmed,
                "bgm_status": self._mute_status,
                "bgm_retries": self._mute_retries,
                "ax_pair_verified": self._ax_gate.result.found == 2,
                "restore_pending": self._ax_gate.needs_restore,
                "native_original": None,
                "bgm_method": "AX voice routing (experimental)",
                "scene": self._observed['scene'],
                "scene_evidence": dict(self._observed.get('evidence', {})),
                "playlist": playlist,
                "available_tracks": len(available),
                "stage": self._observed.get('stage'),
                "resource_override": self._resource_override,
                "resource_name": resource_label(self._resource_override),
                "catalog_size": len(RESOURCES),
                "expected_ssd": self._expected_ssd(self._observed),
                "fighters": dict(self._observed['fighters']),
                "track": self._current_file,
                "folder": str(self.root),
                "mode": self._mode,
                "output": self._output,
                "slot": self._slot,
                "shuffle": self._shuffle,
                "source": self._source,
                "stage_order": self._stage_order,
                "fighter_order": self._fighter_order,
                "fighter_switch": self._fighter_switch,
                "points": dict(self._observed.get('points', {})),
                "opening_fighter": self._opening_character,
                "tag_tracking_armed": self._tag_armed,
                "opening_team_slots": dict(self._opening_team_slots),
                "both_points_idle": bool(self._observed.get('evidence', {}).get('both_points_idle')),
                "music_owner_team": self._tag_selected_team,
                "music_owner_fighter": self._tag_selected_character,
                "tag_event": self._tag_event_serial,
                "fading": self._fade_started is not None,
                "volume": self._volume,
                "stage_override": self._stage_override,
                "character_override": self._char_override,
            }

controller = MusicController()
try:
    controller.ensure_library()
except OSError as error:
    controller._message = f'Could not create music folders: {error}'
atexit.register(controller.stop)

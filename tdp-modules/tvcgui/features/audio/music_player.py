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
import re
import struct
import threading
import time
from typing import Any

from tvcgui.core.paths import user_data_path
from tvcgui.core.constants import SLOTS, OFF_MAX_HP, OFF_CUR_HP, OFF_CHAR_ID, CHAR_NAMES
from tvcgui.features.audio.ax_bgm_gate import AxBgmGate
from tvcgui.features.audio.music_catalog import (
    RESOURCES, BY_KEY, AUTO_SCENES, STAGE_ID_RESOURCE, resource_label,
)
from tvcgui.runtime.session_recording import recorder as session_recorder

STAGE_RESULT = 0x803EB8F4  # candidate game loader result, not stage carousel cursor
STAGE_MENU_PTR = 0x809BCE28
STAGE_GLOBAL_CURSOR = 0x809BCEE0
# Confirmed in four option/menu captures: scene object is self-linked,
# has type 8, and the native battle object is absent. Unlike the former
# idle=>menu fallback, this must be read from Wii RAM.
MENU_OBJECT_TYPE = 8
NATIVE_BATTLE_PTR = 0x803EFB4C  # Kuro's TvcMusicTools.cpp / TvcCharacterSelect.cpp
VALID_EXT = {".ogg", ".mp3", ".wav"}
PLAYLIST_CACHE_SECONDS = 1.0
FAILED_PLAYLIST_RETRY_SECONDS = 30.0
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
    15: 'Yami - First Form',  # resolved arena asset ID used by the Yami route
    17: 'Training Stage',  # actual STAGE_RESULT in captured phase-4 training match
}
YAMI_FORM_RESOURCE = {23: 's_bgm_32', 24: 's_bgm_33', 25: 's_bgm_34'}


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
SAMPLE_INTERVAL = 0.15  # full scene/roster scan
TRANSITION_SAMPLE_INTERVAL = 0.05  # select/load edges need prompt music routing
BOUNDARY_INTERVAL = 0.025  # cheap native-phase edge scan; not scene guessing
MEM2_PRIME_INTERVAL = 0.75  # recover menu mappings after a save-state jump
END_PHASE_SAMPLES = 2  # reject a single bad native phase read
AUDIO_INTERVAL = 0.06  # smooth host-side fades without blocking the UI
TRANSITION_AUDIO_INTERVAL = 0.02
AX_PENDING_RETRY_INTERVAL = 0.025
AX_MATCH_VERIFY_INTERVAL = 0.025  # catch rebuilt BGM routes within about two GUI frames
FADE_OUT_SECONDS = 0.30
BGM_VERIFY_INTERVAL = 0.40  # throttle DME reads/writes on the GUI thread
STABLE_SCENE_SAMPLES = 2
# The KO controller identifies 9-12 as the native result-state family. The
# stage result is a byte-sized game ID; unknown IDs can still use numeric
# folders, so a catalog omission must not silence a user's track.
WINNER_PHASES = frozenset((9, 10, 11, 12))
# Menu sequence numbers vary by subpage; the root object and native battle
# phase decide whether a cached path actually belongs to the current scene.
MENU_ASSET_PATTERN = re.compile(rb'fpack/menu/([0-9]{3})/0000\.fpk\x00')
MENU_RETURN_SAMPLES = 3  # verified menu asset after the sampler's stable-scene check
MENU_ROOT_RETURN_SAMPLES = 18  # weak menu-root cue after a select screen
UNKNOWN_MATCH_RELEASE_SAMPLES = 12  # stale match with no readable battle state
STAGE_RETURN_CONFIRM_SECONDS = 0.75  # intentional backout from character select
CHAR_TO_MATCH_CONFIRM_SECONDS = 0.75  # stale loader state can outlive P2 lock-in
RESTART_LOADING_SAMPLES = 4  # direct live-fight -> loader edge, not KO
WINNER_UNLOAD_SAMPLES = 2  # fighter objects must disappear before winner music
# Shared move IDs in frame_data_preview_profiles.json. A point change with no
# observed ID remains unclassified and follows the existing Per tag behavior.
POINT_ACTIONS = {
    416: 'tag_out', 418: 'tag_out', 425: 'tag_out',
    420: 'cross_counter', 427: 'cross_counter',
    421: 'var', 428: 'dhc',
}
POINT_ACTION_LOOKBACK = 0.6
CHRSEL_ROOTS = (0x809BCE38, 0x80796364)
CHRSEL_SIGNATURE = b"fpack/menu/001/0000.fpk\x00"


SELECT_SCENE_GLOBAL = 0x809BCECC
CHARACTER_UI_STATE_OFFSET = 0x70


def _slug(name: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _valid_float(raw: bytes | None) -> float | None:
    if not raw or len(raw) != 4:
        return None
    value = struct.unpack(">f", raw)[0]
    return value if math.isfinite(value) and 0 <= value <= 1.01 else None



def _read_native_phase(io: Any) -> int | None:
    """Poll Kuro's native match phase without requiring loaded fighter structs.

    The loader briefly retires fighter structs during some restarts; the
    battle object itself is the boundary signal when valid.
    """
    try:
        ptr = io.rd32(NATIVE_BATTLE_PTR)
        if ptr is None or not (0x80000000 <= ptr < 0x81800000 or
                               0x90000000 <= ptr < 0x94000000):
            return None
        phase = io.rd32(ptr + 12)
        return int(phase) if phase is not None and 0 <= phase <= 12 else None
    except Exception:
        return None


def _read_environment(io: Any) -> dict:
    """Read scene, stage, and fighter cues from Dolphin memory."""
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
    main_menu_object = False
    menu_root_live = False
    menu_asset_id = None
    stage_cursor_live = False
    local_cursor = None
    global_cursor = None
    root_type = None
    select_scene_state = u32(SELECT_SCENE_GLOBAL)
    character_ui_state = None
    if mem2_ready and root is not None and 0x90000000 <= root < 0x94000000:
        root_type = u32(root + 4)
        root_linked = u32(root + 0x4c) == root
        stage_menu = root_type == 15 and root_linked
        if stage_menu:
            character_ui_state = u32(root + CHARACTER_UI_STATE_OFFSET)
        menu_root_live = root_type == MENU_OBJECT_TYPE and root_linked
        # This distinguishes the menu captures (type 8, no battle object)
        # from a KO or live match (battle phase 4-12). The match latch below
        # additionally quarantines results, where cached menu objects can
        # reappear without a confirmed return through select.
        # A cached type-8 object alone is a weak menu cue. The sequence path
        # below increases confidence when available.
        if stage_menu:
            # A recycled stage object can pass the above self-pointer check.
            # Require its live carousel cursor to agree with the global cursor
            # before preferring it over a simultaneously cached select asset.
            local_cursor = u32(root + 0x6c)
            global_cursor = u32(STAGE_GLOBAL_CURSOR)
            stage_cursor_live = (local_cursor is not None and
                                 global_cursor is not None and
                                 local_cursor == global_cursor and
                                 0 <= local_cursor <= 255)

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
    # Read any loaded menu page, while excluding the character-select page.
    if menu_root_live and battle_phase is None:
        for ptr_addr in CHRSEL_ROOTS:
            seq = u32(ptr_addr)
            if seq is None or not 0x90000000 <= seq < 0x94000000:
                continue
            try:
                header = io.rbytes(seq, 128)
                match = MENU_ASSET_PATTERN.search(header or b'')
                if match and match.group(1) != b'001':
                    menu_asset_id = int(match.group(1))
                    break
            except Exception:
                continue
    chars = {}
    actions = {}
    health = {}
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
            cur_hp = u32(base + OFF_CUR_HP)
            if cur_hp is not None and cur_hp <= max_hp:
                health[label] = {'cur': cur_hp, 'max': max_hp}
            point_flag = u32(base + POINT_FLAG_OFFSET)
            if point_flag:  # Kuro treats every nonzero point flag as active
                point_candidates[label[:2]].append(label)
    # Presence of cached fighters isn't proof that battle is active. Use the
    # verified native phase when available; retain fighter fallback only if the
    # game's battle object isn't readable (older Continuo versions, early boot).
    fighters_loaded = any(k.startswith('P1') for k in chars) and any(k.startswith('P2') for k in chars)
    # A self-linked type-8 menu object can survive a scene transition, so
    # native phase and live fighter evidence still outrank it. The loaded
    # menu path strengthens the cue but isn't required for menu subpages.
    main_menu_object = menu_root_live and battle_phase is None and not fighters_loaded
    stage_raw = u32(STAGE_RESULT)
    stage = stage_raw if stage_raw is not None and 0 <= stage_raw <= 255 else None
    # Both select screens can retain the same menu object and chrsel asset.
    # These paired state words change with the visible screen: 0/0 on stage
    # select, 1/1 on character select. Require the live cursor as well because
    # the stage object survives part of the VS transition.
    stage_ui_live = (stage_menu and stage_cursor_live and
                     select_scene_state == 0 and character_ui_state == 0)
    character_ui_live = (stage_menu and select_scene_state == 1 and
                         character_ui_state == 1)
    # Phase 4 is Kuro's only confirmed live-battle phase. The prematch
    # entrance animation can briefly mark multiple point fighters active;
    # phases 5-12 aren't proof that the FIGHT opening has happened.
    if battle_phase == 4:
        scene = 'battle'
    elif battle_phase in WINNER_PHASES:
        scene = 'winner'
    elif battle_phase is not None and battle_phase >= 5:
        # KO animation is not the next-match loader.
        scene = 'post_battle'
    elif battle_phase is not None and battle_phase <= 3 and stage is not None:
        scene = 'loading'
    elif character_ui_live:
        scene = 'character_select'
    elif stage_ui_live:
        scene = 'stage_select'
    elif char_select and not stage_menu:
        # Compatibility cue while a menu object is being rebuilt. A cached
        # chrsel sequence alone cannot beat a verified stage-select object.
        scene = 'character_select'
    elif main_menu_object:
        # Main-menu object (also covers its Arcade/Training/Options submenus).
        # Do not infer menu from an absence of battle evidence alone.
        scene = 'menu'
    elif battle_phase is None and fighters_loaded:
        # Retain a compatibility hint, but do not use this as proof that a
        # *new* match began; stale fighter allocations survive the results.
        scene = 'battle_unverified'
    else:
        # Unknown is still not equivalent to main menu.
        scene = 'unknown'
    points = {team: options[0] for team, options in point_candidates.items() if len(options) == 1}
    # A live phase is not sufficient to arm tags: the prematch entrance can
    # leave both point flags active. Observe *both* unique point fighters idle
    # before we accept the point/anchor baseline. Do not write guest memory.
    both_points_idle = (battle_phase == 4 and all(
        team in points and actions.get(points[team]) == POINT_IDLE_ACTION
        for team in ('P1', 'P2')))
    both_points_live = (battle_phase == 4 and
                        all(team in points for team in ('P1', 'P2')))
    evidence = {
        'both_points_idle': both_points_idle,
        'both_points_live': both_points_live,
        'native_phase': battle_phase,
        'stage_object': bool(stage_menu),
        'stage_cursor': bool(stage_cursor_live),
        'stage_local_cursor_id': local_cursor,
        'stage_cursor_id': global_cursor,
        'stage_root_type': root_type,
        'menu_object': bool(main_menu_object),
        'menu_asset_id': menu_asset_id,
        'menu_asset_verified': menu_asset_id is not None,
        'select_asset': bool(char_select),
        'select_scene_state': select_scene_state,
        'character_ui_state': character_ui_state,
        'character_ui_live': bool(character_ui_live),
        'stage_ui_live': bool(stage_ui_live),
        'ambiguous': bool(stage_menu and char_select),
        'raw_stage': stage_raw,
        'select_id_a': u32(0x809BD090) if char_select else None,
        'select_id_b': u32(0x809BD098) if char_select else None,
        'stage_verified': stage is not None,
        'mem2_ready': mem2_ready,
        'fighter_count': len(chars),
    }
    return {'scene': scene, 'stage': stage, 'fighters': chars, 'points': points,
            'actions': actions, 'health': health, 'evidence': evidence}


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
                location = json.loads(self._location_file.read_text(encoding='utf8'))
                requested = self._resolve_music_location(location)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        self.root = Path(root) if self._explicit_root else (requested or self._default_root)
        self.clock = clock or time.monotonic
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None
        self._audio_service: threading.Thread | None = None
        self._audio_service_stop = threading.Event()
        self._audio_wakeup = threading.Event()
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
        self._ax_rebuild_match_candidate = False
        self._mute_status = "Off"
        self._last_mute_check = float('-inf')
        self._mute_urgent_until = float('-inf')
        self._mute_retries = 0
        self._pending_restore: bytes | None = None
        self._transition_ready = False
        self._entered_at: tuple | None = None
        self._seen_raw: tuple | None = None
        self._stable_count = 0
        self._observed: dict = {"scene": "unknown", "stage": None, "fighters": {}, "points": {}, "evidence": {}}
        self._match_latched = False  # a verified phase-4 battle was observed
        self._fight_opened = False  # both point fighters reached live idle
        self._fight_point_reads = 0
        self._match_end_seen = False  # native post-battle phase 5-12 was observed
        self._winner_unload_reads = 0
        self._winner_music_ready = False
        self._end_phase_reads = 0
        self._last_native_phase: int | None = None
        self._restart_loading_reads = 0
        self._round_transition_seen = False
        self._select_since_live = False
        self._match_serial = 0        # advance when select or KO precedes another battle
        self._played_match_serial = 0
        self._has_initial_observation = False
        self._last_audio = 0.0
        self._battle_music_scan_at = float('-inf')
        self._battle_music_scan_result = False
        self._current_owner: str | None = None
        self._interrupted_track: tuple[str, str] | None = None
        self._select_return_track: str | None = None
        self._current_file: str | None = None
        self._loop_preview_channel = None
        self._loop_preview_sound = None
        self._loop_segment_start = 0.0
        self._loop_started_at = 0.0
        self._message = "Off. Put tracks into music folders before enabling."
        self._mode = "auto"  # auto, menu, character_select, stage_select, battle
        self._output = "custom"  # custom or no_bgm; OFF restores native BGM
        self._slot = "P1-point"  # follow verified active fighter on player 1
        self._source = "fighter"  # fighter or stage, explicit music source
        self._stage_order = "sequential"
        self._fighter_order = "sequential"
        self._stage_end_mode = "match"
        self._fighter_end_mode = "match"
        self._fighter_switch = "tag"  # 'match' locks first point until battle ends
        self._tag_scope = 'both'  # selected side or both teams
        self._low_health_enabled = False
        self._low_health_threshold = 30.0
        self._ignored_point_actions: set[str] = set()
        self._fighter_change_cooldown = 0.0
        self._last_fighter_music_change_at = float('-inf')
        self._recent_point_actions: dict[str, tuple[int, float]] = {}
        self._locked_fighter: int | None = None
        self._in_battle = False
        self._last_point: dict[str, int] = {}
        # Kuro-style event ownership: follow side chooses opening music, then
        # each team's *verified* point transition can take over the soundtrack.
        self._opening_character: int | None = None
        self._tag_armed = False
        self._tag_team_slots: dict[str, str] = {}
        # Remember the opening roster before using any unreliable prematch
        # point flags. The first *verified idle* point may already reflect a
        # real tag if it happened while the sampler was waiting to arm.
        self._opening_team_slots: dict[str, str] = {}
        self._tag_selected_character: int | None = None
        self._tag_selected_team: str | None = None
        self._tag_event_serial = 0
        self._played_event_serial = 0
        self._verified_live_stage: int | None = None
        self._verified_live_roster: dict[str, int] = {}
        self._pending_match_boundary = False
        self._menu_return_reads = 0
        self._menu_exit_pending = False
        self._menu_root_reads = 0
        self._stage_select_candidate_since: float | None = None
        self._loading_candidate_since: float | None = None
        self._unknown_match_reads = 0
        self._tick_match_serial = 0
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
        self._random_remaining: dict[str, list[str]] = {}
        self._random_population: dict[str, tuple[str, ...]] = {}
        self._song_cache: dict[tuple[Path, ...], tuple[float, list[Path]]] = {}
        self._failed_playlist: tuple[str, tuple] | None = None
        self._failed_playlist_at = float('-inf')
        self._settings_file = (self.root / 'settings.json' if self._explicit_root
                               else self._config_root / 'music_settings.json')
        self._history_file = (self.root / 'history.json' if self._explicit_root
                              else self._config_root / 'music_history.json')
        self._loop_file = self.root / 'loop_points.json'
        self._loop_points: dict[str, dict] = {}
        self._load()
        self._load_loop_points()

    def _load_loop_points(self) -> None:
        try:
            saved = json.loads(self._loop_file.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            saved = {}
        self._loop_points = {}
        if not isinstance(saved, dict):
            return
        for name, setting in saved.items():
            try:
                self._loop_points[name] = self._valid_loop_setting(name, setting)
            except (TypeError, ValueError):
                continue

    @staticmethod
    def _valid_loop_setting(name: str, setting: dict) -> dict:
        if not isinstance(name, str):
            raise ValueError('Choose a playable OGG, MP3, or WAV track.')
        path = Path(name)
        structured = path.parts[0] in ('stages', 'characters', 'menus', 'extras') if path.parts else False
        legacy_scene = path.parts[0] in ('menu', 'character_select', 'stage_select', 'winner') if path.parts else False
        low_health = (len(path.parts) == 4 and path.parts[0] == 'low_health' and
                      path.parts[1] in ('stages', 'characters'))
        if (path.is_absolute() or '..' in path.parts or
                not ((structured and len(path.parts) == 3) or
                     (legacy_scene and len(path.parts) == 2) or low_health) or
                path.suffix.lower() not in VALID_EXT or not isinstance(setting, dict)):
            raise ValueError('Choose a playable OGG, MP3, or WAV track in the music folder.')
        start = float(setting['start'])
        end = setting.get('end')
        end = None if end in (None, '') else float(end)
        first = setting.get('first', 'beginning')
        if (not math.isfinite(start) or start < 0 or
                (end is not None and (not math.isfinite(end) or end <= start)) or
                first not in ('beginning', 'loop')):
            raise ValueError('Loop end must be after the loop start.')
        if path.suffix.lower() == '.wav' and start > 0:
            raise ValueError('WAV seeking is unavailable; use OGG or MP3 for a loop start after zero.')
        return {'start': start, 'end': end, 'first': first}

    def loop_tracks(self) -> list[str]:
        with self._lock:
            names = []
            for category in ('stages', 'characters', 'menus', 'extras'):
                parent = self.root / category
                if parent.is_dir():
                    names.extend(self._track_identity(song)
                                 for folder in parent.iterdir() if folder.is_dir()
                                 for song in folder.iterdir()
                                 if song.is_file() and song.suffix.lower() in VALID_EXT)
            for scene in ('menu', 'character_select', 'stage_select', 'winner'):
                folder = self.root / scene
                if folder.is_dir():
                    names.extend(self._track_identity(song) for song in folder.iterdir()
                                 if song.is_file() and song.suffix.lower() in VALID_EXT)
            for category in ('stages', 'characters'):
                parent = self.root / 'low_health' / category
                if parent.is_dir():
                    names.extend(self._track_identity(song)
                                 for folder in parent.iterdir() if folder.is_dir()
                                 for song in folder.iterdir()
                                 if song.is_file() and song.suffix.lower() in VALID_EXT)
            return sorted(names, key=str.casefold)

    def loop_setting(self, name: str) -> dict | None:
        with self._lock:
            setting = self._loop_points.get(name)
            return dict(setting) if setting else None

    def loop_duration(self, name: str) -> float:
        """Return a selected custom song's length in seconds."""
        from tvcgui.features.audio.loop_preview import song_duration

        self._valid_loop_setting(name, {'start': 0, 'end': None})
        with self._lock:
            song = self.root / name
        if not song.is_file():
            raise ValueError('Choose a track that exists in this music folder.')
        return song_duration(self._audio(), song)

    def set_loop_setting(self, name: str, *, start: float | None = None,
                         end: float | None = None, first: str = 'beginning') -> None:
        with self._lock:
            if start is None:
                self._loop_points.pop(name, None)
            else:
                setting = self._valid_loop_setting(name, {'start': start, 'end': end,
                                                          'first': first})
                if not (self.root / name).is_file():
                    raise ValueError('Choose a track that exists in this music folder.')
                self._loop_points[name] = setting
            self._save_json(self._loop_file, self._loop_points)
            self._audio_wakeup.set()

    def preview_loop(self, name: str, *, start: float, end: float | None) -> tuple[float, ...]:
        """Play an unsaved loop seam on a separate host mixer channel."""
        from tvcgui.features.audio.loop_preview import build_loop_preview

        self._valid_loop_setting(name, {'start': start, 'end': end, 'first': 'beginning'})
        song = self.root / name
        if not song.is_file():
            raise ValueError('Choose a track that exists in this music folder.')
        preview, before, boundary, loop_start, after = build_loop_preview(
            self._audio(), song, start=start, end=end)
        with self._lock:
            self.stop_loop_preview()
            channel = preview.play()
            if channel is None:
                raise RuntimeError('No free audio channel for the loop preview.')
            channel.set_volume(self._volume)
            self._loop_preview_channel = channel
            self._loop_preview_sound = preview
            if self._current_file and self._fade_started is None:
                self._audio().music.set_volume(self._volume * 0.15)
        return before, boundary, loop_start, after

    def loop_preview_playing(self) -> bool:
        with self._lock:
            if self._loop_preview_channel is None:
                return False
            if self._loop_preview_channel.get_busy():
                return True
            self._loop_preview_channel = None
            self._loop_preview_sound = None
            if self._current_file and self._fade_started is None:
                try:
                    self._audio().music.set_volume(self._volume)
                except Exception:
                    pass
            return False

    def stop_loop_preview(self) -> None:
        with self._lock:
            if self._loop_preview_channel is not None:
                self._loop_preview_channel.stop()
            self._loop_preview_channel = None
            self._loop_preview_sound = None
            if self._current_file and self._fade_started is None:
                try:
                    self._audio().music.set_volume(self._volume)
                except Exception:
                    pass

    def _live_music_volume(self) -> float:
        channel = self._loop_preview_channel
        return self._volume * 0.15 if channel is not None and channel.get_busy() else self._volume

    def _resolve_music_location(self, location: dict) -> Path | None:
        """Load a saved library path, including paths from older portable builds."""
        value = location.get('path')
        if not isinstance(value, str) or not value.strip():
            return None
        path = Path(value).expanduser()
        if location.get('relative_to') == 'config':
            return (self._config_root / path).resolve() if not path.is_absolute() else None
        if not path.is_absolute():
            return None
        # Older builds wrote the default EXE-side library as an absolute path.
        # A copied installation must follow its new data/continuo/music tree,
        # even when the original installation still exists on this machine.
        old_default = (path.name.casefold() == 'music' and
                       path.parent.name.casefold() == 'continuo' and
                       path.parent.parent.name.casefold() == 'data')
        if old_default:
            return self._default_root
        # An external library stays selected while present. If its drive was
        # removed, use the local library instead of recreating an empty one.
        return path if path.is_dir() else self._default_root

    def _music_location_record(self, path: Path) -> dict[str, str]:
        """Keep EXE-side selections portable; retain absolute external paths."""
        try:
            relative = path.relative_to(self._config_root.resolve())
        except ValueError:
            return {'path': str(path)}
        return {'path': str(relative), 'relative_to': 'config'}

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
            self._stage_end_mode = d.get('stage_end_mode', 'match')
            self._fighter_end_mode = d.get('fighter_end_mode', 'match')
            if self._stage_end_mode not in ('match', 'playlist'):
                self._stage_end_mode = 'match'
            if self._fighter_end_mode not in ('match', 'playlist'):
                self._fighter_end_mode = 'match'
            self._fighter_switch = d.get('fighter_switch', 'tag')
            if self._fighter_switch not in ('match', 'tag'):
                self._fighter_switch = 'tag'
            self._tag_scope = d.get('tag_scope', 'both')
            if self._tag_scope not in ('selected', 'both'):
                self._tag_scope = 'both'
            self._low_health_enabled = bool(d.get('low_health_enabled', False))
            threshold = float(d.get('low_health_threshold', 30))
            self._low_health_threshold = threshold if math.isfinite(threshold) and 1 <= threshold <= 100 else 30.0
            ignored = d.get('ignored_point_actions', [])
            self._ignored_point_actions = (set(ignored) & set(POINT_ACTIONS.values())
                                           if isinstance(ignored, list) else set())
            cooldown = float(d.get('fighter_change_cooldown', 0))
            self._fighter_change_cooldown = cooldown if math.isfinite(cooldown) and 0 <= cooldown <= 300 else 0.0
            self._shuffle = bool(d.get("shuffle", False))
            self._volume = min(1., max(0., float(d.get("volume", 0.65))))
            self._stage_override = self._id_or_none(d.get("stage_override"))
            if self._stage_override == 15 and d.get('stage_ids_version', 1) < 2:
                # Older builds called manual ID 15 Training. Its confirmed
                # live ID is 17; ID 15 now correctly names Yami's arena.
                self._stage_override = 17
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
            cycles = hist.get('random_cycles', {})
            if isinstance(cycles, dict):
                for owner, cycle in cycles.items():
                    if not isinstance(owner, str) or not isinstance(cycle, dict):
                        continue
                    population = cycle.get('population')
                    remaining = cycle.get('remaining')
                    if (not isinstance(population, list) or
                            not isinstance(remaining, list) or
                            not all(isinstance(song, str) for song in population + remaining) or
                            len(set(population)) != len(population) or
                            len(set(remaining)) != len(remaining) or
                            not set(remaining).issubset(population)):
                        continue
                    self._random_population[owner] = tuple(population)
                    self._random_remaining[owner] = remaining
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
                  fighter_order=None, stage_end_mode=None, fighter_end_mode=None,
                  fighter_switch=None, tag_scope=None,
                  low_health_enabled=None, low_health_threshold=None,
                  ignored_point_actions=None, fighter_change_cooldown=None,
                  resource=None):
        with self._lock:
            former = (self._mode, self._output, self._slot, self._stage_override, self._char_override,
                      self._source, self._fighter_switch, self._resource_override)
            former_orders = (self._stage_order, self._fighter_order)
            former_end_modes = (self._stage_end_mode, self._fighter_end_mode)
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
            if stage_end_mode in ('match', 'playlist'):
                self._stage_end_mode = stage_end_mode
            if fighter_end_mode in ('match', 'playlist'):
                self._fighter_end_mode = fighter_end_mode
            if former_orders != (self._stage_order, self._fighter_order):
                self._random_remaining.clear()
                self._random_population.clear()
            if fighter_switch in ('match', 'tag'):
                if fighter_switch != self._fighter_switch:
                    self._locked_fighter = None
                self._fighter_switch = fighter_switch
            if tag_scope in ('selected', 'both'):
                self._tag_scope = tag_scope
            if low_health_enabled is not None:
                self._low_health_enabled = bool(low_health_enabled)
            if low_health_threshold is not None:
                threshold = float(low_health_threshold)
                if not math.isfinite(threshold) or not 1 <= threshold <= 100:
                    raise ValueError('Low health threshold must be 1–100%.')
                self._low_health_threshold = threshold
            if ignored_point_actions is not None:
                self._ignored_point_actions = set(ignored_point_actions) & set(POINT_ACTIONS.values())
            if fighter_change_cooldown is not None:
                cooldown = float(fighter_change_cooldown)
                if not math.isfinite(cooldown) or not 0 <= cooldown <= 300:
                    raise ValueError('Fighter music cooldown must be 0–300 seconds.')
                self._fighter_change_cooldown = cooldown
            if volume is not None:
                self._volume = max(0., min(1., float(volume)))
            if stage is not None:
                self._stage_override = self._id_or_none(stage)
            if character is not None:
                self._char_override = self._id_or_none(character)
            if resource is not None:
                self._resource_override = resource if resource in BY_KEY else None
            self._save_settings()
            end_mode_changed_for_current = (
                self._current_owner is not None and
                ((self._current_owner.startswith('stages/') and
                  former_end_modes[0] != self._stage_end_mode) or
                 (self._current_owner.startswith('characters/') and
                  former_end_modes[1] != self._fighter_end_mode)))
            if end_mode_changed_for_current:
                # Reload the current song so pygame receives its new repeat count.
                if self._current_file:
                    self._interrupted_track = (self._current_owner, self._current_file)
                    self._current_owner = None
            # Only restart a track if its owner or playlist binding changes.
            if former != (self._mode, self._output, self._slot, self._stage_override, self._char_override,
                          self._source, self._fighter_switch, self._resource_override):
                self._current_owner = None

    def _save_settings(self) -> None:
        self._save_json(self._settings_file, {
            'scene_mode': self._mode, 'output': self._output,
            'character_slot': self._slot, 'music_source': self._source,
            'stage_order': self._stage_order, 'fighter_order': self._fighter_order,
            'stage_end_mode': self._stage_end_mode,
            'fighter_end_mode': self._fighter_end_mode,
            'fighter_switch': self._fighter_switch, 'volume': self._volume,
            'tag_scope': self._tag_scope,
            'low_health_enabled': self._low_health_enabled,
            'low_health_threshold': self._low_health_threshold,
            'ignored_point_actions': sorted(self._ignored_point_actions),
            'fighter_change_cooldown': self._fighter_change_cooldown,
            'stage_override': self._stage_override,
            'stage_ids_version': 2,
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
                self._ax_gate.request_native_restore()
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
            if item.category == 'stages':
                self._dir_for(f'low_health/{item.folder}').mkdir(parents=True, exist_ok=True)
        for name in ['menu', 'character_select', 'stage_select', 'stages/default']:
            self.root.joinpath(*name.split('/')).mkdir(parents=True, exist_ok=True)
        self._dir_for('low_health/stages/default').mkdir(parents=True, exist_ok=True)
        for cid, name in CHAR_NAMES.items():
            self._dir_for(f'characters/{cid:02d}_{_slug(name)}').mkdir(parents=True, exist_ok=True)
            self._dir_for(f'low_health/characters/{cid:02d}_{_slug(name)}').mkdir(parents=True, exist_ok=True)
        return self.root

    def set_music_root(self, selected: str | Path) -> Path:
        """Choose any writable library folder, including while Custom is on.

        Old files remain in place. An active AX mute is restored before the
        current player is stopped; never switch while that restore is blocked.
        """
        selected = Path(selected).expanduser().resolve()
        selected.mkdir(parents=True, exist_ok=True)
        test = selected / '.continuo-write-test'
        try:
            test.write_bytes(b'ok')
        finally:
            test.unlink(missing_ok=True)
        with self._lock:
            if selected == self.root.resolve():
                self.ensure_library()
                return selected
            self.stop_loop_preview()
            was_enabled = self.enabled
            if was_enabled and self._ax_gate.needs_restore and self._write_protected():
                raise RuntimeError('Leave character select before changing the music folder: '
                                   'the AX route cannot be safely restored there.')
            old_root, old_settings, old_history, old_loop_file, old_loop_points = (
                self.root, self._settings_file, self._history_file,
                self._loop_file, self._loop_points)
            old_desired = self._desired_enabled
            if was_enabled:
                self.stop()
                if self._ax_gate.needs_restore:
                    # If the mute couldn't be restored, don't silently abandon
                    # ownership of that DSP routing state.
                    self.start()
                    raise RuntimeError('Could not restore native BGM; library unchanged.')
            try:
                self.root = selected
                self._loop_file = selected / 'loop_points.json'
                self._load_loop_points()
                if self._explicit_root:
                    self._settings_file = selected / 'settings.json'
                    self._history_file = selected / 'history.json'
                self.ensure_library()
                if self._location_file:
                    self._save_json(self._location_file, self._music_location_record(selected))
            except Exception:
                self.root, self._settings_file, self._history_file = old_root, old_settings, old_history
                self._loop_file, self._loop_points = old_loop_file, old_loop_points
                if was_enabled:
                    self.start()
                raise
            self._current_owner = None
            self._current_file = None
            self._song_cache.clear()
            self._random_remaining.clear()
            self._random_population.clear()
            self._failed_playlist = None
            self._battle_music_scan_at = float('-inf')
            self._desired_enabled = old_desired
            if was_enabled:
                self.start()  # empty new library leaves native BGM audible
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
            if value == 15:
                # Numeric 15 folders belonged to Training in older builds.
                # Never play one as Yami's theme.
                names.append('stages/s_bgm_32')
            else:
                names += [f'stages/{value:02d}', f'stages/{value}',
                          f'stages/stage_{value:02d}']
            if value == 17:
                # Earlier builds wrote Training Stage to 15; older users may
                # have placed songs under its numeric aliases instead.
                names += ['stages/15', 'stages/stage_15']
            names.append('stages/default')
            return [self._dir_for(name) for name in dict.fromkeys(names)]
        if kind == 'characters' and value in CHAR_NAMES:
            return [self._dir_for(f'characters/{value:02d}_{_slug(CHAR_NAMES[value])}'),
                    self._dir_for(f'characters/{value}'),
                    self._dir_for(f'characters/{_slug(CHAR_NAMES[value])}')]
        return []

    def _ssd_candidate_dirs(self, key: str, *legacy: str) -> list[Path]:
        """Human-named resource and legacy scene folders form one playlist."""
        item = BY_KEY.get(key)
        names = ([item.folder] if item else []) + list(legacy)
        return [self._dir_for(path) for path in dict.fromkeys(names)]

    def _songs(self, paths: list[Path]) -> list[Path]:
        """Merge matching folders without a cap, ignoring duplicate *paths*.

        Existing numeric, readable and SSD aliases can all contain tracks.
        An identical filename in two different folders remains two entries;
        the same directory encountered twice does not duplicate its tracks.
        """
        key = tuple(paths)
        now = self.clock()
        cached = self._song_cache.get(key)
        if cached and now - cached[0] < PLAYLIST_CACHE_SECONDS:
            return cached[1]
        result, seen = [], set()
        for folder in paths:
            try:
                if not folder.is_dir():
                    continue
                for song in sorted(folder.iterdir(), key=lambda p: (p.name.casefold(), str(p))):
                    if not song.is_file() or song.suffix.lower() not in VALID_EXT:
                        continue
                    identity = os.path.normcase(str(song.resolve()))
                    if identity not in seen:
                        seen.add(identity)
                        result.append(song)
            except OSError:
                continue  # removable or inaccessible folder; retry next scan
        self._song_cache[key] = (now, result)
        return result

    def _stage_songs(self, stage: int | None) -> list[Path]:
        """Named and legacy variants combine; default is a fallback only."""
        candidates = self._candidate_dirs('stages', stage)
        if stage is None:
            return []  # No verified identity, so do NOT borrow default-stage music.
        found = self._songs([p for p in candidates if p != self._dir_for('stages/default')])
        return found if found else self._songs([self._dir_for('stages/default')])

    def _yami_form_resource(self, observed: dict) -> str | None:
        """The three Yami fights share arena 15; the loaded boss identifies the form."""
        if self._stage_override is not None or observed.get('stage') != 15:
            return None
        forms = {cid for cid in observed.get('fighters', {}).values()
                 if cid in YAMI_FORM_RESOURCE}
        return YAMI_FORM_RESOURCE[next(iter(forms))] if len(forms) == 1 else None

    def _battle_stage_music(self, observed: dict) -> tuple[str, list[Path]]:
        sid = self._stage_override if self._stage_override is not None else observed.get('stage')
        form = self._yami_form_resource(observed)
        if form and form != 's_bgm_32':
            tracks = self._songs(self._ssd_candidate_dirs(form, f'stages/{form}'))
            if not tracks:
                tracks = self._songs([self._dir_for('stages/default')])
            return f'stages/{form}', tracks
        return f'stages/{sid if sid is not None else "default"}', self._stage_songs(sid)

    def _track_identity(self, song: Path) -> str:
        try:
            return song.relative_to(self.root).as_posix()
        except ValueError:
            return str(song)

    @staticmethod
    def _playlist_fingerprint(tracks: list[Path]) -> tuple:
        """Notice replacement of a broken file without rescanning its contents."""
        result = []
        for song in tracks:
            try:
                info = song.stat()
                result.append((str(song), info.st_size, info.st_mtime_ns))
            except OSError:
                result.append((str(song), None, None))
        return tuple(result)

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

    def _point_change_kind(self, observed: dict, old_slot: str | None,
                           new_slot: str) -> str | None:
        actions = observed.get('actions') or {}
        slots = (old_slot, new_slot)
        current = {POINT_ACTIONS.get(actions.get(slot)) for slot in slots}
        kinds = current - {None}
        if not kinds:
            now = self.clock()
            kinds = {POINT_ACTIONS.get(self._recent_point_actions[slot][0])
                     for slot in slots if slot in self._recent_point_actions and
                     now - self._recent_point_actions[slot][1] <= POINT_ACTION_LOOKBACK}
        for kind in ('cross_counter', 'dhc', 'var', 'tag_out'):
            if kind in kinds:
                return kind
        return None

    def _select_point_music(self, observed: dict, team: str, old_slot: str | None,
                            new_slot: str) -> None:
        kind = self._point_change_kind(observed, old_slot, new_slot)
        if kind in self._ignored_point_actions:
            return
        if self.clock() - self._last_fighter_music_change_at < self._fighter_change_cooldown:
            return
        self._tag_selected_team = team
        self._tag_selected_character = observed['fighters'][new_slot]
        self._tag_event_serial += 1

    def _update_tag_owner(self, observed: dict, *, entered_battle=False) -> None:
        """Follow confirmed point changes on the configured team or both.

        The sampler has already required two identical observations. Missing or
        ambiguous point flags are not events. Fight opening is verified before
        this runs; the other team's current action must not block this team.
        Fixed C1/C2 options do not follow tags.
        """
        if self._fighter_switch != 'tag' or self._slot not in ('P1-point', 'P2-point'):
            return
        fighters = observed.get('fighters', {})
        points = observed.get('points', {})
        now = self.clock()
        for slot, action in (observed.get('actions') or {}).items():
            if action in POINT_ACTIONS:
                self._recent_point_actions[slot] = (action, now)
        valid = {team: slot for team, slot in points.items()
                 if team in ('P1', 'P2') and slot in fighters and fighters[slot] in CHAR_NAMES}
        if entered_battle:
            self._tag_team_slots.clear()
            self._recent_point_actions.clear()
            # C1 enters as point. During the intro both point flags can be
            # asserted, so capture roster identity instead of flag state.
            self._opening_team_slots = {
                team: f'{team}-C1' for team in ('P1', 'P2')
                if f'{team}-C1' in fighters
            }
            self._tag_armed = False
            # Keep the prematch Follow theme playing uninterrupted at FIGHT.
            self._tag_selected_character = self._opening_character
            self._tag_selected_team = self._slot[:2]
        followed_team = self._slot[:2]
        tracked_teams = ((followed_team,) if self._tag_scope == 'selected' else
                         (followed_team, 'P2' if followed_team == 'P1' else 'P1'))
        if not self._tag_armed:
            # _fight_opened already passed the two-team idle guard. The first
            # stable, unique point for the followed side is enough to arm.
            # Requiring a second idle interval loses tags made immediately
            # after the opening pose, especially when P2 starts attacking.
            if followed_team not in valid:
                return
            self._tag_team_slots = dict(valid)
            self._tag_armed = True
            # Do NOT silently consume the first real tag as the baseline.
            # If Ken/C1 was the opening point but Ryu/C2 is the first stable
            # idle point, the tag happened before we finished arming. Treat
            # this as a genuine entrance now, after the live-idle gate.
            # In selected-side mode, the other team's entrance is ignored.
            early_events = [team for team in tracked_teams
                            if team in self._opening_team_slots
                            and team in valid
                            and valid[team] != self._opening_team_slots[team]]
            if early_events:
                team = early_events[0]
                self._select_point_music(observed, team,
                                         self._opening_team_slots.get(team), valid[team])
            return
        events = [team for team in tracked_teams if team in valid and (
            (team in self._tag_team_slots and valid[team] != self._tag_team_slots[team]) or
            (team not in self._tag_team_slots and
             team in self._opening_team_slots and
             valid[team] != self._opening_team_slots[team]))]
        old_slots = dict(self._tag_team_slots)
        for team in ('P1', 'P2'):
            if team in valid:
                self._tag_team_slots[team] = valid[team]
        if events:
            team = events[0]
            self._select_point_music(observed, team,
                                     old_slots.get(team, self._opening_team_slots.get(team)),
                                     valid[team])

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
            self._stop_audio(reason='fade_complete')
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
            return self._yami_form_resource(observed) or STAGE_ID_RESOURCE.get(sid)
        return AUTO_SCENES.get(scene)

    def _low_health_survivor(self, observed: dict, team: str) -> int | None:
        health = observed.get('health') or {}
        fighters = observed.get('fighters') or {}
        first, second = f'{team}-C1', f'{team}-C2'
        a, b = health.get(first), health.get(second)
        if not (isinstance(a, dict) and isinstance(b, dict)):
            return None
        dead_a = a.get('cur') == 0
        dead_b = b.get('cur') == 0
        if dead_a == dead_b:
            return None
        survivor_slot = second if dead_a else first
        survivor = health[survivor_slot]
        maximum = survivor.get('max')
        current = survivor.get('cur')
        if (not isinstance(maximum, (int, float)) or maximum <= 0 or
                not isinstance(current, (int, float)) or current <= 0 or
                current / maximum * 100 > self._low_health_threshold):
            return None
        fighter = fighters.get(survivor_slot)
        return fighter if fighter in CHAR_NAMES else None

    def _low_health_context(self, observed: dict, owner: str,
                            tracks: list[Path]) -> tuple[str, list[Path]]:
        if (not self._low_health_enabled or observed.get('scene') != 'battle' or
                (observed.get('evidence') or {}).get('native_phase') != 4):
            return owner, tracks
        if self._source == 'fighter' and self._slot != 'stage_only':
            team = self._tag_selected_team or self._slot[:2]
            fighter = self._low_health_survivor(observed, team)
            if fighter is None:
                return owner, tracks
            candidates = self._candidate_dirs('characters', fighter)
            special_owner = f'low_health/characters/{fighter}'
        else:
            if not any(self._low_health_survivor(observed, team)
                       for team in ('P1', 'P2')):
                return owner, tracks
            stage = self._stage_override if self._stage_override is not None else observed.get('stage')
            form = self._yami_form_resource(observed)
            candidates = (self._ssd_candidate_dirs(form, f'stages/{form}')
                          if form and form != 's_bgm_32' else
                          self._candidate_dirs('stages', stage))
            special_owner = f'low_health/stages/{form or stage}'
        special_dirs = [self.root / 'low_health' / path.relative_to(self.root)
                        for path in candidates]
        if special_owner.startswith('low_health/stages/'):
            default = self.root / 'low_health/stages/default'
            stage_specific = self._songs([path for path in special_dirs if path != default])
            special = stage_specific or self._songs([default])
        else:
            special = self._songs(special_dirs)
        return (special_owner, special) if special else (owner, tracks)

    def _context(self, observed: dict) -> tuple[str, list[Path]]:
        # Explicit manual selection is useful for Wi-Fi, endings, Yami forms,
        # and other screens whose live state is not yet verified. Do not
        # silently claim to have detected their SSD resource in Wii RAM.
        if self._resource_override in BY_KEY:
            key = self._resource_override
            return f'ssd/{key}', self._songs(self._ssd_candidate_dirs(key))
        mode = self._mode if self._mode != 'auto' else observed.get('scene', 'unknown')
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
                return self._battle_stage_music(observed)
            if mode == 'loading':
                return self._battle_stage_music(observed)
            stage_owner, stage_tracks = self._battle_stage_music(observed)
            if self._source == 'stage' or self._slot == 'stage_only':
                return self._low_health_context(observed, stage_owner, stage_tracks)
            fighter = self._point_fighter(observed)
            if self._fighter_switch == 'match':
                if self._locked_fighter is None:
                    self._locked_fighter = (self._opening_character
                                            if self._opening_character in CHAR_NAMES else fighter)
                fighter = self._locked_fighter
            if fighter in CHAR_NAMES:
                tracks = self._songs(self._candidate_dirs('characters', fighter))
                if tracks:
                    return self._low_health_context(observed, f'characters/{fighter}', tracks)
            return self._low_health_context(observed, stage_owner, stage_tracks)
        if mode not in ('menu', 'character_select', 'stage_select', 'winner'):
            return 'unknown', []
        key = mode
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
        if self._battle_stage_music(observed)[1]:
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
        if scene == 'menu':
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
        order = (self._fighter_order if owner.startswith(('characters/', 'low_health/characters/'))
                 else self._stage_order)
        # Historical settings stored just a filename. Full relative paths
        # distinguish tracks sharing a filename in separate legacy folders.
        def matches(p):
            return self._track_identity(p) == last or ('/' not in str(last) and p.name == last)
        if order == 'random' and len(tracks) > 1:
            # A shuffled cycle visits every configured song before repeating.
            # A one-step no-repeat choice can revisit the same small subset
            # indefinitely, which makes larger menu libraries seem truncated.
            population = tuple(self._track_identity(song) for song in tracks)
            if self._random_population.get(owner) != population:
                self._random_population[owner] = population
                self._random_remaining[owner] = []
            remaining = self._random_remaining[owner]
            if not remaining:
                remaining.extend(population)
                random.shuffle(remaining)
                if remaining[0] == last:
                    remaining[0], remaining[1] = remaining[1], remaining[0]
            chosen = remaining[0]
            return tracks[population.index(chosen)]
        previous = next((i for i, p in enumerate(tracks) if matches(p)), -1)
        return tracks[(previous + 1) % len(tracks)]

    def _consume_random_attempts(self, owner: str, attempts: list[Path]) -> None:
        """Advance past the played song and any unreadable files tried first."""
        remaining = self._random_remaining.get(owner)
        if remaining is None:
            return
        for song in attempts:
            identity = self._track_identity(song)
            if identity in remaining:
                remaining.remove(identity)

    def _stop_audio(self, *, reason: str = 'stop'):
        if self._current_file:
            session_recorder.capture_event(self.clock(), 'music_stop', {
                'owner': self._current_owner,
                'track': self._track_identity(Path(self._current_file)),
                'scene': self._observed.get('scene'), 'match_serial': self._match_serial,
                'reason': reason,
            })
        self._fade_started = None
        self._pending_scene_fade = False
        try:
            self._audio().music.stop()
        except Exception:
            pass
        self._current_file = None
        self._loop_segment_start = 0.0
        self._loop_started_at = 0.0

    def _play_music_segment(self, song: Path, *, start: float, loops: int,
                            fade_ms: int = 0) -> None:
        options = {'loops': loops, 'fade_ms': fade_ms}
        if start:
            options['start'] = start
        self._audio().music.play(**options)
        self._loop_segment_start = start
        self._loop_started_at = self.clock()

    def _service_track_loop(self, owner: str, tracks: list[Path], now: float,
                            busy: bool) -> bool:
        """Keep the selected song playing until its music context changes."""
        if not self._current_file or self._current_owner != owner:
            return False
        song = Path(self._current_file)
        if song not in tracks:
            return False
        end_mode = self._end_mode_for_owner(owner)
        if end_mode == 'playlist':
            return False
        setting = self._loop_points.get(self._track_identity(song))
        if setting is None:
            if busy or end_mode != 'match':
                return False
            try:
                self._play_music_segment(song, start=0, loops=-1)
                return True
            except Exception:
                return False
        elapsed = now - self._loop_started_at
        try:
            played_ms = self._audio().music.get_pos()
            if played_ms is not None and played_ms >= 0:
                elapsed = played_ms / 1000.0
        except Exception:
            pass
        end = setting['end']
        if busy and (end is None or self._loop_segment_start + elapsed < end):
            return False
        try:
            self._play_music_segment(song, start=setting['start'], loops=0)
            return True
        except Exception as ex:
            self._stop_audio(reason='loop_seek_failed')
            self._current_owner = None
            self._message = f'Could not loop {song.name}: {ex!s}'
            return False

    def _end_mode_for_owner(self, owner: str) -> str | None:
        if owner.startswith(('stages/', 'low_health/stages/')):
            return self._stage_end_mode
        if owner.startswith(('characters/', 'low_health/characters/')):
            return self._fighter_end_mode
        return None

    def _choose_native_fallback(self, native: bool) -> None:
        """Select the *local* AX output route without changing game options."""
        if native == self._native_fallback:
            return
        self._native_fallback = native
        if native and self._current_file:
            self._begin_fade()
        self._mute_status = ('Native audio requested (AX route)' if native else
                             'Custom audio requested (AX route)')
        if not native:
            self._prioritize_mute()
        else:
            self._last_mute_check = float('-inf')

    def _prioritize_mute(self) -> None:
        """Poll the AX pair promptly for one second after a music boundary."""
        self._mute_urgent_until = self.clock() + 1.0
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
        interval = (AX_PENDING_RETRY_INTERVAL if self._requested_mute and
                    now < self._mute_urgent_until else
                    AX_MATCH_VERIFY_INTERVAL if self._requested_mute and self._match_latched
                    else 0.15)
        if not force and now - self._last_mute_check < interval:
            return
        self._last_mute_check = now
        mute = bool(self.enabled and self._requested_mute and not self._native_fallback)
        # Do not perform any writes if BGM is not requested and we own none.
        if not mute and not self._ax_gate.needs_restore:
            self._native_zero_confirmed = False
            self._transition_ready = True
            self._mute_status = 'Native BGM unchanged (AX gate idle)'
            return
        protected = self._write_protected()
        result = self._ax_gate.apply(self._backend(), mute=mute,
                                     write_protected=protected)
        if result.muted != self._native_zero_confirmed or result.text != self._mute_status:
            session_recorder.capture_event(now, 'music_gate', {
                'muted': result.muted, 'found': getattr(result, 'found', None),
                'rerouted': getattr(result, 'rerouted', False),
                'write_protected': protected,
                'reason': result.text, 'scene': self._observed.get('scene'),
            })
        live_rebuild = (self._match_latched and self._fight_opened and
                        not self._match_end_seen and not self._round_transition_seen)
        if mute and result.muted:
            if getattr(result, 'rerouted', False) and live_rebuild and (
                    self._ax_rebuild_match_candidate or
                                 (self._native_zero_confirmed and self._current_file and
                                  self._last_native_phase == 4)):
                # A restart may remove the old AX pair altogether. Once the
                # replacement pair is verified, advance exactly once even
                # though the old file was stopped during that gap.
                self._match_serial += 1
                self._pending_match_boundary = False
                self._interrupted_track = None
            self._ax_rebuild_match_candidate = False
        elif not mute or not live_rebuild:
            self._ax_rebuild_match_candidate = False
        if mute and not result.muted and self._native_zero_confirmed and self._current_file:
            self._ax_rebuild_match_candidate = live_rebuild
        if mute and not result.muted:
            # A new native stream can appear after the old AX pair vanishes.
            # Poll promptly while waiting for its routes, including menus and
            # result screens where no match is latched.
            self._mute_urgent_until = now + 1.0
        self._native_zero_confirmed = result.muted if mute else False
        self._transition_ready = result.muted if mute else result.ready
        self._mute_status = result.text
        if mute and not result.muted and self._current_file:
            # Don't layer Continuo over a native soundtrack until both
            # routes have been readback-verified as disconnected.
            self._interrupted_track = (self._current_owner, self._current_file)
            self._stop_audio(reason='ax_unverified')
            self._current_owner = None

    def retry_mute(self) -> str:
        with self._lock:
            if not self.enabled:
                return 'Enable Custom or No BGM first.'
            self._maintain_bgm(force=True)
            return self._mute_status

    def bgm_diagnostic(self) -> str:
        """Read-only support report for a Dolphin installation with no AX pair."""
        with self._lock:
            state = self.status()
            lines = [f'Scene: {state["scene"]}; stage: {state["stage"]}',
                     f'Playlist: {state["playlist"]}; tracks: {state["available_tracks"]}',
                     f'Output: {state["output"]}; enabled: {state["enabled"]}',
                     f'Gate: {state["bgm_status"]}', '']
            return '\n'.join(lines) + self._ax_gate.diagnostic(self._backend())

    def dock_active(self) -> bool:
        """Cheap badge state; drawing must not wait for AX or file I/O."""
        return bool(self._desired_enabled)

    def recording_context(self) -> dict:
        """Return cached identity for a HUD recording without another RAM read."""
        with self._lock:
            evidence = self._observed.get('evidence') or {}
            return {
                'scene': self._observed.get('scene'),
                'stage': self._observed.get('stage'),
                'phase': evidence.get('native_phase'),
                'music_owner': self._current_owner,
            }

    def _accept_observation(self, reading: dict) -> None:
        """Apply the current match ID at phase 4 and the winner SSD at result.

        Loader stage writes during KO/results belong to the next match and
        cannot replace the music until select or the next live phase 4.
        """
        scene = reading.get('scene', 'unknown')
        ev = reading.get('evidence') or {}
        phase = ev.get('native_phase')
        stage = reading.get('stage')
        accepted = dict(reading)
        accepted['evidence'] = dict(ev)
        if (scene == 'menu' and not ev.get('menu_asset_verified') and
                self._observed.get('scene') == 'character_select'):
            # P1 -> P2 can briefly expose a cached type-8 menu root while the
            # character-select sequence is being rebuilt. Do not advance its
            # playlist on that weak cue alone.
            self._menu_root_reads += 1
            if self._menu_root_reads < MENU_ROOT_RETURN_SAMPLES:
                scene = accepted['scene'] = 'character_select'
        else:
            self._menu_root_reads = 0
        if (scene == 'character_select' and
                self._observed.get('scene') == 'stage_select' and
                not ev.get('character_ui_live')):
            # The chrsel asset is already cached on stage select. Wait for the
            # character UI state before changing playlists.
            scene = accepted['scene'] = 'stage_select'
        if scene == 'stage_select' and self._observed.get('scene') == 'character_select':
            # Allow an intentional return to stage select, but do not treat a
            # recycled stage object during the VS transition as that return.
            if not ev.get('stage_ui_live'):
                self._stage_select_candidate_since = None
            elif self._stage_select_candidate_since is None:
                self._stage_select_candidate_since = self.clock()
            if (not ev.get('stage_ui_live') or ev.get('character_ui_live') or
                    self.clock() - self._stage_select_candidate_since < STAGE_RETURN_CONFIRM_SECONDS):
                scene = accepted['scene'] = 'character_select'
        else:
            self._stage_select_candidate_since = None

        if (self._observed.get('scene') == 'character_select' and
                scene in ('loading', 'battle')):
            # P2 lock-in can expose the old stage result and battle pointer.
            # Those values do not prove that the next match has loaded.
            if (scene == 'loading' and phase == 0 and
                    not ev.get('select_asset') and stage is not None):
                confirmed = True
            elif scene == 'loading' and not ev.get('select_asset') and stage is not None:
                if self._loading_candidate_since is None:
                    self._loading_candidate_since = self.clock()
                confirmed = (self.clock() - self._loading_candidate_since >=
                             CHAR_TO_MATCH_CONFIRM_SECONDS)
            else:
                confirmed = bool(ev.get('both_points_idle'))
            if not confirmed:
                scene = accepted['scene'] = 'character_select'
        else:
            self._loading_candidate_since = None

        if self._match_latched and phase in WINNER_PHASES:
            self._match_end_seen = True
            self._opening_character = None
        if (self._match_latched and self._match_end_seen and
                not self._winner_music_ready and not self._pending_match_boundary):
            # Phase 9 also covers the winner's KO pose. Keep the match theme
            # until fighter slots vanish or the native battle object unloads.
            # Cached fighter allocations can outlive that object.
            if phase is None or (phase in WINNER_PHASES and not reading.get('fighters')):
                self._winner_unload_reads += 1
                self._winner_music_ready = self._winner_unload_reads >= WINNER_UNLOAD_SAMPLES
            else:
                self._winner_unload_reads = 0

        if scene in ('character_select', 'stage_select') and (
                not self._match_latched or self._fight_opened or self._match_end_seen):
            # A return to either select screen ends the old match even when
            # the short KO phase was missed by the sampler.
            was_in_match = self._match_latched
            self._match_latched = False
            self._fight_opened = False
            self._fight_point_reads = 0
            self._select_since_live = True
            self._pending_match_boundary = was_in_match or self._pending_match_boundary
            self._verified_live_stage = None
            self._verified_live_roster = {}
            self._opening_character = None
            self._winner_unload_reads = 0
            self._winner_music_ready = False
            self._menu_return_reads = 0
            self._menu_exit_pending = False
            self._unknown_match_reads = 0
        elif scene == 'battle' and phase == 4:
            # Select or KO followed by battle starts a fresh playlist cycle.
            # A new stage ID changes the owner directly, even without this cue.
            new_match = bool(self._pending_match_boundary or self._match_end_seen)
            if not self._match_latched or new_match:
                if new_match:
                    self._match_serial += 1
                self._match_latched = True
                self._fight_opened = False
                self._fight_point_reads = 0
                self._match_end_seen = False
                self._winner_unload_reads = 0
                self._winner_music_ready = False
                self._end_phase_reads = 0
                self._pending_match_boundary = False
                self._select_since_live = False
                self._verified_live_stage = stage
                self._verified_live_roster = dict(reading.get('fighters') or {})
            # A phase-4 reading may update the live stage without a sampled
            # select screen. Loader writes during win/results may not.
            self._verified_live_stage = stage
            self._verified_live_roster = dict(reading.get('fighters') or {})
            if ev.get('both_points_idle'):
                self._fight_opened = True
                self._round_transition_seen = False
            elif not self._fight_opened:
                # The opening neutral pose can be missed if either player is
                # already moving. Two stable, unique native point flags still
                # prove the entrance has ended and make tags observable.
                self._fight_point_reads = (self._fight_point_reads + 1
                                           if ev.get('both_points_live') else 0)
                if self._fight_point_reads >= 2:
                    self._fight_opened = True
                    self._round_transition_seen = False
            self._menu_return_reads = 0
            self._menu_exit_pending = False
            self._unknown_match_reads = 0
        elif self._match_latched:
            if self._match_end_seen and phase is not None and phase <= 3:
                # Don't require fighter structs or a `loading` scene label:
                # native loading can temporarily deallocate both teams.
                self._pending_match_boundary = True
                self._winner_music_ready = False
                self._winner_unload_reads = 0
                self._fight_opened = False
                self._verified_live_stage = stage
            elif (scene == 'menu' and ev.get('menu_object')
                  and phase is None and not reading.get('fighters')):
                self._menu_return_reads += 1
                self._menu_exit_pending = True
                self._unknown_match_reads = 0
                needed = (MENU_RETURN_SAMPLES if ev.get('menu_asset_verified')
                          else MENU_ROOT_RETURN_SAMPLES)
                if self._menu_return_reads >= needed:
                    # Loading a save state can skip KO entirely. A sustained,
                    # verified menu still releases the old match identity.
                    self._match_latched = False
                    self._fight_opened = False
                    self._fight_point_reads = 0
                    self._winner_unload_reads = 0
                    self._winner_music_ready = False
                    self._verified_live_stage = None
                    self._verified_live_roster = {}
                    self._opening_character = None
                    self._pending_match_boundary = True
                    self._select_since_live = False
                    self._menu_exit_pending = False
            elif (scene == 'unknown' and phase is None and not reading.get('fighters')
                  and not self._winner_music_ready):
                self._menu_return_reads = 0
                self._unknown_match_reads += 1
                if self._unknown_match_reads >= UNKNOWN_MATCH_RELEASE_SAMPLES:
                    # Without MEM2 a save-state jump to menus may have no
                    # readable scene signature. Stop the stale stage track;
                    # native audio is safer until a scene is identified.
                    self._match_latched = False
                    self._fight_opened = False
                    self._fight_point_reads = 0
                    self._winner_unload_reads = 0
                    self._winner_music_ready = False
                    self._verified_live_stage = None
                    self._verified_live_roster = {}
                    self._opening_character = None
                    self._pending_match_boundary = True
                    self._select_since_live = False
                    self._menu_exit_pending = False
            elif scene == 'unknown' and self._winner_music_ready:
                # Arcade result pages lose the battle object before they gain
                # a verified menu identity. The 12:22 recording showed this
                # unlabeled interval outlasting the generic unknown timeout,
                # which cut the winner song after less than a second.
                self._menu_return_reads = 0
                self._unknown_match_reads = 0
            else:
                self._menu_return_reads = 0
                self._unknown_match_reads = 0
                if scene in ('loading', 'battle') and phase is not None:
                    self._menu_exit_pending = False

        if self._match_latched:
            # The result slot can already contain the NEXT arena on the win
            # screen. Hold this match's verified ID until a select boundary
            # or a phase-4 reading confirms the next live fight.
            new_loading = self._pending_match_boundary and phase is not None and phase <= 3
            if new_loading:
                accepted['scene'] = 'loading'
            elif self._winner_music_ready:
                accepted['scene'] = 'winner'
            else:
                accepted['scene'] = 'battle' if self._fight_opened else 'loading'
            accepted['stage'] = stage if new_loading else self._verified_live_stage
            accepted['evidence']['stage_latched'] = phase != 4 or scene != 'battle'
            if phase != 4 or scene != 'battle':
                # KO point flags can flicker before unload. Do not mistake
                # those for a tag and change the fighter's music.
                ko_pose = self._match_end_seen and not self._winner_music_ready
                accepted['fighters'] = (dict(self._verified_live_roster) if ko_pose
                                        else dict(reading.get('fighters') or {}))
                accepted['points'] = (dict(self._observed.get('points') or {}) if ko_pose
                                      else dict(reading.get('points') or {}))
                accepted['evidence']['both_points_idle'] = False
        else:
            # A transient unverified scene must not promote the stage ID or
            # cached fighter structs to an actual match identity.
            if scene == 'battle_unverified':
                accepted['scene'] = 'unknown'
                accepted['stage'] = None
            if (accepted['scene'] == 'unknown' and
                    self._observed.get('scene') in
                    ('menu', 'character_select', 'stage_select')):
                # Menu objects can disappear briefly while pages load. Keep
                # the last verified song until another scene is identified;
                # an unreadable frame must not advance its playlist.
                accepted['scene'] = self._observed['scene']
            if accepted['scene'] in ('menu', 'character_select', 'stage_select', 'winner', 'unknown'):
                accepted['stage'] = None
        accepted['evidence']['boundary_pending'] = bool(self._pending_match_boundary)
        previous = self._observed
        if (previous.get('scene') == 'character_select' and
                accepted['scene'] == 'stage_select'):
            if self._current_owner == 'character_select':
                self._select_return_track = self._current_file
            elif self._interrupted_track and self._interrupted_track[0] == 'character_select':
                self._select_return_track = self._interrupted_track[1]
        elif accepted['scene'] in ('menu', 'loading', 'battle', 'winner'):
            self._select_return_track = None
        if (accepted['scene'] in ('loading', 'battle') and
                (accepted['scene'], accepted['stage']) !=
                (previous.get('scene'), previous.get('stage'))):
            self._prioritize_mute()
        self._observed = accepted
        self._has_initial_observation = True
        self._audio_wakeup.set()
        if (accepted.get('scene'), accepted.get('stage')) != (previous.get('scene'), previous.get('stage')):
            session_recorder.capture_event(self.clock(), 'music_scene', {
                'from': previous.get('scene'), 'to': accepted.get('scene'),
                'stage': accepted.get('stage'), 'match_serial': self._match_serial,
            })

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

    def ensure_audio_service(self) -> None:
        """Run mixer playback independently of the GUI render cadence."""
        if not self._allow_sampler:
            return  # tests with an injected clock drive tick() explicitly
        worker = self._audio_service
        if (worker is not None and worker.is_alive() and
                not self._audio_service_stop.is_set()):
            return
        with self._lock:
            worker = self._audio_service
            if (worker is not None and worker.is_alive() and
                    not self._audio_service_stop.is_set()):
                return
            if not (self.enabled or self._desired_enabled or self._ax_gate.needs_restore):
                return
            stop_event = threading.Event()
            self._audio_service_stop = stop_event
            self._audio_service = threading.Thread(
                target=self._audio_service_loop, args=(stop_event,), daemon=True,
                name='ContinuoMusicAudio')
            self._audio_service.start()

    def _audio_service_loop(self, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                self.tick()
            except Exception as ex:
                with self._lock:
                    self._message = f'Music service error: {ex!s}'
            with self._lock:
                if not (self.enabled or self._desired_enabled or self._ax_gate.needs_restore):
                    return
            self._audio_wakeup.wait(TRANSITION_AUDIO_INTERVAL)
            self._audio_wakeup.clear()

    def _observe_native_phase(self, phase: int | None) -> None:
        """Track KO, retry, and direct Training Restart phase boundaries.

        Called under the controller lock by the fast sampler. The slower scene
        sampler confirms the selected stage and fighter identity separately.
        """
        previous = self._last_native_phase
        if phase != previous:
            session_recorder.capture_event(self.clock(), 'native_phase', {
                'from': previous, 'to': phase, 'match_serial': self._match_serial,
                'scene': self._observed.get('scene'),
            })
        if phase == 4 and previous != 4:
            # The game can rebuild AX voices exactly as FIGHT opens. Prompt
            # the GUI thread to recheck the gate before the full scene scan.
            self._prioritize_mute()
            if (self._match_latched and self._fight_opened and
                    not self._match_end_seen and
                    self._restart_loading_reads >= RESTART_LOADING_SAMPLES):
                # Training Restart can go straight from live phase 4 back to
                # loading, skipping KO. The next phase-4 fight is a new song.
                self._pending_match_boundary = True
        if (self._match_latched and self._fight_opened and
                not self._match_end_seen and phase is not None and phase <= 3 and
                (previous == 4 or self._restart_loading_reads)):
            self._restart_loading_reads += 1
        elif phase is None or phase >= 4:
            self._restart_loading_reads = 0
        self._last_native_phase = phase
        if not self._match_latched:
            self._end_phase_reads = 0
            self._restart_loading_reads = 0
            return
        if phase in WINNER_PHASES:
            self._end_phase_reads += 1
            if self._end_phase_reads >= END_PHASE_SAMPLES:
                self._match_end_seen = True
            return
        if phase is not None and 5 <= phase <= 8:
            self._round_transition_seen = True
        self._end_phase_reads = 0
        if self._match_end_seen and phase is not None and phase <= 3:
            self._pending_match_boundary = True

    def _sample_loop(self, stop_event):
        # Two independent jobs: fast *native phase edges* and slower,
        # debounced *scene/identity scans*. Never debounce away a restart
        # because fighters or point flags changed between snapshots.
        next_identity_scan = float('-inf')
        next_mem2_prime = float('-inf')
        while not stop_event.wait(BOUNDARY_INTERVAL):
            try:
                io = self._backend()
                phase = _read_native_phase(io)
            except Exception:
                continue
            with self._lock:
                if not self.enabled:
                    continue
                self._observe_native_phase(phase)
            now = self.clock()
            if now < next_identity_scan:
                continue
            if now >= next_mem2_prime:
                next_mem2_prime = now + MEM2_PRIME_INTERVAL
                try:
                    info = getattr(io, 'mem2_latch_info', None)
                    prime = getattr(io, 'prime_mem2_latch', None)
                    if info is not None and prime is not None and not info().get('latched'):
                        prime()
                except Exception:
                    pass  # the scene scan can still use MEM1 evidence
            interval = (TRANSITION_SAMPLE_INTERVAL
                        if self._observed.get('scene') in
                        ('character_select', 'stage_select', 'loading') else SAMPLE_INTERVAL)
            next_identity_scan = now + interval
            try:
                reading = _read_environment(io)
            except Exception:
                continue
            evidence = reading.get('evidence') or {}
            session_recorder.capture_event(self.clock(), 'music_raw', {
                'scene': reading.get('scene'), 'phase': evidence.get('native_phase'),
                'stage': reading.get('stage'), 'raw_stage': evidence.get('raw_stage'),
                'stage_object': evidence.get('stage_object'),
                'stage_cursor': evidence.get('stage_cursor'),
                'stage_cursor_id': evidence.get('stage_cursor_id'),
                'stage_local_cursor_id': evidence.get('stage_local_cursor_id'),
                'stage_root_type': evidence.get('stage_root_type'),
                'select_asset': evidence.get('select_asset'),
                'select_scene_state': evidence.get('select_scene_state'),
                'character_ui_state': evidence.get('character_ui_state'),
                'character_ui_live': evidence.get('character_ui_live'),
                'select_id_a': evidence.get('select_id_a'),
                'select_id_b': evidence.get('select_id_b'),
                'menu_asset_id': evidence.get('menu_asset_id'),
                'fighter_count': evidence.get('fighter_count'),
                'both_points_live': evidence.get('both_points_live'),
                'points': dict(reading.get('points') or {}),
            })
            with self._lock:
                if not self.enabled:
                    continue
                # Scene stability must not depend on volatile fighter
                # points/actions, which change precisely when tags occur.
                raw = (reading['scene'], reading['stage'])
                if raw == self._seen_raw:
                    self._stable_count += 1
                else:
                    self._seen_raw, self._stable_count = raw, 1
                if self._stable_count >= STABLE_SCENE_SAMPLES:
                    self._accept_observation(reading)

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
            self._ax_rebuild_match_candidate = False
            self._mute_retries = 0
            self._last_mute_check = float('-inf')
            self._mute_urgent_until = float('-inf')
            self._entered_at = None
            self._transition_ready = False
            self._seen_raw = None
            self._stable_count = 0
            self._observed = {"scene": "unknown", "stage": None, "fighters": {}, "points": {}, "evidence": {}}
            self._interrupted_track = None
            self._select_return_track = None
            self._has_initial_observation = False
            self._match_latched = False
            self._fight_opened = False
            self._fight_point_reads = 0
            self._match_end_seen = False
            self._winner_unload_reads = 0
            self._winner_music_ready = False
            self._end_phase_reads = 0
            self._last_native_phase = None
            self._restart_loading_reads = 0
            self._round_transition_seen = False
            self._select_since_live = False
            self._match_serial = 0
            self._played_match_serial = 0
            self._tick_match_serial = 0
            self._verified_live_stage = None
            self._verified_live_roster = {}
            self._pending_match_boundary = False
            self._menu_return_reads = 0
            self._menu_exit_pending = False
            self._menu_root_reads = 0
            self._stage_select_candidate_since = None
            self._loading_candidate_since = None
            self._unknown_match_reads = 0
            self._locked_fighter = None
            self._in_battle = False
            self._last_point = {}
            self._tag_team_slots.clear()
            self._recent_point_actions.clear()
            self._last_fighter_music_change_at = float('-inf')
            self._opening_team_slots.clear()
            self._opening_character = None
            self._tag_armed = False
            self._tag_selected_character = None
            self._tag_selected_team = None
            self._tag_event_serial = 0
            self._played_event_serial = 0
            self._failed_playlist = None
            self._fade_started = None
            self._pending_scene_fade = False
            self._message = "Custom music armed; AX voice routing will follow scene playlists."
            self._start_sampler()
            self.ensure_audio_service()
            return True

    def stop(self):
        with self._lock:
            self.stop_loop_preview()
            if not self.enabled and not self._ax_gate.needs_restore:
                return
            self.enabled = False
            self._native_fallback = False
            self._requested_mute = False
            self._stop.set()
            self._audio_service_stop.set()
            self._audio_wakeup.set()
            self._stop_audio()
            restore = None
            self._pending_restore = None
            self.original_volume = None
            self._owns_mute = False
            self._native_zero_confirmed = False
            self._ax_rebuild_match_candidate = False
            self._transition_ready = False
            self._entered_at = None
            self._current_owner = None
            self._interrupted_track = None
            self._select_return_track = None
            self._locked_fighter = None
            self._in_battle = False
            self._match_latched = False
            self._fight_opened = False
            self._fight_point_reads = 0
            self._match_end_seen = False
            self._winner_unload_reads = 0
            self._winner_music_ready = False
            self._end_phase_reads = 0
            self._last_native_phase = None
            self._restart_loading_reads = 0
            self._round_transition_seen = False
            self._select_since_live = False
            self._verified_live_stage = None
            self._verified_live_roster = {}
            self._pending_match_boundary = False
            self._menu_return_reads = 0
            self._menu_exit_pending = False
            self._menu_root_reads = 0
            self._stage_select_candidate_since = None
            self._loading_candidate_since = None
            self._unknown_match_reads = 0
            self._last_point.clear()
            self._tag_team_slots.clear()
            self._recent_point_actions.clear()
            self._last_fighter_music_change_at = float('-inf')
            self._opening_team_slots.clear()
            self._opening_character = None
            self._tag_armed = False
            self._tag_selected_character = None
            self._tag_selected_team = None
            self._last_mute_check = float('-inf')
            self._mute_urgent_until = float('-inf')
            self._maintain_bgm(force=True)
            if self._ax_gate.needs_restore:
                # The native pair can disappear while a scene is changing.
                # Keep a worker alive to restore our routes when it returns.
                self.ensure_audio_service()
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
            self._audio_wakeup.set()

    def tick(self):
        """Host-side playback and bounded BGM verification on the audio service."""
        with self._lock:
            now = self.clock()
            if self._desired_enabled and not self.enabled and now - self._startup_last_attempt >= 2.0:
                self._startup_last_attempt = now
                self.start()
            if not self.enabled and self._ax_gate.needs_restore:
                self._maintain_bgm()
            urgent = self._requested_mute and now < self._mute_urgent_until
            active_match = self._requested_mute and self._match_latched
            interval = (TRANSITION_AUDIO_INTERVAL if urgent else
                        AX_MATCH_VERIFY_INTERVAL if active_match else AUDIO_INTERVAL)
            if now - self._last_audio < interval:
                return
            self._last_audio = now
            if not self.enabled:
                return

            if self._mode == 'auto' and self._match_latched and self._menu_exit_pending:
                # A menu return is visible, but the old match ID stays latched
                # until enough menu reads confirm it. Do not let that stale
                # stage or fighter owner keep playing during the gap.
                self._requested_mute = True
                self._maintain_bgm()
                if self._current_file and self._current_owner and (
                        self._current_owner.startswith(('stages/', 'characters/'))):
                    self._interrupted_track = (self._current_owner, self._current_file)
                    self._stop_audio(reason='menu_return_pending')
                    self._current_owner = None
                self._message = 'Waiting for menu music boundary'
                return

            battle = (self._mode == 'battle' or
                      (self._mode == 'auto' and self._observed.get('scene') == 'battle'))
            new_match_epoch = self._match_serial != self._tick_match_serial
            if new_match_epoch:
                self._tick_match_serial = self._match_serial
            new_battle = battle and (not self._in_battle or new_match_epoch)
            if battle != self._in_battle or new_match_epoch:
                if battle and self._opening_character is None:
                    self._opening_character = self._opening_fighter(self._observed)
                self._locked_fighter = None
                self._last_point.clear()
                self._tag_team_slots.clear()
                self._recent_point_actions.clear()
                if not battle:
                    self._opening_team_slots.clear()
                self._tag_selected_character = self._opening_character if battle else None
                self._tag_selected_team = self._slot[:2] if battle else None
                self._tag_armed = False
                if not battle and self._observed.get('scene') not in ('loading',):
                    self._opening_character = None
                self._in_battle = battle
            if battle:
                self._update_tag_owner(self._observed, entered_battle=new_battle)
            owner, tracks = self._context(self._observed)
            failed_playlist = False
            if tracks and self._failed_playlist is not None:
                fingerprint = (owner, self._playlist_fingerprint(tracks))
                failed_playlist = (fingerprint == self._failed_playlist and
                                   now - self._failed_playlist_at < FAILED_PLAYLIST_RETRY_SECONDS)
                if failed_playlist:
                    tracks = []  # keep native BGM until a file changes or retry is due
                else:
                    self._failed_playlist = None
            # No scene-based fade: post-KO phases / menus often share stale
            # data. Only an owner change, real tag, or new match can fade.
            # Native is the fallback whenever this actual scene/character
            # has no custom track. No song + no other fallback = native NOW.
            want_custom = self._output == 'custom' and bool(tracks)
            self._choose_native_fallback(self._output == 'custom' and not tracks)
            self._requested_mute = self._output == 'no_bgm' or want_custom
            self._maintain_bgm()
            stage_to_character = (owner == 'character_select' and
                                  self._current_owner == 'stage_select' and
                                  (self._observed.get('evidence') or {}).get('character_ui_live'))
            select_to_match = (self._observed.get('scene') == 'loading' and
                               self._observed.get('stage') is not None and
                               self._current_owner == 'character_select')
            if want_custom and (stage_to_character or select_to_match):
                # Both cues identify a new music context. Cut the old select
                # song before loading it instead of adding a 300 ms fade.
                self._stop_audio(reason='character_ui_active' if stage_to_character
                                 else 'match_stage_verified')
                self._current_owner = None
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
                reason = ('All tracks unreadable; retrying later' if failed_playlist
                          else f'No custom theme for {owner}')
                self._message = f'{reason}; {self._mute_status}'
                return
            if not self._native_zero_confirmed:
                # Without verified routing, native audio may still be heard.
                self._message = f'Waiting for isolated AX BGM gate: {self._mute_status}'
                return

            event_changed = (owner.startswith(('characters/', 'low_health/characters/')) and
                             self._source == 'fighter' and self._fighter_switch == 'tag'
                             and self._played_event_serial != self._tag_event_serial)
            tag_owner_changed = (
                self._observed.get('scene') == 'battle' and
                self._source == 'fighter' and self._fighter_switch == 'tag' and
                self._played_event_serial != self._tag_event_serial and
                (owner != self._current_owner or event_changed))
            match_changed = (self._played_match_serial < self._match_serial
                             and self._current_file is not None)
            if match_changed:
                # Restart/Retry has already crossed the new-match boundary.
                # The normal 300 ms fade would carry the old song into the
                # intro pose, so cut it before loading the next track.
                self._stop_audio(reason='new_match')
                self._current_owner = None
                self._interrupted_track = None
            elif tag_owner_changed and self._current_file:
                # The point fighter is verified. Switch on this audio wakeup;
                # a 300 ms fade makes tags sound late even with fast sampling.
                self._stop_audio(reason='tag')
                self._current_owner = None
            changed = (owner != self._current_owner or match_changed or
                       (event_changed and self._current_file is not None))
            if self._current_file and changed:
                self._begin_fade()
            if self._service_fade(now):
                return
            try:
                busy = bool(self._audio().music.get_busy())
            except Exception:
                busy = False
            if not changed and self._service_track_loop(owner, tracks, now, busy):
                return
            if changed or not busy:
                interrupted = self._interrupted_track
                start = (next((track for track in tracks if str(track) == interrupted[1]), None)
                         if interrupted and interrupted[0] == owner else None)
                if (start is None and owner == 'character_select' and
                        self._current_owner != owner and self._select_return_track):
                    start = next((track for track in tracks
                                  if str(track) == self._select_return_track), None)
                if start is None:
                    start = self._pick(owner, tracks)
                self._interrupted_track = None
                if owner == 'character_select':
                    self._select_return_track = None
                idx = tracks.index(start)
                failures = []
                attempted = []
                # A corrupted/unsupported MP3 must not strand an album. Try
                # each different file once, then fail open to native audio.
                for offset in range(len(tracks)):
                    song = tracks[(idx + offset) % len(tracks)]
                    attempted.append(song)
                    try:
                        self._audio().music.stop()
                        self._audio().music.load(str(song))
                        self._audio().music.set_volume(self._live_music_volume())
                        # Character select keeps its chosen song until a real
                        # scene boundary; menu albums still advance on end.
                        end_mode = self._end_mode_for_owner(owner)
                        loop_setting = (None if end_mode == 'playlist' else
                                        self._loop_points.get(self._track_identity(song)))
                        first_start = (loop_setting['start'] if loop_setting and
                                       loop_setting['first'] == 'loop' else 0.0)
                        loop_track = (end_mode == 'match' or owner == 'character_select')
                        self._play_music_segment(song, start=first_start,
                                                 loops=0 if loop_setting else -1 if loop_track else 0,
                                                 fade_ms=160)
                    except Exception as ex:
                        failures.append(f'{song.name}: {ex!s}')
                        continue
                    self._current_file = str(song)
                    session_recorder.capture_event(self.clock(), 'music_play', {
                        'owner': owner, 'track': self._track_identity(song),
                        'scene': self._observed.get('scene'),
                        'stage': self._observed.get('stage'),
                        'match_serial': self._match_serial,
                    })
                    self._consume_random_attempts(owner, attempted)
                    self._failed_playlist = None
                    self._current_owner = owner
                    self._played_event_serial = self._tag_event_serial
                    if (self._source == 'fighter' and self._fighter_switch == 'tag' and
                            self._observed.get('scene') in ('battle', 'loading')):
                        self._last_fighter_music_change_at = self.clock()
                    # A track started in the next prematch already belongs
                    # to that match. Do not advance it again at FIGHT.
                    self._played_match_serial = (self._match_serial + 1
                        if self._observed.get('scene') == 'loading' and
                           self._pending_match_boundary else self._match_serial)
                    self._last_by_owner[owner] = self._track_identity(song)
                    try:
                        self._save_json(self._history_file, {
                            'last_by_owner': self._last_by_owner,
                            'random_cycles': {
                                name: {'population': list(population),
                                       'remaining': self._random_remaining.get(name, [])}
                                for name, population in self._random_population.items()},
                        })
                    except OSError:
                        pass
                    label = (stage_label(int(owner.split('/')[1]))
                             if owner.startswith('stages/') and owner.split('/')[1].isdigit() else owner)
                    self._message = f'{label}: {song.name}'
                    if failures:
                        self._message += f' (skipped {len(failures)} unreadable track(s))'
                    break
                else:
                    self._current_owner = None
                    self._current_file = None
                    self._failed_playlist = (owner, self._playlist_fingerprint(tracks))
                    self._failed_playlist_at = now
                    self._choose_native_fallback(True)
                    self._maintain_bgm(force=True)
                    self._message = f'Could not play any of {len(tracks)} tracks for {owner}: {failures[0]}'
                    self._last_audio = now + 1.0
            else:
                if owner == 'character_select':
                    self._select_return_track = None
                try:
                    self._audio().music.set_volume(self._live_music_volume())
                except Exception:
                    pass

    def status(self) -> dict:
        with self._lock:
            # UI readout must not lock the next match's fighter/song merely
            # because the settings window happened to refresh first.
            saved_lock, saved_points, saved_opener = (self._locked_fighter,
                                                       dict(self._last_point),
                                                       self._opening_character)
            try:
                playlist, available = self._context(self._observed)
            finally:
                self._locked_fighter, self._last_point = saved_lock, saved_points
                self._opening_character = saved_opener
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
                "stage_identity": (f"current:{self._observed['stage']}" if self._observed.get('stage') is not None
                                   else "unverified" if self._match_latched else "not in match"),
                "raw_stage": self._observed.get('evidence', {}).get('raw_stage'),
                "boundary_pending": self._pending_match_boundary,
                "menu_signature_verified": self._observed.get('evidence', {}).get('menu_asset_verified', False),
                "menu_asset_id": self._observed.get('evidence', {}).get('menu_asset_id'),
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
                "stage_end_mode": self._stage_end_mode,
                "fighter_end_mode": self._fighter_end_mode,
                "fighter_switch": self._fighter_switch,
                "tag_scope": self._tag_scope,
                "low_health_enabled": self._low_health_enabled,
                "low_health_threshold": self._low_health_threshold,
                "ignored_point_actions": sorted(self._ignored_point_actions),
                "fighter_change_cooldown": self._fighter_change_cooldown,
                "points": dict(self._observed.get('points', {})),
                "opening_fighter": self._opening_character,
                "tag_tracking_armed": self._tag_armed,
                "opening_team_slots": dict(self._opening_team_slots),
                "both_points_idle": bool(self._observed.get('evidence', {}).get('both_points_idle')),
                "both_points_live": bool(self._observed.get('evidence', {}).get('both_points_live')),
                "music_owner_team": self._tag_selected_team,
                "music_owner_fighter": self._tag_selected_character,
                "tag_event": self._tag_event_serial,
                "fading": self._fade_started is not None,
                "match_serial": self._match_serial,
                "match_live": self._fight_opened,
                "match_identity_latched": self._match_latched,
                "match_end_seen": self._match_end_seen,
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

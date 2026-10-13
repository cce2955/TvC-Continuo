"""Readable library for TvC Ultimate All-Stars' identified SSD music streams.

The source mappings below are taken from KuroRaiden's
``tools/continuo/music/import_music.py`` resource table. Catalog membership
is not proof of automatic runtime screen detection in stock Dolphin.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MusicResource:
    key: str          # Disc basename, without .ssd
    label: str        # Player-facing folder label
    category: str     # stages, menus, extras
    dsp_entry: int    # The game's SRT DSP resource entry

    @property
    def folder(self) -> str:
        return f'{self.category}/{self.label}'


# Character music and stage ID -> filename mapping are separate concerns.
_NORMAL = [
    ('Wasteland', 'stage_01'),
    ("Yatterman's Workshop", 'stage_02'),
    ('Washinkyo, Amehon', 'stage_03'),
    ('Galactor Base', 'stage_04'),
    ('Burning Wasteland', 'stage_05'),
    ('Les Vagas', 'stage_06'),
    ('Daigo Temple (Cherry Blossoms)', 'stage_07'),
    ('Gesellschaft (Clear Skies)', 'stage_08'),
    ('Aensland Castle', 'stage_09'),
    ('Uncharted Region of China', 'stage_10'),
    ('Daigo Temple (Moonlight)', 'stage_11'),
    ('Gesellschaft (Storm)', 'stage_12'),
    ('Orbital Ring Systems Cargo Bay', 'stage_13'),
    ('Willamette Parkview Mall', 'stage_14'),
]
_RESOURCES = [MusicResource(resource, label, 'stages', 29+i)
              for i, (label, resource) in enumerate(_NORMAL)]
_RESOURCES.append(MusicResource('stage_17', 'Training Stage', 'stages', 44))

# The Yami battle soundtracks are s_bgm streams, not stage_15/16 assets.
_RESOURCES.extend([
    MusicResource('s_bgm_32', 'Yami - First Form', 'stages', 14),
    MusicResource('s_bgm_33', 'Yami - Second Form', 'stages', 15),
    MusicResource('s_bgm_34', 'Yami - Third Form', 'stages', 16),
])

_OTHER = [
    ('s_bgm_19', 'Where the Wind Blows', 'extras', 1),
    ('s_bgm_20', 'Ultimate All-Shooters - Title', 'extras', 2),
    ('s_bgm_21', 'Ultimate All-Shooters - Stage', 'extras', 3),
    ('s_bgm_22', 'Ultimate All-Shooters - Boss', 'extras', 4),
    ('s_bgm_23', 'Wi-Fi Mode', 'menus', 5),
    ('s_bgm_24', 'Character Select', 'menus', 6),
    ('s_bgm_25', 'Stage Select', 'menus', 7),
    ('s_bgm_26', 'Winner Screen', 'menus', 8),
    ('s_bgm_27', 'Continue Screen', 'menus', 9),
    ('s_bgm_28', 'VS Screen (Pre-Battle)', 'menus', 10),
    ('s_bgm_29', 'Game Over', 'menus', 11),
    ('s_bgm_30', 'Here Comes a New Hero', 'extras', 12),
    ('s_bgm_31', 'Title Screen', 'menus', 13),
    ('s_bgm_35', 'Can Can (Unused)', 'extras', 17),
    ('s_bgm_36', 'Secret Factor', 'extras', 18),
    ('s_bgm_37', 'Ending 1', 'extras', 19),
    ('s_bgm_38', 'Ending 2', 'extras', 20),
    ('s_bgm_39', 'Ending 3', 'extras', 21),
    ('s_bgm_40', 'Ending 4', 'extras', 22),
    ('s_bgm_41', 'Secret Menu', 'menus', 23),
    ('s_bgm_42', 'Main Menu', 'menus', 24),
    ('s_bgm_43', 'Extra Menu', 'menus', 25),
    ('s_bgm_44', 'Extra Menu 2', 'menus', 26),
    ('s_bgm_45', 'The Can-Can Dance (Staff Roll)', 'extras', 27),
    ('s_bgm_46', 'Ranking', 'menus', 28),
    ('s_bgm_47', 'Unlock SFX', 'extras', 29),
]
_RESOURCES.extend(MusicResource(*item) for item in _OTHER)
RESOURCES = tuple(_RESOURCES)
BY_KEY = {item.key: item for item in RESOURCES}

# Only scenes with confirmed detection in the current Continuo reader can
# be selected automatically. Other resources are reachable through the
# explicit Advanced > SSD resource control until verified runtime probes exist.
AUTO_SCENES = {
    'menu': 's_bgm_42',
    'character_select': 's_bgm_24',
    'stage_select': 's_bgm_25',
}
STAGE_ID_RESOURCE = {n: f'stage_{n:02d}' for n in range(1, 15)}
STAGE_ID_RESOURCE[15] = 'stage_17'


def resource_label(key: str | None) -> str:
    item = BY_KEY.get(key or '')
    return item.label if item else 'Auto (detected scenes)'


def resource_choices() -> list[tuple[str, str]]:
    """(readable display, stable key) for the music-window selector."""
    return [('Auto (detected scenes)', '')] + [
        (f'{item.label}  [{item.key}.ssd]', item.key)
        for item in RESOURCES
    ]

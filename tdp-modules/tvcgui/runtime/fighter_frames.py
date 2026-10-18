"""One bulk fighter read shared by the HUD snapshot and its enrichments."""
from __future__ import annotations

from tvcgui.core.constants import SLOTS
from tvcgui.features.combat.meter import METER_CACHE
from tvcgui.platform.dolphin import addr_in_ram, prime_mem2_latch, rbytes, rd32
from tvcgui.tools.scanners.fighter_state import read_fighter
from tvcgui.tools.scanners.fighter_resolver import pick_posy_off_no_jump

GIANT_IDS = {11, 22}


def resolve_bases(last_base_by_ptr: dict, y_off_by_base: dict) -> list:
    """Resolve four fighter slots and refresh position offsets on pointer churn."""
    resolved = []
    for slotname, ptr_addr, teamtag in SLOTS:
        raw_base = rd32(ptr_addr)
        base = raw_base if raw_base is not None and addr_in_ram(raw_base) else None
        changed = base is not None and last_base_by_ptr.get(ptr_addr) != base
        if base:
            try:
                prime_mem2_latch()
            except Exception:
                pass
        if changed:
            last_base_by_ptr[ptr_addr] = base
            METER_CACHE.drop(base)
            y_off_by_base[base] = pick_posy_off_no_jump(base)
        resolved.append((slotname, teamtag, base))
    return resolved


def compute_team_giant_solo(snaps: dict) -> tuple[bool, bool]:
    """Giant fighters reuse one base for both character slots."""
    def team_solo(prefix: str) -> bool:
        c1 = snaps.get(f"{prefix}-C1")
        c2 = snaps.get(f"{prefix}-C2")
        if not c1 or (c1.get("id") or 0) not in GIANT_IDS:
            return False
        if not c2:
            return True
        b1, b2 = c1.get("base"), c2.get("base")
        return isinstance(b1, int) and isinstance(b2, int) and b1 == b2

    return team_solo("P1"), team_solo("P2")


def read_fighter_frame(base: int, y_offset: int, block_size: int):
    """Return (snapshot, block), keeping both views on the same RAM sample.

    A failed bulk read falls back to the parser's individual reads. The HUD
    still receives ``None`` for an invalid fighter, as it did before.
    """
    try:
        block = rbytes(base, block_size)
        snapshot = read_fighter(base, y_offset, block=block)
    except Exception as error:
        print(f"[fighter frame] read failed for base=0x{base:08X}: {error!r}")
        return None, None
    return snapshot or None, block

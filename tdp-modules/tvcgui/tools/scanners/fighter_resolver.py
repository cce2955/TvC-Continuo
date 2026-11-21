"""Resolve fighter slots and sample candidate vertical-position offsets."""

import math
import time

from tvcgui.platform.dolphin import rd32, rdf32, addr_in_ram
from tvcgui.core.constants import (
    OFF_MAX_HP, OFF_CUR_HP, OFF_AUX_HP,
    POSX_OFF, POSY_CANDS,
    BAD_PTRS, INDIR_PROBES, LAST_GOOD_TTL,
)

def looks_like_hp(maxhp, curhp, auxhp):
    """Apply the resolver's permissive max-HP sanity check.

    Current and auxiliary HP stay in the signature for existing callers but
    do not gate identity: both can change during loading and special states.
    """
    if maxhp is None:
        return False
    if maxhp <= 0 or maxhp > 80_000:
        return False
    return True


def _variance(vals):
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / n
    return sum((v - m) * (v - m) for v in vals) / (n - 1)


def _slope(vals):
    if len(vals) < 2:
        return 0.0
    return (vals[-1] - vals[0]) / (len(vals) - 1)


def _corr(a, b):
    n = len(a)
    if n < 2 or n != len(b):
        return 0.0
    ma = sum(a) / n
    mb = sum(b) / n
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) * (x - ma) for x in a))
    db = math.sqrt(sum((y - mb) * (y - mb) for y in b))
    if da == 0 or db == 0:
        return 0.0
    return num / (da * db)


def pick_posy_off_no_jump(base):
    """Choose a candidate Y offset using approximately one second of samples.

    The score favors stability relative to X; it does not prove that the
    selected offset represents Y for every character or animation.
    """
    xs = []
    ys = {off: [] for off in POSY_CANDS}
    end = time.time() + 1.0
    while time.time() < end:
        x = rdf32(base + POSX_OFF)
        xs.append(x if x is not None else 0.0)
        for off in POSY_CANDS:
            y = rdf32(base + off)
            ys[off].append(y if y is not None else 0.0)
        time.sleep(1.0 / 120.0)

    if len(xs) < 10:
        return 0xF4  # fallback

    best_score = None
    best_off = None
    for off, series in ys.items():
        if len(series) < 10:
            continue
        s = abs(_slope(series))
        v = _variance(series)
        r = abs(_corr(series, xs))
        score = (0.6 * (1 / (1 + v))) + (0.3 * (1 / (1 + s))) + (0.1 * (1 / (1 + r)))
        if best_score is None or score > best_score:
            best_score = score
            best_off = off

    return best_off or 0xF4


class SlotResolver:
    """Resolve slot pointers, retaining recent candidates for continuity."""

    def __init__(self):
        # slot_addr -> (base, ttl_expiration)
        self.last_good = {}

    def _probe(self, slot_val):
        # Try following known indirections.
        for off in INDIR_PROBES:
            a = rd32(slot_val + off)
            if addr_in_ram(a) and a not in BAD_PTRS:
                mh = rd32(a + OFF_MAX_HP)
                ch = rd32(a + OFF_CUR_HP)
                ax = rd32(a + OFF_AUX_HP)
                if looks_like_hp(mh, ch, ax):
                    return a
        return None

    def resolve_base(self, slot_addr):
        """Return a fighter candidate and whether it was freshly validated.

        A cached candidate can be returned briefly when the slot is unreadable;
        callers that need current identity must validate the pointer again.
        """
        now = time.time()
        s = rd32(slot_addr)

        # If pointer is junk, maybe reuse last good
        if not addr_in_ram(s) or s in BAD_PTRS:
            lg = self.last_good.get(slot_addr)
            if lg and now < lg[1]:
                return lg[0], False
            return None, False

        # Check if s looks like a fighter struct
        mh = rd32(s + OFF_MAX_HP)
        ch = rd32(s + OFF_CUR_HP)
        ax = rd32(s + OFF_AUX_HP)
        if looks_like_hp(mh, ch, ax):
            self.last_good[slot_addr] = (s, now + LAST_GOOD_TTL)
            return s, True

        # Try indirect children
        a = self._probe(s)
        if a:
            self.last_good[slot_addr] = (a, now + LAST_GOOD_TTL)
            return a, True

        # Fall back on cache ttl if the module can't confirm new
        lg = self.last_good.get(slot_addr)
        if lg and now < lg[1]:
            return lg[0], False

        return None, False


RESOLVER = SlotResolver()

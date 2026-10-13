"""TvC AXWii *local output* gate. No DOL edits and no master-volume writes.

The two Wii AX music streams in six captured TvC MEM1 snapshots have unique
complementary stream ranges and mixer routes. A live pair is required before
we touch even one byte. We only flip the low *routing* byte at PB+0x0f:
0x0b -> 0x00, preserving links, playback position, stream state and SFX.

This is experimental. Dolphin/netplay synchronization and all track lifetimes
have not been verified in game. Do not bypass Continuo's write quarantine.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

POOL_START = 0x804A0020
POOL_COUNT = 51
STRIDE = 0x140
POOL_SIZE = POOL_COUNT * STRIDE

# Confirmed in all six TvC captures (sound menu and two battle captures).
# Address fields are Wii DSP sample/nibble positions, *not* MEM1 pointers.
BGM_IDENTITIES = {
    'left': (0x227A2B82, 0x22802B7F, 0x04210000),
    'right': (0x22802B82, 0x22862B7F, 0x08420000),
}
ORIGINAL_ROUTE = 0x0B
OFF = 0x00


def _u16(buf, at):
    return struct.unpack_from('>H', buf, at)[0]


def _u32(buf, at):
    return struct.unpack_from('>I', buf, at)[0]


@dataclass(frozen=True)
class Voice:
    role: str
    address: int
    route: int


@dataclass(frozen=True)
class GateResult:
    muted: bool
    ready: bool
    text: str
    found: int = 0


class AxBgmGate:
    def __init__(self):
        self._owned_roles: set[str] = set()
        self._last_result = GateResult(False, False, 'AX gate has not sampled music')

    @property
    def needs_restore(self):
        return bool(self._owned_roles)

    @property
    def result(self):
        return self._last_result

    @staticmethod
    def locate(buf: bytes) -> dict[str, Voice]:
        if len(buf) != POOL_SIZE:
            return {}
        matched: dict[str, Voice] = {}
        for slot in range(POOL_COUNT):
            off = slot * STRIDE
            address = POOL_START + off
            # Protect linked-list topology and avoid mistaking data for a PB.
            next_addr = _u32(buf, off)
            if (_u32(buf, off + 4) != address or
                    next_addr not in (0, address + STRIDE) or
                    _u16(buf, off + 0x10) != 1 or
                    _u16(buf, off + 0x12) != 1 or
                    _u16(buf, off + 0x70) != 0):
                continue
            ctrl = _u32(buf, off + 0x0C)
            route = ctrl & 0xff
            if route not in (OFF, ORIGINAL_ROUTE):
                continue
            loop_addr = _u32(buf, off + 0x72)
            end_addr = _u32(buf, off + 0x76)
            for role, (expected_loop, expected_end, expected_ctrl) in BGM_IDENTITIES.items():
                if (loop_addr, end_addr, ctrl & 0xffffff00) != (expected_loop, expected_end, expected_ctrl):
                    continue
                if role in matched:
                    return {}  # Ambiguous / duplicate BGM voice: never write.
                matched[role] = Voice(role, address, route)
        return matched

    def apply(self, io, *, mute: bool, write_protected: bool = False) -> GateResult:
        try:
            pool = io.rbytes(POOL_START, POOL_SIZE)
        except Exception:
            pool = None
        if pool is None or len(pool) != POOL_SIZE:
            return self._record(False, False, 'AX voice pool unavailable; no writes')
        found = self.locate(pool)
        if set(found) != set(BGM_IDENTITIES):
            return self._record(False, False, f'AX BGM pair not verified ({len(found)}/2); no writes', len(found))
        roles = tuple(BGM_IDENTITIES)
        if mute:
            changes = [(role, found[role]) for role in roles if found[role].route != OFF]
        else:
            # Only restore streams that *this instance* muted, not any
            # unrelated audio route another tool might intentionally suppress.
            changes = [(role, found[role]) for role in roles
                       if role in self._owned_roles and found[role].route == OFF]
        if changes and write_protected:
            return self._record(False, False, 'AX writes blocked by character-select quarantine', 2)
        # Revalidate all identities immediately before a write, not just the
        # first cached scan. Stop if one voice is recycled during sampling.
        if changes:
            try:
                fresh = io.rbytes(POOL_START, POOL_SIZE)
            except Exception:
                fresh = None
            recent = self.locate(fresh) if fresh else {}
            if (set(recent) != set(found) or
                    any(recent[role] != voice for role, voice in found.items())):
                return self._record(False, False, 'AX voice ownership changed; retrying, no writes', len(recent))
            for role, voice in changes:
                at = voice.address + 0x0f
                value = OFF if mute else ORIGINAL_ROUTE
                try:
                    ok = io.wbytes(at, bytes([value]))
                except Exception:
                    ok = False
                if ok is False:
                    return self._record(False, False, 'AX route write failed; retrying safely', 2)
                if mute:
                    self._owned_roles.add(role)
                else:
                    self._owned_roles.discard(role)
        # Readback is necessary because a backend may report success even if
        # a real Dolphin process immediately overwrites this mixer control.
        try:
            verify = self.locate(io.rbytes(POOL_START, POOL_SIZE))
        except Exception:
            verify = {}
        if set(verify) != set(BGM_IDENTITIES):
            return self._record(False, False, 'AX pair changed during verification; retry', len(verify))
        if mute:
            success = all(verify[r].route == OFF for r in roles)
            return self._record(success, success,
                                'AX BGM routes OFF (readback verified)' if success else
                                'AX route reassert pending; game may rewrite the controls', 2)
        success = all(verify[r].route == ORIGINAL_ROUTE for r in roles)
        if success:
            self._owned_roles.clear()
        return self._record(False, success,
                            'Native AX BGM routes restored immediately' if success else
                            'Native AX route restoration pending', 2)

    def _record(self, muted, ready, text, found=0):
        self._last_result = GateResult(muted, ready, text, found)
        return self._last_result

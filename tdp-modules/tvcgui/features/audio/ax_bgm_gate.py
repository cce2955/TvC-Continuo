"""TvC AXWii *local output* gate. No DOL edits and no master-volume writes.

The two Wii AX music streams in six captured TvC MEM1 snapshots have unique
complementary stream ranges and mixer routes. A live pair is required before
we touch even one byte. A shifted stream layout is accepted only when the
same two controls still form one unique adjacent, equal-length pair.
We only flip the low *routing* byte at PB+0x0f:
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
# The tester's October 17 AX inspection showed both exact stream ranges with
# the two canonical channel-control masks combined in each PB. Accept this
# form only on the *exact* known ranges; alternate ranges still require the
# individually identified controls below.
COMBINED_EXACT_CONTROL = 0x0C630000
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
    rerouted: bool = False


class AxBgmGate:
    def __init__(self):
        self._owned_roles: set[str] = set()
        self._explicit_native_restore = False
        self._last_result = GateResult(False, False, 'AX gate has not sampled music')

    @property
    def needs_restore(self):
        return bool(self._owned_roles or self._explicit_native_restore)

    def request_native_restore(self) -> None:
        """An explicit Native selection may recover a verified pair left OFF.

        The old controller's ownership record can be lost after a restart.
        This request still requires both exact BGM identities and readback.
        """
        self._explicit_native_restore = True

    @property
    def result(self):
        return self._last_result

    @staticmethod
    def diagnostic(io) -> str:
        """Read-only AX pool report for an installation whose BGM pair differs.

        Keep the muting criteria in locate() strict. This report exposes only
        the voice fields needed to compare another game's AX layout.
        """
        try:
            pool = io.rbytes(POOL_START, POOL_SIZE)
        except Exception as exc:
            return f'AX pool read failed: {exc!s}'
        if pool is None or len(pool) != POOL_SIZE:
            return f'AX pool unavailable: {len(pool) if pool else 0}/{POOL_SIZE} bytes'
        exact = AxBgmGate._locate_exact(pool)
        verified = AxBgmGate.locate(pool)
        lines = [f'AX pool: {POOL_SIZE} bytes at 0x{POOL_START:08X}',
                 f'Canonical BGM identities: {len(exact)}/2; verified pair: {len(verified)}/2',
                 'Slot  self  link  on  ctrl        loop        end         route']
        candidates = []
        nonzero = 0
        known_controls = {identity[2] for identity in BGM_IDENTITIES.values()}
        known_controls.add(COMBINED_EXACT_CONTROL)
        for slot in range(POOL_COUNT):
            off = slot * STRIDE
            address = POOL_START + off
            next_addr = _u32(pool, off)
            self_ok = _u32(pool, off + 4) == address
            link_ok = next_addr in (0, address + STRIDE)
            on = _u16(pool, off + 0x10) == 1 and _u16(pool, off + 0x12) == 1
            ctrl = _u32(pool, off + 0x0C)
            loop = _u32(pool, off + 0x72)
            end = _u32(pool, off + 0x76)
            if any((next_addr, _u32(pool, off + 4), ctrl, loop, end)):
                nonzero += 1
            structural = self_ok and link_ok and on and _u16(pool, off + 0x70) == 0
            if structural or ctrl & 0xFFFFFF00 in known_controls:
                candidates.append(
                    f'{slot:02d}    {int(self_ok)}     {int(link_ok)}     {int(on)}   '
                    f'{ctrl:08X}    {loop:08X}    {end:08X}    {ctrl & 0xFF:02X}'
                    + ('' if structural else '  [structure differs]'))
        lines.insert(2, f'Nonempty slots: {nonzero}/{POOL_COUNT}; candidates: {len(candidates)}')
        lines.extend(candidates or ['No structurally valid AX voices at this address.'])
        return '\n'.join(lines)

    @staticmethod
    def _locate_exact(buf: bytes) -> dict[str, Voice]:
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
                if ((loop_addr, end_addr) != (expected_loop, expected_end) or
                        (ctrl & 0xffffff00) not in (expected_ctrl, COMBINED_EXACT_CONTROL)):
                    continue
                if role in matched:
                    return {}  # Ambiguous / duplicate BGM voice: never write.
                matched[role] = Voice(role, address, route)
        return matched

    @classmethod
    def locate(cls, buf: bytes) -> dict[str, Voice]:
        """Locate the known pair or a unique pair with alternate DSP offsets.

        The alternate path retains all PB topology, activation, control and
        route checks. Both channels must cover equally sized, consecutive
        sample ranges. Any duplicate or ambiguous channel rejects the pair.
        """
        exact = cls._locate_exact(buf)
        if set(exact) == set(BGM_IDENTITIES):
            return exact
        if len(buf) != POOL_SIZE:
            return {}
        candidates: dict[str, list[tuple[Voice, int, int]]] = {
            role: [] for role in BGM_IDENTITIES}
        for slot in range(POOL_COUNT):
            off = slot * STRIDE
            address = POOL_START + off
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
            loop, end = _u32(buf, off + 0x72), _u32(buf, off + 0x76)
            if loop == 0 or end <= loop:
                continue
            for role, (_expected_loop, _expected_end, expected_ctrl) in BGM_IDENTITIES.items():
                if ctrl & 0xffffff00 == expected_ctrl:
                    candidates[role].append((Voice(role, address, route), loop, end))
        if any(len(found) != 1 for found in candidates.values()):
            return {}
        left, l_start, l_end = candidates['left'][0]
        right, r_start, r_end = candidates['right'][0]
        if (left.address == right.address or
                r_start != l_end + 3 or
                l_end - l_start != r_end - r_start):
            return {}
        return {'left': left, 'right': right}

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
                       if (role in self._owned_roles or self._explicit_native_restore)
                       and found[role].route == OFF]
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
                                'AX route reassert pending; game may rewrite the controls', 2,
                                rerouted=success and bool(changes))
        success = all(verify[r].route == ORIGINAL_ROUTE for r in roles)
        if success:
            self._owned_roles.clear()
            self._explicit_native_restore = False
        return self._record(False, success,
                            'Native AX BGM routes restored immediately' if success else
                            'Native AX route restoration pending', 2)

    def _record(self, muted, ready, text, found=0, *, rerouted=False):
        self._last_result = GateResult(muted, ready, text, found, rerouted)
        return self._last_result

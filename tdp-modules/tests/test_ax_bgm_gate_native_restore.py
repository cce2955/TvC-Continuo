"""Explicit Native selection can recover the two verified AX BGM routes."""
import struct
import unittest

from tvcgui.features.audio.ax_bgm_gate import (
    AxBgmGate, BGM_IDENTITIES, COMBINED_EXACT_CONTROL, ORIGINAL_ROUTE,
    POOL_SIZE, POOL_START, STRIDE,
)


class PoolIO:
    def __init__(self):
        self.pool = bytearray(POOL_SIZE)
        self.writes = []
        for slot, (_role, (loop, end, ctrl)) in enumerate(BGM_IDENTITIES.items()):
            offset = slot * STRIDE
            struct.pack_into('>I', self.pool, offset + 4, POOL_START + offset)
            struct.pack_into('>I', self.pool, offset + 0x0C, ctrl)
            struct.pack_into('>H', self.pool, offset + 0x10, 1)
            struct.pack_into('>H', self.pool, offset + 0x12, 1)
            struct.pack_into('>I', self.pool, offset + 0x72, loop)
            struct.pack_into('>I', self.pool, offset + 0x76, end)

    def rbytes(self, address, size):
        assert (address, size) == (POOL_START, POOL_SIZE)
        return bytes(self.pool)

    def wbytes(self, address, data):
        self.writes.append((address, bytes(data)))
        self.pool[address - POOL_START:address - POOL_START + len(data)] = data
        return True


class AxNativeRestoreTests(unittest.TestCase):
    def test_explicit_native_restores_verified_off_pair_without_old_ownership(self):
        io = PoolIO()
        gate = AxBgmGate()
        self.assertEqual(set(gate.locate(io.rbytes(POOL_START, POOL_SIZE))),
                         set(BGM_IDENTITIES))
        self.assertFalse(gate.apply(io, mute=False).ready)
        self.assertEqual(io.writes, [])

        gate.request_native_restore()
        result = gate.apply(io, mute=False)
        self.assertTrue(result.ready)
        self.assertFalse(gate.needs_restore)
        self.assertEqual(len(io.writes), 2)
        self.assertTrue(all(data == bytes([ORIGINAL_ROUTE]) for _, data in io.writes))

    def test_explicit_native_waits_for_both_verified_voices(self):
        io = PoolIO()
        gate = AxBgmGate()
        gate.request_native_restore()
        struct.pack_into('>I', io.pool, STRIDE + 0x72, 0)
        self.assertFalse(gate.apply(io, mute=False).ready)
        self.assertTrue(gate.needs_restore)
        self.assertEqual(io.writes, [])

    def test_diagnostic_reports_alternate_stream_without_relaxing_gate(self):
        io = PoolIO()
        struct.pack_into('>I', io.pool, 0x72, 0x12345678)
        gate = AxBgmGate()
        report = gate.diagnostic(io)
        self.assertIn('Canonical BGM identities: 1/2; verified pair: 0/2', report)
        self.assertIn('12345678', report)
        self.assertIn('Slot  self  link', report)
        self.assertFalse(gate.apply(io, mute=True).muted)
        self.assertEqual(io.writes, [])

    def test_unique_alternate_stream_pair_can_mute_and_restore(self):
        io = PoolIO()
        for slot in (0, 1):
            off = slot * STRIDE
            io.pool[off + 0x0F] = ORIGINAL_ROUTE
            for field in (0x72, 0x76):
                old = struct.unpack_from('>I', io.pool, off + field)[0]
                struct.pack_into('>I', io.pool, off + field, old + 0x100000)
        gate = AxBgmGate()
        self.assertEqual(set(gate.locate(bytes(io.pool))), set(BGM_IDENTITIES))
        self.assertTrue(gate.apply(io, mute=True).muted)
        self.assertEqual(len(io.writes), 2)
        self.assertTrue(gate.apply(io, mute=False).ready)
        self.assertEqual(len(io.writes), 4)

    def test_ambiguous_alternate_pair_never_writes(self):
        io = PoolIO()
        for slot in (0, 1):
            off = slot * STRIDE
            for field in (0x72, 0x76):
                old = struct.unpack_from('>I', io.pool, off + field)[0]
                struct.pack_into('>I', io.pool, off + field, old + 0x100000)
        io.pool[2 * STRIDE:3 * STRIDE] = io.pool[:STRIDE]
        struct.pack_into('>I', io.pool, 2 * STRIDE + 4, POOL_START + 2 * STRIDE)
        gate = AxBgmGate()
        self.assertEqual(gate.locate(bytes(io.pool)), {})
        self.assertFalse(gate.apply(io, mute=True).muted)
        self.assertEqual(io.writes, [])

    def test_combined_control_on_exact_pair_can_mute_and_restore(self):
        io = PoolIO()
        for slot in (0, 1):
            struct.pack_into('>I', io.pool, slot * STRIDE + 0x0C,
                             COMBINED_EXACT_CONTROL | ORIGINAL_ROUTE)
        gate = AxBgmGate()
        report = gate.diagnostic(io)
        self.assertIn('Canonical BGM identities: 2/2; verified pair: 2/2', report)
        self.assertTrue(gate.apply(io, mute=True).muted)
        self.assertEqual([value for _, value in io.writes], [b'\x00', b'\x00'])
        self.assertTrue(gate.apply(io, mute=False).ready)
        self.assertEqual([value for _, value in io.writes[-2:]], [b'\x0b', b'\x0b'])

    def test_combined_control_does_not_authorize_shifted_or_duplicate_voice(self):
        io = PoolIO()
        for slot in (0, 1):
            struct.pack_into('>I', io.pool, slot * STRIDE + 0x0C,
                             COMBINED_EXACT_CONTROL | ORIGINAL_ROUTE)
        struct.pack_into('>I', io.pool, 0x72, BGM_IDENTITIES['left'][0] + 1)
        gate = AxBgmGate()
        self.assertEqual(gate.locate(bytes(io.pool)), {})
        self.assertFalse(gate.apply(io, mute=True).muted)
        self.assertEqual(io.writes, [])

        struct.pack_into('>I', io.pool, 0x72, BGM_IDENTITIES['left'][0])
        io.pool[2 * STRIDE:3 * STRIDE] = io.pool[:STRIDE]
        struct.pack_into('>I', io.pool, 2 * STRIDE + 4, POOL_START + 2 * STRIDE)
        self.assertEqual(gate.locate(bytes(io.pool)), {})
        self.assertFalse(gate.apply(io, mute=True).muted)
        self.assertEqual(io.writes, [])


if __name__ == '__main__':
    unittest.main()

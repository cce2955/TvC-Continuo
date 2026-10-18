from __future__ import annotations

import struct

from tvcgui.features.hitboxes import renderer


def test_hurtbox_reader_uses_one_descriptor_and_one_matrix_read(monkeypatch):
    base = 0x9246B9C0
    desc = base + renderer.HURTBOX_DESC_BASE
    matrix_ptr = 0x80010000
    descriptor = struct.pack(">II4f", 1, matrix_ptr, 0.5, 0.25, 0.0, 0.4)
    descriptor_blob = descriptor + b"\0" * ((renderer.HURTBOX_DESC_COUNT - 1) * renderer.HURTBOX_DESC_STRIDE)
    matrix = struct.pack(">12f", 1, 0, 0, 1, 0, 1, 0, 2, 0, 0, 1, 3)
    calls = []

    def fake_read(addr, size):
        calls.append((addr, size))
        if addr == desc:
            return descriptor_blob
        if addr == matrix_ptr:
            return matrix
        return b"\0" * size

    monkeypatch.setattr(renderer, "rbytes", fake_read)
    boxes = renderer.read_hurtboxes("P1", base)
    assert calls == [(desc, len(descriptor_blob)), (matrix_ptr, 0x30)]
    assert len(boxes) == 1
    assert (boxes[0].x, boxes[0].y, boxes[0].z) == (1.5, 2.25, 3.0)
    assert abs(boxes[0].radius - 0.4) < 1e-6


def test_hitbox_reader_uses_one_contiguous_memory_read(monkeypatch):
    layout = renderer.HITBOX
    base = 0x9246B9C0 + layout.struct_shift
    start = min(layout.blocks[0], layout.off_flag)
    end = max(layout.blocks[-1] + layout.off_r + 4, layout.off_flag + 1)
    blob = bytearray(end - start)
    blob[layout.off_flag - start] = 0x53
    for index, block in enumerate(layout.blocks):
        for offset, value in ((layout.off_x, index), (layout.off_y, index + 1), (layout.off_r, index + 2)):
            struct.pack_into(">f", blob, block + offset - start, float(value))
    calls = []
    def fake_read(address, size):
        calls.append((address, size))
        return bytes(blob)
    monkeypatch.setattr(renderer, "rbytes", fake_read)
    monkeypatch.setattr(renderer, "rd32", lambda _addr: (_ for _ in ()).throw(AssertionError("scalar read")))
    boxes = renderer.read_hitboxes(0x9246B9C0, layout)
    assert calls == [(base + start, end - start)]
    assert boxes[0] == (0.0, 1.0, 2.0, 0x53)
    assert boxes[-1] == (2.0, 3.0, 4.0, 0x53)


def test_hurtboxes_sharing_a_bone_read_its_matrix_once(monkeypatch):
    base = 0x9246B9C0
    desc = base + renderer.HURTBOX_DESC_BASE
    matrix_ptr = 0x80010000
    one = struct.pack(">II4f", 1, matrix_ptr, 0.5, 0.25, 0.0, 0.4)
    two = struct.pack(">II4f", 1, matrix_ptr, 0.6, 0.25, 0.0, 0.4)
    descriptors = one + two + b"\0" * ((renderer.HURTBOX_DESC_COUNT - 2) * renderer.HURTBOX_DESC_STRIDE)
    matrix = struct.pack(">12f", 1, 0, 0, 1, 0, 1, 0, 2, 0, 0, 1, 3)
    calls = []
    def fake_read(address, size):
        calls.append((address, size))
        return descriptors if address == desc else matrix
    monkeypatch.setattr(renderer, "rbytes", fake_read)
    boxes = renderer.read_hurtboxes("P1", base)
    assert len(boxes) == 2
    assert calls == [(desc, len(descriptors)), (matrix_ptr, 0x30)]


def test_hitbox_reader_falls_back_when_span_read_is_unavailable(monkeypatch):
    layout = renderer.HITBOX
    base = 0x9246B9C0 + layout.struct_shift
    words = {base + (layout.off_flag & ~3): 0x00000053}
    for index, block in enumerate(layout.blocks):
        for offset, value in ((layout.off_x, index), (layout.off_y, index + 1), (layout.off_r, index + 2)):
            words[base + block + offset] = struct.unpack(">I", struct.pack(">f", float(value)))[0]
    monkeypatch.setattr(renderer, "rbytes", lambda _addr, _size: b"")
    monkeypatch.setattr(renderer, "rd32", lambda address: words.get(address, 0))
    assert renderer.read_hitboxes(0x9246B9C0, layout)[-1] == (2.0, 3.0, 4.0, 0x53)


def test_camera_position_and_view_are_sampled_together(monkeypatch):
    blob = bytearray(0x3C)
    for index, offset in enumerate((0, 4, 8, 12, 0x20, 0x24, 0x28, 0x30, 0x34, 0x38)):
        struct.pack_into(">f", blob, offset, float(index + 1))
    calls = []
    def fake_read(address, size):
        calls.append((address, size))
        return bytes(blob)
    monkeypatch.setattr(renderer, "rbytes", fake_read)
    assert renderer.read_camera_frame(renderer.CAMERA) == tuple(float(i) for i in range(1, 11))
    assert calls == [(renderer.CAMERA.base, len(blob))]


def test_master_samples_hitboxes_after_hud_work_and_before_drawing():
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "tvcgui/features/overlay/master_renderer.py").read_text(encoding="utf-8")
    loop = source[source.index("    def run(self) -> None:"):]
    assert loop.index("self.hud_renderer.update(dt, self.control)") < loop.index("self.hitbox_renderer.update(dt, self.control)")
    assert loop.index("self.hitbox_renderer.update(dt, self.control)") < loop.index("self.hitbox_renderer.draw(self.screen, self.control)")

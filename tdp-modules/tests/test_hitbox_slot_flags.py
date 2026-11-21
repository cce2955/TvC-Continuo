import struct

from tvcgui.features.hitboxes import renderer


def test_each_hitbox_slot_reads_its_own_native_flag(monkeypatch):
    base = 0x90000000
    memory = bytearray(0x800)
    for index, flag in enumerate((0x53, 0, 0x53)):
        struct.pack_into('>I', memory, 0x500 + index * 0x40, flag)
        for off, value in ((0x24, 1.0 + index), (0x28, 2.0), (0x3c, 0.5)):
            struct.pack_into('>f', memory, 0x500 + index * 0x40 + off, value)
    monkeypatch.setattr(renderer, 'rbytes', lambda addr, size: bytes(memory[addr-base:addr-base+size]))
    boxes = renderer.read_hitboxes(base, renderer.HITBOX)
    assert [box[3] for box in boxes] == [0x53, 0, 0x53]
    assert [renderer._hitbox_slot_allocated(box[3]) for box in boxes] == [True, False, True]
    assert boxes[1][2] == 0.5  # stale geometry is preserved, but the slot is unallocated


def test_enabled_shape_uses_each_slots_two_low_bits():
    flags = [0x51, 0x53, 0x52]
    assert [renderer._hitbox_slot_enabled(flag) for flag in flags] == [False, True, False]
    assert renderer._hitbox_slot_enabled(0x03)


def test_preview_spans_startup_active_gap_and_recovery():
    preview = renderer.AttackShapePreview()
    def sample(frame, flags, action=256):
        boxes = [(i + 1, 2, 0.5, flag) for i, flag in enumerate(flags)]
        return [(i, active) for i, _, _, _, active in preview.sample(action, frame, True, boxes)]
    assert sample(1, [81, 81, 81]) == [(0, False)]
    assert sample(2, [83, 81, 81]) == [(0, True)]
    assert sample(3, [81, 81, 81]) == [(0, False)]
    assert sample(4, [81, 3, 81]) == [(0, False), (1, True)]
    assert sample(5, [0, 0, 0]) == [(0, False), (1, False)]
    assert sample(1, [0, 0, 0], action=257) == []
    assert sample(1, [81, 81, 81]) == [(0, False), (1, False)]
    preview.end_action()
    assert preview.sample(258, 1, False, [(0, 0, 1, 81)]) == []


def test_saved_reach_supplies_dim_startup_before_allocation(monkeypatch):
    preview = renderer.AttackShapePreview()
    profile = dict(anchor='fighter_root', reach_from_start=1.25, tip_center_from_start=1,
                   tip_y_from_start=.75, tip_z_from_start=0, tip_radius=.25)
    bases = {'P1': 1000, 'P3': 2000}
    monkeypatch.setattr(renderer, 'SLOT_BASES', bases)
    monkeypatch.setattr(renderer, 'rbytes', lambda addr, _: struct.pack('>3f',
                        2 if addr == 1000 + 0xB0 else -2, 1, 0))
    args = ('P1', profile)
    assert preview.sample(256, 0, True, [], *args) == [(0, 1, 1.75, .25, False)]
    boxes = [(3, 2, .5, 0), (3.25, 2, .5, 83)]
    assert preview.sample(256, 2, True, boxes, *args) == [(1, 3.25, 2, .5, True)]
    assert preview.sample(256, 3, True, [], *args) == [(1, 3.25, 2, .5, False)]
    assert preview.sample(257, 0, False, []) == []


def test_box_preview_has_visible_fill_and_active_edge():
    import pygame
    screen = pygame.Surface((80, 80))
    bounds = pygame.Rect(20, 20, 41, 41)
    interior = bounds.inflate(-10, -10)
    pixels = []
    for active in (False, True):
        screen.fill((0, 0, 0))
        renderer._draw_attack_box(screen, bounds, (200, 70, 50), active)
        pixels.append(sum(bool(screen.get_at((x, y)).r) for x in range(interior.left, interior.right)
                          for y in range(interior.top, interior.bottom)))
        assert (screen.get_at((23, 23))[:3] == (250, 245, 235)) == active
    assert 0 < pixels[0] < pixels[1] < interior.width * interior.height


def test_fighter_draw_path_spans_startup_to_recovery_with_core_hud_off(monkeypatch):
    import pygame
    from types import SimpleNamespace
    pygame.font.init()
    base = 0x92460000
    state, flag = 256, 81
    monkeypatch.setattr(renderer, 'SLOT_BASES', {'P1': base})
    monkeypatch.setattr(renderer, 'rd32', lambda _: 12)
    monkeypatch.setattr(renderer, 'rbytes', lambda a, n: bytes(n))
    monkeypatch.setattr(renderer, 'read_state_raw', lambda _: state)
    monkeypatch.setattr(renderer, 'read_action_frame', lambda _: 1)
    monkeypatch.setattr(renderer, 'read_hitboxes', lambda *_: [(1, 1, .3, flag)])
    monkeypatch.setattr(renderer, 'read_projectile_hitboxes', lambda: [])
    monkeypatch.setattr(renderer, '_range_ruler_enabled', lambda: False)
    monkeypatch.setattr(renderer, '_read_filter_payload', lambda: None)
    monkeypatch.setattr(renderer, 'set_legend_window_visible', lambda _: None)
    fd = renderer.MoveFrameData(256, '5A', ((8, 10),))
    monkeypatch.setattr(renderer, 'build_frame_data_cache', lambda: {'P1': {256: fd}})
    monkeypatch.setattr(renderer.HitboxRenderer, '_load_range_profiles', staticmethod(lambda: {'attacks': {}}))
    layer = renderer.HitboxRenderer()
    monkeypatch.setattr(layer.overlay, '_project_hitbox', lambda *_: (40, 40, 20))
    monkeypatch.setattr(renderer, '_slot_filter', {'P1': True})
    monkeypatch.setattr(renderer, '_hitbox_view_mode', 'box')
    control = SimpleNamespace(show_hud=False, show_hitboxes=True, show_hurtboxes=False)
    screen = pygame.Surface((80, 80))
    for state, flag, visible, active in ((256, 81, True, False), (256, 83, True, True),
                                         (256, 0, True, False), (1, 0, False, False)):
        screen.fill((0, 0, 0))
        layer.update(.016, control)
        layer.draw(screen, control)
        assert any(screen.get_at((40, 40))[:3]) == visible, (layer.sampled_attacks, layer.last_counts)
        assert (screen.get_at((23, 23))[:3] == (250, 245, 235)) == active


def test_ruler_keeps_target_geometry_when_all_other_layers_are_off(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    layer = Mock()
    layer._hitboxes_enabled.return_value = False
    layer._hurtboxes_enabled.return_value = False
    layer.last_char_ids = {'P1': 12}
    layer._move_phase_tick = 0
    layer._last_camera_mode_key = 'normal'
    layer.attack_previews = {'P1': renderer.AttackShapePreview()}
    layer.sampled_attacks = {}
    layer._hitbox_ghost_frames = []
    monkeypatch.setattr(renderer, 'SLOT_BASES', {'P1': 1})
    monkeypatch.setattr(renderer, 'rd32', lambda _: 12)
    monkeypatch.setattr(renderer, 'read_camera_pos', lambda _: (0, 0, 0, 0))
    monkeypatch.setattr(renderer, '_range_ruler_enabled', lambda: True)
    monkeypatch.setattr(renderer, 'set_legend_window_visible', lambda _: None)
    monkeypatch.setattr(renderer, '_read_hurtbox_filter', lambda: {'P1': False})
    body = SimpleNamespace(x=1., y=2., radius=.5)
    monkeypatch.setattr(renderer, 'read_hurtboxes', lambda *_: [body])
    monkeypatch.setattr(renderer, '_slot_has_coherent_hurt_rig', lambda *_: True)
    renderer.HitboxRenderer.update(layer, .016, SimpleNamespace(show_hud=False))
    assert layer.cached_hurtboxes == {'P1': [body]}
    assert layer.hurt_counts == {'P1': 0}
    assert layer.sampled_attacks == {}


def test_box_and_sphere_render_different_outlines(monkeypatch):
    import pygame
    overlay = renderer.Overlay.__new__(renderer.Overlay)
    overlay.screen = pygame.Surface((80, 80))
    overlay.frame_index = 1
    overlay.hitbox_last_seen = {}
    overlay.hitbox_spawn_start = {}
    overlay._project_hitbox = lambda *args: (40, 40, 20)
    for mode in ('live', 'box'):
        monkeypatch.setattr(renderer, '_hitbox_view_mode', mode)
        overlay.screen.fill((0, 0, 0))
        overlay.draw_hitbox(0, 0, 0, 1, (255, 0, 0), 'P1[0]')
        assert (overlay.screen.get_at((20, 20)).r == 255) == (mode == 'box')


def test_scalar_fallback_uses_individual_flags(monkeypatch):
    base = 0x90000000
    memory = bytearray(0x800)
    for index, flag in enumerate((0, 0x53, 1)):
        struct.pack_into('>I', memory, 0x500 + index * 0x40, flag)
    monkeypatch.setattr(renderer, 'rbytes', lambda *_: None)
    monkeypatch.setattr(renderer, 'rd32', lambda addr: struct.unpack_from('>I', memory, addr-base)[0])
    boxes = renderer.read_hitboxes(base, renderer.HITBOX)
    assert [box[3] for box in boxes] == [0, 0x53, 1]


def test_master_presents_both_attack_shapes_with_hud_hidden(monkeypatch):
    from unittest.mock import Mock
    import pygame
    from tvcgui.features.overlay import master_renderer

    screen = pygame.Surface((80, 80))
    overlay = renderer.Overlay.__new__(renderer.Overlay)
    overlay.screen = screen
    overlay.frame_index = 1
    overlay.hitbox_last_seen = {}
    overlay.hitbox_spawn_start = {}
    overlay._project_hitbox = lambda *_: (40, 40, 20)
    monkeypatch.setattr(master_renderer, 'sync_overlay_to_dolphin', lambda *_: screen.get_size())
    monkeypatch.setattr(pygame, 'quit', lambda: None)
    for mode in ('live', 'box'):
        monkeypatch.setattr(renderer, '_hitbox_view_mode', mode)
        master = Mock()
        master.screen = screen
        master.control.show_hud = False
        master.control.show_hitboxes = True
        master.clock.tick.return_value = 16
        master.running = True
        master.present.side_effect = lambda: setattr(master, 'running', False)
        master.clear.side_effect = lambda: screen.fill((0, 0, 0))
        master.hitbox_renderer.draw.side_effect = lambda *_: overlay.draw_hitbox(
            0, 0, 0, 1, (255, 0, 0), 'P1[0]')
        master_renderer.MasterOverlay.run(master)
        master.hitbox_renderer.draw.assert_called_once_with(screen, master.control)
        master.hud_renderer.draw.assert_not_called()
        assert pygame.mask.from_threshold(screen, (0, 0, 0), (1, 1, 1)).count() < 80 * 80
        assert (screen.get_at((20, 20)).r == 255) == (mode == 'box')

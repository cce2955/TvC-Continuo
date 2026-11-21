import math
import struct
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tvcgui.features.frame_data.workbench_v3 import LeanFrameDataWindow
from tvcgui.tools.scanners.bone_scanner import scale_matrix_basis


class FrameDataEditorTests(unittest.TestCase):
    root = None

    def hidden_root(self):
        if self.root is None:
            try:
                root = tk.Tk()
            except tk.TclError as exc:
                self.skipTest(str(exc))
            root.withdraw()
            type(self).root = root
            self.addClassCleanup(root.destroy)
        return self.root

    def test_open_shows_move_properties_and_reach_edits(self):
        root = self.hidden_root()
        window = tk.Toplevel(root)
        self.addCleanup(window.destroy)
        window.withdraw()
        move = dict(id=12, move_name="Test move", startup=5, stretch_width=1.0,
                    stretch_width_addr=0x90000000)
        with patch.object(tk, 'Toplevel', return_value=window):
            editor = LeanFrameDataWindow(root, 'P1-C1', dict(moves=[move]))
        self.assertIs(editor._selected_move, move)
        self.assertEqual(editor._notebook.tab(editor._notebook.select(), 'text'), 'Move properties')
        self.assertIn('startup', editor._field_row_to_key.values())
        editor._activate_view('hitbox')
        self.assertIn('stretch_width', editor.tree['displaycolumns'])
        with patch.object(editor, '_edit_static_reach') as edit:
            editor._route_standard_edit('stretch_width', editor._selected_item, move, '1')
            edit.assert_called_once_with(editor._selected_item, move, 'stretch_width')
        editor._search_var.set("absent")
        editor._apply_filters(reset_page=True)
        self.assertIsNone(editor._selected_move)
        self.assertFalse(editor._field_row_to_key)

    def test_scale_button_targets_selected_live_actor(self):
        root = self.hidden_root()
        window = tk.Toplevel(root)
        self.addCleanup(window.destroy)
        window.withdraw()
        base = 0x92460000
        address = base + 0x3040
        raw = struct.pack('>12f', 1, 0, 0, 7, 0, 1, 0, 8, 0, 0, 1, 9)
        editor = SimpleNamespace(root=root, slot_label='P1-C1', target_slot={'char_id': 12, 'slot_base': base})
        result = SimpleNamespace(addr=address, float_count=10, zero_count=6, change_count=0,
                                 pattern_hits=2, score=10.0, sample=(1.0,))
        with (patch.object(tk, 'Toplevel', return_value=window),
              patch('tvcgui.tools.scanners.fighter_resolver.SlotResolver.resolve_base', return_value=(base, True)),
              patch('tvcgui.platform.dolphin.rd32', return_value=12) as character,
              patch('tvcgui.platform.dolphin.rbytes', return_value=raw),
              patch('tvcgui.platform.dolphin.wbytes', return_value=True) as write,
              patch('tvcgui.features.frame_data.workbench_v3.BoneScanner') as scanner,
              patch('tkinter.simpledialog.askfloat', return_value=1.5),
              patch('tkinter.messagebox.showerror') as error):
            scanner.return_value.results = [result]
            LeanFrameDataWindow._show_bones(editor)
            tree = next(w for w in window.winfo_children() if isinstance(w, ttk.Treeview))
            button = next(w for panel in window.winfo_children() for w in panel.winfo_children()
                          if isinstance(w, ttk.Button) and w['text'] == 'Scale selected')
            tree.selection_set(str(address))
            button.invoke()
            write.assert_called_once_with(address, scale_matrix_basis(raw, 1.5))
            character.return_value = 13
            button.invoke()
            self.assertEqual(write.call_count, 1)
            error.assert_called_once()

    def test_rotated_matrix_scale_preserves_translation(self):
        values = (0, -2, 0, 13, 1, 0, 0, -17, 0, 0, 3, 19)
        raw = struct.pack('>12f', *values)
        scaled = struct.unpack('>12f', scale_matrix_basis(raw, 2))
        self.assertEqual(scaled, (0, -4, 0, 13, 2, 0, 0, -17, 0, 0, 6, 19))
        for invalid, factor in ((bytes(48), 1), (raw, math.inf), (raw, 0),
                                (struct.pack('>12f', *([1] * 12)), 1), (raw[:32], 2)):
            with self.assertRaises(ValueError):
                scale_matrix_basis(invalid, factor)

    def test_static_normal_size_resolves_uncached_packet_and_writes_one_float(self):
        base, table, size = 0x9246B9C0, 0x90896640, 0x200
        start = table + 0x18888
        memory = bytearray(size + 64)
        packet = bytes.fromhex('34400020') + struct.pack('>I3fI', 8, 1, 2, 3, 5)
        memory[0x40:0x40+len(packet)] = packet
        memory[size:size+len(packet)] = packet
        row = dict(move_name='5A', id=256, abs=start, kind='normal')
        editor = LeanFrameDataWindow.__new__(LeanFrameDataWindow)
        editor.root = None
        editor.slot_label = 'P1-C1'
        editor.target_slot = dict(char_id=12, chr_tbl_abs=table, fighter_base_abs=base)
        editor._status_var = Mock()
        editor._label_overrides = {}
        editor._refresh_row = Mock()
        editor._refresh_visible_inspector_tab = Mock()
        editor._after_cell_write = Mock()
        from tvcgui.core.constants import OFF_CHAR_ID
        from tvcgui.tools.scanners import normal_scanner
        scanner = 'tvcgui.tools.scanners.fighter_resolver.SlotResolver.resolve_base'
        with (patch(scanner, return_value=(base, True)) as resolve,
              patch('tvcgui.platform.dolphin.rd32', side_effect=lambda a: 12 if a == base + OFF_CHAR_ID else table),
              patch.object(normal_scanner, 'read_and_validate_chr_tbl', return_value=b'table'),
              patch.object(normal_scanner, 'parse_chr_tbl_entries', return_value=[(256, start), (257, start + size)]),
              patch('tvcgui.platform.dolphin.rbytes', side_effect=lambda a,n: bytes(memory[a-start:a-start+n])),
              patch('tvcgui.platform.dolphin.wbytes', return_value=True) as write,
              patch('tkinter.simpledialog.askfloat', return_value=1.5) as ask,
              patch('tkinter.messagebox.showinfo')):
            editor._route_standard_edit('stretch_len', 'row', row, '')
            write.assert_called_once_with(start + 0x48, struct.pack('>f', 1.5))
            self.assertEqual(row['stretch_len'], 1.5)
            editor._after_cell_write.assert_called_once_with('row', row, 'stretch_len')
            def change_actor_during_edit(*args, **kwargs):
                resolve.return_value = (base + 4, True)
                return 2
            ask.side_effect = change_actor_during_edit
            editor._route_standard_edit('stretch_len', 'row', row, '')
            self.assertEqual(write.call_count, 1)
            resolve.return_value = (base, True)
            memory[:size] = bytes(size)
            with self.assertRaisesRegex(ValueError, 'No reach packet'):
                editor._read_static_reach(row)

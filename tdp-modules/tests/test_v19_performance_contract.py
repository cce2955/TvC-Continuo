from __future__ import annotations

import unittest

from tests.v19_contract_helpers import read


class V19PerformanceContractTests(unittest.TestCase):
    def test_workspace_background_is_pixel_identical_and_bounded_on_resize(self):
        import pygame
        from tvcgui.ui import components
        size = (400, 180)
        expected = pygame.Surface(size, pygame.SRCALPHA)
        components._draw_horizontal_gradient(expected, expected.get_rect(), (10, 20, 32), (5, 10, 18), 250)
        pygame.draw.rect(expected, (37, 53, 70), expected.get_rect(), 1, border_radius=5)
        cached = components._workspace_background(size)
        self.assertEqual(pygame.image.tobytes(cached, "RGBA"), pygame.image.tobytes(expected, "RGBA"))
        self.assertIs(cached, components._workspace_background(size))
        self.assertEqual(components._workspace_background((450, 200)).get_size(), (450, 200))
        self.assertEqual(components._workspace_background.cache_info().currsize, 1)

    def test_runtime_csv_export_defaults_off(self):
        source = read("main.py")
        self.assertIn('os.environ.get("TVC_FD_AUTO_EXPORT", "0")', source)

    def test_runtime_exporter_creation_is_opt_in(self):
        source = read("main.py")
        self.assertIn(
            "if FrameDataSpreadsheetExporter is not None and FD_AUTO_EXPORT_ENABLED:",
            source,
        )

    def test_disabled_exporter_cannot_queue_workbench_csv_scan(self):
        source = read("main.py")
        self.assertIn("if frame_data_exporter is not None and scan_worker", source)
        self.assertIn("if scan_worker and frame_data_exporter is not None", source)


if __name__ == "__main__":
    unittest.main()

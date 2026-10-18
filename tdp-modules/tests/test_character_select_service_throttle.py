"""Performance contract for the Character Select service tick."""
from __future__ import annotations

import unittest

import tvcgui.features.character_select.runtime as runtime


class CharacterSelectServiceThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.saved = {
            "needs": runtime.char_test_needs_service,
            "tick": runtime._tick_roster_actions,
            "monotonic": runtime.time.monotonic,
            "last": runtime._CHAR_TEST_LAST_SERVICE,
            "queue": list(runtime._ROSTER_QUEUE),
            "roster_state": dict(runtime._ROSTER_STATE),
        }
        self.now = 100.0
        self.calls = 0
        runtime.char_test_needs_service = lambda: True
        runtime._tick_roster_actions = self._called
        runtime.time.monotonic = lambda: self.now
        runtime._CHAR_TEST_LAST_SERVICE = 0.0
        runtime._ROSTER_QUEUE.clear()
        runtime._ROSTER_STATE["extra_characters_requested"] = True
        runtime._ROSTER_STATE["extra_characters_enabled"] = False

    def tearDown(self) -> None:
        runtime.char_test_needs_service = self.saved["needs"]
        runtime._tick_roster_actions = self.saved["tick"]
        runtime.time.monotonic = self.saved["monotonic"]
        runtime._CHAR_TEST_LAST_SERVICE = self.saved["last"]
        runtime._ROSTER_QUEUE.clear()
        runtime._ROSTER_QUEUE.extend(self.saved["queue"])
        runtime._ROSTER_STATE.clear()
        runtime._ROSTER_STATE.update(self.saved["roster_state"])

    def _called(self) -> None:
        self.calls += 1

    def test_idle_maintenance_respects_configured_interval(self) -> None:
        runtime.tick_char_test()
        runtime.tick_char_test()
        self.assertEqual(self.calls, 1)
        self.now += runtime._CHAR_TEST_SERVICE_MIN_INTERVAL_SEC + 0.001
        runtime.tick_char_test()
        self.assertEqual(self.calls, 2)

    def test_queued_ui_action_bypasses_throttle(self) -> None:
        runtime.tick_char_test()
        runtime._ROSTER_QUEUE.append({"op": "snapshot"})
        runtime.tick_char_test()
        self.assertEqual(self.calls, 2)


if __name__ == "__main__":
    unittest.main()

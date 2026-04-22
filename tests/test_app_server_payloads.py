from __future__ import annotations

import importlib
import time
import unittest

from tests.fake_codex_server import build_large_scenario


APP_SERVER = importlib.import_module("redex.app_server")


class AppServerPayloadTests(unittest.TestCase):
    def test_codex_worktree_paths_group_by_repo_name(self) -> None:
        key, label = APP_SERVER._workspace_group_for_cwd(r"C:\Users\peter\.codex\worktrees\50a1\blst")
        self.assertEqual(key, "repo:blst")
        self.assertEqual(label, "blst")

    def test_pre_first_turn_error_detection(self) -> None:
        self.assertTrue(APP_SERVER._is_pre_first_turn_error(RuntimeError("thread is not materialized yet")))
        self.assertTrue(APP_SERVER._is_pre_first_turn_error(RuntimeError("no rollout found for thread")))
        self.assertTrue(APP_SERVER._is_pre_first_turn_error(RuntimeError("rollout is empty")))
        self.assertFalse(APP_SERVER._is_pre_first_turn_error(RuntimeError("permission denied")))

    def test_normalize_turns_preserves_file_change_summary(self) -> None:
        thread = {
            "id": "thread-1",
            "turns": [
                {
                    "id": "turn-1",
                    "status": "completed",
                    "startedAt": 1710000000,
                    "items": [
                        {
                            "id": "fc-1",
                            "type": "fileChange",
                            "status": "completed",
                            "changes": [
                                {
                                    "path": "src/redex/bridge.py",
                                    "kind": {"type": "modified"},
                                    "diff": "--- a/src/redex/bridge.py\n+++ b/src/redex/bridge.py\n+new line\n-old line\n",
                                }
                            ],
                        }
                    ],
                }
            ],
        }
        messages = APP_SERVER.normalize_turns(thread)
        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertEqual(message["type"], "fileChange")
        self.assertEqual(message["changeSummary"]["files"], 1)
        self.assertEqual(message["changeSummary"]["added"], 1)
        self.assertEqual(message["changeSummary"]["deleted"], 1)

    def test_session_list_payload_budget_large_input(self) -> None:
        scenario = build_large_scenario(session_count=250, turns_per_thread=8)
        raw = {
            "data": scenario.thread_list(),
            "nextCursor": None,
            "backwardsCursor": None,
        }
        started = time.perf_counter()
        payload = APP_SERVER.make_session_list_payload(raw)
        elapsed = time.perf_counter() - started
        self.assertEqual(len(payload["sessions"]), 250)
        self.assertLess(elapsed, 0.6, f"session list payload was too slow: {elapsed:.3f}s")

    def test_session_detail_payload_budget_large_input(self) -> None:
        scenario = build_large_scenario(session_count=1, turns_per_thread=400)
        thread = scenario.get_thread("thread-0000", include_turns=False)
        turns, next_cursor, backwards_cursor = scenario.list_turns("thread-0000", limit=400, sort_direction="asc")
        started = time.perf_counter()
        payload = APP_SERVER.make_session_detail_page_payload(
            {"thread": thread},
            {"data": turns, "nextCursor": next_cursor, "backwardsCursor": backwards_cursor},
            sort_direction="asc",
        )
        elapsed = time.perf_counter() - started
        self.assertEqual(payload["session"]["id"], "thread-0000")
        self.assertEqual(len(payload["messages"]), 800)
        self.assertLess(elapsed, 0.9, f"session detail payload was too slow: {elapsed:.3f}s")


if __name__ == "__main__":
    unittest.main()

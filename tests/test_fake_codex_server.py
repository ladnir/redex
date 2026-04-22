from __future__ import annotations

import importlib
import unittest

APP_SERVER_MODULE = importlib.import_module("redex.app_server")

CodexAppServerClient = APP_SERVER_MODULE.CodexAppServerClient
make_session_detail_page_payload = APP_SERVER_MODULE.make_session_detail_page_payload
make_session_list_payload = APP_SERVER_MODULE.make_session_list_payload

from tests.fake_codex_server import FakeCodexAppServer
from tests.fake_codex_server import build_sample_scenario


class FakeCodexAppServerTests(unittest.TestCase):
    def test_client_can_list_and_read_sessions(self) -> None:
        with FakeCodexAppServer(build_sample_scenario()) as server:
            with CodexAppServerClient(server.url) as client:
                sessions_result = client.list_sessions(limit=10)
                payload = make_session_list_payload(sessions_result)
                self.assertEqual(len(payload["sessions"]), 1)
                session = payload["sessions"][0]
                self.assertEqual(session["id"], "thread-1")
                self.assertEqual(session["title"], "Sample thread")

                thread_result = client.get_session("thread-1", include_turns=False)
                turns_result = client.list_session_turns("thread-1", limit=20, sort_direction="asc")
                detail = make_session_detail_page_payload(thread_result, turns_result, sort_direction="asc")
                self.assertEqual(detail["session"]["id"], "thread-1")
                self.assertEqual(len(detail["messages"]), 2)
                self.assertEqual(detail["messages"][0]["role"], "user")
                self.assertEqual(detail["messages"][1]["phase"], "final_answer")
                self.assertEqual(detail["messages"][1]["text"], "hi")

    def test_send_prompt_handles_interleaved_notifications(self) -> None:
        notifications: list[dict] = []
        with FakeCodexAppServer(build_sample_scenario()) as server:
            with CodexAppServerClient(server.url, notification_handler=notifications.append) as client:
                result = client.send_prompt("thread-1", "what date is it?")
                self.assertEqual(result["turn"]["status"], "completed")

                thread_result = client.get_session("thread-1", include_turns=False)
                turns_result = client.list_session_turns("thread-1", limit=20, sort_direction="asc")
                detail = make_session_detail_page_payload(thread_result, turns_result, sort_direction="asc")

        self.assertGreaterEqual(len(notifications), 4)
        methods = [message.get("method") for message in notifications]
        self.assertIn("item/agentMessage/delta", methods)
        self.assertIn("item/completed", methods)
        self.assertIn("turn/completed", methods)

        assistant_messages = [
            message
            for message in detail["messages"]
            if message.get("role") == "assistant" and message.get("phase") == "final_answer"
        ]
        self.assertGreaterEqual(len(assistant_messages), 2)
        self.assertEqual(assistant_messages[-1]["text"], "Monday, April 20, 2026.")


if __name__ == "__main__":
    unittest.main()

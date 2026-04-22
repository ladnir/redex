from __future__ import annotations

import importlib
import threading
import time
import unittest

from tests.fake_codex_server import FakeCodexAppServer
from tests.fake_codex_server import build_sample_scenario
from tests.support import json_request
from tests.support import read_sse_events
from tests.support import running_bridge


BRIDGE = importlib.import_module("redex.bridge")


class BridgeIntegrationTests(unittest.TestCase):
    def test_sessions_endpoint_and_prompt_flow(self) -> None:
        with FakeCodexAppServer(build_sample_scenario()) as upstream:
            with running_bridge(app_server_url=upstream.url) as (server, _thread):
                host, port = server.server_address
                base_url = f"http://{host}:{port}"

                sessions = json_request(f"{base_url}/api/sessions?limit=10")
                self.assertEqual(len(sessions["sessions"]), 1)
                self.assertEqual(sessions["sessions"][0]["id"], "thread-1")

                detail = json_request(f"{base_url}/api/sessions/thread-1?limit=20")
                self.assertEqual(detail["session"]["id"], "thread-1")
                self.assertEqual(len(detail["messages"]), 2)

                prompt_result = json_request(
                    f"{base_url}/api/sessions/thread-1/prompt",
                    method="POST",
                    payload={"text": "what date is it?"},
                )
                self.assertTrue(prompt_result["ok"])
                self.assertEqual(prompt_result["status"], "completed")

                updated = json_request(f"{base_url}/api/sessions/thread-1?limit=20")
                assistant_messages = [
                    message
                    for message in updated["messages"]
                    if message.get("role") == "assistant" and message.get("phase") == "final_answer"
                ]
                self.assertEqual(assistant_messages[-1]["text"], "Monday, April 20, 2026.")

    def test_events_stream_receives_live_notifications(self) -> None:
        with FakeCodexAppServer(build_sample_scenario()) as upstream:
            with running_bridge(app_server_url=upstream.url) as (server, _thread):
                host, port = server.server_address
                base_url = f"http://{host}:{port}"
                server.live_event_hub.subscribe_session("thread-1")
                subscribed = False
                last_event_id = 0
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and not subscribed:
                    event = server.live_event_hub.wait_for_event(last_event_id, timeout=0.25)
                    if event is None:
                        continue
                    last_event_id = int(event["id"])
                    subscribed = event.get("method") == "redex/subscribed"
                self.assertTrue(subscribed, "live event hub did not subscribe to thread-1 in time")
                prompt_result: dict[str, object] = {}

                def send_prompt() -> None:
                    time.sleep(0.1)
                    prompt_result.update(
                        json_request(
                            f"{base_url}/api/sessions/thread-1/prompt",
                            method="POST",
                            payload={"text": "what date is it?"},
                        )
                    )

                sender = threading.Thread(target=send_prompt, daemon=True)
                sender.start()
                events = read_sse_events(
                    host,
                    port,
                    "/api/events?sessionId=thread-1",
                    max_events=8,
                    timeout=8.0,
                    stop_methods={"turn/completed"},
                )
                sender.join(timeout=8)
                self.assertTrue(prompt_result["ok"])
                self.assertGreaterEqual(len(events), 3)
                methods = [entry["data"]["method"] for entry in events if "data" in entry and "method" in entry["data"]]
                self.assertIn("redex/ready", methods)
                self.assertIn("item/completed", methods)
                self.assertIn("turn/completed", methods)

    def test_observe_notification_only_notifies_on_final_answer(self) -> None:
        calls: list[tuple[str, str]] = []

        class SpyNotifier:
            def notify_final_response(self, thread_id: str, turn_id: str) -> None:
                calls.append((thread_id, turn_id))

        server = object.__new__(BRIDGE.RedexHttpServer)
        server.push_notifier = SpyNotifier()
        server._session_list_cache = {}
        server._session_list_cache_lock = threading.Lock()

        server._observe_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "commentary-1",
                    "type": "agentMessage",
                    "phase": "commentary",
                    "text": "thinking",
                },
            },
        )
        server._observe_notification(
            "item/completed",
            {
                "threadId": "thread-1",
                "turnId": "turn-1",
                "item": {
                    "id": "assistant-1",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "done",
                },
            },
        )

        self.assertEqual(calls, [("thread-1", "turn-1")])

    def test_session_list_cache_reuses_and_invalidates(self) -> None:
        with FakeCodexAppServer(build_sample_scenario()) as upstream:
            with running_bridge(app_server_url=upstream.url) as (server, _thread):
                host, port = server.server_address
                base_url = f"http://{host}:{port}"

                first = json_request(f"{base_url}/api/sessions?limit=10")
                second = json_request(f"{base_url}/api/sessions?limit=10")

                self.assertEqual(len(first["sessions"]), 1)
                self.assertEqual(first, second)
                self.assertEqual(upstream.method_counts["thread/list"], 1)

                prompt_result = json_request(
                    f"{base_url}/api/sessions/thread-1/prompt",
                    method="POST",
                    payload={"text": "cache invalidation"},
                )
                self.assertTrue(prompt_result["ok"])

                third = json_request(f"{base_url}/api/sessions?limit=10")
                self.assertEqual(len(third["sessions"]), 1)
                self.assertGreaterEqual(upstream.method_counts["thread/list"], 2)


if __name__ == "__main__":
    unittest.main()

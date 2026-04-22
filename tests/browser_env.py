from __future__ import annotations

import json
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import tests  # noqa: F401
from tests.fake_codex_server import FakeCodexAppServer
from tests.fake_codex_server import FakeCodexScenario
from tests.fake_codex_server import TurnScript
from tests.fake_codex_server import make_agent_item
from tests.fake_codex_server import make_thread
from tests.fake_codex_server import make_turn
from tests.fake_codex_server import make_user_item

from redex.bridge import BridgeConfig
from redex.bridge import RedexHttpServer


def build_browser_scenario() -> FakeCodexScenario:
    now = time.time()

    long_turns: list[dict[str, Any]] = []
    for index in range(70):
        long_turns.append(
            make_turn(
                turn_id=f"thread-1-turn-{index:03d}",
                status="completed",
                started_at=now - (140 - index),
                items=[
                    make_user_item(item_id=f"thread-1-user-{index:03d}", text=f"User asks about step {index}"),
                    make_agent_item(
                        item_id=f"thread-1-assistant-{index:03d}",
                        phase="final_answer",
                        text=f"Answer {index}: this is a longer transcript row for scroll testing.",
                    ),
                ],
            )
        )

    thread_one = make_thread(
        thread_id="thread-1",
        name="Primary thread",
        cwd=r"C:\Users\peter\repo\redex",
        preview="Answer 69: this is a longer transcript row for scroll testing.",
        updated_at=now - 1,
        turns=long_turns,
    )

    thread_two = make_thread(
        thread_id="thread-2",
        name="Background thread",
        cwd=r"C:\Users\peter\repo\redex",
        preview="waiting for background work",
        updated_at=now - 20,
        turns=[
            make_turn(
                turn_id="thread-2-turn-000",
                status="completed",
                started_at=now - 30,
                items=[
                    make_user_item(item_id="thread-2-user-000", text="hello background"),
                    make_agent_item(item_id="thread-2-assistant-000", phase="final_answer", text="background ready"),
                ],
            )
        ],
    )

    thread_three = make_thread(
        thread_id="thread-3",
        name="Fresh thread",
        cwd=r"C:\Users\peter\repo\redex",
        preview="newest session preview",
        updated_at=now - 10,
        turns=[
            make_turn(
                turn_id="thread-3-turn-000",
                status="completed",
                started_at=now - 15,
                items=[
                    make_user_item(item_id="thread-3-user-000", text="what's next?"),
                    make_agent_item(item_id="thread-3-assistant-000", phase="final_answer", text="ready for next step"),
                ],
            )
        ],
    )

    scenario = FakeCodexScenario(
        threads={
            "thread-1": thread_one,
            "thread-2": thread_two,
            "thread-3": thread_three,
        }
    )

    for thread_id, cwd in {
        "thread-1": thread_one["cwd"],
        "thread-2": thread_two["cwd"],
        "thread-3": thread_three["cwd"],
    }.items():
        scenario.resume_payloads[thread_id] = {
            "threadId": thread_id,
            "model": "gpt-5.4",
            "modelProvider": "openai",
            "approvalPolicy": "auto",
            "approvalsReviewer": None,
            "sandbox": {"type": "dangerFullAccess"},
            "config": None,
            "cwd": cwd,
            "baseInstructions": None,
            "developerInstructions": None,
            "personality": None,
        }

    scenario.turn_scripts["thread-1"] = [
        TurnScript(commentary_text="Thinking through the active-thread update.", final_text="Active thread final answer."),
        TurnScript(commentary_text="Thinking through a second update.", final_text="Second active thread answer."),
    ]
    scenario.turn_scripts["thread-2"] = [
        TurnScript(commentary_text="Running background work.", final_text="Background final answer."),
        TurnScript(commentary_text="More background work.", final_text="Second background final answer."),
    ]
    return scenario


class BrowserControlHandler(BaseHTTPRequestHandler):
    server: "BrowserControlServer"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/prompt":
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            thread_id = payload.get("threadId")
            text = payload.get("text")
            if not isinstance(thread_id, str) or not thread_id:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`threadId` is required."})
                return
            if not isinstance(text, str) or not text.strip():
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`text` is required."})
                return
            result = self.server.upstream.trigger_turn(thread_id, text.strip())
            self._send_json(HTTPStatus.OK, {"ok": True, "result": result})
            return
        if self.path == "/reset":
            self.server.upstream.reset(build_browser_scenario())
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path == "/delay":
            length = int(self.headers.get("Content-Length", "0") or "0")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            method = payload.get("method")
            seconds = payload.get("seconds")
            thread_id = payload.get("threadId")
            if not isinstance(method, str) or not method:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`method` is required."})
                return
            if not isinstance(seconds, (int, float)) or seconds < 0:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`seconds` must be a non-negative number."})
                return
            if thread_id is not None and (not isinstance(thread_id, str) or not thread_id):
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`threadId` must be a non-empty string when provided."})
                return
            self.server.upstream.queue_delay(method=method, seconds=float(seconds), thread_id=thread_id)
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class BrowserControlServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], upstream: FakeCodexAppServer) -> None:
        super().__init__(server_address, BrowserControlHandler)
        self.upstream = upstream


def main() -> None:
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    upstream = FakeCodexAppServer(build_browser_scenario()).start()

    bridge = RedexHttpServer(
        ("127.0.0.1", 0),
        BridgeConfig(app_server_url=upstream.url, default_cwd=None),
    )
    bridge_thread = threading.Thread(target=bridge.serve_forever, name="browser-test-redex", daemon=True)
    bridge_thread.start()

    control = BrowserControlServer(("127.0.0.1", 0), upstream)
    control_thread = threading.Thread(target=control.serve_forever, name="browser-test-control", daemon=True)
    control_thread.start()

    bridge_host, bridge_port = bridge.server_address
    control_host, control_port = control.server_address
    print(
        json.dumps(
            {
                "redexUrl": f"http://{bridge_host}:{bridge_port}",
                "controlUrl": f"http://{control_host}:{control_port}",
            }
        ),
        flush=True,
    )

    try:
        while not stop_event.is_set():
            time.sleep(0.1)
    finally:
        control.shutdown()
        control.server_close()
        control_thread.join(timeout=5)
        bridge.shutdown()
        bridge.server_close()
        bridge_thread.join(timeout=5)
        upstream.stop()


if __name__ == "__main__":
    main()

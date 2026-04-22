from __future__ import annotations

import contextlib
import importlib
import json
import threading
from http.client import HTTPConnection
from pathlib import Path
from typing import Any
from urllib import request


APP_SERVER = importlib.import_module("redex.app_server")
BRIDGE = importlib.import_module("redex.bridge")


def json_request(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, data=data, headers=headers, method=method)
    with request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


def read_sse_events(
    host: str,
    port: int,
    path: str,
    *,
    max_events: int,
    timeout: float = 5.0,
    stop_methods: set[str] | None = None,
) -> list[dict[str, Any]]:
    connection = HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Accept": "text/event-stream"})
        response = connection.getresponse()
        if response.status != 200:
            raise RuntimeError(f"SSE request failed with {response.status}")
        events: list[dict[str, Any]] = []
        event_name = "message"
        data_parts: list[str] = []
        while len(events) < max_events:
            raw_line = response.fp.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8").rstrip("\r\n")
            if not line:
                if data_parts:
                    payload = json.loads("\n".join(data_parts))
                    event_payload = {"event": event_name, "data": payload}
                    events.append(event_payload)
                    method = payload.get("method") if isinstance(payload, dict) else None
                    if stop_methods and isinstance(method, str) and method in stop_methods:
                        break
                event_name = "message"
                data_parts = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
                continue
            if line.startswith("data:"):
                data_parts.append(line.split(":", 1)[1].lstrip())
        return events
    finally:
        connection.close()


@contextlib.contextmanager
def running_bridge(*, app_server_url: str | None, default_cwd: str | None = None):
    server = BRIDGE.RedexHttpServer(
        ("127.0.0.1", 0),
        BRIDGE.BridgeConfig(app_server_url=app_server_url, default_cwd=default_cwd),
    )
    thread = threading.Thread(target=server.serve_forever, name="test-redex-bridge", daemon=True)
    thread.start()
    try:
        yield server, thread
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]

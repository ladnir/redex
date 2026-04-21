from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from websocket import WebSocket
from websocket import WebSocketConnectionClosedException
from websocket import WebSocketException
from websocket import WebSocketTimeoutException
from websocket import create_connection


NOTIFICATIONS_TO_OPT_OUT = [
    "command/exec/outputDelta",
    "item/agentMessage/delta",
    "item/plan/delta",
    "item/fileChange/outputDelta",
    "item/reasoning/summaryTextDelta",
    "item/reasoning/textDelta",
]

DEFAULT_APP_SERVER_URL = "ws://127.0.0.1:4222"
DISCOVERY_PATH = Path.home() / ".codex" / "runtime" / "app-server.json"


class CodexAppServerError(RuntimeError):
    """Raised when the Codex app-server returns an error or an unexpected message."""


class CodexAppServerUnavailable(CodexAppServerError):
    """Raised when the Codex app-server cannot be reached."""


@dataclass(frozen=True)
class ClientIdentity:
    name: str = "redex"
    title: str = "Redex"
    version: str = "0.1.0"


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _discovered_app_server_url() -> str | None:
    try:
        payload = json.loads(DISCOVERY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    if isinstance(pid, int) and not _pid_is_alive(pid):
        return None
    url = payload.get("url")
    return url if isinstance(url, str) and url.startswith("ws://") else None


def resolve_app_server_url(explicit_url: str | None = None) -> str:
    if explicit_url:
        return explicit_url
    return _discovered_app_server_url() or DEFAULT_APP_SERVER_URL


def _epoch_seconds_to_iso(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_text_chunks(chunks: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        if chunk.get("type") != "text":
            continue
        text = chunk.get("text")
        if isinstance(text, str) and text:
            parts.append(text.rstrip())
    return "\n\n".join(part for part in parts if part).strip()


def normalize_thread(thread: dict[str, Any]) -> dict[str, Any]:
    git_info = thread.get("gitInfo")
    git_branch = git_info.get("branch") if isinstance(git_info, dict) else None
    status = thread.get("status")
    status_type = status.get("type") if isinstance(status, dict) else status
    title = thread.get("name")
    preview = thread.get("preview")
    return {
        "id": thread.get("id"),
        "title": title or preview or thread.get("id"),
        "preview": preview.strip() if isinstance(preview, str) else None,
        "cwd": thread.get("cwd"),
        "path": thread.get("path"),
        "source": thread.get("source"),
        "modelProvider": thread.get("modelProvider"),
        "createdAt": _epoch_seconds_to_iso(thread.get("createdAt")),
        "updatedAt": _epoch_seconds_to_iso(thread.get("updatedAt")),
        "status": status_type,
        "gitBranch": git_branch,
    }


def normalize_turns(thread: dict[str, Any]) -> list[dict[str, Any]]:
    turns = thread.get("turns")
    if not isinstance(turns, list):
        return []

    messages: list[dict[str, Any]] = []
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        turn_id = turn.get("id")
        turn_status = turn.get("status")
        started_at = _epoch_seconds_to_iso(turn.get("startedAt"))
        items = turn.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "userMessage":
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                text = _extract_text_chunks(content)
                if not text:
                    continue
                messages.append(
                    {
                        "turnId": turn_id,
                        "turnStatus": turn_status,
                        "timestamp": started_at,
                        "role": "user",
                        "phase": None,
                        "text": text,
                    }
                )
            elif item_type == "agentMessage":
                text = item.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                messages.append(
                    {
                        "turnId": turn_id,
                        "turnStatus": turn_status,
                        "timestamp": started_at,
                        "role": "assistant",
                        "phase": item.get("phase"),
                        "text": text.rstrip(),
                    }
                )
    return messages


def make_session_list_payload(result: dict[str, Any]) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, list):
        raise CodexAppServerError("thread/list response did not include a data array")
    return {
        "sessions": [normalize_thread(thread) for thread in data if isinstance(thread, dict)],
        "nextCursor": result.get("nextCursor"),
        "backwardsCursor": result.get("backwardsCursor"),
    }


def make_session_detail_payload(result: dict[str, Any]) -> dict[str, Any]:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise CodexAppServerError("thread/read response did not include a thread object")
    return {
        "session": normalize_thread(thread),
        "messages": normalize_turns(thread),
    }


class CodexAppServerClient:
    def __init__(
        self,
        url: str | None = None,
        *,
        identity: ClientIdentity | None = None,
        timeout: float = 10.0,
        notification_handler: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.url = resolve_app_server_url(url)
        self.identity = identity or ClientIdentity()
        self.timeout = timeout
        self.notification_handler = notification_handler
        self._socket: WebSocket | None = None

    def __enter__(self) -> "CodexAppServerClient":
        self.connect()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def connect(self) -> None:
        if self._socket is not None:
            return
        try:
            # The Codex app-server rejects browser-style Origin headers with 403,
            # so Redex suppresses Origin entirely for localhost websocket use.
            self._socket = create_connection(
                self.url,
                timeout=self.timeout,
                suppress_origin=True,
            )
        except (OSError, WebSocketException) as exc:
            fallback_url = DEFAULT_APP_SERVER_URL
            if self.url != fallback_url:
                try:
                    self._socket = create_connection(
                        fallback_url,
                        timeout=self.timeout,
                        suppress_origin=True,
                    )
                    self.url = fallback_url
                except (OSError, WebSocketException):
                    raise CodexAppServerUnavailable(
                        f"Could not connect to Codex app-server at {self.url}."
                    ) from exc
            else:
                raise CodexAppServerUnavailable(
                    f"Could not connect to Codex app-server at {self.url}."
                ) from exc
        self.initialize()

    def close(self) -> None:
        if self._socket is None:
            return
        try:
            self._socket.close()
        finally:
            self._socket = None

    def initialize(self) -> dict[str, Any]:
        result = self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.identity.name,
                    "title": self.identity.title,
                    "version": self.identity.version,
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": NOTIFICATIONS_TO_OPT_OUT,
                },
            },
        )
        self._notify("initialized")
        return result

    def list_sessions(
        self,
        *,
        limit: int = 20,
        cwd: str | None = None,
        archived: bool = False,
        search_term: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "thread/list",
            {
                "cursor": None,
                "limit": limit,
                "sortKey": None,
                "sortDirection": None,
                "modelProviders": None,
                "sourceKinds": None,
                "archived": archived,
                "cwd": cwd,
                "searchTerm": search_term,
            },
        )

    def get_session(self, session_id: str, *, include_turns: bool = True) -> dict[str, Any]:
        return self._request(
            "thread/read",
            {
                "threadId": session_id,
                "includeTurns": include_turns,
            },
        )

    def resume_session(self, session_id: str) -> dict[str, Any]:
        return self._request(
            "thread/resume",
            {
                "threadId": session_id,
                "history": None,
                "path": None,
                "model": None,
                "modelProvider": None,
                "serviceTier": None,
                "cwd": None,
                "approvalPolicy": None,
                "approvalsReviewer": None,
                "sandbox": None,
                "config": None,
                "baseInstructions": None,
                "developerInstructions": None,
                "personality": None,
                "persistExtendedHistory": False,
            },
        )

    def send_prompt(self, session_id: str, text: str) -> dict[str, Any]:
        self.resume_session(session_id)
        return self._request(
            "turn/start",
            {
                "threadId": session_id,
                "input": [
                    {
                        "type": "text",
                        "text": text,
                        "text_elements": [],
                    }
                ],
                "responsesapiClientMetadata": None,
                "cwd": None,
                "approvalPolicy": None,
                "approvalsReviewer": None,
                "sandboxPolicy": None,
                "model": None,
                "serviceTier": None,
                "effort": None,
                "summary": None,
                "personality": None,
                "collaborationMode": None,
                "outputSchema": None,
            },
        )

    def read_message(self, *, timeout: float | None = None) -> dict[str, Any] | None:
        socket = self._require_socket()
        previous_timeout = socket.gettimeout()
        if timeout is not None:
            socket.settimeout(timeout)
        try:
            return self._read_json()
        except WebSocketTimeoutException:
            return None
        finally:
            if timeout is not None and self._socket is not None:
                socket.settimeout(previous_timeout)

    def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        self._send_json(
            {
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        while True:
            message = self._read_json()
            message_id = message.get("id")
            if message_id == request_id and "result" in message:
                result = message["result"]
                if not isinstance(result, dict):
                    raise CodexAppServerError(f"{method} returned a non-object result")
                return result
            if message_id == request_id and "error" in message:
                error = message.get("error")
                raise CodexAppServerError(f"{method} failed: {json.dumps(error, indent=2)}")
            if "method" in message and "id" not in message:
                self._handle_notification(message)
                continue
            if "method" in message and "id" in message:
                raise CodexAppServerError(
                    f"{method} triggered an unsupported server request: {message.get('method')}"
                )

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"method": method}
        if params is not None:
            payload["params"] = params
        self._send_json(payload)

    def _send_json(self, payload: dict[str, Any]) -> None:
        socket = self._require_socket()
        socket.send(json.dumps(payload))

    def _read_json(self) -> dict[str, Any]:
        socket = self._require_socket()
        try:
            raw = socket.recv()
        except WebSocketConnectionClosedException as exc:
            raise CodexAppServerUnavailable(f"Codex app-server at {self.url} closed the connection") from exc
        if not isinstance(raw, str):
            raise CodexAppServerError("Codex app-server returned a non-text websocket frame")
        try:
            message = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexAppServerError(f"Codex app-server returned invalid JSON: {raw!r}") from exc
        if not isinstance(message, dict):
            raise CodexAppServerError("Codex app-server returned a non-object JSON payload")
        return message

    def _handle_notification(self, message: dict[str, Any]) -> None:
        handler = self.notification_handler
        if handler is None:
            return
        handler(message)

    def _require_socket(self) -> WebSocket:
        if self._socket is None:
            raise CodexAppServerError("Codex app-server client is not connected")
        return self._socket

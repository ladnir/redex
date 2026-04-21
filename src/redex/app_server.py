from __future__ import annotations

import json
import os
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from websocket import WebSocket
from websocket import WebSocketConnectionClosedException
from websocket import WebSocketException
from websocket import WebSocketTimeoutException
from websocket import create_connection


NOTIFICATIONS_TO_OPT_OUT = [
    "command/exec/outputDelta",
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


def _diff_stats(diff: str | None) -> tuple[int, int]:
    if not isinstance(diff, str) or not diff:
        return 0, 0
    added = 0
    deleted = 0
    for line in diff.splitlines():
        if not line:
            continue
        if line.startswith("+++ ") or line.startswith("--- ") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            deleted += 1
    return added, deleted


def _normalize_file_changes(changes: Any) -> tuple[list[dict[str, Any]], int, int]:
    if not isinstance(changes, list):
        return [], 0, 0
    normalized: list[dict[str, Any]] = []
    total_added = 0
    total_deleted = 0
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = change.get("path")
        kind = change.get("kind")
        diff = change.get("diff")
        if not isinstance(path, str):
            continue
        kind_type = kind.get("type") if isinstance(kind, dict) else None
        move_path = kind.get("move_path") if isinstance(kind, dict) else None
        added, deleted = _diff_stats(diff if isinstance(diff, str) else None)
        total_added += added
        total_deleted += deleted
        normalized.append(
            {
                "path": path,
                "kind": kind_type,
                "movePath": move_path if isinstance(move_path, str) else None,
                "diff": diff if isinstance(diff, str) else "",
                "added": added,
                "deleted": deleted,
            }
        )
    return normalized, total_added, total_deleted


def _sandbox_mode_from_policy(policy: Any) -> str | None:
    if isinstance(policy, str):
        if policy == "dangerFullAccess":
            return "danger-full-access"
        if policy == "workspaceWrite":
            return "workspace-write"
        if policy == "readOnly":
            return "read-only"
        return policy
    if not isinstance(policy, dict):
        return None
    policy_type = policy.get("type")
    if not isinstance(policy_type, str):
        return None
    if policy_type == "dangerFullAccess":
        return "danger-full-access"
    if policy_type == "workspaceWrite":
        return "workspace-write"
    if policy_type == "readOnly":
        return "read-only"
    return None


def _error_text(error: Exception) -> str:
    return str(error).lower()


def _is_pre_first_turn_error(error: Exception) -> bool:
    text = _error_text(error)
    return (
        "not materialized yet" in text
        or "includeTurns is unavailable before first user message".lower() in text
        or "no rollout found" in text
        or "rollout is empty" in text
    )


@lru_cache(maxsize=512)
def _workspace_group_for_cwd(cwd: str | None) -> tuple[str | None, str | None]:
    if not cwd:
        return None, None

    def repo_group(repo_name: str) -> tuple[str, str]:
        return f"repo:{repo_name.lower()}", repo_name

    cwd_path = Path(cwd)
    parts = cwd_path.parts
    lowered_parts = [part.lower() for part in parts]
    try:
        codex_index = lowered_parts.index(".codex")
    except ValueError:
        codex_index = -1

    if (
        codex_index >= 0
        and len(parts) > codex_index + 3
        and lowered_parts[codex_index + 1] == "worktrees"
    ):
        repo_label = parts[codex_index + 3]
        return repo_group(repo_label)

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                cwd,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return cwd, cwd_path.name or cwd

    common_dir = result.stdout.strip()
    if not common_dir:
        return cwd, cwd_path.name or cwd

    common_path = Path(common_dir)
    if common_path.name.lower() == ".git":
        repo_root = common_path.parent
    else:
        repo_root = common_path

    label = repo_root.name or str(repo_root)
    return repo_group(label)


def normalize_thread(thread: dict[str, Any]) -> dict[str, Any]:
    git_info = thread.get("gitInfo")
    git_branch = git_info.get("branch") if isinstance(git_info, dict) else None
    status = thread.get("status")
    status_type = status.get("type") if isinstance(status, dict) else status
    title = thread.get("name")
    preview = thread.get("preview")
    cwd = thread.get("cwd")
    workspace_group, workspace_group_label = _workspace_group_for_cwd(cwd if isinstance(cwd, str) else None)
    return {
        "id": thread.get("id"),
        "title": title or preview or thread.get("id"),
        "preview": preview.strip() if isinstance(preview, str) else None,
        "cwd": cwd,
        "path": thread.get("path"),
        "source": thread.get("source"),
        "modelProvider": thread.get("modelProvider"),
        "createdAt": _epoch_seconds_to_iso(thread.get("createdAt")),
        "updatedAt": _epoch_seconds_to_iso(thread.get("updatedAt")),
        "status": status_type,
        "gitBranch": git_branch,
        "workspaceGroup": workspace_group,
        "workspaceGroupLabel": workspace_group_label,
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
                        "itemId": item.get("id"),
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
                        "itemId": item.get("id"),
                        "turnId": turn_id,
                        "turnStatus": turn_status,
                        "timestamp": started_at,
                        "role": "assistant",
                        "phase": item.get("phase"),
                        "text": text.rstrip(),
                    }
                )
            elif item_type == "fileChange":
                changes, added, deleted = _normalize_file_changes(item.get("changes"))
                if not changes:
                    continue
                messages.append(
                    {
                        "itemId": item.get("id"),
                        "turnId": turn_id,
                        "turnStatus": turn_status,
                        "timestamp": started_at,
                        "role": "assistant",
                        "phase": None,
                        "type": "fileChange",
                        "fileChangeStatus": item.get("status"),
                        "changes": changes,
                        "changeSummary": {
                            "files": len(changes),
                            "added": added,
                            "deleted": deleted,
                        },
                        "text": "",
                    }
                )
    return messages


def normalize_turn_page(turns: list[dict[str, Any]], *, sort_direction: str = "desc") -> list[dict[str, Any]]:
    ordered_turns = list(turns)
    if sort_direction.lower() == "desc":
        ordered_turns.reverse()
    return normalize_turns({"turns": ordered_turns})


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


def make_session_detail_page_payload(
    thread_result: dict[str, Any],
    turns_result: dict[str, Any],
    *,
    sort_direction: str = "desc",
) -> dict[str, Any]:
    thread = thread_result.get("thread")
    if not isinstance(thread, dict):
        raise CodexAppServerError("thread/read response did not include a thread object")
    turns = turns_result.get("data")
    if not isinstance(turns, list):
        raise CodexAppServerError("thread/turns/list response did not include a data array")
    next_cursor = turns_result.get("nextCursor")
    backwards_cursor = turns_result.get("backwardsCursor")
    return {
        "session": normalize_thread(thread),
        "messages": normalize_turn_page([turn for turn in turns if isinstance(turn, dict)], sort_direction=sort_direction),
        "nextCursor": next_cursor if isinstance(next_cursor, str) else None,
        "backwardsCursor": backwards_cursor if isinstance(backwards_cursor, str) else None,
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

    def list_session_turns(
        self,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        sort_direction: str | None = None,
    ) -> dict[str, Any]:
        return self._request(
            "thread/turns/list",
            {
                "threadId": session_id,
                "cursor": cursor,
                "limit": limit,
                "sortDirection": sort_direction,
            },
        )

    def create_session(self, *, cwd: str | None = None, template_session_id: str | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": None,
            "modelProvider": None,
            "cwd": cwd,
            "approvalPolicy": None,
            "approvalsReviewer": None,
            "sandbox": None,
            "config": None,
            "ephemeral": False,
            "sessionStartSource": None,
            "persistExtendedHistory": True,
            "baseInstructions": None,
            "developerInstructions": None,
            "personality": None,
        }
        if template_session_id:
            template = self.resume_session(template_session_id)
            params.update(
                {
                    "model": template.get("model"),
                    "modelProvider": template.get("modelProvider"),
                    "approvalPolicy": template.get("approvalPolicy"),
                    "approvalsReviewer": template.get("approvalsReviewer"),
                    "sandbox": _sandbox_mode_from_policy(template.get("sandbox")),
                    "config": template.get("config"),
                    "baseInstructions": template.get("baseInstructions"),
                    "developerInstructions": template.get("developerInstructions"),
                    "personality": template.get("personality"),
                }
            )
            if not cwd:
                template_cwd = template.get("cwd")
                if isinstance(template_cwd, str) and template_cwd:
                    params["cwd"] = template_cwd
        return self._request("thread/start", params)

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
        try:
            self.resume_session(session_id)
        except CodexAppServerError as exc:
            if not _is_pre_first_turn_error(exc):
                raise
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

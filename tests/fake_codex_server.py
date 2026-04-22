from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection


def _now() -> float:
    return time.time()


def _text_content(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": text}]


def make_user_item(*, item_id: str, text: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "userMessage",
        "content": _text_content(text),
    }


def make_agent_item(*, item_id: str, phase: str, text: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "agentMessage",
        "phase": phase,
        "text": text,
    }


def make_thread(
    *,
    thread_id: str,
    name: str,
    cwd: str,
    preview: str | None = None,
    updated_at: float | None = None,
    turns: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    timestamp = updated_at or _now()
    return {
        "id": thread_id,
        "name": name,
        "preview": preview or name,
        "cwd": cwd,
        "path": cwd,
        "source": "desktop",
        "modelProvider": "openai",
        "createdAt": timestamp - 30,
        "updatedAt": timestamp,
        "status": {"type": "idle"},
        "gitInfo": {"branch": "main"},
        "turns": list(turns or []),
    }


def make_turn(
    *,
    turn_id: str,
    status: str,
    started_at: float | None = None,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "id": turn_id,
        "status": status,
        "startedAt": started_at or _now(),
        "items": list(items),
    }


@dataclass
class TurnScript:
    final_text: str
    commentary_text: str = "Thinking..."


def build_sample_scenario() -> "FakeCodexScenario":
    thread_id = "thread-1"
    first_turn = make_turn(
        turn_id="turn-0",
        status="completed",
        items=[
            make_user_item(item_id="user-0", text="say hi"),
            make_agent_item(item_id="assistant-0", phase="final_answer", text="hi"),
        ],
    )
    thread = make_thread(
        thread_id=thread_id,
        name="Sample thread",
        cwd=r"C:\Users\peter\repo\redex",
        preview="say hi",
        turns=[first_turn],
    )
    scenario = FakeCodexScenario(threads={thread_id: thread})
    scenario.resume_payloads[thread_id] = {
        "threadId": thread_id,
        "model": "gpt-5.4",
        "modelProvider": "openai",
        "approvalPolicy": "auto",
        "approvalsReviewer": None,
        "sandbox": {"type": "dangerFullAccess"},
        "config": None,
        "cwd": thread["cwd"],
        "baseInstructions": None,
        "developerInstructions": None,
        "personality": None,
    }
    scenario.turn_scripts[thread_id] = [
        TurnScript(
            commentary_text="Checking the current system date now so I can give you the exact local date.",
            final_text="Monday, April 20, 2026.",
        )
    ]
    return scenario


def build_large_scenario(
    *,
    session_count: int,
    turns_per_thread: int,
    cwd_root: str = r"C:\Users\peter\repo",
) -> "FakeCodexScenario":
    threads: dict[str, dict[str, Any]] = {}
    now = _now()
    for session_index in range(session_count):
        thread_id = f"thread-{session_index:04d}"
        turns: list[dict[str, Any]] = []
        for turn_index in range(turns_per_thread):
            turns.append(
                make_turn(
                    turn_id=f"{thread_id}-turn-{turn_index:04d}",
                    status="completed",
                    started_at=now - ((session_count - session_index) * 10 + turn_index),
                    items=[
                        make_user_item(
                            item_id=f"{thread_id}-user-{turn_index:04d}",
                            text=f"user prompt {turn_index}",
                        ),
                        make_agent_item(
                            item_id=f"{thread_id}-assistant-{turn_index:04d}",
                            phase="final_answer",
                            text=f"assistant reply {turn_index}",
                        ),
                    ],
                )
            )
        thread = make_thread(
            thread_id=thread_id,
            name=f"Load thread {session_index}",
            cwd=f"{cwd_root}\\repo-{session_index % 7}",
            preview=f"assistant reply {turns_per_thread - 1}",
            updated_at=now - session_index,
            turns=turns,
        )
        threads[thread_id] = thread
    scenario = FakeCodexScenario(threads=threads)
    for thread_id, thread in threads.items():
        scenario.resume_payloads[thread_id] = {
            "threadId": thread_id,
            "model": "gpt-5.4",
            "modelProvider": "openai",
            "approvalPolicy": "auto",
            "approvalsReviewer": None,
            "sandbox": {"type": "dangerFullAccess"},
            "config": None,
            "cwd": thread["cwd"],
            "baseInstructions": None,
            "developerInstructions": None,
            "personality": None,
        }
    return scenario


class FakeCodexScenario:
    def __init__(self, *, threads: dict[str, dict[str, Any]] | None = None) -> None:
        self.threads: dict[str, dict[str, Any]] = deepcopy(threads or {})
        self.resume_payloads: dict[str, dict[str, Any]] = {}
        self.turn_scripts: dict[str, list[TurnScript]] = {}
        self._lock = threading.Lock()

    def thread_list(self) -> list[dict[str, Any]]:
        with self._lock:
            threads = [
                {key: deepcopy(value) for key, value in thread.items() if key != "turns"}
                for thread in self.threads.values()
            ]
        threads.sort(key=lambda thread: thread.get("updatedAt", 0), reverse=True)
        return threads

    def get_thread(self, thread_id: str, *, include_turns: bool) -> dict[str, Any]:
        with self._lock:
            thread = deepcopy(self.threads[thread_id])
        if not include_turns:
            thread.pop("turns", None)
        return thread

    def list_turns(
        self,
        thread_id: str,
        *,
        limit: int | None,
        sort_direction: str | None,
        cursor: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None, str | None]:
        with self._lock:
            turns = deepcopy(self.threads[thread_id].get("turns", []))
        sort_value = (sort_direction or "asc").lower()
        if sort_value == "desc":
            end = len(turns)
            if isinstance(cursor, str) and cursor.isdigit():
                end = max(0, min(len(turns), int(cursor)))
            if isinstance(limit, int) and limit > 0:
                start = max(0, end - limit)
            else:
                start = 0
            page = turns[start:end]
            page.reverse()
            next_cursor = str(start) if start > 0 else None
            backwards_cursor = str(end) if end < len(turns) else None
            return page, next_cursor, backwards_cursor
        if isinstance(limit, int) and limit > 0:
            turns = turns[:limit]
        return turns, None, None

    def resume(self, thread_id: str) -> dict[str, Any]:
        payload = self.resume_payloads.get(thread_id)
        if payload:
            return deepcopy(payload)
        thread = self.get_thread(thread_id, include_turns=False)
        return {
            "threadId": thread_id,
            "model": "gpt-5.4",
            "modelProvider": "openai",
            "approvalPolicy": "auto",
            "approvalsReviewer": None,
            "sandbox": {"type": "dangerFullAccess"},
            "config": None,
            "cwd": thread.get("cwd"),
            "baseInstructions": None,
            "developerInstructions": None,
            "personality": None,
        }

    def create_thread(self, *, cwd: str | None) -> dict[str, Any]:
        thread_id = f"thread-{uuid.uuid4().hex[:8]}"
        thread = make_thread(
            thread_id=thread_id,
            name="New chat",
            cwd=cwd or r"C:\Users\peter\repo\redex",
            preview="",
            turns=[],
        )
        with self._lock:
            self.threads[thread_id] = thread
        return deepcopy(thread)

    def start_turn(self, thread_id: str, text: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        with self._lock:
            thread = self.threads[thread_id]
            script_queue = self.turn_scripts.setdefault(thread_id, [])
            script = script_queue.pop(0) if script_queue else TurnScript(final_text=f"Echo: {text}")
            turn_id = f"turn-{uuid.uuid4().hex[:8]}"
            user_item_id = f"user-{uuid.uuid4().hex[:8]}"
            commentary_item_id = f"commentary-{uuid.uuid4().hex[:8]}"
            final_item_id = f"assistant-{uuid.uuid4().hex[:8]}"
            turn = make_turn(
                turn_id=turn_id,
                status="completed",
                items=[
                    make_user_item(item_id=user_item_id, text=text),
                    make_agent_item(item_id=commentary_item_id, phase="commentary", text=script.commentary_text),
                    make_agent_item(item_id=final_item_id, phase="final_answer", text=script.final_text),
                ],
            )
            thread.setdefault("turns", []).append(turn)
            thread["preview"] = text
            thread["updatedAt"] = _now()

        notifications = [
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": user_item_id,
                        "type": "userMessage",
                        "content": _text_content(text),
                    },
                },
            },
            {
                "method": "item/started",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": commentary_item_id,
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "",
                    },
                },
            },
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "itemId": commentary_item_id,
                    "delta": script.commentary_text,
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": commentary_item_id,
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": script.commentary_text,
                    },
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "threadId": thread_id,
                    "turnId": turn_id,
                    "item": {
                        "id": final_item_id,
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": script.final_text,
                    },
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {
                        "id": turn_id,
                        "threadId": thread_id,
                        "status": "completed",
                    },
                },
            },
        ]
        return (
            {
                "turn": {
                    "id": turn_id,
                    "threadId": thread_id,
                    "status": "completed",
                }
            },
            notifications,
        )


class FakeCodexAppServer:
    def __init__(self, scenario: FakeCodexScenario | None = None) -> None:
        self.scenario = scenario or build_sample_scenario()
        self.url: str | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: websockets.asyncio.server.Server | None = None
        self._started = threading.Event()
        self._stop_requested = threading.Event()
        self._subscriptions: dict[ServerConnection, set[str]] = {}
        self.method_counts: Counter[str] = Counter()
        self._delayed_methods: dict[tuple[str, str | None], list[float]] = {}

    def start(self) -> "FakeCodexAppServer":
        self._thread = threading.Thread(target=self._run, name="fake-codex-app-server", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=5):
            raise RuntimeError("Fake Codex app-server did not start in time.")
        return self

    def stop(self) -> None:
        if not self._thread:
            return
        self._stop_requested.set()
        if self._loop is not None:
            self._loop.call_soon_threadsafe(lambda: None)
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeCodexAppServer":
        return self.start()

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.stop()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    async def _serve(self) -> None:
        self._server = await websockets.serve(self._handle_connection, "127.0.0.1", 0)
        socket = self._server.sockets[0]
        port = socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        self._started.set()
        try:
            while not self._stop_requested.is_set():
                await asyncio.sleep(0.05)
        finally:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        self._subscriptions[websocket] = set()
        try:
            async for raw in websocket:
                message = json.loads(raw)
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                method = message.get("method")
                params = message.get("params") or {}
                if request_id is None and isinstance(method, str):
                    continue
                try:
                    result = await self._dispatch(websocket, str(method), params if isinstance(params, dict) else {})
                except KeyError as exc:
                    await websocket.send(
                        json.dumps(
                            {
                                "id": request_id,
                                "error": {
                                    "code": -32602,
                                    "message": f"Unknown thread: {exc.args[0]}",
                                },
                            }
                        )
                    )
                    continue
                await websocket.send(json.dumps({"id": request_id, "result": result}))
        finally:
            self._subscriptions.pop(websocket, None)

    async def _broadcast_notifications(self, thread_id: str, notifications: list[dict[str, Any]]) -> None:
        if not notifications:
            return
        for websocket, subscriptions in list(self._subscriptions.items()):
            if thread_id not in subscriptions:
                continue
            for notification in notifications:
                await websocket.send(json.dumps(notification))

    def trigger_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        if self._loop is None:
            raise RuntimeError("Fake Codex app-server is not running.")
        future = asyncio.run_coroutine_threadsafe(self._trigger_turn(thread_id, text), self._loop)
        return future.result(timeout=5)

    def trigger_stream(
        self,
        thread_id: str,
        *,
        prompt_text: str,
        deltas: list[str],
        final_text: str,
        phase: str = "final_answer",
        delay_seconds: float = 0.05,
    ) -> None:
        if self._loop is None:
            raise RuntimeError("Fake Codex app-server is not running.")
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                self._trigger_stream(
                    thread_id,
                    prompt_text=prompt_text,
                    deltas=deltas,
                    final_text=final_text,
                    phase=phase,
                    delay_seconds=delay_seconds,
                )
            )
        )

    def reset(self, scenario: FakeCodexScenario) -> None:
        if self._loop is None:
            raise RuntimeError("Fake Codex app-server is not running.")
        future = asyncio.run_coroutine_threadsafe(self._reset(scenario), self._loop)
        future.result(timeout=5)

    def queue_delay(self, *, method: str, seconds: float, thread_id: str | None = None) -> None:
        if self._loop is None:
            raise RuntimeError("Fake Codex app-server is not running.")
        future = asyncio.run_coroutine_threadsafe(
            self._queue_delay(method=method, seconds=seconds, thread_id=thread_id),
            self._loop,
        )
        future.result(timeout=5)

    async def _trigger_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        result, notifications = self.scenario.start_turn(thread_id, text)
        await self._broadcast_notifications(thread_id, notifications)
        return result

    async def _trigger_stream(
        self,
        thread_id: str,
        *,
        prompt_text: str,
        deltas: list[str],
        final_text: str,
        phase: str,
        delay_seconds: float,
    ) -> None:
        turn_id = f"turn-{uuid.uuid4().hex[:8]}"
        user_item_id = f"user-{uuid.uuid4().hex[:8]}"
        assistant_item_id = f"assistant-{uuid.uuid4().hex[:8]}"
        with self.scenario._lock:
            thread = self.scenario.threads[thread_id]
            turn = make_turn(
                turn_id=turn_id,
                status="running",
                items=[
                    make_user_item(item_id=user_item_id, text=prompt_text),
                    {
                        "id": assistant_item_id,
                        "type": "agentMessage",
                        "phase": phase,
                        "text": "",
                    },
                ],
            )
            thread.setdefault("turns", []).append(turn)
            thread["preview"] = prompt_text
            thread["updatedAt"] = _now()

        await self._broadcast_notifications(
            thread_id,
            [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "id": user_item_id,
                            "type": "userMessage",
                            "content": _text_content(prompt_text),
                        },
                    },
                },
                {
                    "method": "item/started",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "id": assistant_item_id,
                            "type": "agentMessage",
                            "phase": phase,
                            "text": "",
                        },
                    },
                }
            ],
        )

        accumulated = ""
        for chunk in deltas:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
            accumulated += chunk
            await self._broadcast_notifications(
                thread_id,
                [
                    {
                        "method": "item/agentMessage/delta",
                        "params": {
                            "threadId": thread_id,
                            "turnId": turn_id,
                            "itemId": assistant_item_id,
                            "delta": chunk,
                        },
                    }
                ],
            )

        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)

        with self.scenario._lock:
            thread = self.scenario.threads[thread_id]
            turn = next((candidate for candidate in thread.get("turns", []) if candidate.get("id") == turn_id), None)
            if turn is not None:
                turn["status"] = "completed"
                for item in turn.get("items", []):
                    if item.get("id") == assistant_item_id:
                        item["text"] = final_text
            thread["preview"] = prompt_text
            thread["updatedAt"] = _now()

        await self._broadcast_notifications(
            thread_id,
            [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": thread_id,
                        "turnId": turn_id,
                        "item": {
                            "id": assistant_item_id,
                            "type": "agentMessage",
                            "phase": phase,
                            "text": final_text,
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": thread_id,
                        "turn": {
                            "id": turn_id,
                            "threadId": thread_id,
                            "status": "completed",
                        },
                    },
                },
            ],
        )

    async def _reset(self, scenario: FakeCodexScenario) -> None:
        self.scenario = scenario
        self.method_counts.clear()
        self._delayed_methods.clear()

    async def _queue_delay(self, *, method: str, seconds: float, thread_id: str | None) -> None:
        key = (method, thread_id)
        self._delayed_methods.setdefault(key, []).append(max(0.0, seconds))

    async def _apply_delay_if_needed(self, method: str, thread_id: str | None) -> None:
        keys = ((method, thread_id), (method, None))
        for key in keys:
            queue = self._delayed_methods.get(key)
            if not queue:
                continue
            delay = queue.pop(0)
            if not queue:
                self._delayed_methods.pop(key, None)
            if delay > 0:
                await asyncio.sleep(delay)
            return

    async def _dispatch(
        self,
        websocket: ServerConnection,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        self.method_counts[method] += 1
        thread_id = params.get("threadId") if isinstance(params.get("threadId"), str) else None
        await self._apply_delay_if_needed(method, thread_id)
        if method == "initialize":
            return {"serverInfo": {"name": "fake-codex", "version": "0.1"}}
        if method == "thread/list":
            return {"data": self.scenario.thread_list(), "nextCursor": None, "backwardsCursor": None}
        if method == "thread/read":
            thread_id = str(params["threadId"])
            include_turns = bool(params.get("includeTurns", True))
            return {"thread": self.scenario.get_thread(thread_id, include_turns=include_turns)}
        if method == "thread/turns/list":
            thread_id = str(params["threadId"])
            limit = params.get("limit")
            sort_direction = params.get("sortDirection")
            cursor = params.get("cursor")
            turns, next_cursor, backwards_cursor = self.scenario.list_turns(
                thread_id,
                limit=limit if isinstance(limit, int) else None,
                sort_direction=sort_direction if isinstance(sort_direction, str) else None,
                cursor=cursor if isinstance(cursor, str) else None,
            )
            return {
                "data": turns,
                "nextCursor": next_cursor,
                "backwardsCursor": backwards_cursor,
            }
        if method == "thread/resume":
            thread_id = str(params["threadId"])
            self._subscriptions.setdefault(websocket, set()).add(thread_id)
            return self.scenario.resume(thread_id)
        if method == "thread/start":
            thread = self.scenario.create_thread(cwd=params.get("cwd") if isinstance(params.get("cwd"), str) else None)
            return {"thread": thread}
        if method == "turn/start":
            thread_id = str(params["threadId"])
            input_items = params.get("input")
            text = ""
            if isinstance(input_items, list):
                for item in input_items:
                    if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                        text = item["text"]
                        break
            result, notifications = self.scenario.start_turn(thread_id, text)
            await self._broadcast_notifications(thread_id, notifications)
            return result
        raise RuntimeError(f"Unsupported method: {method}")

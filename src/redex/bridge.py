from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlparse

from .app_server import CodexAppServerClient
from .app_server import CodexAppServerError
from .app_server import CodexAppServerUnavailable
from .app_server import make_session_detail_payload
from .app_server import make_session_list_payload


EVENT_BACKLOG_LIMIT = 500


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Redex</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f4f0e8;
      --panel: rgba(255, 252, 247, 0.92);
      --ink: #1d1a17;
      --muted: #6c6256;
      --line: rgba(58, 45, 28, 0.15);
      --accent: #0f6a5b;
      --accent-soft: rgba(15, 106, 91, 0.12);
      --user: #f3d9b1;
      --assistant: #d7ece4;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Segoe UI", ui-sans-serif, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(15, 106, 91, 0.12), transparent 34rem),
        radial-gradient(circle at top right, rgba(194, 132, 54, 0.10), transparent 24rem),
        var(--bg);
    }
    .app {
      min-height: 100vh;
      max-width: 72rem;
      margin: 0 auto;
      padding: 1rem;
      display: grid;
      gap: 1rem;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 1rem;
      box-shadow: 0 1rem 2rem rgba(24, 18, 12, 0.06);
      backdrop-filter: blur(10px);
    }
    .hero {
      padding: 1.25rem;
    }
    .hero h1 {
      margin: 0 0 0.5rem;
      font-size: 1.5rem;
    }
    .hero p {
      margin: 0;
      color: var(--muted);
      line-height: 1.4;
    }
    .workspace-pill {
      display: inline-block;
      margin-top: 0.75rem;
      padding: 0.4rem 0.65rem;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.85rem;
    }
    .layout {
      display: grid;
      gap: 1rem;
    }
    .sidebar,
    .session {
      padding: 1rem;
    }
    .toolbar {
      display: flex;
      gap: 0.5rem;
      align-items: center;
      margin-bottom: 0.75rem;
    }
    button,
    textarea {
      font: inherit;
    }
    button {
      border: 0;
      border-radius: 0.85rem;
      padding: 0.7rem 1rem;
      background: var(--accent);
      color: white;
      cursor: pointer;
    }
    button.secondary {
      background: rgba(29, 26, 23, 0.08);
      color: var(--ink);
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
    }
    .session-list {
      display: grid;
      gap: 0.6rem;
    }
    .session-card {
      width: 100%;
      text-align: left;
      background: white;
      color: var(--ink);
      border: 1px solid var(--line);
      padding: 0.9rem;
    }
    .session-card.active {
      outline: 2px solid rgba(15, 106, 91, 0.35);
      background: rgba(15, 106, 91, 0.07);
    }
    .session-card strong {
      display: block;
      margin-bottom: 0.3rem;
    }
    .meta,
    .preview,
    .status,
    .empty,
    .error {
      color: var(--muted);
      font-size: 0.92rem;
      line-height: 1.35;
    }
    .conversation {
      display: grid;
      gap: 0.75rem;
      margin: 1rem 0;
    }
    .bubble {
      padding: 0.85rem 0.95rem;
      border-radius: 1rem;
      border: 1px solid var(--line);
      white-space: pre-wrap;
      line-height: 1.45;
    }
    .bubble.user {
      background: var(--user);
    }
    .bubble.assistant {
      background: var(--assistant);
    }
    .bubble-header {
      display: flex;
      gap: 0.5rem;
      align-items: baseline;
      margin-bottom: 0.4rem;
      font-size: 0.82rem;
      color: var(--muted);
    }
    textarea {
      width: 100%;
      min-height: 7rem;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 1rem;
      padding: 0.9rem;
      background: white;
    }
    .composer-actions {
      display: flex;
      gap: 0.5rem;
      justify-content: flex-end;
      margin-top: 0.75rem;
    }
    @media (min-width: 860px) {
      .layout {
        grid-template-columns: 22rem 1fr;
        align-items: start;
      }
      .sidebar,
      .session {
        min-height: 70vh;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <section class="panel hero">
      <h1>Redex</h1>
      <p>External control for local Codex chats. Pick a session, read the transcript, and push a real new prompt into the existing chat.</p>
      <div class="workspace-pill">Default workspace: __DEFAULT_CWD__</div>
    </section>

    <section class="layout">
      <aside class="panel sidebar">
        <div class="toolbar">
          <button id="refreshButton" class="secondary" type="button">Refresh</button>
        </div>
        <div id="sessionList" class="session-list"></div>
      </aside>

      <main class="panel session">
        <div class="toolbar">
          <button id="reloadSessionButton" class="secondary" type="button" disabled>Reload Session</button>
        </div>
        <div id="sessionMeta" class="meta">Select a session.</div>
        <div id="conversation" class="conversation"></div>
        <form id="composer">
          <textarea id="promptInput" placeholder="Send a new prompt into this session..." disabled></textarea>
          <div class="composer-actions">
            <button id="sendButton" type="submit" disabled>Send Prompt</button>
          </div>
        </form>
      </main>
    </section>
  </div>

  <script>
    const state = {
      sessions: [],
      activeSessionId: null,
      defaultCwd: "__DEFAULT_CWD__",
      eventSource: null,
      eventSourceSessionId: null,
      eventSourceHealthy: false,
      activeReloadTimer: null,
      sessionReloadTimer: null,
      activePollTimer: null,
      sessionPollTimer: null,
    };

    const sessionList = document.getElementById("sessionList");
    const sessionMeta = document.getElementById("sessionMeta");
    const conversation = document.getElementById("conversation");
    const promptInput = document.getElementById("promptInput");
    const sendButton = document.getElementById("sendButton");
    const reloadSessionButton = document.getElementById("reloadSessionButton");

    function escapeHtml(value) {
      return value
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function setStatus(message, isError = false) {
      sessionMeta.className = isError ? "error" : "meta";
      sessionMeta.textContent = message;
    }

    function renderSessions() {
      if (!state.sessions.length) {
        sessionList.innerHTML = '<div class="empty">No sessions found yet.</div>';
        return;
      }
      sessionList.innerHTML = state.sessions.map((session) => {
        const activeClass = session.id === state.activeSessionId ? "active" : "";
        return `
          <button class="session-card ${activeClass}" type="button" data-session-id="${session.id}">
            <strong>${escapeHtml(session.title || session.id)}</strong>
            <div class="preview">${escapeHtml(session.preview || "")}</div>
            <div class="status">${escapeHtml(session.status || "unknown")} - ${escapeHtml(session.updatedAt || "")}</div>
          </button>
        `;
      }).join("");
      for (const element of sessionList.querySelectorAll("[data-session-id]")) {
        element.addEventListener("click", () => loadSession(element.dataset.sessionId));
      }
    }

    function renderConversation(detail) {
      const messages = detail.messages || [];
      if (!messages.length) {
        conversation.innerHTML = '<div class="empty">No persisted transcript items yet.</div>';
        return;
      }
      conversation.innerHTML = messages.map((message) => `
        <article class="bubble ${message.role}">
          <div class="bubble-header">
            <strong>${escapeHtml(message.role)}</strong>
            <span>${escapeHtml(message.phase || "")}</span>
            <span>${escapeHtml(message.timestamp || "")}</span>
          </div>
          <div>${escapeHtml(message.text || "")}</div>
        </article>
      `).join("");
    }

    function renderSessionDetail(detail) {
      const session = detail.session || {};
      setStatus(`${session.title || session.id} - ${session.status || "unknown"} - ${session.updatedAt || ""}`);
      renderConversation(detail);
    }

    async function fetchJson(url, init) {
      const response = await fetch(url, init);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.error || `Request failed: ${response.status}`);
      }
      return data;
    }

    async function loadSessions() {
      const suffix = state.defaultCwd && state.defaultCwd !== "all workspaces"
        ? `?cwd=${encodeURIComponent(state.defaultCwd)}&limit=30`
        : "?limit=30";
      const data = await fetchJson(`/api/sessions${suffix}`);
      state.sessions = data.sessions || [];
      if (!state.activeSessionId && state.sessions.length) {
        state.activeSessionId = state.sessions[0].id;
      }
      renderSessions();
      if (state.activeSessionId) {
        await loadSession(state.activeSessionId);
      } else {
        setStatus("No session selected.");
      }
      ensurePolling();
    }

    async function refreshSessionListOnly() {
      const suffix = state.defaultCwd && state.defaultCwd !== "all workspaces"
        ? `?cwd=${encodeURIComponent(state.defaultCwd)}&limit=30`
        : "?limit=30";
      const data = await fetchJson(`/api/sessions${suffix}`);
      state.sessions = data.sessions || [];
      renderSessions();
    }

    async function loadSession(sessionId) {
      state.activeSessionId = sessionId;
      ensureEventSource(sessionId);
      renderSessions();
      promptInput.disabled = true;
      sendButton.disabled = true;
      reloadSessionButton.disabled = true;
      setStatus("Loading session...");
      try {
        const detail = await fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}`);
        renderSessionDetail(detail);
        promptInput.disabled = false;
        sendButton.disabled = false;
        reloadSessionButton.disabled = false;
      } catch (error) {
        conversation.innerHTML = "";
        setStatus(error.message || String(error), true);
      }
    }

    async function refreshActiveSessionSilently() {
      if (!state.activeSessionId || document.hidden) {
        return;
      }
      try {
        const detail = await fetchJson(`/api/sessions/${encodeURIComponent(state.activeSessionId)}`);
        renderSessionDetail(detail);
      } catch {
        // Keep the currently rendered session if a transient refresh fails.
      }
    }

    async function sendPrompt(event) {
      event.preventDefault();
      if (!state.activeSessionId) {
        return;
      }
      const text = promptInput.value.trim();
      if (!text) {
        return;
      }
      promptInput.disabled = true;
      sendButton.disabled = true;
      setStatus("Sending prompt...");
      try {
        const payload = await fetchJson(`/api/sessions/${encodeURIComponent(state.activeSessionId)}/prompt`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text }),
        });
        promptInput.value = "";
        setStatus(`Prompt accepted. Turn ${payload.turnId || ""} is ${payload.status || "in progress"}.`);
        await loadSession(state.activeSessionId);
      } catch (error) {
        setStatus(error.message || String(error), true);
      } finally {
        promptInput.disabled = false;
        sendButton.disabled = false;
      }
    }

    function ensureEventSource(sessionId) {
      if (state.eventSource && state.eventSourceSessionId === sessionId) {
        return;
      }
      if (state.eventSource) {
        state.eventSource.close();
      }
      state.eventSourceSessionId = sessionId;
      state.eventSourceHealthy = false;
      state.eventSource = new EventSource(`/api/events?sessionId=${encodeURIComponent(sessionId)}`);
      state.eventSource.onopen = () => {
        state.eventSourceHealthy = true;
      };
      state.eventSource.addEventListener("notification", (event) => {
        state.eventSourceHealthy = true;
        const payload = JSON.parse(event.data);
        handleLiveEvent(payload);
      });
      state.eventSource.onerror = () => {
        state.eventSourceHealthy = false;
        // EventSource reconnects automatically; keep the UI usable while it does.
      };
    }

    function eventThreadId(payload) {
      const params = payload.params || {};
      if (params.threadId) {
        return params.threadId;
      }
      if (params.thread && params.thread.id) {
        return params.thread.id;
      }
      if (params.turn && params.turn.threadId) {
        return params.turn.threadId;
      }
      if (params.item && params.item.threadId) {
        return params.item.threadId;
      }
      return null;
    }

    function handleLiveEvent(payload) {
      const method = payload.method || "";
      if (method === "redex/ready" || method === "redex/subscribed") {
        return;
      }
      const threadId = eventThreadId(payload);
      const affectsActive = threadId && threadId === state.activeSessionId;
      if (method.startsWith("thread/")) {
        scheduleSessionListReload();
      }
      if (affectsActive && (method.startsWith("thread/") || method.startsWith("turn/") || method.startsWith("item/"))) {
        scheduleActiveSessionReload();
      }
    }

    function scheduleActiveSessionReload() {
      clearTimeout(state.activeReloadTimer);
      state.activeReloadTimer = setTimeout(() => {
        if (state.activeSessionId) {
          refreshActiveSessionSilently();
        }
      }, 120);
    }

    function scheduleSessionListReload() {
      clearTimeout(state.sessionReloadTimer);
      state.sessionReloadTimer = setTimeout(() => {
        refreshSessionListOnly().catch(() => {});
      }, 180);
    }

    function ensurePolling() {
      if (!state.activePollTimer) {
        state.activePollTimer = setInterval(() => {
          if (!document.hidden && !state.eventSourceHealthy) {
            refreshActiveSessionSilently();
          }
        }, 1500);
      }
      if (!state.sessionPollTimer) {
        state.sessionPollTimer = setInterval(() => {
          if (!document.hidden && !state.eventSourceHealthy) {
            refreshSessionListOnly().catch(() => {});
          }
        }, 5000);
      }
    }

    document.getElementById("refreshButton").addEventListener("click", () => loadSessions());
    reloadSessionButton.addEventListener("click", () => {
      if (state.activeSessionId) {
        loadSession(state.activeSessionId);
      }
    });
    document.getElementById("composer").addEventListener("submit", sendPrompt);

    loadSessions().catch((error) => setStatus(error.message || String(error), true));
  </script>
</body>
</html>
"""


@dataclass(frozen=True)
class BridgeConfig:
    app_server_url: str | None
    default_cwd: str | None


class LiveEventHub:
    def __init__(self, app_server_url: str | None) -> None:
        self.app_server_url = app_server_url
        self._events: deque[dict[str, Any]] = deque(maxlen=EVENT_BACKLOG_LIMIT)
        self._condition = threading.Condition()
        self._pending_subscriptions: set[str] = set()
        self._subscribed_sessions: set[str] = set()
        self._commands: queue.SimpleQueue[str] = queue.SimpleQueue()
        self._next_event_id = 1
        self._thread = threading.Thread(target=self._run, name="redex-live-event-hub", daemon=True)
        self._thread.start()

    def subscribe_session(self, session_id: str) -> None:
        with self._condition:
            if session_id in self._subscribed_sessions or session_id in self._pending_subscriptions:
                return
            self._pending_subscriptions.add(session_id)
        self._commands.put(session_id)

    def wait_for_event(self, last_event_id: int, *, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self._events:
                    if event["id"] > last_event_id:
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _run(self) -> None:
        client: CodexAppServerClient | None = None
        while True:
            try:
                if client is None:
                    client = CodexAppServerClient(
                        self.app_server_url,
                        notification_handler=self._publish_notification,
                    )
                    client.connect()
                    self._publish("redex/connected", {"appServerUrl": self.app_server_url})
                    self._mark_all_subscriptions_pending()
                self._drain_subscription_commands(client)
                message = client.read_message(timeout=0.5)
                if message is None:
                    continue
                if "method" in message and "id" not in message:
                    self._publish_notification(message)
            except CodexAppServerError as exc:
                self._publish("redex/error", {"message": str(exc)})
                if client is not None:
                    client.close()
                    client = None
                self._mark_all_subscriptions_pending()
                time.sleep(1.0)

    def _drain_subscription_commands(self, client: CodexAppServerClient) -> None:
        while True:
            try:
                session_id = self._commands.get_nowait()
            except queue.Empty:
                break
            self._subscribe_now(client, session_id)

    def _subscribe_now(self, client: CodexAppServerClient, session_id: str) -> None:
        with self._condition:
            if session_id in self._subscribed_sessions:
                self._pending_subscriptions.discard(session_id)
                return
        client.resume_session(session_id)
        with self._condition:
            self._pending_subscriptions.discard(session_id)
            self._subscribed_sessions.add(session_id)
        self._publish("redex/subscribed", {"threadId": session_id})

    def _mark_all_subscriptions_pending(self) -> None:
        with self._condition:
            for session_id in self._subscribed_sessions:
                if session_id not in self._pending_subscriptions:
                    self._pending_subscriptions.add(session_id)
                    self._commands.put(session_id)
            self._subscribed_sessions.clear()

    def _publish_notification(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = message.get("params")
        self._publish(method, params if isinstance(params, dict) else {})

    def _publish(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._condition:
            event = {
                "id": self._next_event_id,
                "method": method,
                "params": params or {},
            }
            self._next_event_id += 1
            self._events.append(event)
            self._condition.notify_all()


class RedexHttpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], config: BridgeConfig) -> None:
        super().__init__(server_address, RedexHandler)
        self.config = config
        self.live_event_hub = LiveEventHub(config.app_server_url)


class RedexHandler(BaseHTTPRequestHandler):
    server: RedexHttpServer

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._handle_index()
            return
        if parsed.path == "/healthz":
            self._handle_health()
            return
        if parsed.path == "/api/sessions":
            self._handle_list_sessions(parsed.query)
            return
        if parsed.path == "/api/events":
            self._handle_events(parsed.query)
            return
        if parsed.path.startswith("/api/sessions/"):
            session_id = parsed.path.removeprefix("/api/sessions/")
            self._handle_get_session(session_id)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/sessions/") and parsed.path.endswith("/prompt"):
            prefix = "/api/sessions/"
            session_id = parsed.path[len(prefix) : -len("/prompt")]
            self._handle_send_prompt(session_id)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _handle_index(self) -> None:
        default_cwd = self.server.config.default_cwd or "all workspaces"
        html = INDEX_HTML.replace("__DEFAULT_CWD__", default_cwd)
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_health(self) -> None:
        try:
            with self._client() as client:
                client.list_sessions(limit=1, cwd=self.server.config.default_cwd)
        except CodexAppServerError as exc:
            self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, {"ok": True})

    def _handle_list_sessions(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        limit = _first_int(query.get("limit"), default=30)
        archived = _first_bool(query.get("archived"), default=False)
        search = _first(query.get("search"))
        cwd = _first(query.get("cwd"))
        if cwd is None:
            cwd = self.server.config.default_cwd
        try:
            with self._client() as client:
                result = client.list_sessions(limit=limit, cwd=cwd, archived=archived, search_term=search)
        except CodexAppServerError as exc:
            self._send_error_json(exc)
            return
        self._send_json(HTTPStatus.OK, make_session_list_payload(result))

    def _handle_events(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        session_id = _first(query.get("sessionId"))
        if session_id:
            self.server.live_event_hub.subscribe_session(session_id)
        last_event_id = _first_int([self.headers.get("Last-Event-ID", "")], default=0)

        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        ready_event = {
            "id": 0,
            "method": "redex/ready",
            "params": {"sessionId": session_id},
        }
        try:
            self._write_sse_event(ready_event)
            while True:
                event = self.server.live_event_hub.wait_for_event(last_event_id, timeout=15.0)
                if event is None:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                last_event_id = int(event["id"])
                self._write_sse_event(event)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return

    def _handle_get_session(self, session_id: str) -> None:
        try:
            with self._client() as client:
                result = client.get_session(session_id, include_turns=True)
        except CodexAppServerError as exc:
            self._send_error_json(exc)
            return
        self._send_json(HTTPStatus.OK, make_session_detail_payload(result))

    def _handle_send_prompt(self, session_id: str) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Body must include non-empty string field `text`."})
            return
        try:
            with self._client() as client:
                result = client.send_prompt(session_id, text.strip())
        except CodexAppServerError as exc:
            self._send_error_json(exc)
            return
        turn = result.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        status = turn.get("status") if isinstance(turn, dict) else None
        self._send_json(
            HTTPStatus.ACCEPTED,
            {
                "ok": True,
                "sessionId": session_id,
                "turnId": turn_id,
                "status": status,
            },
        )

    def _client(self) -> CodexAppServerClient:
        return CodexAppServerClient(self.server.config.app_server_url)

    def _read_json_body(self) -> dict[str, Any]:
        length_header = self.headers.get("Content-Length")
        if not length_header:
            raise ValueError("Missing Content-Length header.")
        try:
            length = int(length_header)
        except ValueError as exc:
            raise ValueError("Invalid Content-Length header.") from exc
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Request body must be valid JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Request body must be a JSON object.")
        return data

    def _send_error_json(self, error: CodexAppServerError) -> None:
        status = HTTPStatus.BAD_GATEWAY
        if isinstance(error, CodexAppServerUnavailable):
            status = HTTPStatus.SERVICE_UNAVAILABLE
        self._send_json(status, {"error": str(error)})

    def _write_sse_event(self, event: dict[str, Any]) -> None:
        event_id = event.get("id", 0)
        body = json.dumps(event, separators=(",", ":"))
        self.wfile.write(f"id: {event_id}\n".encode("utf-8"))
        self.wfile.write(b"event: notification\n")
        self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(status)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")


def serve(*, host: str, port: int, app_server_url: str | None, default_cwd: str | None) -> None:
    config = BridgeConfig(
        app_server_url=app_server_url,
        default_cwd=default_cwd,
    )
    server = RedexHttpServer((host, port), config)
    print(f"Redex listening on http://{host}:{port}")
    if default_cwd:
        print(f"Default workspace filter: {default_cwd}")
    print(f"Upstream Codex app-server: {app_server_url or 'auto-discover'}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _first_int(values: list[str] | None, *, default: int) -> int:
    value = _first(values)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _first_bool(values: list[str] | None, *, default: bool) -> bool:
    value = _first(values)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}

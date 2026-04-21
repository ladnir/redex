from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Iterable

from .app_server import CodexAppServerClient
from .app_server import CodexAppServerError
from .app_server import make_session_detail_payload
from .app_server import make_session_list_payload
from .bridge import serve as serve_bridge


@dataclass(frozen=True)
class ThreadRecord:
    id: str
    title: str
    cwd: str
    rollout_path: Path
    updated_at_ms: int
    created_at_ms: int
    archived: bool
    model: str | None
    reasoning_effort: str | None
    source: str


@dataclass(frozen=True)
class TranscriptItem:
    timestamp: str
    role: str
    text: str
    phase: str | None = None


class CodexState:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.state_db = self._find_latest("state_*.sqlite")
        self.session_index = self.root / "session_index.jsonl"

    def _find_latest(self, pattern: str) -> Path | None:
        matches = sorted(self.root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        return matches[0] if matches else None

    def _open_state_db(self) -> sqlite3.Connection:
        if self.state_db is None:
            raise FileNotFoundError(f"No state database found under {self.root}")
        uri = f"{self.state_db.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def list_threads(self, *, include_archived: bool, cwd: str | None = None) -> list[ThreadRecord]:
        if self.state_db is not None:
            return self._list_threads_from_db(include_archived=include_archived, cwd=cwd)
        return self._list_threads_from_index(include_archived=include_archived, cwd=cwd)

    def _list_threads_from_db(self, *, include_archived: bool, cwd: str | None = None) -> list[ThreadRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if not include_archived:
            clauses.append("archived = 0")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT
                id,
                title,
                cwd,
                rollout_path,
                COALESCE(updated_at_ms, updated_at * 1000) AS updated_at_ms,
                COALESCE(created_at_ms, created_at * 1000) AS created_at_ms,
                archived,
                model,
                reasoning_effort,
                source
            FROM threads
            {where}
            ORDER BY updated_at_ms DESC
        """
        with self._open_state_db() as conn:
            rows = conn.execute(query, params).fetchall()
        threads = [
            ThreadRecord(
                id=row["id"],
                title=row["title"],
                cwd=normalize_cwd(row["cwd"]),
                rollout_path=Path(row["rollout_path"]),
                updated_at_ms=int(row["updated_at_ms"] or 0),
                created_at_ms=int(row["created_at_ms"] or 0),
                archived=bool(row["archived"]),
                model=row["model"],
                reasoning_effort=row["reasoning_effort"],
                source=row["source"],
            )
            for row in rows
        ]
        if cwd:
            wanted = normalize_cwd(cwd)
            threads = [thread for thread in threads if thread.cwd == wanted]
        return threads

    def _list_threads_from_index(self, *, include_archived: bool, cwd: str | None = None) -> list[ThreadRecord]:
        if not self.session_index.exists():
            raise FileNotFoundError(
                f"No state database or session index found under {self.root}"
            )
        records: list[ThreadRecord] = []
        for line in self.session_index.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            thread_id = row["id"]
            rollout_path = find_rollout_path(self.root, thread_id)
            if rollout_path is None:
                continue
            meta = read_session_meta(rollout_path)
            thread_cwd = normalize_cwd(meta.get("cwd", ""))
            if cwd and thread_cwd != normalize_cwd(cwd):
                continue
            records.append(
                ThreadRecord(
                    id=thread_id,
                    title=row.get("thread_name", thread_id),
                    cwd=thread_cwd,
                    rollout_path=rollout_path,
                    updated_at_ms=parse_iso_to_ms(row["updated_at"]),
                    created_at_ms=parse_iso_to_ms(meta.get("timestamp", row["updated_at"])),
                    archived=bool("archived_sessions" in rollout_path.parts),
                    model=None,
                    reasoning_effort=None,
                    source=str(meta.get("source", "unknown")),
                )
            )
        if not include_archived:
            records = [record for record in records if not record.archived]
        return sorted(records, key=lambda record: record.updated_at_ms, reverse=True)

    def resolve_thread(
        self,
        spec: str,
        *,
        include_archived: bool,
        cwd: str | None = None,
    ) -> ThreadRecord:
        threads = self.list_threads(include_archived=include_archived, cwd=cwd)
        if not threads:
            raise SystemExit("No matching Codex threads found.")
        if spec == "latest":
            return threads[0]
        exact = [thread for thread in threads if thread.id == spec]
        if exact:
            return exact[0]
        prefix = [thread for thread in threads if thread.id.startswith(spec)]
        if len(prefix) == 1:
            return prefix[0]
        lowered = spec.lower()
        title_matches = [thread for thread in threads if lowered in thread.title.lower()]
        if len(title_matches) == 1:
            return title_matches[0]
        if len(prefix) > 1:
            raise SystemExit(f"Thread id prefix '{spec}' matched {len(prefix)} threads. Be more specific.")
        if len(title_matches) > 1:
            raise SystemExit(f"Thread title filter '{spec}' matched {len(title_matches)} threads. Be more specific.")
        raise SystemExit(f"No thread matched '{spec}'.")


def normalize_cwd(path: str) -> str:
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def parse_iso_to_ms(value: str) -> int:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(dt.timestamp() * 1000)


def format_ms(value: int) -> str:
    dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def read_session_meta(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        first_line = handle.readline()
    if not first_line:
        return {}
    obj = json.loads(first_line)
    return obj.get("payload", {}) if obj.get("type") == "session_meta" else {}


def find_rollout_path(root: Path, thread_id: str) -> Path | None:
    session_matches = list(root.glob(f"sessions/**/rollout-*-{thread_id}.jsonl"))
    if session_matches:
        return session_matches[0]
    archived_matches = list(root.glob(f"archived_sessions/rollout-*-{thread_id}.jsonl"))
    return archived_matches[0] if archived_matches else None


def extract_text(content: list[dict[str, object]]) -> str:
    chunks: list[str] = []
    for item in content:
        item_type = item.get("type")
        if item_type in {"input_text", "output_text"}:
            text = item.get("text")
            if isinstance(text, str) and text:
                chunks.append(text.rstrip())
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()


def iter_transcript(
    rollout_path: Path,
    *,
    phase_filter: str,
) -> Iterable[TranscriptItem]:
    with rollout_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError:
                continue
            item_type = obj.get("type")
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if item_type == "event_msg" and payload.get("type") == "user_message":
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    yield TranscriptItem(
                        timestamp=str(obj.get("timestamp", "")),
                        role="user",
                        text=message.rstrip(),
                    )
                continue
            if item_type == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
                phase = payload.get("phase")
                if phase_filter == "final" and phase != "final_answer":
                    continue
                if phase_filter == "commentary" and phase != "commentary":
                    continue
                content = payload.get("content")
                if not isinstance(content, list):
                    continue
                text = extract_text(content)
                if text:
                    yield TranscriptItem(
                        timestamp=str(obj.get("timestamp", "")),
                        role="assistant",
                        text=text,
                        phase=phase if isinstance(phase, str) else None,
                    )


def render_transcript_item(item: TranscriptItem) -> str:
    phase_suffix = f"/{item.phase}" if item.phase else ""
    stamp = ""
    if item.timestamp:
        try:
            local = datetime.fromisoformat(item.timestamp.replace("Z", "+00:00")).astimezone()
            stamp = local.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            stamp = item.timestamp
    header = f"[{stamp}] {item.role}{phase_suffix}" if stamp else f"{item.role}{phase_suffix}"
    return f"{header}\n{item.text}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Redex external bridge for Codex desktop sessions.")
    parser.add_argument(
        "--data-root",
        default="~/.codex",
        help="Codex state directory. Defaults to ~/.codex.",
    )
    parser.add_argument(
        "--app-server-url",
        default=None,
        help="Codex app-server websocket URL. Defaults to the discovered desktop runtime, then ws://127.0.0.1:4222.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    threads = subparsers.add_parser("threads", help="List known threads.")
    threads.add_argument("--limit", type=int, default=20, help="Maximum number of threads to print.")
    threads.add_argument("--archived", action="store_true", help="Include archived threads.")
    threads.add_argument("--cwd", help="Only show threads for this exact workspace path.")
    threads.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    show = subparsers.add_parser("show", help="Show a transcript.")
    show.add_argument("thread", nargs="?", default="latest", help="Thread id, id prefix, title filter, or 'latest'.")
    show.add_argument(
        "--phase",
        choices=["all", "commentary", "final"],
        default="all",
        help="Which assistant phases to include.",
    )
    show.add_argument("--limit", type=int, default=0, help="Only print the last N transcript items.")
    show.add_argument("--archived", action="store_true", help="Allow resolving archived threads.")
    show.add_argument("--cwd", help="Resolve the thread within this exact workspace path.")
    show.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    watch = subparsers.add_parser("watch", help="Follow a live transcript.")
    watch.add_argument("thread", nargs="?", default="latest", help="Thread id, id prefix, title filter, or 'latest'.")
    watch.add_argument(
        "--phase",
        choices=["all", "commentary", "final"],
        default="all",
        help="Which assistant phases to include.",
    )
    watch.add_argument("--last", type=int, default=6, help="Print the last N transcript items before following.")
    watch.add_argument("--interval", type=float, default=1.0, help="Polling interval in seconds.")
    watch.add_argument("--archived", action="store_true", help="Allow resolving archived threads.")
    watch.add_argument("--cwd", help="Resolve the thread within this exact workspace path.")

    list_sessions = subparsers.add_parser("list-sessions", help="List sessions via the live Codex app-server.")
    list_sessions.add_argument("--limit", type=int, default=20, help="Maximum number of sessions to return.")
    list_sessions.add_argument("--archived", action="store_true", help="Include archived sessions.")
    list_sessions.add_argument("--cwd", help="Only show sessions for this exact workspace path.")
    list_sessions.add_argument("--search", help="Filter sessions by title substring.")
    list_sessions.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    get_session = subparsers.add_parser("get-session", help="Read a session via the live Codex app-server.")
    get_session.add_argument("session_id", help="Exact session id to read.")
    get_session.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    send_prompt = subparsers.add_parser("send-prompt", help="Send a new prompt into an existing live session.")
    send_prompt.add_argument("session_id", help="Exact session id to resume.")
    send_prompt.add_argument("text", help="Prompt text to inject into the session.")
    send_prompt.add_argument("--json", action="store_true", help="Emit JSON instead of text.")

    serve = subparsers.add_parser("serve", help="Run a tiny HTTP bridge for phone access.")
    serve.add_argument("--host", default="127.0.0.1", help="HTTP listen host. Use 0.0.0.0 for tailnet access.")
    serve.add_argument("--port", type=int, default=8765, help="HTTP listen port.")
    serve.add_argument(
        "--cwd",
        help="Optional default workspace filter for listSessions. Defaults to all workspaces.",
    )

    return parser


def cmd_threads(state: CodexState, args: argparse.Namespace) -> int:
    threads = state.list_threads(include_archived=args.archived, cwd=args.cwd)
    threads = threads[: args.limit]
    if args.json:
        print(json.dumps([asdict_thread(thread) for thread in threads], indent=2))
        return 0
    if not threads:
        print("No threads found.")
        return 0
    for thread in threads:
        status = "archived" if thread.archived else "active"
        print(f"{thread.id}  {format_ms(thread.updated_at_ms)}  {status}")
        print(f"  title: {thread.title}")
        print(f"  cwd:   {thread.cwd}")
        print(f"  path:  {thread.rollout_path}")
    return 0


def cmd_show(state: CodexState, args: argparse.Namespace) -> int:
    thread = state.resolve_thread(args.thread, include_archived=args.archived, cwd=args.cwd)
    items = list(iter_transcript(thread.rollout_path, phase_filter=args.phase))
    if args.limit > 0:
        items = items[-args.limit :]
    if args.json:
        payload = {
            "thread": asdict_thread(thread),
            "items": [asdict(item) for item in items],
        }
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Thread: {thread.title}")
    print(f"ID: {thread.id}")
    print(f"Updated: {format_ms(thread.updated_at_ms)}")
    print(f"CWD: {thread.cwd}")
    print(f"Rollout: {thread.rollout_path}")
    print("")
    for index, item in enumerate(items):
        if index:
            print("")
        print(render_transcript_item(item))
    return 0


def cmd_watch(state: CodexState, args: argparse.Namespace) -> int:
    thread = state.resolve_thread(args.thread, include_archived=args.archived, cwd=args.cwd)
    print(f"Watching {thread.id}  {thread.title}")
    print(f"Rollout: {thread.rollout_path}")
    existing = list(iter_transcript(thread.rollout_path, phase_filter=args.phase))
    if args.last > 0:
        existing = existing[-args.last :]
    for item in existing:
        print("")
        print(render_transcript_item(item))
    with thread.rollout_path.open("r", encoding="utf-8") as handle:
        handle.seek(0, 2)
        while True:
            line = handle.readline()
            if not line:
                time.sleep(args.interval)
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            item: TranscriptItem | None = None
            if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
                message = payload.get("message")
                if isinstance(message, str) and message.strip():
                    item = TranscriptItem(str(obj.get("timestamp", "")), "user", message.rstrip())
            elif obj.get("type") == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
                phase = payload.get("phase")
                if args.phase == "final" and phase != "final_answer":
                    item = None
                elif args.phase == "commentary" and phase != "commentary":
                    item = None
                else:
                    content = payload.get("content")
                    if isinstance(content, list):
                        text = extract_text(content)
                        if text:
                            item = TranscriptItem(
                                str(obj.get("timestamp", "")),
                                "assistant",
                                text,
                                phase if isinstance(phase, str) else None,
                            )
            if item is not None:
                print("")
                print(render_transcript_item(item))
                sys.stdout.flush()


def cmd_list_sessions(args: argparse.Namespace) -> int:
    with CodexAppServerClient(args.app_server_url) as client:
        result = client.list_sessions(
            limit=args.limit,
            cwd=args.cwd,
            archived=args.archived,
            search_term=args.search,
        )
    payload = make_session_list_payload(result)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    sessions = payload["sessions"]
    if not sessions:
        print("No sessions found.")
        return 0
    for session in sessions:
        print(f"{session['id']}  {session.get('updatedAt') or ''}  {session.get('status') or ''}")
        print(f"  title: {session.get('title')}")
        print(f"  cwd:   {session.get('cwd')}")
        print(f"  path:  {session.get('path')}")
        preview = session.get("preview")
        if preview:
            print(f"  prompt: {preview}")
    return 0


def cmd_get_session(args: argparse.Namespace) -> int:
    with CodexAppServerClient(args.app_server_url) as client:
        result = client.get_session(args.session_id, include_turns=True)
    payload = make_session_detail_payload(result)
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    session = payload["session"]
    print(f"Session: {session['title']}")
    print(f"ID: {session['id']}")
    print(f"Updated: {session.get('updatedAt')}")
    print(f"CWD: {session.get('cwd')}")
    print("")
    for index, item in enumerate(payload["messages"]):
        if index:
            print("")
        phase = f"/{item['phase']}" if item.get("phase") else ""
        stamp = item.get("timestamp") or ""
        header = f"[{stamp}] {item['role']}{phase}" if stamp else f"{item['role']}{phase}"
        print(header)
        print(item["text"])
    return 0


def cmd_send_prompt(args: argparse.Namespace) -> int:
    with CodexAppServerClient(args.app_server_url) as client:
        result = client.send_prompt(args.session_id, args.text)
    turn = result.get("turn", {})
    payload = {
        "ok": True,
        "sessionId": args.session_id,
        "turnId": turn.get("id"),
        "status": turn.get("status"),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Accepted prompt for {args.session_id}")
    print(f"Turn: {payload['turnId']}")
    print(f"Status: {payload['status']}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    serve_bridge(
        host=args.host,
        port=args.port,
        app_server_url=args.app_server_url,
        default_cwd=args.cwd,
    )
    return 0


def asdict_thread(thread: ThreadRecord) -> dict[str, object]:
    payload = asdict(thread)
    payload["rollout_path"] = str(thread.rollout_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in {"threads", "show", "watch"}:
        state = CodexState(Path(args.data_root))
        if args.command == "threads":
            return cmd_threads(state, args)
        if args.command == "show":
            return cmd_show(state, args)
        if args.command == "watch":
            return cmd_watch(state, args)
    try:
        if args.command == "list-sessions":
            return cmd_list_sessions(args)
        if args.command == "get-session":
            return cmd_get_session(args)
        if args.command == "send-prompt":
            return cmd_send_prompt(args)
        if args.command == "serve":
            return cmd_serve(args)
    except CodexAppServerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    parser.error(f"Unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

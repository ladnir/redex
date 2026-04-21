# Redex

`redex` is an external control surface for Codex desktop sessions.

It has three practical pieces:

- read-only local inspection from `~/.codex`
- live `listSessions`, `getSession`, and `sendPrompt(sessionId, text)` over the Codex app-server
- browser live updates over a Redex SSE feed subscribed to the selected Codex session

The live path is the important one for phone access: it sends a real new user turn into an existing chat instead of abusing hooks.

Redex is intentionally outside Codex. Codex stays the local engine and session authority; Redex is the external app that talks to the Codex app-server.

## Design Goal

The point of Redex is to keep Codex as close to upstream as possible.

- Codex owns local session state and the existing app-server protocol.
- Redex owns the external control surface, phone-friendly UI, and any remote networking story.
- Any Codex-side changes should stay narrowly focused on exposing or stabilizing the existing local control plane, not reimplementing Redex inside Codex.

## Install

```bash
pip install -e .
```

## Quick Start

Redex needs a live Codex app-server. There are two good ways to provide one:

1. Preferred: a desktop-managed shared runtime that publishes discovery metadata at `~/.codex/runtime/app-server.json`
2. Fallback: a standalone websocket app-server listening on `ws://127.0.0.1:4222`

Once one of those exists, start the Redex web bridge:

```bash
python redex.py serve --host 127.0.0.1 --port 8765
```

Then open:

```text
http://127.0.0.1:8765/
```

By default, Redex auto-discovers the live Codex desktop runtime from `~/.codex/runtime/app-server.json` and only falls back to `ws://127.0.0.1:4222` if no discovered runtime is available.

## Runtime Models

### Shared desktop runtime

This is the cleanest model for phone access. The desktop Codex app owns the session runtime, and Redex connects to that same live instance through a localhost websocket sidecar.

That requires a Codex build that:

- starts the app-server in its normal desktop-owned mode
- also exposes a localhost websocket sidecar
- writes discovery metadata to `~/.codex/runtime/app-server.json`

### Standalone websocket app-server

If you are not using the shared-runtime Codex patch yet, Redex can also talk to a separate Codex app-server process:

```bash
cd /path/to/codex/codex-rs
target/debug/codex app-server --listen ws://127.0.0.1:4222
```

Or point Redex at another websocket endpoint explicitly with `--app-server-url`.

## Windows Convenience Scripts

This repo includes PowerShell launchers for the Windows setup described above.

Launch Codex with the repo backend plus the Redex web bridge:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Redex.ps1 -RestartCodex -OpenBrowser
```

This script:

- launches the Windows Codex UI against the repo-built `codex.exe`
- enables the shared localhost websocket sidecar on the desktop-owned runtime
- starts Redex at `http://127.0.0.1:8765`

To publish the same local Redex instance to your tailnet with Tailscale Serve:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-RedexTailnet.ps1 -RestartCodex
```

If you only want to switch Codex itself:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-CodexRepoCore.ps1 -Restart
```

## CLI

List live sessions for the current workspace:

```bash
python redex.py list-sessions --cwd "/path/to/workspace"
```

Read one session:

```bash
python redex.py get-session 019dad2c-8c96-7dd3-9d8d-5ffa372881dd
```

Send a real prompt into an existing session:

```bash
python redex.py send-prompt 019dad2c-8c96-7dd3-9d8d-5ffa372881dd "what should we do next?"
```

The old read-only local transcript tools still exist too:

```bash
python redex.py threads
python redex.py show latest
python redex.py watch latest
```

## HTTP Bridge

Run the phone-friendly local web bridge:

```bash
python redex.py serve --host 127.0.0.1 --port 8765
```

By default, `serve` shows sessions from all workspaces. If you want to filter to one workspace, pass `--cwd`:

```bash
python redex.py serve --cwd "/path/to/workspace"
```

Useful JSON endpoints:

- `GET /healthz`
- `GET /api/sessions?limit=30`
- `GET /api/sessions/:id`
- `POST /api/sessions/:id/prompt` with body `{"text":"..."}`

Live browser updates use:

- `GET /api/events?sessionId=:id`

The browser subscribes to the selected session and refreshes that transcript when Codex emits relevant thread, turn, or item notifications.

## Phone Access Over Tailscale

Once Redex is running locally, the easy next step is to expose it to the phone over your tailnet.

Direct tailnet access:

```bash
python redex.py serve --host 0.0.0.0 --port 8765
```

Then on the phone, with Tailscale enabled, open the machine's Tailscale IP on port `8765`.

Or keep Redex bound to localhost and publish it through Tailscale Serve:

```bash
tailscale serve http / http://127.0.0.1:8765
```

## Notes

- The Codex app-server rejects websocket clients that send an `Origin` header, so Redex suppresses `Origin` on connect.
- On Windows, Redex validates discovered PIDs with the Win32 process API instead of `os.kill(pid, 0)`.
- The live prompt path uses `thread/resume` plus `turn/start`.
- The live event path uses a long-lived Redex connection and `thread/resume` to subscribe to the selected session.
- `sendPrompt` is currently fire-and-return: it accepts the turn and returns the new turn id immediately. The browser page can then refresh the session to watch the assistant finish.

## Compatibility

The old `codex-shim` name still works as a compatibility alias, but `redex` is now the intended external app name.

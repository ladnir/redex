![Redex logo](assets/redex-logo.svg)

# Redex

Use Codex from your phone without giving up your desktop session.




Redex is a lightweight web companion for Codex. It lets you open your chats in a phone-friendly UI, keep reading while you are away from your desk, and send real prompts back into the same live session.

The goal is simple:

- keep Codex as the source of truth
- make remote access feel natural
- avoid hooky automation and fake chat relays

## Why Redex

If you already like working in Codex on your computer, Redex gives you a second screen for the same work:

- pick up a live Codex session from your phone
- watch turns arrive in near real time
- reply into the same thread, not a copy
- browse all your sessions in one place
- expose it safely over your tailnet

Redex is not trying to replace Codex. It is trying to make Codex feel portable.

## What It Looks Like

https://github.com/user-attachments/assets/469d8106-c67e-4e27-bac4-27c287bb471c

## Recommended Setup

For the best experience, use Redex with the Redex-compatible Codex fork:

- Codex fork: [ladnir/codex `redex` branch](https://github.com/ladnir/codex/tree/redex)
- Redex: [ladnir/redex](https://github.com/ladnir/redex)

That setup gives you the important thing Redex wants most: both the Codex desktop app and Redex talking to the same live runtime.

If you are not using the fork, Redex can still connect to a standalone Codex app-server over websocket, but that is the less integrated fallback.

## Quick Start

Install Redex:

```bash
pip install -e .
```

Start the web UI:

```bash
python redex.py serve --host 127.0.0.1 --port 8765
```

Open:

```text
http://127.0.0.1:8765/
```

By default, Redex will try to discover a live Codex desktop runtime from:

```text
~/.codex/runtime/app-server.json
```

If it does not find one, it falls back to:

```text
ws://127.0.0.1:4222
```

## Best Experience

The nicest setup is:

1. Codex desktop is running normally.
2. The Codex fork exposes a localhost sidecar for that same runtime.
3. Redex discovers it automatically.
4. Your phone opens Redex over Tailscale.

That gives you live desktop and phone access to the same session instead of juggling copies.

## Phone Access

Once Redex is running locally, the easiest way to get it on your phone is Tailscale.

You can either bind Redex directly on the machine:

```bash
python redex.py serve --host 0.0.0.0 --port 8765
```

Or keep Redex on localhost and publish it through Tailscale Serve:

```bash
tailscale serve http / http://127.0.0.1:8765
```

## Windows Convenience Scripts

This repo includes PowerShell launchers for the Windows flow:

Launch Codex with the repo backend and Redex:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-Redex.ps1 -RestartCodex -OpenBrowser
```

Launch Codex with Redex and publish through Tailscale Serve:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-RedexTailnet.ps1 -RestartCodex
```

Switch only Codex into the repo-core setup:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\Start-CodexRepoCore.ps1 -Restart
```

## Developer Notes

Redex is intentionally external to Codex.

- Codex owns session state, runtime behavior, and the app-server protocol.
- Redex owns the browser UI, phone flow, and remote access story.
- The preferred Codex-side change is small: expose the desktop-owned runtime through a discoverable local sidecar.

That keeps the fork small and makes Redex easier to evolve on its own.

## Runtime Models

Redex supports two practical runtime shapes.

### Shared desktop runtime

This is the preferred model.

The desktop Codex app owns the runtime, and Redex connects to that same live instance through a localhost websocket sidecar. Discovery metadata is written to:

```text
~/.codex/runtime/app-server.json
```

### Standalone websocket app-server

If you are not using the shared-runtime Codex patch yet, Redex can also talk to a separate Codex app-server process:

```bash
cd /path/to/codex/codex-rs
target/debug/codex app-server --listen ws://127.0.0.1:4222
```

You can also point Redex at another websocket endpoint with `--app-server-url`.

## CLI

List sessions:

```bash
python redex.py list-sessions --cwd "/path/to/workspace"
```

Read a session:

```bash
python redex.py get-session 019dad2c-8c96-7dd3-9d8d-5ffa372881dd
```

Send a prompt into an existing session:

```bash
python redex.py send-prompt 019dad2c-8c96-7dd3-9d8d-5ffa372881dd "what should we do next?"
```

The older local transcript helpers still exist too:

```bash
python redex.py threads
python redex.py show latest
python redex.py watch latest
```

## HTTP Bridge

Run the local web bridge:

```bash
python redex.py serve --host 127.0.0.1 --port 8765
```

Useful endpoints:

- `GET /healthz`
- `GET /api/sessions?limit=30`
- `GET /api/sessions/:id`
- `POST /api/sessions/:id/prompt` with body `{"text":"..."}`
- `GET /api/events?sessionId=:id`

## Compatibility

The old `codex-shim` name still works as a compatibility alias, but `redex` is now the intended external app name.

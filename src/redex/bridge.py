from __future__ import annotations

import json
import mimetypes
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlparse

from .app_server import CodexAppServerClient
from .app_server import CodexAppServerError
from .app_server import CodexAppServerUnavailable
from .app_server import _is_pre_first_turn_error
from .app_server import make_session_detail_payload
from .app_server import make_session_list_payload
from .app_server import normalize_thread


EVENT_BACKLOG_LIMIT = 500
ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#122133">
  <title>Redex</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #122133;
      --panel: rgba(24, 38, 56, 0.96);
      --panel-strong: rgba(31, 47, 68, 0.99);
      --ink: #f8fbff;
      --muted: #8e9bab;
      --line: rgba(170, 205, 238, 0.16);
      --accent: #dcecff;
      --accent-strong: #f8fbff;
      --accent-soft: rgba(220, 236, 255, 0.12);
      --accent-glow: rgba(128, 176, 224, 0.12);
      --tint: #7fb0de;
      --tint-strong: #e3f0ff;
      --tint-soft: rgba(127, 176, 222, 0.12);
      --user: rgba(64, 84, 109, 0.97);
      --assistant: rgba(18, 30, 44, 0.97);
      --danger: #ffffff;
      --danger-soft: rgba(255, 255, 255, 0.08);
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Aptos", "Segoe UI", ui-sans-serif, sans-serif;
      color: var(--ink);
      background:
        radial-gradient(circle at top left, rgba(127, 176, 222, 0.24), transparent 24rem),
        radial-gradient(circle at top right, rgba(90, 130, 171, 0.16), transparent 20rem),
        linear-gradient(180deg, rgba(180, 212, 245, 0.1), transparent 24rem),
        linear-gradient(180deg, #172739 0%, #122133 100%);
    }
    .app {
      min-height: 100vh;
      max-width: 88rem;
      margin: 0 auto;
      padding: 1.1rem;
      display: grid;
      gap: 1.1rem;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 1.2rem;
      box-shadow: 0 1rem 2.8rem rgba(0, 0, 0, 0.42);
      backdrop-filter: blur(14px);
    }
    .hero {
      padding: 0.85rem 1rem;
    }
    .hero-grid {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }
    .hero-copy {
      display: flex;
      align-items: center;
      gap: 0.7rem;
      min-width: 0;
    }
    .hero-wordmark {
      display: inline-flex;
      align-items: center;
      gap: 0.68rem;
      min-width: 0;
    }
    .hero-mark {
      width: 2rem;
      height: 2rem;
      flex: 0 0 auto;
      display: block;
    }
    .hero-title {
      display: inline-flex;
      align-items: baseline;
      gap: 0;
      font-size: 1.18rem;
      font-weight: 800;
      letter-spacing: -0.04em;
      color: var(--ink);
      line-height: 1;
      white-space: nowrap;
    }
    .hero-title-accent {
      color: #7fb0de;
    }
    .stat-pill {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      padding: 0.38rem 0.6rem;
      border-radius: 999px;
      border: 1px solid var(--line);
      background: rgba(77, 111, 146, 0.34);
    }
    .stat-label {
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
    }
    .stat-value {
      font-size: 0.84rem;
      font-weight: 700;
    }
    .layout {
      display: grid;
      gap: 1rem;
      min-width: 0;
    }
    .sidebar,
    .session {
      padding: 1rem;
      min-width: 0;
    }
    .sidebar {
      display: flex;
      flex-direction: column;
    }
    .session {
      display: flex;
      flex-direction: column;
      min-height: 0;
    }
    .sidebar-header,
    .session-header {
      display: flex;
      gap: 0.5rem;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 0.9rem;
    }
    .sidebar-copy h2,
    .session-heading h2 {
      margin: 0;
      font-size: 1.1rem;
      letter-spacing: -0.02em;
    }
    .sidebar-copy p,
    .session-heading p {
      margin: 0.2rem 0 0;
      color: var(--muted);
      font-size: 0.9rem;
    }
    button,
    textarea,
    input {
      font: inherit;
    }
    button {
      border: 0;
      border-radius: 0.85rem;
      padding: 0.7rem 1rem;
      background: var(--accent);
      color: #111111;
      cursor: pointer;
      transition: transform 120ms ease, box-shadow 120ms ease, background 120ms ease;
      box-shadow: 0 0.45rem 1rem rgba(4, 10, 18, 0.34);
    }
    button:hover:not(:disabled) {
      transform: translateY(-1px);
      box-shadow: 0 0.7rem 1.25rem rgba(4, 10, 18, 0.42);
      background: var(--accent-strong);
    }
    button.secondary {
      background: rgba(57, 81, 108, 0.88);
      color: var(--ink);
      box-shadow: none;
    }
    button:disabled {
      opacity: 0.55;
      cursor: not-allowed;
      transform: none;
      box-shadow: none;
    }
    .search {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 0.95rem;
      padding: 0.8rem 0.9rem;
      background: rgba(47, 69, 94, 0.88);
      color: var(--ink);
      margin-bottom: 0.85rem;
    }
    .search::placeholder,
    textarea::placeholder {
      color: rgba(142, 167, 187, 0.8);
    }
    .session-list {
      display: grid;
      gap: 0.6rem;
      max-height: 20rem;
      overflow: auto;
      overflow-x: hidden;
      padding-right: 0.15rem;
      min-width: 0;
    }
    .session-card {
      width: 100%;
      text-align: left;
      background: rgba(38, 56, 78, 0.9);
      color: var(--ink);
      border: 1px solid var(--line);
      padding: 0.95rem;
      box-shadow: none;
      min-width: 0;
      overflow: hidden;
    }
    .session-card:hover {
      background: var(--panel-strong);
    }
    .session-card.active {
      outline: 2px solid rgba(255, 255, 255, 0.14);
      background: rgba(68, 99, 132, 0.72);
      box-shadow: 0 0 0 0.35rem rgba(127, 176, 222, 0.12);
    }
    .session-card.unseen {
      border-color: rgba(180, 221, 255, 0.42);
      box-shadow: inset 0 0 0 1px rgba(180, 221, 255, 0.16);
    }
    .session-card-title {
      display: flex;
      align-items: start;
      justify-content: space-between;
      gap: 0.75rem;
      margin-bottom: 0.35rem;
    }
    .session-card strong {
      display: block;
      font-size: 0.98rem;
      line-height: 1.25;
      overflow-wrap: anywhere;
    }
    .session-id {
      flex: 0 0 auto;
      color: var(--muted);
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      font-size: 0.75rem;
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
    .preview {
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 1;
      overflow: hidden;
      min-height: 1.35em;
    }
    .session-card-footer {
      display: flex;
      flex-wrap: wrap;
      gap: 0.45rem;
      margin-top: 0.55rem;
    }
    .session-group {
      display: grid;
      gap: 0.6rem;
    }
    .session-group + .session-group {
      margin-top: 0.35rem;
    }
    .session-group-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 0.75rem;
      padding: 0 0.2rem;
    }
    .session-group-title {
      font-size: 0.86rem;
      font-weight: 700;
      letter-spacing: -0.01em;
    }
    .session-group-subtitle {
      color: var(--muted);
      font-size: 0.76rem;
      line-height: 1.35;
      overflow-wrap: anywhere;
    }
    .group-toggle {
      justify-self: start;
      padding: 0.45rem 0.7rem;
      border-radius: 999px;
      background: rgba(55, 80, 108, 0.86);
      color: var(--ink);
      box-shadow: none;
      font-size: 0.82rem;
    }
    .group-toggle:hover:not(:disabled) {
      background: rgba(75, 106, 140, 0.92);
    }
    .group-header-actions {
      display: flex;
      gap: 0.4rem;
      align-items: center;
      flex-wrap: wrap;
    }
    .group-heading-line {
      display: flex;
      align-items: center;
      gap: 0.45rem;
      min-width: 0;
    }
    .unseen-dot {
      width: 0.52rem;
      height: 0.52rem;
      border-radius: 999px;
      background: #dcecff;
      box-shadow: 0 0 0.55rem rgba(220, 236, 255, 0.68);
      flex: 0 0 auto;
      margin-top: 0.18rem;
    }
    .unseen-pill {
      background: rgba(220, 236, 255, 0.16);
      border-color: rgba(220, 236, 255, 0.28);
      color: var(--ink);
      font-weight: 700;
    }
    .group-collapse {
      padding: 0.34rem 0.64rem;
      border-radius: 999px;
      background: rgba(55, 80, 108, 0.86);
      color: var(--ink);
      box-shadow: none;
      font-size: 0.78rem;
    }
    .group-create {
      width: 1.9rem;
      height: 1.9rem;
      min-width: 1.9rem;
      padding: 0;
      border-radius: 999px;
      background: rgba(55, 80, 108, 0.86);
      color: var(--ink);
      box-shadow: none;
      font-size: 1rem;
      line-height: 1;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
    .group-collapse:hover:not(:disabled) {
      background: rgba(75, 106, 140, 0.92);
    }
    .group-create:hover:not(:disabled) {
      background: rgba(75, 106, 140, 0.92);
    }
    .status-pill,
    .mini-pill,
    .live-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.38rem;
      padding: 0.26rem 0.58rem;
      border-radius: 999px;
      font-size: 0.78rem;
      border: 1px solid var(--line);
      background: rgba(49, 74, 102, 0.86);
      color: var(--ink);
    }
    .status-pill {
      background: var(--tint-soft);
      color: var(--tint-strong);
      border-color: rgba(159, 214, 255, 0.16);
    }
    .status-pill.unknown {
      background: rgba(57, 81, 108, 0.88);
      color: var(--muted);
    }
    .live-pill.live {
      background: var(--tint-soft);
      color: var(--tint-strong);
      border-color: rgba(159, 214, 255, 0.16);
    }
    .live-pill.syncing {
      background: rgba(57, 81, 108, 0.88);
      color: var(--ink);
      border-color: rgba(170, 205, 238, 0.16);
    }
    .live-pill.error {
      background: var(--danger-soft);
      color: var(--danger);
      border-color: rgba(157, 63, 44, 0.22);
    }
    .detail-strip {
      display: flex;
      flex-wrap: wrap;
      gap: 0.42rem;
      margin-bottom: 0.7rem;
      min-width: 0;
    }
    .detail-chip {
      display: inline-flex;
      align-items: center;
      gap: 0.34rem;
      max-width: 100%;
      min-width: 0;
      padding: 0.3rem 0.55rem;
      border-radius: 999px;
      background: rgba(45, 68, 94, 0.84);
      border: 1px solid var(--line);
    }
    .detail-chip-label {
      color: var(--muted);
      font-size: 0.68rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }
    .detail-chip-value {
      font-size: 0.8rem;
      line-height: 1.2;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .detail-chip-value.mono {
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      font-size: 0.74rem;
    }
    .conversation {
      display: grid;
      gap: 0.45rem;
      margin: 0.75rem 0;
      padding-right: 0.2rem;
      min-height: 18rem;
      max-height: 50vh;
      overflow: auto;
      overflow-x: hidden;
      min-width: 0;
      padding-bottom: 0.4rem;
    }
    .bubble {
      padding: 0.58rem 0.74rem;
      border-radius: 0.9rem;
      border: 1px solid var(--line);
      line-height: 1.36;
      width: fit-content;
      max-width: min(54rem, 100%);
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .bubble.user {
      background: var(--user);
      margin-left: auto;
    }
    .bubble.assistant {
      background: transparent;
      margin-right: auto;
      border: 0;
      border-left: 2px solid rgba(134, 185, 230, 0.2);
      border-radius: 0;
      box-shadow: none;
      padding: 0.18rem 0 0.18rem 0.78rem;
      max-width: min(48rem, 100%);
    }
    .bubble.commentary {
      background: transparent;
      color: rgba(220, 232, 246, 0.72);
      font-size: 0.93rem;
      border: 0;
      border-left: 2px solid rgba(134, 185, 230, 0.18);
      border-radius: 0;
      box-shadow: none;
      padding: 0.05rem 0 0.05rem 0.7rem;
      margin-right: auto;
      margin-left: 0;
      max-width: min(46rem, 100%);
    }
    .commentary-details {
      width: 100%;
    }
    .commentary-summary {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      cursor: pointer;
      color: var(--muted);
      font-size: 0.78rem;
      list-style: none;
      user-select: none;
    }
    .commentary-summary::-webkit-details-marker {
      display: none;
    }
    .commentary-summary::before {
      content: ">";
      display: inline-block;
      transition: transform 120ms ease;
    }
    .commentary-details[open] .commentary-summary::before {
      transform: rotate(90deg);
    }
    .commentary-body {
      margin-top: 0.32rem;
    }
    .meta-details {
      margin-bottom: 0.65rem;
    }
    .meta-summary {
      display: inline-flex;
      align-items: center;
      gap: 0.45rem;
      cursor: pointer;
      color: var(--muted);
      font-size: 0.78rem;
      list-style: none;
      user-select: none;
    }
    .meta-summary::-webkit-details-marker {
      display: none;
    }
    .meta-summary::before {
      content: ">";
      display: inline-block;
      transition: transform 120ms ease;
    }
    .meta-details[open] .meta-summary::before {
      transform: rotate(90deg);
    }
    .bubble-header {
      display: flex;
      flex-wrap: wrap;
      gap: 0.5rem;
      align-items: baseline;
      margin-bottom: 0.22rem;
      font-size: 0.76rem;
      color: var(--muted);
    }
    .phase-chip {
      display: inline-flex;
      align-items: center;
      padding: 0.12rem 0.4rem;
      border-radius: 999px;
      background: rgba(57, 81, 108, 0.84);
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .bubble.commentary .phase-chip {
      background: rgba(49, 74, 102, 0.8);
      color: rgba(142, 167, 187, 0.9);
    }
    .markdown {
      font-size: 0.96rem;
      min-width: 0;
    }
    .bubble.assistant .markdown {
      color: #ffffff;
    }
    .bubble.commentary .markdown {
      font-size: 0.92rem;
      color: rgba(225, 235, 247, 0.78);
    }
    .markdown p {
      margin: 0;
    }
    .markdown p + p,
    .markdown p + ul,
    .markdown p + ol,
    .markdown ul + p,
    .markdown ol + p,
    .markdown pre + p {
      margin-top: 0.48rem;
    }
    .markdown ul,
    .markdown ol {
      margin: 0.36rem 0 0.36rem 1.2rem;
      padding: 0;
    }
    .markdown li + li {
      margin-top: 0.2rem;
    }
    .markdown a {
      color: var(--tint-strong);
      text-decoration: underline;
      text-decoration-thickness: 0.08em;
      text-underline-offset: 0.14em;
      word-break: break-word;
    }
    .markdown code {
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      background: rgba(57, 81, 108, 0.82);
      border-radius: 0.45rem;
      padding: 0.12rem 0.34rem;
      font-size: 0.88em;
    }
    .markdown pre {
      margin: 0.48rem 0 0;
      padding: 0.7rem 0.8rem;
      border-radius: 0.8rem;
      background: rgba(15, 24, 36, 0.92);
      color: #f8fbff;
      overflow: auto;
      border: 1px solid rgba(196, 220, 242, 0.14);
    }
    .markdown pre code {
      background: transparent;
      padding: 0;
      color: inherit;
      border-radius: 0;
      font-size: 0.9rem;
    }
    .markdown img,
    .markdown table {
      max-width: 100%;
    }
    .math-inline,
    .math-block {
      color: var(--tint-strong);
    }
    .math-block {
      margin-top: 0.48rem;
      padding: 0.55rem 0.7rem;
      border-radius: 0.8rem;
      background: rgba(68, 99, 132, 0.42);
      overflow-x: auto;
    }
    .composer-shell {
      border-top: 1px solid var(--line);
      padding-top: 0.75rem;
      min-width: 0;
      background: linear-gradient(180deg, rgba(7, 16, 25, 0.2), rgba(7, 16, 25, 0.92) 22%, rgba(7, 16, 25, 0.98));
      backdrop-filter: blur(10px);
      flex: 0 0 auto;
    }
    textarea {
      width: 100%;
      min-height: 3.25rem;
      max-height: 9rem;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 1rem;
      padding: 0.9rem;
      background: rgba(45, 68, 94, 0.9);
      color: var(--ink);
    }
    .composer-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 0.65rem;
      align-items: end;
    }
    .composer-actions {
      display: flex;
      align-items: flex-end;
    }
    .send-icon-button {
      width: 3rem;
      height: 3rem;
      min-width: 3rem;
      padding: 0;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 1rem;
      line-height: 1;
    }
    .empty-state {
      padding: 1.1rem;
      border-radius: 1rem;
      background: rgba(45, 68, 94, 0.72);
      border: 1px dashed var(--line);
    }
    @media (min-width: 860px) {
      html, body {
        height: 100%;
        overflow: hidden;
      }
      .app {
        height: 100vh;
        grid-template-rows: auto minmax(0, 1fr);
        overflow: hidden;
      }
      .layout {
        grid-template-columns: 22rem 1fr;
        align-items: stretch;
        min-height: 0;
        height: 100%;
        overflow: hidden;
      }
      .sidebar,
      .session {
        height: 100%;
        min-height: 0;
        overflow: hidden;
      }
      .sidebar {
        display: grid;
        grid-template-rows: auto auto minmax(0, 1fr);
        align-content: stretch;
      }
      .session {
        display: grid;
        grid-template-rows: auto auto minmax(0, 1fr) auto;
        align-content: stretch;
      }
      .session-list {
        min-height: 0;
        max-height: none;
      }
      .conversation {
        min-height: 0;
        max-height: none;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <section class="panel hero">
      <div class="hero-grid">
        <div class="hero-copy">
          <div class="hero-wordmark" aria-label="Redex">
            <svg class="hero-mark" viewBox="0 0 64 64" aria-hidden="true">
              <path d="M22 14 L8 32 L22 50" fill="none" stroke="#f8fbff" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M42 14 L56 32 L42 50" fill="none" stroke="#7fb0de" stroke-width="5.5" stroke-linecap="round" stroke-linejoin="round"/>
              <circle cx="32" cy="18" r="3.6" fill="#f8fbff"/>
              <circle cx="32" cy="46" r="4.8" fill="#7fb0de"/>
              <path d="M25 46 A 7 7 0 0 1 39 46" fill="none" stroke="#7fb0de" stroke-width="2.1" stroke-linecap="round" opacity="0.95"/>
              <path d="M19 46 A 13 13 0 0 1 45 46" fill="none" stroke="#7fb0de" stroke-width="1.9" stroke-linecap="round" opacity="0.48"/>
            </svg>
            <span class="hero-title">
              <span>re</span><span class="hero-title-accent">dex</span>
            </span>
          </div>
          <div class="stat-pill">
            <span class="stat-label">Runtime</span>
            <span id="connectionBadge" class="stat-value">Connecting</span>
          </div>
        </div>
        <span id="sessionCount" class="mini-pill">0 sessions</span>
      </div>
    </section>

    <section class="layout">
      <aside class="panel sidebar">
        <div class="sidebar-header">
          <div class="sidebar-copy">
            <h2>Sessions</h2>
            <p>Attach to an active Codex thread.</p>
          </div>
          <button id="refreshButton" class="secondary" type="button">Refresh</button>
        </div>
        <input id="searchInput" class="search" type="search" placeholder="Search titles, prompts, or ids">
        <div id="sessionList" class="session-list"></div>
      </aside>

      <main class="panel session">
        <div class="session-header">
          <div class="session-heading">
            <h2 id="sessionTitle">Select a session</h2>
          </div>
          <div style="display:flex; gap:0.5rem; align-items:center;">
            <span id="streamBadge" class="live-pill syncing">Syncing</span>
            <button id="reloadSessionButton" class="secondary" type="button" disabled>Reload</button>
          </div>
        </div>
        <div id="sessionMeta" class="detail-strip"></div>
        <div id="conversation" class="conversation"></div>
        <form id="composer" class="composer-shell">
          <div class="composer-row">
            <textarea id="promptInput" placeholder="Send a new prompt into this session..." disabled></textarea>
            <div class="composer-actions">
              <button id="sendButton" class="send-icon-button" type="submit" disabled aria-label="Send prompt" title="Send prompt">↑</button>
            </div>
          </div>
        </form>
      </main>
    </section>
  </div>

  <script>
    window.MathJax = {
      tex: {
        inlineMath: [["\\\\(", "\\\\)"]],
        displayMath: [["\\\\[", "\\\\]"]],
      },
      options: {
        skipHtmlTags: ["script", "noscript", "style", "textarea", "pre", "code"],
      },
    };
    const state = {
      sessions: [],
      activeSessionId: null,
      draftSession: null,
      initialSelectionDone: false,
      unseenSessionIds: {},
      lastRenderedSessionId: null,
      lastRenderedMetaKey: null,
      lastRenderedConversationKey: null,
      defaultCwd: "__DEFAULT_CWD__",
      searchQuery: "",
      expandedGroups: {},
      collapsedGroups: {},
      mathJaxRequested: false,
      eventSource: null,
      eventSourceSessionId: null,
      eventSourceHealthy: false,
      activeReloadTimer: null,
      sessionReloadTimer: null,
      activePollTimer: null,
      sessionPollTimer: null,
    };

    const sessionList = document.getElementById("sessionList");
    const sessionTitle = document.getElementById("sessionTitle");
    const sessionMeta = document.getElementById("sessionMeta");
    const conversation = document.getElementById("conversation");
    const promptInput = document.getElementById("promptInput");
    const sendButton = document.getElementById("sendButton");
    const reloadSessionButton = document.getElementById("reloadSessionButton");
    const connectionBadge = document.getElementById("connectionBadge");
    const sessionCount = document.getElementById("sessionCount");
    const streamBadge = document.getElementById("streamBadge");
    const searchInput = document.getElementById("searchInput");

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;");
    }

    function basename(path) {
      if (!path) {
        return "";
      }
      const parts = String(path).split(/[\\\\/]/).filter(Boolean);
      return parts.length ? parts[parts.length - 1] : String(path);
    }

    function shortId(id) {
      if (!id) {
        return "";
      }
      return String(id).slice(0, 8);
    }

    function sessionGroupKey(session) {
      return session.workspaceGroup || session.cwd || "all-workspaces";
    }

    function sessionStatusClass(status) {
      return status ? String(status).replace(/[^a-z0-9_-]+/gi, "-").toLowerCase() : "unknown";
    }

    function phaseLabel(message) {
      const raw = String(message.phase || (message.role === "assistant" ? "reply" : "prompt")).toLowerCase();
      if (raw === "prompt" || raw === "final_answer" || raw === "reply") {
        return "";
      }
      return raw.replaceAll("_", " ");
    }

    function visibleSessions() {
      const query = (state.searchQuery || "").trim().toLowerCase();
      if (!query) {
        return state.sessions;
      }
      return state.sessions.filter((session) => {
        const haystack = [
          session.title,
          session.preview,
          session.id,
          session.cwd,
          session.gitBranch,
        ]
          .filter(Boolean)
          .join("\\n")
          .toLowerCase();
        return haystack.includes(query);
      });
    }

    function groupedSessions() {
      const groups = new Map();
      for (const session of visibleSessions()) {
        const key = session.workspaceGroup || session.cwd || "all-workspaces";
        if (!groups.has(key)) {
          groups.set(key, {
            key,
            label: session.workspaceGroupLabel || basename(session.workspaceGroup || session.cwd) || "All workspaces",
            subtitle: session.workspaceGroup || session.cwd || "All workspaces",
            newest: session.updatedAt || "",
            sessions: [],
          });
        }
        const group = groups.get(key);
        group.sessions.push(session);
        if ((session.updatedAt || "") > group.newest) {
          group.newest = session.updatedAt || "";
        }
      }
      return Array.from(groups.values())
        .map((group) => ({
          ...group,
          sessions: [...group.sessions].sort((left, right) => (right.updatedAt || "").localeCompare(left.updatedAt || "")),
        }))
        .sort((left, right) => (right.newest || "").localeCompare(left.newest || ""));
    }

    function newestActiveSessionId() {
      const sessions = visibleSessions();
      if (!sessions.length) {
        return null;
      }
      const sorted = [...sessions].sort((left, right) => (right.updatedAt || "").localeCompare(left.updatedAt || ""));
      return sorted[0]?.id || null;
    }

    function sessionsInGroup(groupKey) {
      return state.sessions
        .filter((session) => sessionGroupKey(session) === groupKey)
        .sort((left, right) => (right.updatedAt || "").localeCompare(left.updatedAt || ""));
    }

    function preferredNewSessionCwd(groupKey) {
      const sessions = sessionsInGroup(groupKey);
      const active = sessions.find((session) => session.id === state.activeSessionId);
      if (active && active.cwd) {
        return active.cwd;
      }
      if (sessions.length && sessions[0].cwd) {
        return sessions[0].cwd;
      }
      if (state.defaultCwd && state.defaultCwd !== "all workspaces") {
        return state.defaultCwd;
      }
      return null;
    }

    function preferredTemplateSessionId(groupKey) {
      const sessions = sessionsInGroup(groupKey);
      const active = sessions.find((session) => session.id === state.activeSessionId);
      return active?.id || sessions[0]?.id || state.activeSessionId || null;
    }

    function draftSessionForGroup(groupKey) {
      if (!state.draftSession || state.draftSession.groupKey !== groupKey) {
        return null;
      }
      return {
        id: "__draft__",
        title: "New chat",
        preview: "Send the first prompt to create this chat.",
        isDraft: true,
      };
    }

    function sessionsForGroup(group) {
      const allSessions = group.sessions || [];
      if (state.expandedGroups[group.key] || allSessions.length <= 4) {
        return allSessions;
      }
      return allSessions.slice(0, 4);
    }

    function applyInlineMarkdown(text) {
      let working = String(text || "");
      const slots = [];
      const stash = (pattern, render) => {
        working = working.replace(pattern, (...args) => {
          const html = render(...args);
          const index = slots.push(html) - 1;
          return `@@REDX_SLOT_${index}@@`;
        });
      };

      stash(/`([^`\\n]+)`/g, (_, code) => `<code>${escapeHtml(code)}</code>`);
      stash(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g, (_, label, href) => (
        `<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(label)}</a>`
      ));
      stash(/(^|[\\s(])(https?:\\/\\/[^\\s<)]+)/g, (_, prefix, href) => (
        `${prefix}<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(href)}</a>`
      ));
      stash(/\\$\\$([\\s\\S]+?)\\$\\$/g, (_, expr) => `<div class="math-block">\\\\[${escapeHtml(expr.trim())}\\\\]</div>`);
      stash(/\\$([^\\n$]+?)\\$/g, (_, expr) => `<span class="math-inline">\\\\(${escapeHtml(expr.trim())}\\\\)</span>`);

      working = escapeHtml(working).replace(/\\n/g, "<br>");
      return working.replace(/@@REDX_SLOT_(\\d+)@@/g, (_, index) => slots[Number(index)] || "");
    }

    function renderMarkdown(text) {
      const normalized = String(text || "").replace(/\\r\\n?/g, "\\n");
      const lines = normalized.split("\\n");
      const parts = [];
      let index = 0;

      while (index < lines.length) {
        const line = lines[index];
        if (!line.trim()) {
          index += 1;
          continue;
        }
        if (line.startsWith("```")) {
          const language = line.slice(3).trim();
          index += 1;
          const block = [];
          while (index < lines.length && !lines[index].startsWith("```")) {
            block.push(lines[index]);
            index += 1;
          }
          if (index < lines.length) {
            index += 1;
          }
          parts.push(`<pre><code data-lang="${escapeHtml(language)}">${escapeHtml(block.join("\\n"))}</code></pre>`);
          continue;
        }
        if (line.trim() === "$$") {
          index += 1;
          const block = [];
          while (index < lines.length && lines[index].trim() !== "$$") {
            block.push(lines[index]);
            index += 1;
          }
          if (index < lines.length) {
            index += 1;
          }
          parts.push(`<div class="math-block">\\\\[${escapeHtml(block.join("\\n").trim())}\\\\]</div>`);
          continue;
        }
        if (/^\\s*[-*]\\s+/.test(line)) {
          const items = [];
          while (index < lines.length && /^\\s*[-*]\\s+/.test(lines[index])) {
            items.push(lines[index].replace(/^\\s*[-*]\\s+/, ""));
            index += 1;
          }
          parts.push(`<ul>${items.map((item) => `<li>${applyInlineMarkdown(item)}</li>`).join("")}</ul>`);
          continue;
        }
        const paragraph = [line];
        index += 1;
        while (
          index < lines.length &&
          lines[index].trim() &&
          !lines[index].startsWith("```") &&
          lines[index].trim() !== "$$" &&
          !/^\\s*[-*]\\s+/.test(lines[index])
        ) {
          paragraph.push(lines[index]);
          index += 1;
        }
        parts.push(`<p>${applyInlineMarkdown(paragraph.join("\\n"))}</p>`);
      }

      return parts.join("");
    }

    function transcriptNeedsMath(messages) {
      return (messages || []).some((message) => {
        const text = String(message.text || "");
        return text.includes("$$") || text.includes("\\\\(") || text.includes("\\\\[") || /\\$[^\\n$]+\\$/.test(text);
      });
    }

    function coalescedMessages(messages) {
      const collapsed = [];
      for (const message of messages || []) {
        if (
          message &&
          message.phase === "commentary" &&
          collapsed.length &&
          collapsed[collapsed.length - 1].phase === "commentary"
        ) {
          const previous = collapsed[collapsed.length - 1];
          previous.text = `${previous.text || ""}\n\n${message.text || ""}`.trim();
          continue;
        }
        collapsed.push({ ...message });
      }
      return collapsed;
    }

    function ensureMathJax() {
      if (window.MathJax && window.MathJax.typesetPromise) {
        return;
      }
      if (state.mathJaxRequested) {
        return;
      }
      state.mathJaxRequested = true;
      const script = document.createElement("script");
      script.src = "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js";
      script.async = true;
      script.onload = () => window.dispatchEvent(new Event("redex-math-ready"));
      document.head.appendChild(script);
    }

    function maybeTypesetMath() {
      if (!window.MathJax || !window.MathJax.typesetPromise) {
        return Promise.resolve();
      }
      return window.MathJax.typesetPromise([conversation]).catch(() => {});
    }

    function updateConnectionBadge() {
      connectionBadge.textContent = state.eventSourceHealthy ? "Live" : "Polling";
      streamBadge.className = `live-pill ${state.eventSourceHealthy ? "live" : "syncing"}`;
      streamBadge.textContent = state.eventSourceHealthy ? "Live updates" : "Reconnecting";
    }

    function updateSessionCount() {
      const count = visibleSessions().length;
      sessionCount.textContent = `${count} session${count === 1 ? "" : "s"}`;
    }

    function sessionIndexById(sessions) {
      const index = new Map();
      for (const session of sessions || []) {
        if (session && session.id) {
          index.set(session.id, session);
        }
      }
      return index;
    }

    function noteBackgroundSessionChanges(previousSessions, nextSessions) {
      const previousById = sessionIndexById(previousSessions);
      for (const session of nextSessions || []) {
        if (!session || !session.id || session.id === state.activeSessionId) {
          continue;
        }
        const previous = previousById.get(session.id);
        const isNewSession = !previous;
        const changed = previous && (
          (previous.updatedAt || "") !== (session.updatedAt || "") ||
          (previous.preview || "") !== (session.preview || "") ||
          (previous.status || "") !== (session.status || "") ||
          (previous.title || "") !== (session.title || "")
        );
        if (isNewSession || changed) {
          state.unseenSessionIds[session.id] = true;
        }
      }
    }

    function unseenCountForGroup(groupKey) {
      let count = 0;
      for (const session of sessionsInGroup(groupKey)) {
        if (session && session.id && state.unseenSessionIds[session.id]) {
          count += 1;
        }
      }
      return count;
    }

    function sessionMetaKey(session) {
      return JSON.stringify({
        id: session.id || "",
        title: session.title || "",
        status: session.status || "",
        cwd: session.cwd || "",
        updatedAt: session.updatedAt || "",
      });
    }

    function conversationKey(messages) {
      return JSON.stringify((messages || []).map((message) => [
        message.turnId || "",
        message.turnStatus || "",
        message.timestamp || "",
        message.role || "",
        message.phase || "",
        message.text || "",
      ]));
    }

    function renderSessions() {
      const groups = groupedSessions();
      updateSessionCount();
      if (!groups.length) {
        sessionList.innerHTML = '<div class="empty empty-state">No sessions match this view yet.</div>';
        return;
      }
      sessionList.innerHTML = groups.map((group) => {
        const baseVisibleSessions = sessionsForGroup(group);
        const groupDraft = draftSessionForGroup(group.key);
        const visibleGroupSessions = groupDraft ? [groupDraft, ...baseVisibleSessions] : baseVisibleSessions;
        const hiddenCount = Math.max(0, group.sessions.length - baseVisibleSessions.length);
        const isCollapsed = !!state.collapsedGroups[group.key];
        const unseenCount = unseenCountForGroup(group.key);
        return `
          <section class="session-group">
            <div class="session-group-header">
              <div>
                <div class="group-heading-line">
                  ${unseenCount ? '<span class="unseen-dot" aria-hidden="true"></span>' : ""}
                  <div class="session-group-title">${escapeHtml(group.label)}</div>
                  ${unseenCount ? `<span class="mini-pill unseen-pill">${escapeHtml(String(unseenCount))}</span>` : ""}
                </div>
                <div class="session-group-subtitle">${escapeHtml(group.subtitle)}</div>
              </div>
              <div class="group-header-actions">
                <button class="group-create secondary" type="button" data-group-create="${escapeHtml(group.key)}" aria-label="Create chat in ${escapeHtml(group.label)}" title="Create chat">+</button>
                <span class="mini-pill">${escapeHtml(String(group.sessions.length))}</span>
                <button class="group-collapse secondary" type="button" data-group-collapse="${escapeHtml(group.key)}">${isCollapsed ? "Expand" : "Collapse"}</button>
              </div>
            </div>
            ${isCollapsed ? "" : visibleGroupSessions.map((session) => {
              const isUnseen = !!(session.id && state.unseenSessionIds[session.id]);
              const activeClass = session.isDraft
                ? (!state.activeSessionId && state.draftSession?.groupKey === group.key ? "active" : "")
                : (session.id === state.activeSessionId ? "active" : "");
              return `
                <button class="session-card ${activeClass} ${isUnseen ? "unseen" : ""}" type="button" ${session.isDraft ? `data-draft-group="${escapeHtml(group.key)}"` : `data-session-id="${session.id}"`}>
                  <div class="session-card-title">
                    <strong>${escapeHtml(session.title || session.id)}</strong>
                    ${session.isDraft ? "" : `
                      <span style="display:flex; align-items:center; gap:0.38rem;">
                        ${isUnseen ? '<span class="unseen-dot" aria-hidden="true"></span>' : ""}
                        <span class="session-id">${escapeHtml(shortId(session.id))}</span>
                      </span>
                    `}
                  </div>
                  <div class="preview">${escapeHtml(session.preview || "")}</div>
                </button>
              `;
            }).join("")}
            ${!isCollapsed && hiddenCount > 0 ? `<button class="group-toggle secondary" type="button" data-group-key="${escapeHtml(group.key)}">Show ${hiddenCount} more</button>` : ""}
            ${!isCollapsed && group.sessions.length > 4 && state.expandedGroups[group.key] ? `<button class="group-toggle secondary" type="button" data-group-key="${escapeHtml(group.key)}" data-collapse="1">Show less</button>` : ""}
          </section>
        `;
      }).join("");
      for (const element of sessionList.querySelectorAll("[data-session-id]")) {
        element.addEventListener("click", () => loadSession(element.dataset.sessionId));
      }
      for (const element of sessionList.querySelectorAll("[data-draft-group]")) {
        element.addEventListener("click", () => createSession(element.dataset.draftGroup));
      }
      for (const element of sessionList.querySelectorAll("[data-group-create]")) {
        element.addEventListener("click", () => createSession(element.dataset.groupCreate, element));
      }
      for (const element of sessionList.querySelectorAll("[data-group-collapse]")) {
        element.addEventListener("click", () => {
          const key = element.dataset.groupCollapse;
          if (!key) {
            return;
          }
          if (state.collapsedGroups[key]) {
            delete state.collapsedGroups[key];
          } else {
            state.collapsedGroups[key] = true;
          }
          renderSessions();
        });
      }
      for (const element of sessionList.querySelectorAll("[data-group-key]")) {
        element.addEventListener("click", () => {
          const key = element.dataset.groupKey;
          if (!key) {
            return;
          }
          if (element.dataset.collapse === "1") {
            delete state.expandedGroups[key];
          } else {
            state.expandedGroups[key] = true;
          }
          renderSessions();
        });
      }
    }

    function renderConversation(detail, forceStick = false) {
      const messages = coalescedMessages(detail.messages || []);
      const nextConversationKey = conversationKey(messages);
      if (
        !forceStick &&
        state.lastRenderedSessionId === state.activeSessionId &&
        state.lastRenderedConversationKey === nextConversationKey
      ) {
        return;
      }
      const previousScrollTop = conversation.scrollTop;
      if (!messages.length) {
        conversation.innerHTML = '<div class="empty empty-state">No persisted transcript items yet.</div>';
        state.lastRenderedConversationKey = nextConversationKey;
        if (forceStick) {
          requestAnimationFrame(() => {
            conversation.scrollTop = conversation.scrollHeight;
          });
        }
        return;
      }
      conversation.innerHTML = messages.map((message) => `
        <article class="bubble ${message.role} ${message.phase === "commentary" ? "commentary" : ""}">
          ${message.phase === "commentary" ? `
            <details class="commentary-details">
              <summary class="commentary-summary">
                <strong>Commentary</strong>
              </summary>
              <div class="commentary-body markdown">${renderMarkdown(message.text || "")}</div>
            </details>
          ` : `
            <div class="bubble-header">
              <strong>${escapeHtml(message.role === "assistant" ? "Codex" : "You")}</strong>
              ${!phaseLabel(message) ? "" : `<span class="phase-chip">${escapeHtml(phaseLabel(message))}</span>`}
              <span>${escapeHtml(message.timestamp || "")}</span>
            </div>
            <div class="markdown">${renderMarkdown(message.text || "")}</div>
          `}
        </article>
      `).join("");
      state.lastRenderedConversationKey = nextConversationKey;
      if (transcriptNeedsMath(messages)) {
        ensureMathJax();
      }
      if (forceStick) {
        requestAnimationFrame(() => {
          conversation.scrollTop = conversation.scrollHeight;
          maybeTypesetMath();
        });
      } else {
        requestAnimationFrame(() => {
          conversation.scrollTop = previousScrollTop;
          maybeTypesetMath();
        });
      }
    }

    function renderSessionDetail(detail, forceStick = false) {
      const session = detail.session || {};
      const nextMetaKey = sessionMetaKey(session);
      if (
        forceStick ||
        state.lastRenderedSessionId !== session.id ||
        state.lastRenderedMetaKey !== nextMetaKey
      ) {
        sessionTitle.textContent = session.title || session.id || "Session";
        sessionMeta.innerHTML = `
          <details class="meta-details">
            <summary class="meta-summary">Session details</summary>
            <div class="detail-strip" style="margin-top:0.5rem; margin-bottom:0;">
              <div class="detail-chip">
                <span class="detail-chip-label">Status</span>
                <span class="detail-chip-value">${escapeHtml(session.status || "unknown")}</span>
              </div>
              <div class="detail-chip">
                <span class="detail-chip-label">Workspace</span>
                <span class="detail-chip-value">${escapeHtml(session.cwd || "all workspaces")}</span>
              </div>
              <div class="detail-chip">
                <span class="detail-chip-label">Updated</span>
                <span class="detail-chip-value">${escapeHtml(session.updatedAt || "unknown")}</span>
              </div>
              <div class="detail-chip">
                <span class="detail-chip-label">Thread</span>
                <span class="detail-chip-value mono">${escapeHtml(session.id || "")}</span>
              </div>
            </div>
          </details>
        `;
        state.lastRenderedMetaKey = nextMetaKey;
      }
      state.lastRenderedSessionId = session.id || null;
      renderConversation(detail, forceStick);
    }

    function renderDraftSession() {
      const draft = state.draftSession;
      state.lastRenderedSessionId = null;
      state.lastRenderedMetaKey = null;
      state.lastRenderedConversationKey = null;
      sessionTitle.textContent = "New chat";
      sessionMeta.innerHTML = draft ? `
        <details class="meta-details">
          <summary class="meta-summary">Session details</summary>
          <div class="detail-strip" style="margin-top:0.5rem; margin-bottom:0;">
            <div class="detail-chip">
              <span class="detail-chip-label">Workspace</span>
              <span class="detail-chip-value">${escapeHtml(draft.cwd || "all workspaces")}</span>
            </div>
          </div>
        </details>
      ` : "";
      conversation.innerHTML = '<div class="empty empty-state">Send the first prompt to create this chat.</div>';
      promptInput.disabled = false;
      sendButton.disabled = false;
      reloadSessionButton.disabled = true;
      requestAnimationFrame(() => {
        promptInput.focus();
      });
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
      const previousSessions = state.sessions;
      state.sessions = data.sessions || [];
      if (!state.draftSession) {
        if (state.initialSelectionDone) {
          const stillActive = state.activeSessionId && state.sessions.some((session) => session.id === state.activeSessionId);
          if (!stillActive) {
            state.activeSessionId = state.sessions.length ? newestActiveSessionId() : null;
          }
        } else if (state.sessions.length) {
          state.activeSessionId = newestActiveSessionId();
        }
      }
      if (state.initialSelectionDone) {
        noteBackgroundSessionChanges(previousSessions, state.sessions);
      }
      state.initialSelectionDone = true;
      renderSessions();
      if (state.draftSession) {
        renderDraftSession();
      } else if (state.activeSessionId) {
        await loadSession(state.activeSessionId);
      }
      updateSessionCount();
      ensurePolling();
    }

    async function refreshSessionListOnly() {
      const suffix = state.defaultCwd && state.defaultCwd !== "all workspaces"
        ? `?cwd=${encodeURIComponent(state.defaultCwd)}&limit=30`
        : "?limit=30";
      const data = await fetchJson(`/api/sessions${suffix}`);
      const previousSessions = state.sessions;
      state.sessions = data.sessions || [];
      noteBackgroundSessionChanges(previousSessions, state.sessions);
      if (state.activeSessionId && !state.sessions.some((session) => session.id === state.activeSessionId)) {
        state.activeSessionId = state.sessions.length ? newestActiveSessionId() : null;
      }
      renderSessions();
    }

    async function loadSession(sessionId) {
      state.draftSession = null;
      state.activeSessionId = sessionId;
      state.lastRenderedSessionId = null;
      state.lastRenderedMetaKey = null;
      state.lastRenderedConversationKey = null;
      if (sessionId) {
        delete state.unseenSessionIds[sessionId];
      }
      ensureEventSource(sessionId);
      renderSessions();
      sessionTitle.textContent = "Loading session...";
      sessionMeta.innerHTML = "";
      promptInput.disabled = true;
      sendButton.disabled = true;
      reloadSessionButton.disabled = true;
      try {
        const detail = await fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}`);
        renderSessionDetail(detail, true);
        promptInput.disabled = false;
        sendButton.disabled = false;
        reloadSessionButton.disabled = false;
      } catch (error) {
        conversation.innerHTML = "";
        sessionTitle.textContent = error.message || String(error);
      }
    }

    async function refreshActiveSessionSilently() {
      if (!state.activeSessionId || document.hidden) {
        return;
      }
      try {
        const detail = await fetchJson(`/api/sessions/${encodeURIComponent(state.activeSessionId)}`);
        renderSessionDetail(detail, false);
      } catch {
        // Keep the currently rendered session if a transient refresh fails.
      }
    }

    async function sendPrompt(event) {
      event.preventDefault();
      if (!state.activeSessionId && !state.draftSession) {
        return;
      }
      const text = promptInput.value.trim();
      if (!text) {
        return;
      }
      promptInput.disabled = true;
      sendButton.disabled = true;
      sendButton.textContent = "…";
      sessionTitle.textContent = state.activeSessionId ? "Sending..." : "Starting chat...";
      try {
        let sessionId = state.activeSessionId;
        if (!sessionId && state.draftSession) {
          const payload = await fetchJson("/api/sessions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              cwd: state.draftSession.cwd,
              templateSessionId: state.draftSession.templateSessionId,
              text,
            }),
          });
          promptInput.value = "";
          await refreshSessionListOnly();
          sessionId = payload.session?.id || null;
          if (!sessionId) {
            throw new Error("New chat did not return a session id.");
          }
          state.draftSession = null;
        } else {
          await fetchJson(`/api/sessions/${encodeURIComponent(sessionId)}/prompt`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
          });
          promptInput.value = "";
        }
        await loadSession(sessionId);
      } catch (error) {
        sessionTitle.textContent = error.message || String(error);
      } finally {
        promptInput.disabled = false;
        sendButton.disabled = false;
        sendButton.textContent = "↑";
      }
    }

    function createSession(groupKey, buttonElement = null) {
      if (!groupKey) {
        return;
      }
      state.draftSession = {
        groupKey,
        cwd: preferredNewSessionCwd(groupKey),
        templateSessionId: preferredTemplateSessionId(groupKey),
      };
      state.activeSessionId = null;
      ensureEventSource(null);
      renderSessions();
      renderDraftSession();
    }

    function ensureEventSource(sessionId) {
      if (!sessionId) {
        if (state.eventSource) {
          state.eventSource.close();
          state.eventSource = null;
        }
        state.eventSourceSessionId = null;
        state.eventSourceHealthy = false;
        updateConnectionBadge();
        return;
      }
      if (state.eventSource && state.eventSourceSessionId === sessionId) {
        return;
      }
      if (state.eventSource) {
        state.eventSource.close();
      }
      state.eventSourceSessionId = sessionId;
      state.eventSourceHealthy = false;
      updateConnectionBadge();
      state.eventSource = new EventSource(`/api/events?sessionId=${encodeURIComponent(sessionId)}`);
      state.eventSource.onopen = () => {
        state.eventSourceHealthy = true;
        updateConnectionBadge();
      };
      state.eventSource.addEventListener("notification", (event) => {
        state.eventSourceHealthy = true;
        updateConnectionBadge();
        const payload = JSON.parse(event.data);
        handleLiveEvent(payload);
      });
      state.eventSource.onerror = () => {
        state.eventSourceHealthy = false;
        updateConnectionBadge();
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
      if (method.startsWith("thread/") || method.startsWith("turn/") || method.startsWith("item/")) {
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
          if (!document.hidden) {
            refreshActiveSessionSilently();
          }
        }, 1500);
      }
      if (!state.sessionPollTimer) {
        state.sessionPollTimer = setInterval(() => {
          if (!document.hidden) {
            refreshSessionListOnly().catch(() => {});
          }
        }, 2500);
      }
    }

    document.getElementById("refreshButton").addEventListener("click", () => loadSessions());
    searchInput.addEventListener("input", (event) => {
      state.searchQuery = event.target.value || "";
      renderSessions();
    });
    promptInput.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey) {
        return;
      }
      event.preventDefault();
      if (sendButton.disabled) {
        return;
      }
      document.getElementById("composer").requestSubmit();
    });
    reloadSessionButton.addEventListener("click", () => {
      if (state.activeSessionId) {
        loadSession(state.activeSessionId);
      }
    });
    document.getElementById("composer").addEventListener("submit", sendPrompt);
    window.addEventListener("redex-math-ready", () => maybeTypesetMath());

    updateConnectionBadge();
    loadSessions().catch((error) => {
      sessionTitle.textContent = error.message || String(error);
    });
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

    def _read_session_detail_with_retry(self, session_id: str) -> dict[str, Any]:
        last_error: CodexAppServerError | None = None
        for attempt in range(6):
            try:
                with self._client() as client:
                    try:
                        return client.get_session(session_id, include_turns=True)
                    except CodexAppServerError as exc:
                        if not _is_pre_first_turn_error(exc):
                            raise
                        last_error = exc
                        try:
                            return client.get_session(session_id, include_turns=False)
                        except CodexAppServerError as fallback_exc:
                            if not _is_pre_first_turn_error(fallback_exc):
                                raise
                            last_error = fallback_exc
            except CodexAppServerError as exc:
                if not _is_pre_first_turn_error(exc):
                    raise
                last_error = exc
            if attempt < 5:
                time.sleep(0.15)
        if last_error is not None:
            raise last_error
        raise CodexAppServerError(f"thread/read failed for {session_id}")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path.startswith("/assets/"):
            self._handle_asset(parsed.path)
            return
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
        if parsed.path == "/api/sessions":
            self._handle_create_session()
            return
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

    def _handle_asset(self, path: str) -> None:
        relative = path.removeprefix("/assets/").strip()
        if not relative:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        asset_path = (ASSETS_DIR / relative).resolve()
        try:
            asset_path.relative_to(ASSETS_DIR.resolve())
        except ValueError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        if not asset_path.is_file():
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            return
        try:
            body = asset_path.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Could not read asset"})
            return
        content_type, _ = mimetypes.guess_type(str(asset_path))
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
            result = self._read_session_detail_with_retry(session_id)
        except CodexAppServerError as exc:
            self._send_error_json(exc)
            return
        self._send_json(HTTPStatus.OK, make_session_detail_payload(result))

    def _handle_create_session(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        cwd = payload.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`cwd` must be a string when provided."})
            return
        if not cwd:
            cwd = self.server.config.default_cwd
        if cwd == "all workspaces":
            cwd = None
        template_session_id = payload.get("templateSessionId")
        if template_session_id is not None and not isinstance(template_session_id, str):
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {"error": "`templateSessionId` must be a string when provided."},
            )
            return
        text = payload.get("text")
        if text is not None:
            if not isinstance(text, str) or not text.strip():
                self._send_json(
                    HTTPStatus.BAD_REQUEST,
                    {"error": "`text` must be a non-empty string when provided."},
                )
                return
        try:
            with self._client() as client:
                result = client.create_session(cwd=cwd, template_session_id=template_session_id)
                thread = result.get("thread")
                if not isinstance(thread, dict):
                    self._send_json(HTTPStatus.BAD_GATEWAY, {"error": "thread/start response did not include a thread."})
                    return
                turn_id = None
                status = None
                if text is not None:
                    send_result = client.send_prompt(str(thread.get("id")), text.strip())
                    turn = send_result.get("turn")
                    if isinstance(turn, dict):
                        turn_id = turn.get("id")
                        status = turn.get("status")
        except CodexAppServerError as exc:
            self._send_error_json(exc)
            return
        self._send_json(
            HTTPStatus.CREATED,
            {
                "ok": True,
                "session": normalize_thread(thread),
                "turnId": turn_id,
                "status": status,
            },
        )

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

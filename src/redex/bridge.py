from __future__ import annotations

import base64
import json
import mimetypes
import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import Callable
from urllib.parse import parse_qs
from urllib.parse import urlparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException
from pywebpush import webpush

from .app_server import CodexAppServerClient
from .app_server import CodexAppServerError
from .app_server import CodexAppServerUnavailable
from .app_server import _is_pre_first_turn_error
from .app_server import make_session_detail_page_payload
from .app_server import make_session_list_payload
from .app_server import normalize_thread


EVENT_BACKLOG_LIMIT = 500
ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets"
DEFAULT_TURN_PAGE_LIMIT = 40
MAX_TURN_PAGE_LIMIT = 100
RUNTIME_DIR = Path.home() / ".codex" / "runtime"
WEB_PUSH_CONFIG_PATH = RUNTIME_DIR / "redex-webpush-vapid.json"
WEB_PUSH_SUBSCRIPTIONS_PATH = RUNTIME_DIR / "redex-webpush-subscriptions.json"
MANIFEST_JSON = json.dumps(
    {
        "name": "Redex",
        "short_name": "Redex",
        "description": "Continue live Codex chats from your phone.",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "background_color": "#122133",
        "theme_color": "#122133",
        "icons": [
            {
                "src": "/assets/redex-mark.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable",
            }
        ],
    },
    indent=2,
)
SERVICE_WORKER_JS = """const CACHE_NAME = "redex-shell-v2";
const SHELL_URLS = ["/", "/manifest.webmanifest", "/assets/redex-mark.svg"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_URLS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)))
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") {
    return;
  }
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) {
    return;
  }
  if (url.pathname.startsWith("/api/") || url.pathname === "/api/events") {
    return;
  }
  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
        return response;
      })
      .catch(() => caches.match(request).then((cached) => cached || caches.match("/"))),
  );
});

self.addEventListener("push", (event) => {
  if (!event.data) {
    return;
  }
  let payload = {};
  try {
    payload = event.data.json();
  } catch {
    return;
  }
  const title = payload.title || "Codex replied";
  const options = {
    body: payload.body || "A session has a new final response.",
    icon: "/assets/redex-mark.svg",
    badge: "/assets/redex-mark.svg",
    tag: payload.threadId || "redex-final-response",
    renotify: false,
    data: {
      url: payload.url || "/",
      threadId: payload.threadId || "",
      turnId: payload.turnId || "",
    },
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  const targetUrl = new URL(data.url || "/", self.location.origin).toString();
  event.waitUntil(
    clients.matchAll({ type: "window", includeUncontrolled: true }).then(async (clientList) => {
      for (const client of clientList) {
        if (!client || !client.url) {
          continue;
        }
        const clientUrl = new URL(client.url);
        if (clientUrl.origin !== self.location.origin) {
          continue;
        }
        client.postMessage({
          type: "redex-open-session",
          url: targetUrl,
          sessionId: data.threadId || "",
        });
        if ("focus" in client) {
          await client.focus();
        }
        if ("navigate" in client && client.url !== targetUrl) {
          await client.navigate(targetUrl);
        }
        return;
      }
      if (clients.openWindow) {
        return clients.openWindow(targetUrl);
      }
      return undefined;
    }),
  );
});
"""


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#122133">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Redex">
  <link rel="manifest" href="/manifest.webmanifest">
  <link rel="icon" href="/assets/redex-mark.svg" type="image/svg+xml">
  <link rel="apple-touch-icon" href="/assets/redex-mark.svg">
  <title>Redex</title>
  <style>
    :root {
      color-scheme: dark;
      --redex-app-height: 100vh;
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
      min-height: 100vh;
      overscroll-behavior-y: none;
    }
    .app {
      min-height: 0;
      max-width: 88rem;
      margin: 0 auto;
      padding:
        calc(env(safe-area-inset-top, 0px) + 0.72rem)
        0.82rem
        calc(env(safe-area-inset-bottom, 0px) + 0.82rem);
      display: grid;
      gap: 0.82rem;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 1.2rem;
      box-shadow: 0 1rem 2.8rem rgba(0, 0, 0, 0.42);
      backdrop-filter: blur(14px);
    }
    .hero {
      padding: 0.68rem 0.82rem;
      position: sticky;
      top: 0;
      z-index: 30;
    }
    .hero-grid {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
    }
    .hero-actions {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      flex: 0 0 auto;
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
    button.mobile-only {
      display: none;
    }
    .install-button {
      padding: 0.48rem 0.82rem;
      border-radius: 999px;
      font-size: 0.84rem;
      line-height: 1;
      white-space: nowrap;
    }
    .notification-button.is-enabled {
      background: rgba(127, 176, 222, 0.24);
      color: var(--accent-strong);
      border: 1px solid rgba(220, 236, 255, 0.2);
    }
    .notification-button.is-blocked {
      background: rgba(83, 92, 106, 0.92);
      color: var(--muted);
    }
    .notify-menu {
      position: relative;
    }
    .notify-summary {
      display: inline-flex;
      align-items: center;
      gap: 0.38rem;
      cursor: pointer;
      list-style: none;
      user-select: none;
    }
    .notify-summary::-webkit-details-marker {
      display: none;
    }
    .notify-summary::after {
      content: "▾";
      font-size: 0.72rem;
      opacity: 0.8;
      transition: transform 120ms ease;
    }
    .notify-menu[open] .notify-summary::after {
      transform: rotate(180deg);
    }
    .notify-popover {
      position: absolute;
      right: 0;
      top: calc(100% + 0.45rem);
      z-index: 80;
      width: min(17rem, calc(100vw - 1.4rem));
      padding: 0.72rem;
      border-radius: 0.9rem;
      border: 1px solid var(--line);
      background: rgba(24, 38, 56, 0.98);
      box-shadow: 0 1rem 2.2rem rgba(0, 0, 0, 0.34);
      display: grid;
      gap: 0.55rem;
    }
    .notify-title {
      font-size: 0.84rem;
      font-weight: 700;
      color: var(--ink);
    }
    .notify-copy {
      color: var(--muted);
      font-size: 0.8rem;
      line-height: 1.4;
    }
    .notify-action {
      width: 100%;
      justify-content: center;
      padding: 0.48rem 0.76rem;
      border-radius: 999px;
      font-size: 0.84rem;
      line-height: 1.1;
    }
    .mobile-sidebar-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(6, 10, 16, 0.54);
      opacity: 0;
      pointer-events: none;
      transition: opacity 160ms ease;
      z-index: 45;
    }
    .layout-debug {
      position: fixed;
      left: 0.7rem;
      top: calc(env(safe-area-inset-top, 0px) + 5rem);
      z-index: 120;
      max-width: min(24rem, calc(100vw - 1.4rem));
      padding: 0.72rem 0.8rem;
      border-radius: 0.85rem;
      border: 1px solid rgba(255, 214, 102, 0.42);
      background: rgba(24, 16, 6, 0.96);
      color: #f8fbff;
      box-shadow: 0 1rem 2.2rem rgba(0, 0, 0, 0.4);
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      font-size: 0.8rem;
      line-height: 1.35;
      white-space: pre-wrap;
      pointer-events: auto;
      display: none;
    }
    .layout-debug.visible {
      display: block;
    }
    .layout {
      display: grid;
      gap: 1rem;
      min-width: 0;
    }
    .sidebar,
    .session {
      padding: 0.82rem;
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
      margin-bottom: 0.72rem;
    }
    .session-heading {
      display: flex;
      align-items: center;
      gap: 0.6rem;
      min-width: 0;
      flex-wrap: wrap;
    }
    .session-heading h2 {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .session-heading-meta {
      display: inline-flex;
      align-items: center;
      min-width: 0;
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
    .icon-button {
      width: 2rem;
      height: 2rem;
      min-width: 2rem;
      padding: 0;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 0.96rem;
      line-height: 1;
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
      padding: 0.72rem 0.82rem;
      background: rgba(47, 69, 94, 0.88);
      color: var(--ink);
      margin-bottom: 0.72rem;
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
    .conversation-shell {
      position: relative;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
    .scroll-end-button {
      position: absolute;
      right: 0.5rem;
      bottom: 0.65rem;
      z-index: 3;
      width: 2.4rem;
      height: 2.4rem;
      min-width: 2.4rem;
      padding: 0;
      border-radius: 999px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 1rem;
      line-height: 1;
      opacity: 0;
      pointer-events: none;
      transform: translateY(0.35rem);
      transition: opacity 120ms ease, transform 120ms ease, background 120ms ease;
      background: rgba(30, 47, 70, 0.94);
      box-shadow: 0 10px 28px rgba(5, 10, 18, 0.28);
      backdrop-filter: blur(10px);
    }
    .scroll-end-button.visible {
      opacity: 1;
      pointer-events: auto;
      transform: translateY(0);
    }
    .scroll-end-button:hover:not(:disabled) {
      background: rgba(48, 72, 103, 0.98);
    }
    .history-loader {
      display: flex;
      justify-content: center;
      margin-bottom: 0.1rem;
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
    .bubble.file-change-item {
      background: transparent;
      border: 0;
      padding: 0;
      margin-right: auto;
      margin-left: 0;
      max-width: min(54rem, 100%);
      box-shadow: none;
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
    .meta-summary {
      display: inline-flex;
      align-items: center;
      gap: 0.28rem;
      cursor: pointer;
      color: var(--muted);
      font-size: 0.78rem;
      list-style: none;
      user-select: none;
      white-space: nowrap;
    }
    .meta-summary::-webkit-details-marker {
      display: none;
    }
    .meta-summary::before {
      content: ">";
      display: inline-block;
      transition: transform 120ms ease;
    }
    .meta-summary-label {
      text-transform: lowercase;
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
    .file-change-card {
      width: min(52rem, 100%);
      margin-right: auto;
      border-radius: 1rem;
      border: 1px solid rgba(170, 205, 238, 0.12);
      background: rgba(29, 36, 45, 0.94);
      overflow: hidden;
      box-shadow: 0 10px 26px rgba(4, 8, 14, 0.18);
    }
    .file-change-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      padding: 0.76rem 0.92rem;
      color: #f8fbff;
      font-size: 0.95rem;
    }
    .file-change-title {
      display: inline-flex;
      align-items: baseline;
      gap: 0.5rem;
      flex-wrap: wrap;
    }
    .diff-stat-add {
      color: #4fd08b;
    }
    .diff-stat-del {
      color: #f26d6d;
    }
    .file-change-status {
      color: var(--muted);
      font-size: 0.76rem;
      text-transform: lowercase;
    }
    .file-change-list {
      border-top: 1px solid rgba(170, 205, 238, 0.08);
    }
    .file-change-item {
      border-top: 1px solid rgba(170, 205, 238, 0.08);
    }
    .file-change-item:first-child {
      border-top: 0;
    }
    .file-change-item-summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.75rem;
      padding: 0.78rem 0.92rem;
      cursor: pointer;
      list-style: none;
      user-select: none;
    }
    .file-change-item-summary::-webkit-details-marker {
      display: none;
    }
    .file-change-item-summary::after {
      content: "⌄";
      color: var(--muted);
      font-size: 0.92rem;
      transition: transform 120ms ease;
      flex: 0 0 auto;
    }
    .file-change-item[open] .file-change-item-summary::after {
      transform: rotate(180deg);
    }
    .file-change-item-main {
      display: inline-flex;
      align-items: baseline;
      gap: 0.55rem;
      flex-wrap: wrap;
      min-width: 0;
    }
    .file-change-path {
      color: #f8fbff;
      font-size: 0.95rem;
      word-break: break-word;
    }
    .file-change-kind {
      color: var(--muted);
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }
    .file-change-move {
      color: var(--muted);
      font-size: 0.78rem;
    }
    .file-change-item-stats {
      display: inline-flex;
      align-items: baseline;
      gap: 0.38rem;
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      font-size: 0.88rem;
      white-space: nowrap;
      flex: 0 0 auto;
    }
    .file-change-diff {
      margin: 0;
      padding: 0.18rem 0 0.42rem;
      background: rgba(22, 28, 35, 0.98);
      border-top: 1px solid rgba(170, 205, 238, 0.08);
      overflow: auto;
    }
    .file-change-line {
      display: block;
      padding: 0 0.92rem;
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      font-size: 0.8rem;
      line-height: 1.45;
      white-space: pre;
    }
    .file-change-line.add {
      background: rgba(37, 82, 56, 0.44);
      color: #c8f0d8;
    }
    .file-change-line.del {
      background: rgba(106, 42, 42, 0.4);
      color: #ffd2d2;
    }
    .file-change-line.hunk {
      color: #7fb0de;
    }
    .file-change-line.meta {
      color: #aebfd2;
    }
    .file-change-line.ctx {
      color: #dde7f2;
    }
    .thinking-indicator {
      display: inline-flex;
      align-items: center;
      gap: 0.48rem;
      margin-right: auto;
      padding: 0.12rem 0 0.12rem 0.78rem;
      color: rgba(225, 235, 247, 0.8);
      border-left: 2px solid rgba(134, 185, 230, 0.2);
      font-size: 0.9rem;
    }
    .thinking-dots {
      display: inline-flex;
      align-items: center;
      gap: 0.22rem;
    }
    .thinking-dot {
      width: 0.34rem;
      height: 0.34rem;
      border-radius: 999px;
      background: rgba(159, 214, 255, 0.78);
      animation: redex-thinking-pulse 1.1s ease-in-out infinite;
    }
    .thinking-dot:nth-child(2) {
      animation-delay: 0.16s;
    }
    .thinking-dot:nth-child(3) {
      animation-delay: 0.32s;
    }
    @keyframes redex-thinking-pulse {
      0%, 80%, 100% {
        opacity: 0.28;
        transform: translateY(0);
      }
      40% {
        opacity: 1;
        transform: translateY(-1px);
      }
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
    .directive-list {
      display: grid;
      gap: 0.42rem;
      margin-top: 0.48rem;
    }
    .directive-card {
      display: grid;
      gap: 0.24rem;
      padding: 0.62rem 0.74rem;
      border-radius: 0.78rem;
      border: 1px solid rgba(170, 205, 238, 0.16);
      background: rgba(24, 38, 56, 0.82);
    }
    .directive-name {
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      font-size: 0.82rem;
      color: #f8fbff;
    }
    .directive-meta {
      display: flex;
      flex-wrap: wrap;
      gap: 0.34rem;
    }
    .directive-chip {
      display: inline-flex;
      align-items: center;
      gap: 0.28rem;
      padding: 0.18rem 0.42rem;
      border-radius: 999px;
      border: 1px solid rgba(170, 205, 238, 0.14);
      background: rgba(49, 74, 102, 0.7);
      color: rgba(235, 244, 255, 0.92);
      font-size: 0.74rem;
      min-width: 0;
    }
    .directive-chip-key {
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-size: 0.68rem;
    }
    .directive-chip-value {
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .directive-preview {
      display: grid;
      gap: 0.42rem;
      margin-top: 0.1rem;
    }
    .directive-preview-summary {
      display: flex;
      flex-wrap: wrap;
      gap: 0.34rem;
    }
    .directive-file-chip {
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
      padding: 0.18rem 0.44rem;
      border-radius: 999px;
      border: 1px solid rgba(170, 205, 238, 0.14);
      background: rgba(57, 81, 108, 0.62);
      font-size: 0.75rem;
      min-width: 0;
    }
    .directive-file-path {
      color: #f8fbff;
      min-width: 0;
      overflow-wrap: anywhere;
    }
    .directive-file-delta {
      color: var(--muted);
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      font-size: 0.72rem;
      white-space: nowrap;
    }
    .directive-preview-details {
      margin-top: 0.08rem;
    }
    .directive-preview-details summary {
      cursor: pointer;
      color: var(--tint-strong);
      font-size: 0.78rem;
      user-select: none;
    }
    .directive-preview-pre {
      margin: 0.46rem 0 0;
      padding: 0.7rem 0.8rem;
      border-radius: 0.8rem;
      background: rgba(15, 24, 36, 0.92);
      color: #f8fbff;
      overflow: auto;
      border: 1px solid rgba(196, 220, 242, 0.14);
      font-family: ui-monospace, "Cascadia Code", Consolas, monospace;
      font-size: 0.81rem;
      line-height: 1.4;
      white-space: pre-wrap;
    }
    .directive-preview-note {
      color: var(--muted);
      font-size: 0.74rem;
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
      padding-top: 0.48rem;
      padding-bottom: max(0rem, env(safe-area-inset-bottom, 0px));
      min-width: 0;
      background: rgba(18, 33, 51, 0.98);
      flex: 0 0 auto;
    }
    textarea {
      width: 100%;
      min-height: 2.9rem;
      max-height: 9rem;
      resize: vertical;
      border: 1px solid var(--line);
      border-radius: 1rem;
      padding: 0.74rem 0.82rem;
      background: rgba(45, 68, 94, 0.9);
      color: var(--ink);
    }
    .composer-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 0.52rem;
      align-items: center;
    }
    .composer-actions {
      display: flex;
      align-items: center;
    }
    .send-icon-button {
      width: 2.7rem;
      height: 2.7rem;
      min-width: 2.7rem;
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
        grid-template-rows: auto minmax(0, 1fr) auto;
        align-content: stretch;
      }
      .conversation-shell {
        min-height: 0;
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
    @media (max-width: 859px) {
      html, body {
        height: 100%;
        overflow: hidden;
      }
      .app {
        min-height: 0;
        height: var(--redex-app-height, 100svh);
        display: flex;
        flex-direction: column;
        gap: 0.65rem;
        overflow: hidden;
        padding-top: calc(env(safe-area-inset-top, 0px) + 0.5rem);
        padding-inline: 0.62rem;
        padding-bottom: 0.32rem;
      }
      .hero {
        padding: 0.52rem 0.62rem;
        margin-bottom: 0;
      }
      .hero-grid {
        gap: 0.42rem;
      }
      .hero-copy {
        gap: 0.4rem;
      }
      .hero-wordmark {
        gap: 0.4rem;
      }
      .hero-title {
        font-size: 1.06rem;
      }
      .hero-mark {
        width: 1.8rem;
        height: 1.8rem;
      }
      .stat-pill {
        padding: 0.3rem 0.48rem;
      }
      .stat-label {
        display: none;
      }
      button.mobile-only {
        display: inline-flex;
      }
      .mini-pill {
        font-size: 0.73rem;
      }
      .layout {
        display: block;
        flex: 1 1 auto;
        min-height: 0;
        height: auto;
        overflow: hidden;
      }
      .sidebar {
        position: fixed;
        top: calc(env(safe-area-inset-top, 0px) + 0.55rem);
        left: 0.62rem;
        bottom: calc(env(safe-area-inset-bottom, 0px) + 0.55rem);
        width: min(25rem, calc(100vw - 1.24rem));
        z-index: 50;
        transform: translateX(calc(-100% - 1rem));
        transition: transform 160ms ease;
        display: grid;
        grid-template-rows: auto auto minmax(0, 1fr);
        align-content: stretch;
      }
      body.mobile-sidebar-open .sidebar {
        transform: translateX(0);
      }
      body.mobile-sidebar-open .mobile-sidebar-backdrop {
        opacity: 1;
        pointer-events: auto;
      }
      .session {
        display: flex;
        flex-direction: column;
        gap: 0.48rem;
        height: 100%;
        min-height: 0;
        overflow: hidden;
        padding-bottom: 0;
      }
      .session-list {
        min-height: 0;
        max-height: none;
      }
      .conversation-shell {
        flex: 1 1 auto;
        min-height: 0;
      }
      .sidebar-copy p {
        display: none;
      }
      .session-header {
        position: static;
        z-index: auto;
        background: transparent;
        backdrop-filter: none;
        padding-bottom: 0;
        margin-bottom: 0;
        flex: 0 0 auto;
      }
      .session-heading h2 {
        font-size: 1rem;
      }
      .detail-strip {
        margin-bottom: 0.55rem;
      }
      .conversation {
        flex: 1 1 auto;
        min-height: 0;
        max-height: none;
        overflow: auto;
        margin: 0;
        padding-bottom: 0.2rem;
      }
      .bubble {
        max-width: 100%;
      }
      textarea {
        min-height: 3.4rem;
      }
      .composer-shell {
        flex: 0 0 auto;
        align-self: stretch;
        margin: 0;
        padding-top: 0.38rem;
        padding-bottom: calc(env(safe-area-inset-bottom, 0px) + 0.34rem);
        background: rgba(18, 33, 51, 0.98);
      }
      .composer-row {
        align-items: end;
      }
    }
  </style>
</head>
<body>
  <div id="mobileSidebarBackdrop" class="mobile-sidebar-backdrop" aria-hidden="true"></div>
  <div id="layoutDebug" class="layout-debug" aria-hidden="true"></div>
  <div class="app">
    <section class="panel hero">
      <div class="hero-grid">
        <div class="hero-copy">
          <button id="mobileSessionsButton" class="secondary icon-button mobile-only" type="button" aria-label="Open sessions" title="Sessions">≡</button>
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
        <div class="hero-actions">
          <details id="notificationsMenu" class="notify-menu">
            <summary id="notificationsButton" class="secondary install-button notification-button notify-summary">Notify...</summary>
            <div class="notify-popover">
              <div id="notificationsTitle" class="notify-title">Final reply notifications</div>
              <div id="notificationsCopy" class="notify-copy">Only final responses notify.</div>
              <button id="notificationsActionButton" class="secondary notify-action" type="button">Enable</button>
            </div>
          </details>
          <button id="installButton" class="secondary install-button" type="button" hidden>Install</button>
          <span id="sessionCount" class="mini-pill">0 sessions</span>
        </div>
      </div>
    </section>

    <section class="layout">
      <aside class="panel sidebar">
        <div class="sidebar-header">
          <div class="sidebar-copy">
            <h2>Sessions</h2>
            <p>Attach to an active Codex thread.</p>
          </div>
          <button id="refreshButton" class="secondary icon-button" type="button" aria-label="Refresh sessions" title="Refresh sessions">↻</button>
        </div>
        <input id="searchInput" class="search" type="search" placeholder="Search titles, prompts, or ids">
        <div id="sessionList" class="session-list"></div>
      </aside>

      <main class="panel session">
        <div class="session-header">
          <div class="session-heading">
            <h2 id="sessionTitle">Select a session</h2>
            <div id="sessionMetaInline" class="session-heading-meta"></div>
          </div>
          <div style="display:flex; gap:0.5rem; align-items:center;">
            <button id="reloadSessionButton" class="secondary icon-button" type="button" disabled aria-label="Reload thread" title="Reload thread">↻</button>
          </div>
        </div>
        <div class="conversation-shell">
          <div id="conversation" class="conversation"></div>
          <button id="scrollToEndButton" class="secondary icon-button scroll-end-button" type="button" aria-label="Scroll to end" title="Scroll to end">↓</button>
        </div>
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
      activeSessionRequestNonce: 0,
      sessionListRequestNonce: 0,
      activeSessionDetail: null,
      sessionDetailCache: {},
      loadingOlderHistory: false,
      recentTurnsLimit: 40,
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
      deferredInstallPrompt: null,
      notificationsSupported: false,
      notificationsEnabled: false,
      notificationsBusy: false,
      notificationsPermission: "default",
      notificationsPublicKey: "",
      notificationsEndpoint: "",
      notificationsError: "",
    };

    const sessionList = document.getElementById("sessionList");
    const sessionTitle = document.getElementById("sessionTitle");
    const sessionMetaInline = document.getElementById("sessionMetaInline");
    const conversation = document.getElementById("conversation");
    const promptInput = document.getElementById("promptInput");
    const sendButton = document.getElementById("sendButton");
    const reloadSessionButton = document.getElementById("reloadSessionButton");
    const scrollToEndButton = document.getElementById("scrollToEndButton");
    const connectionBadge = document.getElementById("connectionBadge");
    const sessionCount = document.getElementById("sessionCount");
    const searchInput = document.getElementById("searchInput");
    const installButton = document.getElementById("installButton");
    const notificationsMenu = document.getElementById("notificationsMenu");
    const notificationsButton = document.getElementById("notificationsButton");
    const notificationsTitle = document.getElementById("notificationsTitle");
    const notificationsCopy = document.getElementById("notificationsCopy");
    const notificationsActionButton = document.getElementById("notificationsActionButton");
    const mobileSessionsButton = document.getElementById("mobileSessionsButton");
    const mobileSidebarBackdrop = document.getElementById("mobileSidebarBackdrop");
    const layoutDebug = document.getElementById("layoutDebug");
    const ACTIVE_SESSION_STORAGE_KEY = "redex.activeSessionId";

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#39;");
    }

    function basename(path) {
      if (!path) {
        return "";
      }
      const parts = String(path).split(/[\\\\/]/).filter(Boolean);
      return parts.length ? parts[parts.length - 1] : String(path);
    }

    function cloneJson(value) {
      return JSON.parse(JSON.stringify(value));
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

    function sessionIdFromUrl() {
      const params = new URLSearchParams(window.location.search);
      const raw = params.get("session");
      return raw && raw.trim() ? raw.trim() : null;
    }

    function updateSessionUrl(sessionId) {
      const url = new URL(window.location.href);
      if (sessionId) {
        url.searchParams.set("session", sessionId);
      } else {
        url.searchParams.delete("session");
      }
      const nextUrl = `${url.pathname}${url.search}${url.hash}`;
      const currentUrl = `${window.location.pathname}${window.location.search}${window.location.hash}`;
      if (nextUrl !== currentUrl) {
        window.history.replaceState({}, "", nextUrl);
      }
    }

    function phaseLabel(message) {
      const raw = String(message.phase || (message.role === "assistant" ? "reply" : "prompt")).toLowerCase();
      if (raw === "prompt" || raw === "final_answer" || raw === "reply") {
        return "";
      }
      return raw.replaceAll("_", " ");
    }

    function extractLiveUserText(content) {
      if (!Array.isArray(content)) {
        return "";
      }
      return content
        .filter((part) => part && part.type === "text" && typeof part.text === "string" && part.text.trim())
        .map((part) => part.text.replace(/\\s+$/g, ""))
        .join("\\n\\n")
        .trim();
    }

    function normalizeLiveItemMessage(item, turnId) {
      if (!item || typeof item !== "object") {
        return null;
      }
      if (item.type === "userMessage") {
        const text = extractLiveUserText(item.content);
        if (!text) {
          return null;
        }
        return {
          itemId: item.id || "",
          turnId: turnId || "",
          turnStatus: "",
          timestamp: "",
          role: "user",
          phase: null,
          text,
        };
      }
      if (item.type === "agentMessage") {
        return {
          itemId: item.id || "",
          turnId: turnId || "",
          turnStatus: "",
          timestamp: "",
          role: "assistant",
          phase: item.phase || null,
          text: typeof item.text === "string" ? item.text.replace(/\\s+$/g, "") : "",
        };
      }
      return null;
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
      const renderLink = (label, href) => {
        const rawHref = String(href || "").trim();
        const lowerHref = rawHref.toLowerCase();
        if (!rawHref || lowerHref.startsWith("javascript:") || lowerHref.startsWith("data:") || lowerHref.startsWith("vbscript:")) {
          return escapeHtml(label);
        }
        const external = /^[a-z][a-z0-9+.-]*:\\/\\//i.test(rawHref);
        const attrs = external ? ' target="_blank" rel="noreferrer"' : "";
        return `<a href="${escapeHtml(rawHref)}"${attrs}>${escapeHtml(label)}</a>`;
      };

      stash(/`([^`\\n]+)`/g, (_, code) => `<code>${escapeHtml(code)}</code>`);
      stash(/\\[([^\\]]+)\\]\\(([^\\s)]+)\\)/g, (_, label, href) => renderLink(label, href));
      stash(/(^|[\\s(])(https?:\\/\\/[^\\s<)]+)/g, (_, prefix, href) => (
        `${prefix}<a href="${escapeHtml(href)}" target="_blank" rel="noreferrer">${escapeHtml(href)}</a>`
      ));
      stash(/\\$\\$([\\s\\S]+?)\\$\\$/g, (_, expr) => `<div class="math-block">\\\\[${escapeHtml(expr.trim())}\\\\]</div>`);
      stash(/\\$([^\\n$]+?)\\$/g, (_, expr) => `<span class="math-inline">\\\\(${escapeHtml(expr.trim())}\\\\)</span>`);

      working = escapeHtml(working).replace(/\\n/g, "<br>");
      return working.replace(/@@REDX_SLOT_(\\d+)@@/g, (_, index) => slots[Number(index)] || "");
    }

    function parseDirective(line) {
      const match = String(line || "").trim().match(/^::([a-z0-9-]+)\\{(.*)\\}$/i);
      if (!match) {
        return null;
      }
      const [, name, body] = match;
      const attrs = [];
      const attrPattern = /([a-zA-Z][a-zA-Z0-9_-]*)="([^"]*)"/g;
      let attrMatch;
      while ((attrMatch = attrPattern.exec(body)) !== null) {
        attrs.push({ key: attrMatch[1], value: attrMatch[2] });
      }
      return { name, attrs };
    }

    function renderDirectiveCard(directive) {
      const chips = directive.attrs.map((attr) => `
        <span class="directive-chip">
          <span class="directive-chip-key">${escapeHtml(attr.key)}</span>
          <span class="directive-chip-value">${escapeHtml(attr.value)}</span>
        </span>
      `).join("");
      const dataAttrs = directive.attrs.map((attr) => ` data-attr-${escapeHtml(attr.key)}="${escapeHtml(attr.value)}"`).join("");
      return `
        <div class="directive-card" data-directive-name="${escapeHtml(directive.name)}"${dataAttrs}>
          <div class="directive-name">::${escapeHtml(directive.name)}</div>
          ${chips ? `<div class="directive-meta">${chips}</div>` : ""}
          ${directive.name.startsWith("git-") ? '<div class="directive-preview"><div class="directive-preview-note">Loading git preview...</div></div>' : ""}
        </div>
      `;
    }

    async function hydrateDirectivePreviews() {
      for (const card of conversation.querySelectorAll(".directive-card[data-directive-name^='git-']")) {
        if (card.dataset.previewHydrated === "1") {
          continue;
        }
        card.dataset.previewHydrated = "1";
        const preview = card.querySelector(".directive-preview");
        if (!preview) {
          continue;
        }
        const params = new URLSearchParams({
          name: card.dataset.directiveName || "",
          cwd: card.dataset.attrCwd || "",
        });
        if (card.dataset.attrBranch) {
          params.set("branch", card.dataset.attrBranch);
        }
        try {
          const payload = await fetchJson(`/api/git-preview?${params.toString()}`);
          const entries = Array.isArray(payload.entries) ? payload.entries : [];
          const summary = entries.length
            ? `<div class="directive-preview-summary">${entries.map((entry) => `
                <span class="directive-file-chip">
                  <span class="directive-file-path">${escapeHtml(entry.path || "")}</span>
                  <span class="directive-file-delta">${escapeHtml(`+${entry.added ?? "-"} -${entry.deleted ?? "-"}`)}</span>
                </span>
              `).join("")}</div>`
            : '<div class="directive-preview-note">No file changes to preview.</div>';
          const diffBlock = payload.diff
            ? `
              <details class="directive-preview-details">
                <summary>Show diff</summary>
                <pre class="directive-preview-pre">${escapeHtml(payload.diff)}</pre>
              </details>
            `
            : "";
          const note = payload.truncated
            ? '<div class="directive-preview-note">Diff preview truncated.</div>'
            : (payload.source ? `<div class="directive-preview-note">Preview source: ${escapeHtml(payload.source)}</div>` : "");
          preview.innerHTML = `
            <div class="directive-name">${escapeHtml(payload.title || "Git preview")}</div>
            ${summary}
            ${note}
            ${diffBlock}
          `;
        } catch (error) {
          preview.innerHTML = `<div class="directive-preview-note">${escapeHtml(error.message || String(error))}</div>`;
        }
      }
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
        const directive = parseDirective(line);
        if (directive) {
          const directives = [directive];
          index += 1;
          while (index < lines.length) {
            const nextDirective = parseDirective(lines[index]);
            if (!nextDirective) {
              break;
            }
            directives.push(nextDirective);
            index += 1;
          }
          parts.push(`<div class="directive-list">${directives.map(renderDirectiveCard).join("")}</div>`);
          continue;
        }
        const paragraph = [line];
        index += 1;
        while (
          index < lines.length &&
          lines[index].trim() &&
          !lines[index].startsWith("```") &&
          lines[index].trim() !== "$$" &&
          !/^\\s*[-*]\\s+/.test(lines[index]) &&
          !parseDirective(lines[index])
        ) {
          paragraph.push(lines[index]);
          index += 1;
        }
        parts.push(`<p>${applyInlineMarkdown(paragraph.join("\\n"))}</p>`);
      }

      return parts.join("");
    }

    function renderDiffLines(diff) {
      const lines = String(diff || "").split("\\n");
      return lines.map((line) => {
        let cls = "ctx";
        if (line.startsWith("@@")) {
          cls = "hunk";
        } else if (line.startsWith("+++ ") || line.startsWith("--- ")) {
          cls = "meta";
        } else if (line.startsWith("+")) {
          cls = "add";
        } else if (line.startsWith("-")) {
          cls = "del";
        }
        return `<span class="file-change-line ${cls}">${escapeHtml(line || " ")}</span>`;
      }).join("");
    }

    function renderFileChangeCard(message) {
      const summary = message.changeSummary || {};
      const changes = Array.isArray(message.changes) ? message.changes : [];
      const files = Number(summary.files || changes.length || 0);
      const added = Number(summary.added || 0);
      const deleted = Number(summary.deleted || 0);
      return `
        <div class="file-change-card">
          <div class="file-change-header">
            <div class="file-change-title">
              <strong>${escapeHtml(`${files} file${files === 1 ? "" : "s"} changed`)}</strong>
              <span class="diff-stat-add">+${escapeHtml(String(added))}</span>
              <span class="diff-stat-del">-${escapeHtml(String(deleted))}</span>
            </div>
            ${message.fileChangeStatus ? `<span class="file-change-status">${escapeHtml(String(message.fileChangeStatus))}</span>` : ""}
          </div>
          <div class="file-change-list">
            ${changes.map((change) => `
              <details class="file-change-item">
                <summary class="file-change-item-summary">
                  <span class="file-change-item-main">
                    <span class="file-change-path">${escapeHtml(change.path || "")}</span>
                    ${change.kind ? `<span class="file-change-kind">${escapeHtml(change.kind)}</span>` : ""}
                    ${change.movePath ? `<span class="file-change-move">→ ${escapeHtml(change.movePath)}</span>` : ""}
                  </span>
                  <span class="file-change-item-stats">
                    <span class="diff-stat-add">+${escapeHtml(String(change.added ?? 0))}</span>
                    <span class="diff-stat-del">-${escapeHtml(String(change.deleted ?? 0))}</span>
                  </span>
                </summary>
                <pre class="file-change-diff">${renderDiffLines(change.diff || "")}</pre>
              </details>
            `).join("")}
          </div>
        </div>
      `;
    }

    function normalizedTurnStatus(status) {
      return String(status || "").toLowerCase();
    }

    function isTerminalTurnStatus(status) {
      const normalized = normalizedTurnStatus(status);
      return normalized === "completed" || normalized === "failed" || normalized === "cancelled" || normalized === "canceled" || normalized === "aborted";
    }

    function hasFinalAssistantMessageForTurn(messages, turnId) {
      return (messages || []).some((message) => (
        message &&
        message.turnId === turnId &&
        message.role === "assistant" &&
        message.type !== "fileChange" &&
        message.phase !== "commentary" &&
        String(message.text || "").trim()
      ));
    }

    function aggregateTurnFileChanges(messages) {
      const byTurn = new Map();
      for (const message of messages || []) {
        if (message?.type !== "fileChange") {
          continue;
        }
        if (!isTerminalTurnStatus(message.turnStatus) || String(message.fileChangeStatus || "").toLowerCase() !== "completed") {
          continue;
        }
        const turnId = message.turnId || "";
        if (!turnId) {
          continue;
        }
        let entry = byTurn.get(turnId);
        if (!entry) {
          entry = new Map();
          byTurn.set(turnId, entry);
        }
        for (const change of Array.isArray(message.changes) ? message.changes : []) {
          const key = `${change.path || ""}::${change.movePath || ""}`;
          entry.set(key, change);
        }
      }
      const result = new Map();
      for (const [turnId, changesByKey] of byTurn.entries()) {
        const changes = Array.from(changesByKey.values());
        if (!changes.length) {
          continue;
        }
        const summary = changes.reduce((acc, change) => ({
          files: acc.files + 1,
          added: acc.added + Number(change.added || 0),
          deleted: acc.deleted + Number(change.deleted || 0),
        }), { files: 0, added: 0, deleted: 0 });
        result.set(turnId, {
          type: "fileChange",
          turnId,
          fileChangeStatus: "completed",
          changes,
          changeSummary: summary,
        });
      }
      return result;
    }

    function isRenderableFinalAssistantMessage(message) {
      return !!(
        message &&
        message.role === "assistant" &&
        message.type !== "fileChange" &&
        message.phase !== "commentary" &&
        String(message.text || "").trim()
      );
    }

    function isLastRenderableFinalAssistantMessageForTurn(message, visibleMessages, index) {
      if (!isRenderableFinalAssistantMessage(message)) {
        return false;
      }
      for (let nextIndex = index + 1; nextIndex < visibleMessages.length; nextIndex += 1) {
        const candidate = visibleMessages[nextIndex];
        if (candidate?.turnId === message.turnId && isRenderableFinalAssistantMessage(candidate)) {
          return false;
        }
      }
      return true;
    }

    function shouldShowThinkingIndicator(detail, messages) {
      const sessionStatus = String(detail?.session?.status || "").toLowerCase();
      if (sessionStatus === "active") {
        return true;
      }
      const latestTurnMessage = [...(messages || [])].reverse().find((message) => message && message.turnId);
      if (!latestTurnMessage) {
        return false;
      }
      return !isTerminalTurnStatus(latestTurnMessage.turnStatus);
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
    }

    function updateSessionCount() {
      const count = visibleSessions().length;
      sessionCount.textContent = `${count} session${count === 1 ? "" : "s"}`;
    }

    function isMobileViewport() {
      return window.matchMedia("(max-width: 859px)").matches;
    }

    function setMobileSidebarOpen(open) {
      document.body.classList.toggle("mobile-sidebar-open", !!open);
    }

    function closeMobileSidebar() {
      setMobileSidebarOpen(false);
    }

    function maybeCloseMobileSidebar() {
      if (isMobileViewport()) {
        closeMobileSidebar();
      }
    }

    function restoreActiveSessionPreference() {
      try {
        return window.localStorage.getItem(ACTIVE_SESSION_STORAGE_KEY);
      } catch {
        return null;
      }
    }

    function persistActiveSessionPreference(sessionId) {
      try {
        if (sessionId) {
          window.localStorage.setItem(ACTIVE_SESSION_STORAGE_KEY, sessionId);
        } else {
          window.localStorage.removeItem(ACTIVE_SESSION_STORAGE_KEY);
        }
      } catch {
        // Ignore localStorage issues.
      }
    }

    function updateInstallButtonVisibility() {
      installButton.hidden = !state.deferredInstallPrompt;
    }

    function pushNotificationsSupported() {
      return (
        window.isSecureContext &&
        "Notification" in window &&
        "serviceWorker" in navigator &&
        "PushManager" in window
      );
    }

    function updateNotificationsButton() {
      if (!pushNotificationsSupported()) {
        state.notificationsSupported = false;
        notificationsButton.classList.remove("is-enabled");
        notificationsButton.classList.add("is-blocked");
        if (window.isSecureContext) {
          notificationsButton.textContent = "Notify unavailable";
          notificationsTitle.textContent = "Notifications unavailable";
          notificationsCopy.textContent = "This browser can open Redex, but Push/Service Worker notifications are not available here.";
        } else {
          notificationsButton.textContent = "Notify needs HTTPS";
          notificationsTitle.textContent = "Notifications need HTTPS";
          notificationsCopy.textContent = "Phone push needs a secure origin. Your current Redex URL is plain HTTP, so the browser will not allow push registration.";
        }
        notificationsActionButton.disabled = true;
        notificationsActionButton.textContent = "Unavailable";
        return;
      }
      state.notificationsSupported = true;
      state.notificationsError = state.notificationsBusy ? state.notificationsError : state.notificationsError;
      notificationsButton.classList.toggle("is-enabled", !!state.notificationsEnabled);
      notificationsButton.classList.toggle(
        "is-blocked",
        state.notificationsPermission === "denied" && !state.notificationsEnabled,
      );
      if (state.notificationsBusy) {
        notificationsButton.textContent = "Working...";
        notificationsTitle.textContent = "Final reply notifications";
        notificationsCopy.textContent = "Only final responses notify. Updating notification registration now.";
        notificationsActionButton.disabled = true;
        notificationsActionButton.textContent = "Working...";
        return;
      }
      if (state.notificationsError) {
        notificationsButton.textContent = "Notify error";
        notificationsTitle.textContent = "Notifications hit an error";
        notificationsCopy.textContent = state.notificationsError;
        notificationsActionButton.disabled = false;
        notificationsActionButton.textContent = state.notificationsEnabled ? "Try disabling again" : "Try again";
        return;
      }
      if (state.notificationsPermission === "denied") {
        notificationsButton.textContent = "Notify blocked";
        notificationsTitle.textContent = "Notifications blocked";
        notificationsCopy.textContent = "Browser notifications are blocked for this origin. Re-enable them in browser site settings, then try again.";
        notificationsActionButton.disabled = true;
        notificationsActionButton.textContent = "Blocked";
        return;
      }
      notificationsButton.textContent = state.notificationsEnabled ? "Notify on" : "Notify off";
      notificationsTitle.textContent = "Final reply notifications";
      notificationsCopy.textContent = state.notificationsEnabled
        ? "Only final responses notify. Tap below to turn notifications off on this device."
        : "Only final responses notify. Tap below to enable notifications on this device.";
      notificationsActionButton.disabled = state.notificationsBusy || !state.notificationsPublicKey;
      notificationsActionButton.textContent = state.notificationsEnabled ? "Disable notifications" : "Enable notifications";
    }

    function withTimeout(promise, ms, label) {
      return new Promise((resolve, reject) => {
        const timer = window.setTimeout(() => reject(new Error(`${label} timed out.`)), ms);
        Promise.resolve(promise)
          .then((value) => {
            window.clearTimeout(timer);
            resolve(value);
          })
          .catch((error) => {
            window.clearTimeout(timer);
            reject(error);
          });
      });
    }

    function urlBase64ToUint8Array(value) {
      const padding = "=".repeat((4 - (value.length % 4)) % 4);
      const normalized = (value + padding).replaceAll("-", "+").replaceAll("_", "/");
      const decoded = window.atob(normalized);
      return Uint8Array.from(decoded, (char) => char.charCodeAt(0));
    }

    async function ensureServiceWorkerRegistration() {
      if (!("serviceWorker" in navigator)) {
        return null;
      }
      const existing = await withTimeout(navigator.serviceWorker.getRegistration(), 4000, "Service worker lookup");
      if (existing) {
        await withTimeout(navigator.serviceWorker.ready, 6000, "Service worker activation");
        return existing;
      }
      await withTimeout(navigator.serviceWorker.register("/sw.js"), 6000, "Service worker registration");
      return withTimeout(navigator.serviceWorker.ready, 6000, "Service worker activation");
    }

    async function currentPushSubscription() {
      const registration = await ensureServiceWorkerRegistration();
      if (!registration || !("pushManager" in registration)) {
        return null;
      }
      return registration.pushManager.getSubscription();
    }

    async function syncNotificationsState(clearError = false) {
      if (clearError) {
        state.notificationsError = "";
      }
      state.notificationsPermission = "Notification" in window ? Notification.permission : "default";
      if (!pushNotificationsSupported()) {
        updateNotificationsButton();
        return;
      }
      try {
        const status = await fetchJson("/api/notifications");
        state.notificationsPublicKey = typeof status.publicKey === "string" ? status.publicKey : "";
      } catch {
        state.notificationsPublicKey = "";
      }
      try {
        const subscription = await withTimeout(currentPushSubscription(), 6000, "Notification state check");
        if (subscription && state.notificationsPublicKey) {
          await fetchJson("/api/notifications/subscribe", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ subscription: subscription.toJSON() }),
          }).catch(() => ({}));
        }
        state.notificationsEnabled = !!subscription;
        state.notificationsEndpoint = subscription?.endpoint || "";
      } catch {
        state.notificationsEnabled = false;
        state.notificationsEndpoint = "";
      }
      updateNotificationsButton();
    }

    async function enableNotifications() {
      const permission = Notification.permission === "granted"
        ? "granted"
        : await withTimeout(Notification.requestPermission(), 10000, "Notification permission");
      state.notificationsPermission = permission;
      if (permission !== "granted") {
        state.notificationsEnabled = false;
        updateNotificationsButton();
        return;
      }
      const registration = await ensureServiceWorkerRegistration();
      if (!registration || !state.notificationsPublicKey) {
        throw new Error("Notifications are not ready yet.");
      }
      let subscription = await withTimeout(registration.pushManager.getSubscription(), 6000, "Push subscription lookup");
      if (!subscription) {
        subscription = await withTimeout(
          registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(state.notificationsPublicKey),
          }),
          15000,
          "Push subscription",
        );
      }
      await withTimeout(fetchJson("/api/notifications/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ subscription: subscription.toJSON() }),
      }), 8000, "Redex subscription sync");
      state.notificationsEnabled = true;
      state.notificationsEndpoint = subscription.endpoint || "";
    }

    async function disableNotifications() {
      const subscription = await withTimeout(currentPushSubscription(), 6000, "Push subscription lookup");
      const endpoint = subscription?.endpoint || state.notificationsEndpoint;
      if (endpoint) {
        await withTimeout(fetchJson("/api/notifications/unsubscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint }),
        }), 8000, "Redex unsubscribe sync").catch(() => ({}));
      }
      if (subscription) {
        await withTimeout(subscription.unsubscribe(), 8000, "Push unsubscribe").catch(() => false);
      }
      state.notificationsEnabled = false;
      state.notificationsEndpoint = "";
    }

    async function toggleNotifications() {
      if (!pushNotificationsSupported()) {
        return;
      }
      state.notificationsBusy = true;
      state.notificationsError = "";
      updateNotificationsButton();
      try {
        if (state.notificationsEnabled) {
          await disableNotifications();
        } else {
          await enableNotifications();
        }
      } catch (error) {
        console.error(error);
        state.notificationsError = error?.message || String(error);
      } finally {
        state.notificationsBusy = false;
        await syncNotificationsState();
      }
    }

    function layoutDebugEnabled() {
      const params = new URLSearchParams(window.location.search);
      return params.get("debug") === "layout";
    }

    function updateLayoutDebug() {
      if (!layoutDebugEnabled()) {
        layoutDebug.classList.remove("visible");
        layoutDebug.textContent = "";
        return;
      }
      const app = document.querySelector(".app");
      const sessionPanel = document.querySelector(".session");
      const composer = document.getElementById("composer");
      const vv = window.visualViewport;
      const appRect = app ? app.getBoundingClientRect() : null;
      const sessionRect = sessionPanel ? sessionPanel.getBoundingClientRect() : null;
      const convoRect = conversation ? conversation.getBoundingClientRect() : null;
      const composerRect = composer ? composer.getBoundingClientRect() : null;
      const lines = [
        `DEBUG LAYOUT`,
        `url=${window.location.search || "(none)"}`,
        `win.inner=${Math.round(window.innerWidth)}x${Math.round(window.innerHeight)}`,
        `vv=${vv ? `${Math.round(vv.width)}x${Math.round(vv.height)} @ ${Math.round(vv.offsetTop)}` : "none"}`,
        `app.h=${appRect ? Math.round(appRect.height) : "?"}`,
        `session.h=${sessionRect ? Math.round(sessionRect.height) : "?"}`,
        `conversation top=${convoRect ? Math.round(convoRect.top) : "?"} h=${convoRect ? Math.round(convoRect.height) : "?"} bottom=${convoRect ? Math.round(convoRect.bottom) : "?"}`,
        `composer top=${composerRect ? Math.round(composerRect.top) : "?"} h=${composerRect ? Math.round(composerRect.height) : "?"} bottom=${composerRect ? Math.round(composerRect.bottom) : "?"}`,
        `body.sidebar=${document.body.classList.contains("mobile-sidebar-open") ? "open" : "closed"}`,
      ];
      layoutDebug.textContent = lines.join(String.fromCharCode(10));
      layoutDebug.classList.add("visible");
    }

    function syncViewportHeight() {
      const viewport = window.visualViewport;
      const height = viewport && viewport.height ? viewport.height : window.innerHeight;
      document.documentElement.style.setProperty("--redex-app-height", `${Math.round(height)}px`);
      updateLayoutDebug();
    }

    function conversationDistanceFromEnd() {
      return Math.max(0, conversation.scrollHeight - conversation.clientHeight - conversation.scrollTop);
    }

    function shouldShowScrollToEnd() {
      return conversationDistanceFromEnd() > 56;
    }

    function updateScrollToEndButton() {
      scrollToEndButton.classList.toggle("visible", shouldShowScrollToEnd());
    }

    function scrollConversationToEnd(behavior = "smooth") {
      conversation.scrollTo({
        top: conversation.scrollHeight,
        behavior,
      });
      requestAnimationFrame(updateScrollToEndButton);
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
          (previous.status || "") !== (session.status || "") ||
          (
            (previous.preview || "") !== (session.preview || "") &&
            (session.status || "") !== "active"
          )
        );
        if (isNewSession || changed) {
          state.unseenSessionIds[session.id] = true;
        }
      }
    }

    function shouldMarkUnseenFromEvent(payload) {
      const method = payload.method || "";
      const params = payload.params || {};
      const item = params.item || {};
      if (method.startsWith("item/")) {
        if (item.type === "agentMessage") {
          return String(item.phase || "").toLowerCase() !== "commentary";
        }
        return false;
      }
      if (method === "turn/completed" || method === "turn/failed") {
        return true;
      }
      return false;
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

    function cachedSessionDetail(sessionId) {
      if (!sessionId) {
        return null;
      }
      return state.sessionDetailCache[sessionId] || null;
    }

    function cacheSessionDetail(detail) {
      const sessionId = detail?.session?.id;
      if (!sessionId) {
        return;
      }
      state.sessionDetailCache[sessionId] = detail;
    }

    function upsertLiveMessage(detail, message) {
      const messages = [...(detail.messages || [])];
      const index = messages.findIndex((entry) => entry.itemId && entry.itemId === message.itemId);
      if (index >= 0) {
        messages[index] = {
          ...messages[index],
          ...message,
          timestamp: message.timestamp || messages[index].timestamp || "",
          turnStatus: message.turnStatus || messages[index].turnStatus || "",
          phase: message.phase ?? messages[index].phase ?? null,
          text: typeof message.text === "string" && message.text.length ? message.text : (messages[index].text || ""),
        };
      } else {
        messages.push(message);
      }
      return {
        ...detail,
        messages,
      };
    }

    function appendLiveAgentDelta(detail, params) {
      const itemId = params.itemId || "";
      const delta = typeof params.delta === "string" ? params.delta : "";
      if (!itemId || !delta) {
        return detail;
      }
      const messages = [...(detail.messages || [])];
      const index = messages.findIndex((entry) => entry.itemId && entry.itemId === itemId);
      if (index >= 0) {
        messages[index] = {
          ...messages[index],
          text: `${messages[index].text || ""}${delta}`,
        };
      } else {
        messages.push({
          itemId,
          turnId: params.turnId || "",
          turnStatus: "",
          timestamp: "",
          role: "assistant",
          phase: null,
          text: delta,
        });
      }
      return {
        ...detail,
        messages,
      };
    }

    function renderActiveSessionFromLiveUpdate(nextDetail) {
      if (!nextDetail) {
        return;
      }
      const shouldStick = conversationDistanceFromEnd() <= 56;
      renderSessionDetail(nextDetail, shouldStick);
    }

    function applyIncrementalLiveEvent(payload) {
      if (!state.activeSessionDetail || !state.activeSessionId) {
        return false;
      }
      const method = payload.method || "";
      const params = payload.params || {};
      const threadId = eventThreadId(payload);
      if (!threadId || threadId !== state.activeSessionId) {
        return false;
      }
      if (method === "item/agentMessage/delta") {
        const nextDetail = appendLiveAgentDelta(cloneJson(state.activeSessionDetail), params);
        renderActiveSessionFromLiveUpdate(nextDetail);
        return true;
      }
      if (method === "item/started" || method === "item/completed") {
        const message = normalizeLiveItemMessage(params.item, params.turnId);
        if (!message) {
          return false;
        }
        const nextDetail = upsertLiveMessage(cloneJson(state.activeSessionDetail), message);
        renderActiveSessionFromLiveUpdate(nextDetail);
        return true;
      }
      return false;
    }

    function shouldRefreshSessionListForEvent(method) {
      if (!method) {
        return false;
      }
      if (
        method === "item/agentMessage/delta" ||
        method === "item/reasoning/textDelta" ||
        method === "item/reasoning/summaryTextDelta" ||
        method === "item/plan/delta" ||
        method === "item/commandExecution/outputDelta" ||
        method === "command/exec/outputDelta" ||
        method === "item/fileChange/outputDelta"
      ) {
        return false;
      }
      return method.startsWith("thread/") || method.startsWith("turn/") || method.startsWith("item/");
    }

    function shouldRefreshActiveSessionForEvent(method) {
      if (!method) {
        return false;
      }
      if (method === "item/agentMessage/delta" || method === "item/started" || method === "item/completed") {
        return false;
      }
      return method.startsWith("thread/") || method.startsWith("turn/") || method.startsWith("item/");
    }

    function conversationKey(messages) {
      return JSON.stringify((messages || []).map((message) => [
        message.itemId || "",
        message.turnId || "",
        message.turnStatus || "",
        message.timestamp || "",
        message.role || "",
        message.type || "",
        message.phase || "",
        message.text || "",
        JSON.stringify(message.changeSummary || null),
        JSON.stringify(message.changes || null),
      ]));
    }

    function messageIdentity(message) {
      if (message && message.itemId) {
        return `item:${message.itemId}`;
      }
      return JSON.stringify([
        message?.turnId || "",
        message?.turnStatus || "",
        message?.timestamp || "",
        message?.role || "",
        message?.type || "",
        message?.phase || "",
        message?.text || "",
        JSON.stringify(message?.changeSummary || null),
        JSON.stringify(message?.changes || null),
      ]);
    }

    function mergeMessages(existingMessages, incomingMessages) {
      const merged = new Map();
      for (const message of existingMessages || []) {
        merged.set(messageIdentity(message), message);
      }
      for (const message of incomingMessages || []) {
        merged.set(messageIdentity(message), message);
      }
      return Array.from(merged.values());
    }

    function prependMessages(olderMessages, existingMessages) {
      const merged = new Map();
      for (const message of olderMessages || []) {
        merged.set(messageIdentity(message), message);
      }
      for (const message of existingMessages || []) {
        merged.set(messageIdentity(message), message);
      }
      return Array.from(merged.values());
    }

    function sessionDetailUrl(sessionId, options = {}) {
      const params = new URLSearchParams();
      const limit = options.limit || state.recentTurnsLimit;
      if (limit) {
        params.set("limit", String(limit));
      }
      if (options.cursor) {
        params.set("cursor", options.cursor);
      }
      const suffix = params.toString();
      return `/api/sessions/${encodeURIComponent(sessionId)}${suffix ? `?${suffix}` : ""}`;
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
        element.addEventListener("click", () => {
          maybeCloseMobileSidebar();
          loadSession(element.dataset.sessionId);
        });
      }
      for (const element of sessionList.querySelectorAll("[data-draft-group]")) {
        element.addEventListener("click", () => {
          maybeCloseMobileSidebar();
          createSession(element.dataset.draftGroup);
        });
      }
      for (const element of sessionList.querySelectorAll("[data-group-create]")) {
        element.addEventListener("click", () => {
          maybeCloseMobileSidebar();
          createSession(element.dataset.groupCreate, element);
        });
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
      const turnFileChanges = aggregateTurnFileChanges(messages);
      const visibleMessages = messages.filter((message) => message?.type !== "fileChange");
      const nextConversationKey = `${conversationKey(messages)}|older:${detail.nextCursor || ""}|loading:${state.loadingOlderHistory ? "1" : "0"}`;
      const trailingCommentaryIndex = (() => {
        for (let index = visibleMessages.length - 1; index >= 0; index -= 1) {
          const message = visibleMessages[index];
          if (message?.phase === "commentary") {
            return index;
          }
          if (message?.role === "assistant") {
            break;
          }
        }
        return -1;
      })();
      if (
        !forceStick &&
        state.lastRenderedSessionId === state.activeSessionId &&
        state.lastRenderedConversationKey === nextConversationKey
      ) {
        return;
      }
      const previousScrollTop = conversation.scrollTop;
      const loadOlderMarkup = detail.nextCursor
        ? `<div class="history-loader"><button id="loadOlderButton" class="secondary group-toggle" type="button"${state.loadingOlderHistory ? " disabled" : ""}>${state.loadingOlderHistory ? "Loading..." : "Load older"}</button></div>`
        : "";
      if (!visibleMessages.length) {
        conversation.innerHTML = `
          ${loadOlderMarkup}
          <div class="empty empty-state">No persisted transcript items yet.</div>
        `;
        state.lastRenderedConversationKey = nextConversationKey;
        const emptyLoadOlderButton = document.getElementById("loadOlderButton");
        if (emptyLoadOlderButton) {
          emptyLoadOlderButton.addEventListener("click", () => loadOlderHistory());
        }
        if (forceStick) {
          requestAnimationFrame(() => {
            conversation.scrollTop = conversation.scrollHeight;
            updateScrollToEndButton();
          });
        }
        return;
      }
      const thinkingMarkup = shouldShowThinkingIndicator(detail, messages)
        ? `
          <div class="thinking-indicator">
            <span>Thinking...</span>
            <span class="thinking-dots" aria-hidden="true">
              <span class="thinking-dot"></span>
              <span class="thinking-dot"></span>
              <span class="thinking-dot"></span>
            </span>
          </div>
        `
        : "";
      conversation.innerHTML = `${loadOlderMarkup}${visibleMessages.map((message, index) => `
        <article class="bubble ${message.role} ${message.phase === "commentary" ? "commentary" : ""}">
          ${message.phase === "commentary" ? `
            <details class="commentary-details"${index === trailingCommentaryIndex ? " open" : ""}>
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
            ${isLastRenderableFinalAssistantMessageForTurn(message, visibleMessages, index) && turnFileChanges.has(message.turnId || "") ? renderFileChangeCard(turnFileChanges.get(message.turnId || "")) : ""}
          `}
        </article>
      `).join("")}${thinkingMarkup}`;
      state.lastRenderedConversationKey = nextConversationKey;
      const loadOlderButton = document.getElementById("loadOlderButton");
      if (loadOlderButton) {
        loadOlderButton.addEventListener("click", () => loadOlderHistory());
      }
      if (transcriptNeedsMath(messages)) {
        ensureMathJax();
      }
      if (forceStick) {
        requestAnimationFrame(() => {
          conversation.scrollTop = conversation.scrollHeight;
          updateScrollToEndButton();
          maybeTypesetMath();
          hydrateDirectivePreviews().catch(() => {});
        });
      } else {
        requestAnimationFrame(() => {
          conversation.scrollTop = previousScrollTop;
          updateScrollToEndButton();
          maybeTypesetMath();
          hydrateDirectivePreviews().catch(() => {});
        });
      }
    }

    function renderSessionDetail(detail, forceStick = false) {
      const session = detail.session || {};
      cacheSessionDetail(detail);
      state.activeSessionDetail = detail;
      const nextMetaKey = sessionMetaKey(session);
      if (
        forceStick ||
        state.lastRenderedSessionId !== session.id ||
        state.lastRenderedMetaKey !== nextMetaKey
      ) {
        sessionTitle.textContent = session.title || session.id || "Session";
        sessionMetaInline.innerHTML = `
          <details class="meta-details">
            <summary class="meta-summary"><span class="meta-summary-label">details</span></summary>
            <div class="detail-strip" style="margin-top:0.4rem; margin-bottom:0;">
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
      if (session.id && session.id === state.activeSessionId) {
        promptInput.disabled = false;
        sendButton.disabled = false;
        reloadSessionButton.disabled = false;
      }
      renderConversation(detail, forceStick);
    }

    function renderDraftSession() {
      const draft = state.draftSession;
      state.activeSessionDetail = null;
      state.loadingOlderHistory = false;
      state.lastRenderedSessionId = null;
      state.lastRenderedMetaKey = null;
      state.lastRenderedConversationKey = null;
      sessionTitle.textContent = "New chat";
      sessionMetaInline.innerHTML = draft ? `
        <details class="meta-details">
          <summary class="meta-summary"><span class="meta-summary-label">details</span></summary>
          <div class="detail-strip" style="margin-top:0.4rem; margin-bottom:0;">
            <div class="detail-chip">
              <span class="detail-chip-label">Workspace</span>
              <span class="detail-chip-value">${escapeHtml(draft.cwd || "all workspaces")}</span>
            </div>
          </div>
        </details>
      ` : "";
      conversation.innerHTML = '<div class="empty empty-state">Send the first prompt to create this chat.</div>';
      updateScrollToEndButton();
      promptInput.disabled = false;
      sendButton.disabled = false;
      reloadSessionButton.disabled = true;
      requestAnimationFrame(() => {
        promptInput.focus();
      });
    }

    function renderLoadingSession() {
      state.activeSessionDetail = null;
      state.loadingOlderHistory = false;
      state.lastRenderedSessionId = null;
      state.lastRenderedMetaKey = null;
      state.lastRenderedConversationKey = null;
      sessionTitle.textContent = "Loading session...";
      sessionMetaInline.innerHTML = "";
      conversation.innerHTML = '<div class="empty empty-state">Loading transcript...</div>';
      updateScrollToEndButton();
      promptInput.disabled = true;
      sendButton.disabled = true;
      reloadSessionButton.disabled = true;
    }

    async function fetchJson(url, init) {
      const response = await fetch(url, init);
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.error || `Request failed: ${response.status}`);
      }
      return data;
    }

    function mergeActiveDetail(incomingDetail) {
      if (!state.activeSessionDetail) {
        return incomingDetail;
      }
      return {
        ...state.activeSessionDetail,
        ...incomingDetail,
        session: incomingDetail.session || state.activeSessionDetail.session,
        messages: mergeMessages(state.activeSessionDetail.messages || [], incomingDetail.messages || []),
        nextCursor: state.activeSessionDetail.nextCursor,
        backwardsCursor: incomingDetail.backwardsCursor || state.activeSessionDetail.backwardsCursor || null,
      };
    }

    async function loadSessions() {
      const requestNonce = ++state.sessionListRequestNonce;
      const suffix = state.defaultCwd && state.defaultCwd !== "all workspaces"
        ? `?cwd=${encodeURIComponent(state.defaultCwd)}&limit=30`
        : "?limit=30";
      const data = await fetchJson(`/api/sessions${suffix}`);
      if (requestNonce !== state.sessionListRequestNonce) {
        return;
      }
      const previousSessions = state.sessions;
      state.sessions = data.sessions || [];
      const selectedSessionId = state.activeSessionId;
      const persistedSessionId = restoreActiveSessionPreference();
      const requestedSessionId = sessionIdFromUrl();
      if (!state.draftSession) {
        if (requestedSessionId) {
          state.activeSessionId = requestedSessionId;
        } else if (state.initialSelectionDone) {
          const stillActive = state.activeSessionId && state.sessions.some((session) => session.id === state.activeSessionId);
          if (!stillActive) {
            state.activeSessionId = state.sessions.length ? newestActiveSessionId() : null;
          }
        } else if (state.sessions.length) {
          const preferredSession = persistedSessionId && state.sessions.some((session) => session.id === persistedSessionId)
            ? persistedSessionId
            : newestActiveSessionId();
          state.activeSessionId = preferredSession;
        }
      }
      if (state.initialSelectionDone) {
        noteBackgroundSessionChanges(previousSessions, state.sessions);
      }
      state.initialSelectionDone = true;
      renderSessions();
      if (state.draftSession) {
        renderDraftSession();
      } else if (state.activeSessionId && state.activeSessionId === selectedSessionId) {
        await loadSession(state.activeSessionId);
      } else if (state.activeSessionId) {
        await loadSession(state.activeSessionId);
      }
      updateSessionCount();
      ensurePolling();
    }

    async function refreshSessionListOnly() {
      const requestNonce = ++state.sessionListRequestNonce;
      const suffix = state.defaultCwd && state.defaultCwd !== "all workspaces"
        ? `?cwd=${encodeURIComponent(state.defaultCwd)}&limit=30`
        : "?limit=30";
      const data = await fetchJson(`/api/sessions${suffix}`);
      if (requestNonce !== state.sessionListRequestNonce) {
        return;
      }
      const previousSessions = state.sessions;
      state.sessions = data.sessions || [];
      noteBackgroundSessionChanges(previousSessions, state.sessions);
      if (state.activeSessionId && !state.sessions.some((session) => session.id === state.activeSessionId)) {
        state.activeSessionId = state.sessions.length ? newestActiveSessionId() : null;
      }
      renderSessions();
    }

    async function loadSession(sessionId) {
      state.sessionListRequestNonce += 1;
      const requestNonce = ++state.activeSessionRequestNonce;
      state.draftSession = null;
      state.activeSessionId = sessionId;
      persistActiveSessionPreference(sessionId);
      updateSessionUrl(sessionId);
      state.activeSessionDetail = null;
      state.loadingOlderHistory = false;
      state.lastRenderedSessionId = null;
      state.lastRenderedMetaKey = null;
      state.lastRenderedConversationKey = null;
      if (sessionId) {
        delete state.unseenSessionIds[sessionId];
      }
      ensureEventSource(sessionId);
      renderSessions();
      const cachedDetail = cachedSessionDetail(sessionId);
      if (cachedDetail) {
        renderSessionDetail(cachedDetail, true);
      } else {
        renderLoadingSession();
      }
      try {
        const detail = await fetchJson(sessionDetailUrl(sessionId));
        if (state.activeSessionId !== sessionId || state.activeSessionRequestNonce !== requestNonce) {
          return;
        }
        renderSessionDetail(detail, true);
        promptInput.disabled = false;
        sendButton.disabled = false;
        reloadSessionButton.disabled = false;
      } catch (error) {
        if (state.activeSessionId !== sessionId || state.activeSessionRequestNonce !== requestNonce) {
          return;
        }
        conversation.innerHTML = "";
        sessionTitle.textContent = error.message || String(error);
      }
    }

    async function refreshActiveSessionSilently() {
      if (!state.activeSessionId || document.hidden) {
        return;
      }
      const sessionId = state.activeSessionId;
      const requestNonce = state.activeSessionRequestNonce;
      try {
        const detail = await fetchJson(sessionDetailUrl(sessionId));
        if (state.activeSessionId !== sessionId || state.activeSessionRequestNonce !== requestNonce) {
          return;
        }
        renderSessionDetail(mergeActiveDetail(detail), false);
      } catch {
        // Keep the currently rendered session if a transient refresh fails.
      }
    }

    async function loadOlderHistory() {
      const currentDetail = state.activeSessionDetail;
      if (
        !state.activeSessionId ||
        !currentDetail ||
        !currentDetail.nextCursor ||
        state.loadingOlderHistory
      ) {
        return;
      }
      const sessionId = state.activeSessionId;
      const requestNonce = state.activeSessionRequestNonce;
      const previousScrollTop = conversation.scrollTop;
      const previousScrollHeight = conversation.scrollHeight;
      state.loadingOlderHistory = true;
      renderConversation(currentDetail, false);
      try {
        const olderDetail = await fetchJson(
          sessionDetailUrl(sessionId, {
            limit: state.recentTurnsLimit,
            cursor: currentDetail.nextCursor,
          }),
        );
        if (state.activeSessionId !== sessionId || state.activeSessionRequestNonce !== requestNonce) {
          return;
        }
        const combinedDetail = {
          ...currentDetail,
          session: olderDetail.session || currentDetail.session,
          messages: prependMessages(olderDetail.messages || [], currentDetail.messages || []),
          nextCursor: olderDetail.nextCursor || null,
          backwardsCursor: currentDetail.backwardsCursor || olderDetail.backwardsCursor || null,
        };
        state.loadingOlderHistory = false;
        renderSessionDetail(combinedDetail, false);
        requestAnimationFrame(() => {
          const addedHeight = conversation.scrollHeight - previousScrollHeight;
          conversation.scrollTop = previousScrollTop + Math.max(0, addedHeight);
          updateScrollToEndButton();
        });
      } catch {
        // Leave the current transcript rendered if loading older history fails.
        if (state.activeSessionId === sessionId && state.activeSessionRequestNonce === requestNonce) {
          state.loadingOlderHistory = false;
          renderConversation(currentDetail, false);
        }
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
      state.sessionListRequestNonce += 1;
      state.activeSessionRequestNonce += 1;
      state.draftSession = {
        groupKey,
        cwd: preferredNewSessionCwd(groupKey),
        templateSessionId: preferredTemplateSessionId(groupKey),
      };
      state.activeSessionId = null;
      persistActiveSessionPreference(null);
      updateSessionUrl(null);
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
      if (threadId && !affectsActive && shouldMarkUnseenFromEvent(payload)) {
        state.unseenSessionIds[threadId] = true;
        renderSessions();
      }
      let handledIncrementally = false;
      if (affectsActive) {
        handledIncrementally = applyIncrementalLiveEvent(payload);
      }
      if (shouldRefreshSessionListForEvent(method)) {
        scheduleSessionListReload();
      }
      if (affectsActive && !handledIncrementally && shouldRefreshActiveSessionForEvent(method)) {
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
    mobileSessionsButton.addEventListener("click", () => setMobileSidebarOpen(true));
    mobileSidebarBackdrop.addEventListener("click", closeMobileSidebar);
    installButton.addEventListener("click", async () => {
      if (!state.deferredInstallPrompt) {
        return;
      }
      const prompt = state.deferredInstallPrompt;
      state.deferredInstallPrompt = null;
      updateInstallButtonVisibility();
      await prompt.prompt();
      await prompt.userChoice.catch(() => null);
    });
    notificationsActionButton.addEventListener("click", () => {
      toggleNotifications().catch(() => {});
    });
    document.addEventListener("click", (event) => {
      if (!notificationsMenu.open) {
        return;
      }
      if (notificationsMenu.contains(event.target)) {
        return;
      }
      notificationsMenu.open = false;
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
    conversation.addEventListener("scroll", updateScrollToEndButton, { passive: true });
    scrollToEndButton.addEventListener("click", () => scrollConversationToEnd());
    window.addEventListener("redex-math-ready", () => maybeTypesetMath());
    window.addEventListener("beforeinstallprompt", (event) => {
      event.preventDefault();
      state.deferredInstallPrompt = event;
      updateInstallButtonVisibility();
    });
    window.addEventListener("appinstalled", () => {
      state.deferredInstallPrompt = null;
      updateInstallButtonVisibility();
    });
    window.addEventListener("resize", syncViewportHeight);
    window.addEventListener("resize", () => {
      if (!isMobileViewport()) {
        closeMobileSidebar();
      }
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        closeMobileSidebar();
      }
    });
    if ("serviceWorker" in navigator) {
      window.addEventListener("load", () => {
        navigator.serviceWorker.register("/sw.js").catch(() => {});
      });
      navigator.serviceWorker.addEventListener("message", (event) => {
        const payload = event.data || {};
        if (payload.type !== "redex-open-session") {
          return;
        }
        const sessionId = typeof payload.sessionId === "string" ? payload.sessionId.trim() : "";
        if (!sessionId) {
          return;
        }
        updateSessionUrl(sessionId);
        loadSession(sessionId).catch((error) => {
          sessionTitle.textContent = error.message || String(error);
        });
      });
    }
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", syncViewportHeight);
      window.visualViewport.addEventListener("scroll", syncViewportHeight);
    }

    syncViewportHeight();
    updateConnectionBadge();
    updateInstallButtonVisibility();
    updateNotificationsButton();
    syncNotificationsState().catch(() => {});
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


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_or_create_web_push_config() -> dict[str, str]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(WEB_PUSH_CONFIG_PATH.read_text(encoding="utf-8"))
        if (
            isinstance(payload, dict)
            and isinstance(payload.get("publicKey"), str)
            and isinstance(payload.get("privateKeyPem"), str)
        ):
            return {
                "publicKey": payload["publicKey"],
                "privateKeyPem": payload["privateKeyPem"],
            }
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_numbers = private_key.private_numbers()
    public_numbers = private_numbers.public_numbers
    public_key_bytes = b"\x04" + public_numbers.x.to_bytes(32, "big") + public_numbers.y.to_bytes(32, "big")
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    payload = {
        "publicKey": _b64url_encode(public_key_bytes),
        "privateKeyPem": private_key_pem,
    }
    WEB_PUSH_CONFIG_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


class PushSubscriptionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._load())

    def upsert(self, subscription: dict[str, Any]) -> None:
        endpoint = subscription.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError("Push subscription must include an endpoint.")
        with self._lock:
            subscriptions = [entry for entry in self._load() if entry.get("endpoint") != endpoint]
            subscriptions.append(subscription)
            self._save(subscriptions)

    def remove(self, endpoint: str) -> None:
        if not endpoint:
            return
        with self._lock:
            subscriptions = [entry for entry in self._load() if entry.get("endpoint") != endpoint]
            self._save(subscriptions)

    def _load(self) -> list[dict[str, Any]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [entry for entry in payload if isinstance(entry, dict)]

    def _save(self, subscriptions: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(subscriptions, indent=2), encoding="utf-8")


class PushNotifier:
    def __init__(self, app_server_url: str | None, subscription_store: PushSubscriptionStore) -> None:
        self.app_server_url = app_server_url
        self.subscription_store = subscription_store
        self.web_push_config = _load_or_create_web_push_config()
        self._lock = threading.Lock()
        self._recent_turn_ids: deque[str] = deque(maxlen=200)

    @property
    def public_key(self) -> str:
        return self.web_push_config["publicKey"]

    def subscription_status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.subscription_store.list()),
            "count": len(self.subscription_store.list()),
            "publicKey": self.public_key,
        }

    def notify_final_response(self, thread_id: str, turn_id: str) -> None:
        if not thread_id or not turn_id:
            return
        with self._lock:
            if turn_id in self._recent_turn_ids:
                return
            self._recent_turn_ids.append(turn_id)
        subscriptions = self.subscription_store.list()
        if not subscriptions:
            return
        title = "Codex replied"
        body = "A session has a new final response."
        try:
            with CodexAppServerClient(self.app_server_url) as client:
                thread = client.get_session(thread_id, include_turns=False).get("thread")
                if isinstance(thread, dict):
                    thread_title = normalize_thread(thread).get("title")
                    if isinstance(thread_title, str) and thread_title.strip():
                        body = thread_title.strip()
        except CodexAppServerError:
            pass
        payload = json.dumps(
            {
                "title": title,
                "body": body,
                "url": f"/?session={thread_id}",
                "threadId": thread_id,
                "turnId": turn_id,
            }
        )
        vapid_claims = {"sub": "mailto:redex@local"}
        for subscription in subscriptions:
            try:
                webpush(
                    subscription_info=subscription,
                    data=payload,
                    vapid_private_key=self.web_push_config["privateKeyPem"],
                    vapid_claims=vapid_claims,
                )
            except WebPushException:
                endpoint = subscription.get("endpoint")
                if isinstance(endpoint, str):
                    self.subscription_store.remove(endpoint)


class LiveEventHub:
    def __init__(
        self,
        app_server_url: str | None,
        *,
        notification_observer: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.app_server_url = app_server_url
        self.notification_observer = notification_observer
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
        if self.notification_observer is not None and isinstance(params, dict):
            try:
                self.notification_observer(method, params)
            except Exception:
                pass
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
        self.push_subscription_store = PushSubscriptionStore(WEB_PUSH_SUBSCRIPTIONS_PATH)
        self.push_notifier = PushNotifier(config.app_server_url, self.push_subscription_store)
        self.live_event_hub = LiveEventHub(
            config.app_server_url,
            notification_observer=self._observe_notification,
        )

    def _observe_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "item/completed":
            return
        item = params.get("item")
        if not isinstance(item, dict):
            return
        if item.get("type") != "agentMessage":
            return
        if str(item.get("phase") or "").lower() != "final_answer":
            return
        thread_id = params.get("threadId")
        turn_id = params.get("turnId")
        if not isinstance(thread_id, str) or not isinstance(turn_id, str):
            return
        self.push_notifier.notify_final_response(thread_id, turn_id)


class RedexHandler(BaseHTTPRequestHandler):
    server: RedexHttpServer

    def _read_session_detail_with_retry(
        self,
        session_id: str,
        *,
        cursor: str | None = None,
        limit: int = DEFAULT_TURN_PAGE_LIMIT,
        sort_direction: str = "desc",
    ) -> dict[str, Any]:
        last_error: CodexAppServerError | None = None
        for attempt in range(6):
            try:
                with self._client() as client:
                    thread_result = client.get_session(session_id, include_turns=False)
                    try:
                        turns_result = client.list_session_turns(
                            session_id,
                            cursor=cursor,
                            limit=limit,
                            sort_direction=sort_direction,
                        )
                    except CodexAppServerError as exc:
                        if not _is_pre_first_turn_error(exc):
                            raise
                        last_error = exc
                        turns_result = {
                            "data": [],
                            "nextCursor": None,
                            "backwardsCursor": None,
                        }
                    return make_session_detail_page_payload(
                        thread_result,
                        turns_result,
                        sort_direction=sort_direction,
                    )
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
        if parsed.path == "/manifest.webmanifest":
            self._handle_manifest()
            return
        if parsed.path == "/sw.js":
            self._handle_service_worker()
            return
        if parsed.path == "/":
            self._handle_index()
            return
        if parsed.path == "/healthz":
            self._handle_health()
            return
        if parsed.path == "/api/git-preview":
            self._handle_git_preview(parsed.query)
            return
        if parsed.path == "/api/sessions":
            self._handle_list_sessions(parsed.query)
            return
        if parsed.path == "/api/events":
            self._handle_events(parsed.query)
            return
        if parsed.path == "/api/notifications":
            self._handle_notifications_status()
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
        if parsed.path == "/api/notifications/subscribe":
            self._handle_notifications_subscribe()
            return
        if parsed.path == "/api/notifications/unsubscribe":
            self._handle_notifications_unsubscribe()
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

    def _handle_notifications_status(self) -> None:
        status = self.server.push_notifier.subscription_status()
        self._send_json(HTTPStatus.OK, status)

    def _handle_manifest(self) -> None:
        body = MANIFEST_JSON.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_service_worker(self) -> None:
        body = SERVICE_WORKER_JS.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self._send_cors_headers()
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Service-Worker-Allowed", "/")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def _handle_git_preview(self, raw_query: str) -> None:
        query = parse_qs(raw_query)
        name = _first(query.get("name"))
        cwd = _first(query.get("cwd"))
        branch = _first(query.get("branch"))
        if not name or not cwd:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "`name` and `cwd` are required."})
            return
        try:
            payload = _build_git_preview(name, cwd, branch)
        except CodexAppServerError as exc:
            self._send_error_json(exc)
            return
        self._send_json(HTTPStatus.OK, payload)

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
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        cursor = _first(query.get("cursor"))
        limit = _first_int(query.get("limit"), default=DEFAULT_TURN_PAGE_LIMIT)
        limit = max(1, min(limit, MAX_TURN_PAGE_LIMIT))
        try:
            result = self._read_session_detail_with_retry(
                session_id,
                cursor=cursor,
                limit=limit,
                sort_direction="desc",
            )
        except CodexAppServerError as exc:
            self._send_error_json(exc)
            return
        self._send_json(HTTPStatus.OK, result)

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

    def _handle_notifications_subscribe(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        subscription = payload.get("subscription")
        if not isinstance(subscription, dict):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Body must include `subscription` object."})
            return
        try:
            self.server.push_subscription_store.upsert(subscription)
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        self._send_json(HTTPStatus.OK, self.server.push_notifier.subscription_status())

    def _handle_notifications_unsubscribe(self) -> None:
        try:
            payload = self._read_json_body()
        except ValueError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        endpoint = payload.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Body must include non-empty string field `endpoint`."})
            return
        self.server.push_subscription_store.remove(endpoint)
        self._send_json(HTTPStatus.OK, self.server.push_notifier.subscription_status())

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


def _run_git(cwd: str, args: list[str], *, timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodexAppServerError(f"git preview failed in {cwd}: {exc}") from exc
    return result.stdout


def _parse_numstat(output: str) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added_text, deleted_text, path = parts
        entries.append(
            {
                "path": path,
                "added": int(added_text) if added_text.isdigit() else None,
                "deleted": int(deleted_text) if deleted_text.isdigit() else None,
            }
        )
    return entries


def _truncate_diff(text: str, *, limit: int = 120_000) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _build_git_preview(name: str, cwd: str, branch: str | None = None) -> dict[str, Any]:
    if name == "git-stage":
        entries = _parse_numstat(_run_git(cwd, ["diff", "--cached", "--numstat"]))
        diff, truncated = _truncate_diff(_run_git(cwd, ["diff", "--cached", "--no-color"]))
        return {
            "title": "Staged changes",
            "entries": entries,
            "diff": diff,
            "truncated": truncated,
            "source": "current staged diff",
        }
    if name == "git-commit":
        entries = _parse_numstat(_run_git(cwd, ["show", "--numstat", "--format=", "--no-color", "HEAD"]))
        diff, truncated = _truncate_diff(_run_git(cwd, ["show", "--no-color", "--format=medium", "HEAD"]))
        return {
            "title": "Latest commit",
            "entries": entries,
            "diff": diff,
            "truncated": truncated,
            "source": "HEAD",
        }
    if name == "git-push":
        target = branch or "HEAD"
        entries = _parse_numstat(_run_git(cwd, ["show", "--numstat", "--format=", "--no-color", target]))
        diff, truncated = _truncate_diff(_run_git(cwd, ["show", "--no-color", "--format=medium", target]))
        return {
            "title": f"Pushed {target}",
            "entries": entries,
            "diff": diff,
            "truncated": truncated,
            "source": f"latest commit on {target}",
        }
    raise CodexAppServerError(f"unsupported git preview directive: {name}")

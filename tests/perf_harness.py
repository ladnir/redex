from __future__ import annotations

import argparse
import statistics
import time
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import tests  # noqa: F401

from tests.fake_codex_server import FakeCodexAppServer
from tests.fake_codex_server import build_large_scenario
from tests.support import json_request
from tests.support import running_bridge


def benchmark(label: str, fn, *, iterations: int) -> dict[str, float]:
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    samples_ms = [sample * 1000 for sample in samples]
    stats = {
        "min_ms": min(samples_ms),
        "avg_ms": statistics.fmean(samples_ms),
        "p95_ms": sorted(samples_ms)[max(0, int(len(samples_ms) * 0.95) - 1)],
        "max_ms": max(samples_ms),
    }
    print(
        f"{label:20} min={stats['min_ms']:.1f}ms "
        f"avg={stats['avg_ms']:.1f}ms p95={stats['p95_ms']:.1f}ms max={stats['max_ms']:.1f}ms"
    )
    return stats


def benchmark_cold_and_warm(label: str, fn, *, iterations: int) -> None:
    cold_started = time.perf_counter()
    fn()
    cold_ms = (time.perf_counter() - cold_started) * 1000
    warm_stats = benchmark(f"{label} (warm)", fn, iterations=max(1, iterations - 1))
    print(f"{label:20} cold={cold_ms:.1f}ms")
    print(
        f"{'':20} warm_avg={warm_stats['avg_ms']:.1f}ms "
        f"warm_p95={warm_stats['p95_ms']:.1f}ms"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Redex against a large fake Codex runtime.")
    parser.add_argument("--sessions", type=int, default=200)
    parser.add_argument("--turns", type=int, default=80)
    parser.add_argument("--iterations", type=int, default=8)
    parser.add_argument("--thread-id", default="thread-0000")
    args = parser.parse_args()

    scenario = build_large_scenario(session_count=args.sessions, turns_per_thread=args.turns)
    with FakeCodexAppServer(scenario) as upstream:
        with running_bridge(app_server_url=upstream.url) as (server, _thread):
            host, port = server.server_address
            base_url = f"http://{host}:{port}"
            print(
                f"Redex perf harness: sessions={args.sessions} turns_per_thread={args.turns} "
                f"iterations={args.iterations}"
            )
            benchmark_cold_and_warm(
                "GET /api/sessions",
                lambda: json_request(f"{base_url}/api/sessions?limit={args.sessions}"),
                iterations=args.iterations,
            )
            benchmark(
                "GET /api/sessions/:id",
                lambda: json_request(f"{base_url}/api/sessions/{args.thread_id}?limit=40"),
                iterations=args.iterations,
            )
            benchmark(
                "POST prompt",
                lambda: json_request(
                    f"{base_url}/api/sessions/{args.thread_id}/prompt",
                    method="POST",
                    payload={"text": "benchmark prompt"},
                ),
                iterations=max(1, min(4, args.iterations)),
            )


if __name__ == "__main__":
    main()

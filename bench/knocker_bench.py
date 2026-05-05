#!/usr/bin/env python3
"""Corrected Knocker baseline benchmarks (Phase 011).

Fixes the Phase 010 benchmark-shape artifact where synchronous ingest in a
tight async loop starved the event loop. All ingest load now runs in
dedicated threads; workers run on the asyncio event loop as before.

Invocation:
    uv run --group dev python bench/knocker_bench.py
"""
from __future__ import annotations

import argparse
import asyncio
import platform
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_shared import (
    drain_until,
    get_env_info,
    run_producers,
    run_workers_drain,
    setup_db,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Knocker baseline benchmarks (corrected).")
    parser.add_argument("--ingest-n", type=int, default=5_000, help="rows for durable ingest")
    parser.add_argument("--worker-n", type=int, default=5_000, help="rows for worker drain")
    return parser.parse_args()


async def _bench_ingest_async(db_path: str, count: int) -> tuple[float, float]:
    print(f"Running ingest benchmark ({count} events, 1 producer thread)...")
    elapsed, failures, retries = await run_producers(
        db_path, count, producer_count=1, use_receive=False
    )
    if failures:
        print(f"  note: {failures} lock failures, {retries} retries")
    return elapsed, elapsed / count


async def _bench_worker_drain(db_path: str, count: int) -> tuple[float, float]:
    print(f"Running worker drain benchmark ({count} events, 1 worker)...")

    import knocker

    app = None
    try:
        app = knocker.open(db_path)

        @app.handle(endpoint="stripe", event_type="checkout.session.completed")
        def handle(event, tx):
            return None

        await run_producers(db_path, count, producer_count=1, use_receive=False)

        stop = asyncio.Event()
        worker_task = asyncio.create_task(
            run_workers_drain(app, worker_count=1, stop_event=stop)
        )
        start = time.perf_counter()
        try:
            worker_elapsed = await drain_until(
                db_path, app, count, timeout_s=60.0, poll_s=0.05
            )
        finally:
            stop.set()
            await asyncio.wait_for(worker_task, timeout=5.0)
        elapsed = time.perf_counter() - start
        return elapsed, elapsed / count
    finally:
        if app is not None:
            app.close()


async def main() -> None:
    args = _parse_args()

    with tempfile.TemporaryDirectory(prefix="knocker-bench-") as tmpdir:
        ingest_db = str(Path(tmpdir) / "ingest.db")
        worker_db = str(Path(tmpdir) / "worker.db")
        setup_db(ingest_db)
        setup_db(worker_db)

        print("Knocker benchmark (corrected)")
        info = get_env_info()
        for k, v in info.items():
            print(f"{k}={v}")
        print(f"ingest_n={args.ingest_n}")
        print(f"worker_n={args.worker_n}")
        print()

        ingest_elapsed, ingest_avg = await _bench_ingest_async(ingest_db, args.ingest_n)
        print(
            "durable ingress-only: "
            f"{ingest_elapsed:.3f}s total, {args.ingest_n / ingest_elapsed:,.0f}/s, "
            f"{ingest_avg * 1_000:.3f} ms/event"
        )

        worker_elapsed, worker_avg = await _bench_worker_drain(worker_db, args.worker_n)
        print(
            "no-op-handler worker drain: "
            f"{worker_elapsed:.3f}s total, {args.worker_n / worker_elapsed:,.0f}/s, "
            f"{worker_avg * 1_000:.3f} ms/event"
        )


if __name__ == "__main__":
    asyncio.run(main())

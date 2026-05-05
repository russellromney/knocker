#!/usr/bin/env python3
"""Topology experiments: 1/1, 1/N, N/1, N/N producer/worker configurations.

Each topology runs in-process with a fresh temp database. Producers run in
dedicated threads so they do not starve the asyncio event loop.

Invocation:
    uv run --group dev python bench/experiment_mixed_load.py [--events N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import knocker
from bench_shared import (
    ExperimentHarness,
    drain_until,
    print_summary_table,
    run_producers,
    run_workers_drain,
    setup_db,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Topology experiments.")
    parser.add_argument("--events", type=int, default=2_000)
    return parser.parse_args()


async def _run_topology(
    harness: ExperimentHarness,
    producer_count: int,
    worker_count: int,
    events: int,
    label: str,
) -> None:
    db_path = harness.db_path
    app = knocker.open(db_path)

    @app.handle(endpoint="stripe", event_type="checkout.session.completed")
    def handle(event, tx):
        return None

    stop = asyncio.Event()
    worker_task = asyncio.create_task(
        run_workers_drain(app, worker_count, stop, idle_poll_s=0.005)
    )

    start = time.perf_counter()
    prod_elapsed, failures, retries = await run_producers(
        db_path, events, producer_count=producer_count, use_receive=False
    )
    try:
        worker_elapsed = await drain_until(db_path, app, events, timeout_s=60.0)
    finally:
        stop.set()
        await asyncio.wait_for(worker_task, timeout=5.0)
    total_elapsed = time.perf_counter() - start

    harness.record(
        params={"label": label},
        producer_count=producer_count,
        worker_count=worker_count,
        event_count=events,
        producer_elapsed_s=prod_elapsed,
        worker_elapsed_s=worker_elapsed,
        lock_failures=failures,
        lock_retries=retries,
        notes=f"total wall={total_elapsed:.2f}s",
    )


async def main() -> None:
    args = _parse_args()
    harness = ExperimentHarness("topology")
    events = args.events
    try:
        for pc, wc, label in [
            (1, 1, "1p-1w"),
            (1, 4, "1p-4w"),
            (4, 1, "4p-1w"),
            (4, 4, "4p-4w"),
        ]:
            print(f"Running {label} ({pc}p/{wc}w, {events} events)...")
            harness.setup()
            await _run_topology(harness, pc, wc, events, label)
            harness.cleanup()
    finally:
        harness.cleanup()

    print_summary_table(harness.results)


if __name__ == "__main__":
    asyncio.run(main())

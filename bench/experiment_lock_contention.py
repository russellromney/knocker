#!/usr/bin/env python3
"""Lock contention repro: quantify `database is locked` under mixed load.

Tests increasing producer counts with a single worker. Each test uses a
fresh temp database.

Invocation:
    uv run --group dev python bench/experiment_lock_contention.py [--events N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
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
    parser = argparse.ArgumentParser(description="Lock contention experiments.")
    parser.add_argument("--events", type=int, default=1_500)
    return parser.parse_args()


async def _run_lock_experiment(
    harness: ExperimentHarness,
    producer_count: int,
    events: int,
) -> None:
    db_path = harness.db_path
    app = knocker.open(db_path)

    @app.handle(endpoint="stripe", event_type="checkout.session.completed")
    def handle(event, tx):
        return None

    stop = asyncio.Event()
    worker_task = asyncio.create_task(
        run_workers_drain(app, 1, stop, idle_poll_s=0.005)
    )

    prod_elapsed, failures, retries = await run_producers(
        db_path, events, producer_count=producer_count
    )
    try:
        worker_elapsed = await drain_until(db_path, app, events, timeout_s=60.0)
    finally:
        stop.set()
        await asyncio.wait_for(worker_task, timeout=5.0)

    harness.record(
        params={"producers": producer_count},
        producer_count=producer_count,
        worker_count=1,
        event_count=events,
        producer_elapsed_s=prod_elapsed,
        worker_elapsed_s=worker_elapsed,
        lock_failures=failures,
        lock_retries=retries,
        notes="",
    )


async def main() -> None:
    args = _parse_args()
    harness = ExperimentHarness("lock-contention")
    try:
        for n in [1, 2, 4]:
            print(f"Running {n}p-1w ({args.events} events)...")
            harness.setup()
            await _run_lock_experiment(harness, n, args.events)
            harness.cleanup()
    finally:
        harness.cleanup()

    print_summary_table(harness.results)


if __name__ == "__main__":
    asyncio.run(main())

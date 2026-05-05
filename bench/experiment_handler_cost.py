#!/usr/bin/env python3
"""Handler cost experiments: no-op vs tiny DB-write vs slow synthetic.

Invocation:
    uv run --group dev python bench/experiment_handler_cost.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bench_shared import (
    ExperimentHarness,
    drain_until,
    print_summary_table,
    run_producers,
    run_workers_drain,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Handler cost experiments.")
    parser.add_argument("--events", type=int, default=3_000)
    return parser.parse_args()


HANDLERS = {
    "no-op": lambda event, tx: None,
    "db-write": lambda event, tx: tx.execute(
        "INSERT INTO _handler_audit (event_id) VALUES (?)", [event.id]
    ),
    "sleep-1ms": lambda event, tx: time.sleep(0.001),
}


async def _run_handler_experiment(
    harness: ExperimentHarness,
    handler_name: str,
    events: int,
) -> None:
    db_path = harness.db_path
    import knocker

    app = knocker.open(db_path)

    if handler_name == "db-write":
        with app.db.transaction() as tx:
            tx.execute(
                "CREATE TABLE IF NOT EXISTS _handler_audit ("
                "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "  event_id INTEGER NOT NULL,"
                "  created_at INTEGER NOT NULL DEFAULT (unixepoch())"
                ")"
            )

    @app.handle(endpoint="stripe", event_type="checkout.session.completed")
    def handle(event, tx):
        HANDLERS[handler_name](event, tx)

    stop = asyncio.Event()
    worker_task = asyncio.create_task(
        run_workers_drain(app, 1, stop, idle_poll_s=0.005)
    )

    prod_elapsed, failures, retries = await run_producers(
        db_path, events, producer_count=1
    )
    try:
        worker_elapsed = await drain_until(db_path, app, events, timeout_s=60.0)
    finally:
        stop.set()
        await asyncio.wait_for(worker_task, timeout=5.0)

    harness.record(
        params={"handler": handler_name},
        producer_count=1,
        worker_count=1,
        event_count=events,
        producer_elapsed_s=prod_elapsed,
        worker_elapsed_s=worker_elapsed,
        lock_failures=failures,
        lock_retries=retries,
    )


async def main() -> None:
    args = _parse_args()
    harness = ExperimentHarness("handler-cost")
    try:
        for name in HANDLERS:
            harness.setup()
            await _run_handler_experiment(harness, name, args.events)
            harness.cleanup()
    finally:
        harness.cleanup()

    print_summary_table(harness.results)


if __name__ == "__main__":
    asyncio.run(main())

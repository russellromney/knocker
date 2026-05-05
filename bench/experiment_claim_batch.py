#!/usr/bin/env python3
"""Claim batch size experiment.

Monkey-patches _HonkerQueue.claim_batch to test different batch sizes.
With the worker iterator's claimed-job buffer, this now measures end-to-end
worker drain throughput for larger claim batches rather than only the raw
claim-transaction amortisation cost.

Invocation:
    uv run --group dev python bench/experiment_claim_batch.py [--events N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import knocker
from knocker.queue import _HonkerQueue
from bench_shared import (
    ExperimentHarness,
    drain_until,
    print_summary_table,
    run_producers,
    run_workers_drain,
    setup_db,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Claim batch size experiments.")
    parser.add_argument("--events", type=int, default=2_000)
    return parser.parse_args()


async def _run_batch_size(
    harness: ExperimentHarness,
    batch_size: int,
    events: int,
) -> None:
    db_path = harness.db_path
    original_claim_batch = _HonkerQueue.claim_batch

    def patched_claim_batch(self, worker_id, n):
        return original_claim_batch(self, worker_id, batch_size)

    _HonkerQueue.claim_batch = patched_claim_batch

    try:
        app = knocker.open(db_path)

        @app.handle(endpoint="stripe", event_type="checkout.session.completed")
        def handle(event, tx):
            return None

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
            params={"batch_size": batch_size},
            producer_count=1,
            worker_count=1,
            event_count=events,
            producer_elapsed_s=prod_elapsed,
            worker_elapsed_s=worker_elapsed,
            lock_failures=failures,
            lock_retries=retries,
            notes="end-to-end batch dispatch via buffered claimed jobs",
        )
    finally:
        _HonkerQueue.claim_batch = original_claim_batch


async def main() -> None:
    args = _parse_args()
    harness = ExperimentHarness("claim-batch")
    try:
        for size in [1, 2, 5, 10]:
            print(f"Running batch_size={size} ({args.events} events)...")
            harness.setup()
            await _run_batch_size(harness, size, args.events)
            harness.cleanup()
    finally:
        _HonkerQueue.claim_batch = _HonkerQueue.claim_batch  # NOP
        harness.cleanup()

    print_summary_table(harness.results)


if __name__ == "__main__":
    asyncio.run(main())

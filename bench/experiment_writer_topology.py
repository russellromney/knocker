#!/usr/bin/env python3
"""Writer-handle topology experiments.

Tests single shared `knocker.open(...)` vs multiple independent opens.
Each mode uses a fresh temp database. Multiple-opens are simulated by
producers opening their own `Knocker` instance in threads.

Invocation:
    uv run --group dev python bench/experiment_writer_topology.py [--events N]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import knocker
from bench_shared import (
    ExperimentHarness,
    drain_until,
    print_summary_table,
    run_producers,
    run_producers_shared_handle,
    run_workers_drain,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Writer topology experiments.")
    parser.add_argument("--events", type=int, default=2_000)
    return parser.parse_args()


def _producer_multi_open(
    db_path: str,
    count: int,
    offset: int,
    metrics: dict,
) -> None:
    app = knocker.open(db_path)
    failures = 0
    retries = 0
    start = time.perf_counter()
    for idx in range(count):
        event_id = f"evt-{offset}-{idx}"
        try:
            app.ingest(
                endpoint="stripe",
                body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                headers={},
                provider_event_id=event_id,
                provider_delivery_id=f"delivery-{offset}-{idx}",
                event_type="checkout.session.completed",
            )
        except Exception as exc:
            msg = str(exc).lower()
            if "database is locked" in msg or "busy" in msg:
                failures += 1
                retries += 1
                try:
                    time.sleep(0.001)
                    app.ingest(
                        endpoint="stripe",
                        body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                        headers={},
                        provider_event_id=event_id,
                        provider_delivery_id=f"delivery-{offset}-{idx}",
                        event_type="checkout.session.completed",
                    )
                except Exception:
                    retries += 1
            else:
                raise
    metrics["elapsed"] = time.perf_counter() - start
    metrics["failures"] = failures
    metrics["retries"] = retries
    del app


async def _run_shared_handle(harness: ExperimentHarness, events: int) -> None:
    db_path = harness.db_path
    app = knocker.open(db_path)

    @app.handle(endpoint="stripe", event_type="checkout.session.completed")
    def handle(event, tx):
        return None

    stop = asyncio.Event()
    worker_task = asyncio.create_task(
        run_workers_drain(app, 1, stop, idle_poll_s=0.005)
    )

    prod_elapsed, failures, retries = await run_producers_shared_handle(
        app, events, producer_count=2
    )
    try:
        worker_elapsed = await drain_until(db_path, app, events, timeout_s=60.0)
    finally:
        stop.set()
        await asyncio.wait_for(worker_task, timeout=5.0)

    harness.record(
        params={"mode": "shared-handle"},
        producer_count=2,
        worker_count=1,
        event_count=events,
        producer_elapsed_s=prod_elapsed,
        worker_elapsed_s=worker_elapsed,
        lock_failures=failures,
        lock_retries=retries,
    )


async def _run_multi_open(harness: ExperimentHarness, events: int) -> None:
    db_path = harness.db_path
    worker_app = knocker.open(db_path)

    @worker_app.handle(endpoint="stripe", event_type="checkout.session.completed")
    def handle(event, tx):
        return None

    stop = asyncio.Event()
    worker_task = asyncio.create_task(
        run_workers_drain(worker_app, 1, stop, idle_poll_s=0.005)
    )

    per_producer = events // 2
    threads: list[threading.Thread] = []
    all_metrics: list[dict] = []
    for i in range(2):
        metrics: dict = {}
        all_metrics.append(metrics)
        t = threading.Thread(
            target=_producer_multi_open,
            args=(db_path, per_producer, i, metrics),
        )
        threads.append(t)
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    prod_elapsed = time.perf_counter() - start
    failures = sum(m.get("failures", 0) for m in all_metrics)
    retries = sum(m.get("retries", 0) for m in all_metrics)

    try:
        worker_elapsed = await drain_until(db_path, worker_app, events, timeout_s=60.0)
    finally:
        stop.set()
        await asyncio.wait_for(worker_task, timeout=5.0)
    del worker_app

    harness.record(
        params={"mode": "multi-open"},
        producer_count=2,
        worker_count=1,
        event_count=events,
        producer_elapsed_s=prod_elapsed,
        worker_elapsed_s=worker_elapsed,
        lock_failures=failures,
        lock_retries=retries,
    )


async def main() -> None:
    args = _parse_args()
    harness = ExperimentHarness("writer-topology")
    try:
        print("Running shared-handle ...")
        harness.setup()
        await _run_shared_handle(harness, args.events)
        harness.cleanup()

        print("Running multi-open ...")
        harness.setup()
        await _run_multi_open(harness, args.events)
        harness.cleanup()
    finally:
        harness.cleanup()

    print_summary_table(harness.results)


if __name__ == "__main__":
    asyncio.run(main())

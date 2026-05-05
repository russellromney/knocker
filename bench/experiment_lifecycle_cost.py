#!/usr/bin/env python3
"""Lifecycle cost experiment: measure the overhead of mark_processing.

This uses a monkeypatch to bypass the ``knocker_mark_processing`` call in
``_dispatch_job`` to isolate its cost. The change is temporary and does not
affect production tests or the public API.

Invocation:
    uv run --group dev python bench/experiment_lifecycle_cost.py
"""
from __future__ import annotations

import argparse
import asyncio
import sys
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
    parser = argparse.ArgumentParser(description="Lifecycle cost experiments.")
    parser.add_argument("--events", type=int, default=3_000)
    return parser.parse_args()


async def _run_with_processing(harness: ExperimentHarness, events: int) -> None:
    db_path = harness.db_path
    import knocker

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
        params={"mode": "with-mark_processing"},
        producer_count=1,
        worker_count=1,
        event_count=events,
        producer_elapsed_s=prod_elapsed,
        worker_elapsed_s=worker_elapsed,
        lock_failures=failures,
        lock_retries=retries,
    )


async def _run_without_processing(harness: ExperimentHarness, events: int) -> None:
    import knocker._knocker as _knocker_module

    original_dispatch_job = _knocker_module.Knocker._dispatch_job

    async def patched_dispatch_job(self, job):
        import time
        from knocker.job_payload import _required_payload_int
        from knocker.queue import _QueueTransitionError, _require_queue_transition

        start = time.perf_counter()
        event_id = _required_payload_int(job.payload, "event_id")
        self._set_worker_state(job.worker_id, current_event_id=event_id)
        try:
            with self.db.transaction() as tx:
                event = _knocker_module._get_event_or_none(tx, event_id)
                if event is None:
                    self._queue.ack(job.id, job.worker_id, tx=tx)
                    return
                if event.status == "ignored":
                    self._queue.ack(job.id, job.worker_id, tx=tx)
                    return
                handler = self._resolve_handler(event)
                if handler is None:
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    tx.query(
                        "SELECT knocker_mark_failed(?, ?, ?, ?, ?)",
                        [event_id, int(job.attempts), "no handler", 1, duration_ms],
                    )
                    _require_queue_transition(
                        self._queue.fail(job.id, job.worker_id, "no handler", tx=tx),
                        action="fail",
                        job_id=job.id,
                        event_id=event_id,
                    )
                    return
                handler(event, tx)
                duration_ms = int((time.perf_counter() - start) * 1000)
                tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, duration_ms])
                _require_queue_transition(
                    self._queue.ack(job.id, job.worker_id, tx=tx),
                    action="ack",
                    job_id=job.id,
                    event_id=event_id,
                )
        except _QueueTransitionError:
            raise
        except Exception as exc:
            await self._fail_job(job, event_id, exc, int((time.perf_counter() - start) * 1000))
        finally:
            self._set_worker_state(job.worker_id, current_event_id=None)

    _knocker_module.Knocker._dispatch_job = patched_dispatch_job

    try:
        db_path = harness.db_path
        import knocker

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
            params={"mode": "without-mark_processing"},
            producer_count=1,
            worker_count=1,
            event_count=events,
            producer_elapsed_s=prod_elapsed,
            worker_elapsed_s=worker_elapsed,
            lock_failures=failures,
            lock_retries=retries,
        )
    finally:
        _knocker_module.Knocker._dispatch_job = original_dispatch_job


async def main() -> None:
    args = _parse_args()
    harness = ExperimentHarness("lifecycle-cost")
    try:
        harness.setup()
        await _run_with_processing(harness, args.events)
        harness.cleanup()

        harness.setup()
        await _run_without_processing(harness, args.events)
        harness.cleanup()
    finally:
        harness.cleanup()

    print_summary_table(harness.results)


if __name__ == "__main__":
    asyncio.run(main())

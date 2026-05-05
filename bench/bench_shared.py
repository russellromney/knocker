#!/usr/bin/env python3
"""Common benchmark and experiment utilities for Knocker throughput investigation."""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import platform
import sqlite3
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import knocker


def get_env_info() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "sqlite": sqlite3.sqlite_version,
        "processor": os.environ.get("PROCESSOR_IDENTIFIER", "unknown"),
    }


@dataclass
class ExperimentResult:
    name: str
    params: dict[str, Any] = field(default_factory=dict)
    producer_count: int = 0
    worker_count: int = 0
    event_count: int = 0
    producer_elapsed_s: float = 0.0
    worker_elapsed_s: float = 0.0
    lock_failures: int = 0
    lock_retries: int = 0
    notes: str = ""

    @property
    def producer_throughput(self) -> float:
        return self.event_count / self.producer_elapsed_s if self.producer_elapsed_s > 0 else 0.0

    @property
    def worker_throughput(self) -> float:
        return self.event_count / self.worker_elapsed_s if self.worker_elapsed_s > 0 else 0.0

    def to_row(self) -> dict[str, Any]:
        return {
            "experiment": self.name,
            **{f"param_{k}": v for k, v in self.params.items()},
            "producers": self.producer_count,
            "workers": self.worker_count,
            "events": self.event_count,
            "producer_elapsed_s": round(self.producer_elapsed_s, 3),
            "worker_elapsed_s": round(self.worker_elapsed_s, 3),
            "producer_tps": round(self.producer_throughput, 1),
            "worker_tps": round(self.worker_throughput, 1),
            "lock_failures": self.lock_failures,
            "lock_retries": self.lock_retries,
            "notes": self.notes,
        }


def results_to_csv(results: list[ExperimentResult]) -> str:
    if not results:
        return ""
    buf = io.StringIO()
    fieldnames = list(results[0].to_row().keys())
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in results:
        writer.writerow(r.to_row())
    return buf.getvalue()


def results_to_markdown(results: list[ExperimentResult]) -> str:
    if not results:
        return ""
    rows = [r.to_row() for r in results]
    headers = list(rows[0].keys())
    lines = [" | ".join(headers), " | ".join(["---"] * len(headers))]
    for row in rows:
        lines.append(" | ".join(str(row.get(h, "")) for h in headers))
    return "\n".join(lines)


def print_summary_table(results: list[ExperimentResult]) -> None:
    print("\n" + "=" * 80)
    print("RESULTS SUMMARY")
    print("=" * 80)
    print(f"{'Experiment':<30} {'P/W':>5} {'Events':>8} {'Prod tps':>10} {'Work tps':>10} {'Locks':>8}")
    print("-" * 80)
    for r in results:
        pw = f"{r.producer_count}/{r.worker_count}"
        print(
            f"{r.name:<30} {pw:>5} {r.event_count:>8} "
            f"{r.producer_throughput:>10.1f} {r.worker_throughput:>10.1f} {r.lock_failures:>8}"
        )
    print("=" * 80)


def setup_db(db_path: str) -> None:
    app = knocker.open(db_path)
    try:
        app.add_endpoint(name="stripe", path="/webhooks/stripe")
    finally:
        app.close()


def count_handled(db_path: str, shared_app: knocker.Knocker | None = None) -> int:
    if shared_app is not None:
        rows = shared_app.db.query(
            "SELECT COUNT(*) AS c FROM knocker_events WHERE status='handled'"
        )
        return int(rows[0]["c"])
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM knocker_events WHERE status='handled'"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def count_received(db_path: str) -> int:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM knocker_events WHERE status='received'"
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def total_events(db_path: str) -> int:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = conn.execute("SELECT COUNT(*) FROM knocker_events").fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def producer_thread_target(
    db_path: str,
    count: int,
    offset: int,
    metrics: dict[str, Any],
    use_receive: bool = False,
) -> None:
    app = knocker.open(db_path)
    try:
        failures = 0
        retries = 0
        start = time.perf_counter()
        for idx in range(count):
            event_id = f"evt-{offset}-{idx}"
            delivery_id = f"delivery-{offset}-{idx}"
            try:
                if use_receive:
                    app.receive(
                        endpoint="stripe",
                        body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                        headers={},
                        provider_event_id=event_id,
                        provider_delivery_id=delivery_id,
                        event_type="checkout.session.completed",
                    )
                else:
                    app.ingest(
                        endpoint="stripe",
                        body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                        headers={},
                        provider_event_id=event_id,
                        provider_delivery_id=delivery_id,
                        event_type="checkout.session.completed",
                    )
            except Exception as exc:
                msg = str(exc).lower()
                if "database is locked" in msg or "busy" in msg:
                    failures += 1
                    retries += 1
                    try:
                        time.sleep(0.001)
                        if use_receive:
                            app.receive(
                                endpoint="stripe",
                                body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                                headers={},
                                provider_event_id=event_id,
                                provider_delivery_id=delivery_id,
                                event_type="checkout.session.completed",
                            )
                        else:
                            app.ingest(
                                endpoint="stripe",
                                body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                                headers={},
                                provider_event_id=event_id,
                                provider_delivery_id=delivery_id,
                                event_type="checkout.session.completed",
                            )
                    except Exception:
                        retries += 1
                else:
                    raise
        elapsed = time.perf_counter() - start
        metrics["elapsed"] = elapsed
        metrics["failures"] = failures
        metrics["retries"] = retries
        metrics["count"] = count
    finally:
        app.close()


def producer_thread_target_shared_app(
    app: knocker.Knocker,
    count: int,
    offset: int,
    metrics: dict[str, Any],
    use_receive: bool = False,
) -> None:
    failures = 0
    retries = 0
    start = time.perf_counter()
    for idx in range(count):
        event_id = f"evt-{offset}-{idx}"
        delivery_id = f"delivery-{offset}-{idx}"
        try:
            if use_receive:
                app.receive(
                    endpoint="stripe",
                    body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                    headers={},
                    provider_event_id=event_id,
                    provider_delivery_id=delivery_id,
                    event_type="checkout.session.completed",
                )
            else:
                app.ingest(
                    endpoint="stripe",
                    body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                    headers={},
                    provider_event_id=event_id,
                    provider_delivery_id=delivery_id,
                    event_type="checkout.session.completed",
                )
        except Exception as exc:
            msg = str(exc).lower()
            if "database is locked" in msg or "busy" in msg:
                failures += 1
                retries += 1
                try:
                    time.sleep(0.001)
                    if use_receive:
                        app.receive(
                            endpoint="stripe",
                            body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                            headers={},
                            provider_event_id=event_id,
                            provider_delivery_id=delivery_id,
                            event_type="checkout.session.completed",
                        )
                    else:
                        app.ingest(
                            endpoint="stripe",
                            body=f'{{"id":"{event_id}"}}'.encode("utf-8"),
                            headers={},
                            provider_event_id=event_id,
                            provider_delivery_id=delivery_id,
                            event_type="checkout.session.completed",
                        )
                except Exception:
                    retries += 1
            else:
                raise
    elapsed = time.perf_counter() - start
    metrics["elapsed"] = elapsed
    metrics["failures"] = failures
    metrics["retries"] = retries
    metrics["count"] = count


async def run_producers(
    db_path: str,
    total_events: int,
    producer_count: int,
    use_receive: bool = False,
) -> tuple[float, int, int]:
    per_producer = total_events // producer_count
    threads: list[threading.Thread] = []
    all_metrics: list[dict[str, Any]] = []
    for i in range(producer_count):
        metrics: dict[str, Any] = {}
        all_metrics.append(metrics)
        t = threading.Thread(
            target=producer_thread_target,
            args=(db_path, per_producer, i, metrics, use_receive),
        )
        threads.append(t)
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    failures = sum(m.get("failures", 0) for m in all_metrics)
    retries = sum(m.get("retries", 0) for m in all_metrics)
    return elapsed, failures, retries


async def run_producers_shared_handle(
    app: knocker.Knocker,
    total_events: int,
    producer_count: int,
    use_receive: bool = False,
) -> tuple[float, int, int]:
    per_producer = total_events // producer_count
    threads: list[threading.Thread] = []
    all_metrics: list[dict[str, Any]] = []
    for i in range(producer_count):
        metrics: dict[str, Any] = {}
        all_metrics.append(metrics)
        t = threading.Thread(
            target=producer_thread_target_shared_app,
            args=(app, per_producer, i, metrics, use_receive),
        )
        threads.append(t)
    start = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.perf_counter() - start
    failures = sum(m.get("failures", 0) for m in all_metrics)
    retries = sum(m.get("retries", 0) for m in all_metrics)
    return elapsed, failures, retries


async def run_workers_drain(
    app: knocker.Knocker,
    worker_count: int,
    stop_event: asyncio.Event,
    idle_poll_s: float = 0.005,
) -> None:
    tasks = [
        asyncio.create_task(
            app.run_worker(
                worker_id=f"worker-{i}",
                stop_event=stop_event,
                idle_poll_s=idle_poll_s,
            )
        )
        for i in range(worker_count)
    ]
    await asyncio.gather(*tasks)


async def drain_until(
    db_path: str,
    app: knocker.Knocker | None,
    expected_count: int,
    timeout_s: float = 30.0,
    poll_s: float = 0.05,
) -> float:
    start = time.perf_counter()
    while time.perf_counter() - start < timeout_s:
        handled = count_handled(db_path, app)
        if handled >= expected_count:
            return time.perf_counter() - start
        await asyncio.sleep(poll_s)
    elapsed = time.perf_counter() - start
    handled = count_handled(db_path, app)
    if handled < expected_count:
        raise RuntimeError(
            f"Timed out after {elapsed:.1f}s: only {handled}/{expected_count} handled"
        )
    return elapsed


class ExperimentHarness:
    def __init__(self, name: str):
        self.name = name
        self.results: list[ExperimentResult] = []
        self.tmpdir: str | None = None
        self.db_path: str | None = None

    def setup(self, prefix: str = "knocker-bench-") -> str:
        self.tmpdir = tempfile.mkdtemp(prefix=prefix)
        self.db_path = str(Path(self.tmpdir) / "test.db")
        setup_db(self.db_path)
        return self.db_path

    def cleanup(self) -> None:
        if self.tmpdir:
            import shutil
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def record(
        self,
        params: dict[str, Any],
        producer_count: int,
        worker_count: int,
        event_count: int,
        producer_elapsed_s: float,
        worker_elapsed_s: float,
        lock_failures: int = 0,
        lock_retries: int = 0,
        notes: str = "",
    ) -> ExperimentResult:
        r = ExperimentResult(
            name=self.name,
            params=params,
            producer_count=producer_count,
            worker_count=worker_count,
            event_count=event_count,
            producer_elapsed_s=producer_elapsed_s,
            worker_elapsed_s=worker_elapsed_s,
            lock_failures=lock_failures,
            lock_retries=lock_retries,
            notes=notes,
        )
        self.results.append(r)
        return r


def write_evidence(
    path: str,
    results: list[ExperimentResult],
    conclusions: list[str],
) -> None:
    with open(path, "w") as f:
        f.write("# Experiment Evidence\n\n")
        f.write("## Environment\n\n")
        for k, v in get_env_info().items():
            f.write(f"- {k}: {v}\n")
        f.write("\n## Results\n\n")
        f.write(results_to_markdown(results))
        f.write("\n\n## Conclusions\n\n")
        for c in conclusions:
            f.write(f"- {c}\n")
        f.write("\n")


def write_csv(path: str, results: list[ExperimentResult]) -> None:
    with open(path, "w") as f:
        f.write(results_to_csv(results))

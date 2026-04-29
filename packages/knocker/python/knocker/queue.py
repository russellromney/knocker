from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class _HonkerJob:
    id: int
    worker_id: str
    attempts: int
    claim_expires_at: int
    payload: dict[str, Any]


class _HonkerQueue:
    def __init__(self, db: Any, name: str, visibility_timeout_s: int, max_attempts: int):
        self.db = db
        self.name = name
        self.visibility_timeout_s = int(visibility_timeout_s)
        self.max_attempts = int(max_attempts)

    def claim_batch(self, worker_id: str, n: int) -> list[_HonkerJob]:
        with self.db.transaction() as tx:
            rows = tx.query(
                "SELECT honker_claim_batch(?, ?, ?, ?) AS rows_json",
                [self.name, worker_id, int(n), self.visibility_timeout_s],
            )
        data = json.loads(rows[0]["rows_json"])
        return [
            _HonkerJob(
                id=int(row["id"]),
                worker_id=row["worker_id"],
                attempts=int(row["attempts"]),
                claim_expires_at=int(row["claim_expires_at"]),
                payload=json.loads(row["payload"]),
            )
            for row in data
        ]

    def ack(self, job_id: int, worker_id: str, tx: Any | None = None) -> bool:
        if tx is not None:
            rows = tx.query("SELECT honker_ack(?, ?) AS r", [int(job_id), worker_id])
            return bool(rows[0]["r"])
        with self.db.transaction() as own_tx:
            rows = own_tx.query("SELECT honker_ack(?, ?) AS r", [int(job_id), worker_id])
        return bool(rows[0]["r"])

    def retry(self, job_id: int, worker_id: str, delay_s: int, error: str, tx: Any | None = None) -> bool:
        if tx is not None:
            rows = tx.query(
                "SELECT honker_retry(?, ?, ?, ?) AS r",
                [int(job_id), worker_id, int(delay_s), error],
            )
            return bool(rows[0]["r"])
        with self.db.transaction() as own_tx:
            rows = own_tx.query(
                "SELECT honker_retry(?, ?, ?, ?) AS r",
                [int(job_id), worker_id, int(delay_s), error],
            )
        return bool(rows[0]["r"])

    def fail(self, job_id: int, worker_id: str, error: str, tx: Any | None = None) -> bool:
        if tx is not None:
            rows = tx.query("SELECT honker_fail(?, ?, ?) AS r", [int(job_id), worker_id, error])
            return bool(rows[0]["r"])
        with self.db.transaction() as own_tx:
            rows = own_tx.query("SELECT honker_fail(?, ?, ?) AS r", [int(job_id), worker_id, error])
        return bool(rows[0]["r"])

    def claim(self, worker_id: str, idle_poll_s: float = 5.0) -> "_WorkerQueueIter":
        return _WorkerQueueIter(self, worker_id, idle_poll_s)


class _QueueTransitionError(RuntimeError):
    pass


class _WorkerQueueIter:
    def __init__(self, queue: _HonkerQueue, worker_id: str, idle_poll_s: float):
        self.queue = queue
        self.worker_id = worker_id
        self.idle_poll_s = float(idle_poll_s)
        self._wal = queue.db.wal_events()

    def __aiter__(self) -> "_WorkerQueueIter":
        return self

    async def __anext__(self) -> _HonkerJob:
        while True:
            jobs = self.queue.claim_batch(self.worker_id, 1)
            if jobs:
                return jobs[0]
            try:
                await asyncio.wait_for(self._wal.__anext__(), timeout=self.idle_poll_s)
            except asyncio.TimeoutError:
                continue


def _require_queue_transition(ok: bool, *, action: str, job_id: int, event_id: int) -> None:
    if not ok:
        raise _QueueTransitionError(
            f"honker {action} failed for job_id={job_id} event_id={event_id}; claim no longer valid"
        )

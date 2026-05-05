from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Optional

import honker

_HonkerJob = honker.Job


class _QueueTransitionError(RuntimeError):
    pass


class _WorkerQueueIter:
    def __init__(
        self,
        queue: "_HonkerQueue",
        worker_id: str,
        idle_poll_s: Optional[float],
        claim_batch_size: int,
    ):
        self.queue = queue
        self.worker_id = worker_id
        self.idle_poll_s = idle_poll_s
        self.claim_batch_size = max(1, int(claim_batch_size))
        self._updates = queue.db.update_events()
        self._buffer = deque()
        self._closed = False

    def __aiter__(self):
        return self

    def __del__(self):
        self._close_updates()

    def has_buffered_jobs(self) -> bool:
        return bool(self._buffer)

    def _close_updates(self):
        if self._closed:
            return
        self._closed = True
        updates = getattr(self, "_updates", None)
        if updates is not None:
            close = getattr(updates, "close", None)
            if callable(close):
                close()
        self._updates = None

    async def __anext__(self):
        try:
            while True:
                if self._buffer:
                    return self._buffer.popleft()
                jobs = self.queue.claim_batch(self.worker_id, self.claim_batch_size)
                if jobs:
                    self._buffer.extend(jobs[1:])
                    return jobs[0]
                timeout_s = self.idle_poll_s
                next_claim_at = self.queue._next_claim_at()
                if next_claim_at > 0:
                    until_deadline = max(0.0, next_claim_at - time.time())
                    timeout_s = (
                        until_deadline
                        if timeout_s is None
                        else min(timeout_s, until_deadline)
                    )
                try:
                    await asyncio.wait_for(
                        self._updates.__anext__(),
                        timeout=timeout_s,
                    )
                except asyncio.TimeoutError:
                    pass
                except StopAsyncIteration:
                    self._close_updates()
                    raise StopAsyncIteration
        except asyncio.CancelledError:
            self._close_updates()
            raise


class _HonkerQueue:
    def __init__(
        self,
        db: honker.Database,
        name: str,
        *,
        visibility_timeout_s: int = 300,
        max_attempts: int = 3,
    ):
        self.db = db
        self._inner = db.queue(
            name,
            visibility_timeout_s=visibility_timeout_s,
            max_attempts=max_attempts,
        )

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def visibility_timeout_s(self) -> int:
        return self._inner.visibility_timeout_s

    @property
    def max_attempts(self) -> int:
        return self._inner.max_attempts

    def enqueue(self, *args, **kwargs):
        return self._inner.enqueue(*args, **kwargs)

    def claim_batch(self, worker_id: str, n: int) -> list[_HonkerJob]:
        return self._inner.claim_batch(worker_id, n)

    def claim(
        self,
        worker_id: str,
        idle_poll_s: Optional[float] = 5.0,
        *,
        claim_batch_size: int = 1,
    ) -> _WorkerQueueIter:
        return _WorkerQueueIter(
            self,
            worker_id,
            idle_poll_s,
            claim_batch_size,
        )

    def ack(self, job_id: int, worker_id: str, tx=None) -> bool:
        if tx is not None:
            rows = tx.query("SELECT honker_ack(?, ?) AS r", [int(job_id), worker_id])
            return bool(rows[0]["r"])
        return self._inner.ack(job_id, worker_id)

    def retry(self, job_id: int, worker_id: str, delay_s: int, error: str, tx=None) -> bool:
        if tx is not None:
            rows = tx.query(
                "SELECT honker_retry(?, ?, ?, ?) AS r",
                [int(job_id), worker_id, int(delay_s), error],
            )
            return bool(rows[0]["r"])
        return self._inner.retry(job_id, worker_id, delay_s, error)

    def fail(self, job_id: int, worker_id: str, error: str, tx=None) -> bool:
        if tx is not None:
            rows = tx.query("SELECT honker_fail(?, ?, ?) AS r", [int(job_id), worker_id, error])
            return bool(rows[0]["r"])
        return self._inner.fail(job_id, worker_id, error)

    def _next_claim_at(self) -> int:
        return self._inner._next_claim_at()


def _require_queue_transition(ok: bool, *, action: str, job_id: int, event_id: int) -> None:
    if not ok:
        raise _QueueTransitionError(
            f"honker {action} failed for job_id={job_id} event_id={event_id}; claim no longer valid"
        )

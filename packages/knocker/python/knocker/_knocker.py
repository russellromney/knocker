from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from knocker._knocker_native import open as _core_open


@dataclass(frozen=True, slots=True)
class IngestResult:
    delivery_id: int
    event_id: Optional[int]
    duplicate: bool
    status_code: int


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    endpoint: str
    event_type: Optional[str]
    provider_event_id: Optional[str]
    provider_delivery_id: Optional[str]
    dedupe_key: Optional[str]
    status: str
    attempt_count: int
    headers: dict[str, Any]
    query: dict[str, Any]
    body: bytes
    received_at: int
    handled_at: Optional[int]
    last_error: Optional[str]


@dataclass(frozen=True, slots=True)
class Delivery:
    id: int
    event_id: Optional[int]
    endpoint: str
    event_type: Optional[str]
    provider_event_id: Optional[str]
    provider_delivery_id: Optional[str]
    dedupe_key: Optional[str]
    method: str
    headers: dict[str, Any]
    query: dict[str, Any]
    body: bytes
    received_at: int
    signature_valid: Optional[bool]
    signature_error: Optional[str]


@dataclass(frozen=True, slots=True)
class PruneEventsResult:
    events_pruned: int
    attempts_pruned: int
    deliveries_pruned: int
    live_jobs_pruned: int


@dataclass(frozen=True, slots=True)
class PruneDeliveriesResult:
    deliveries_pruned: int


Handler = Callable[[Event, Any], None]
KeyExtractor = Callable[["_IngressRequest"], Optional[str]]
_UNSET = object()


@dataclass(frozen=True, slots=True)
class _VerificationResult:
    valid: bool
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class _IngressRequest:
    method: str
    headers: dict[str, Any]
    query: dict[str, Any]
    body: bytes


@dataclass(frozen=True, slots=True)
class _EndpointConfig:
    verifier: Optional["_RequestVerifier"] = None
    delivery_key: Optional[KeyExtractor] = None
    event_key: Optional[KeyExtractor] = None
    event_type: Optional[KeyExtractor] = None


@dataclass(frozen=True, slots=True)
class _ProviderPreset:
    delivery_key: Optional[KeyExtractor] = None
    event_key: Optional[KeyExtractor] = None
    event_type: Optional[KeyExtractor] = None


class _RequestVerifier:
    def verify(self, body: bytes, headers: dict[str, Any]) -> _VerificationResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _GenericHmacVerifier(_RequestVerifier):
    header: str
    secrets: tuple[bytes, ...]
    prefix: Optional[str]

    def verify(self, body: bytes, headers: dict[str, Any]) -> _VerificationResult:
        header_value = _get_header(headers, self.header)
        if header_value is None:
            return _VerificationResult(False, f"missing signature header: {self.header}")
        for secret in self.secrets:
            digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
            expected = f"{self.prefix}{digest}" if self.prefix is not None else digest
            if hmac.compare_digest(header_value, expected):
                return _VerificationResult(True)
        return _VerificationResult(False, "signature mismatch")


@dataclass(frozen=True, slots=True)
class _StripeVerifier(_RequestVerifier):
    secrets: tuple[bytes, ...]
    tolerance_s: int

    def verify(self, body: bytes, headers: dict[str, Any]) -> _VerificationResult:
        header_value = _get_header(headers, "stripe-signature")
        if header_value is None:
            return _VerificationResult(False, "missing signature header: stripe-signature")
        try:
            timestamp, signatures = _parse_stripe_signature(header_value)
        except ValueError as exc:
            return _VerificationResult(False, str(exc))
        if abs(int(time.time()) - timestamp) > self.tolerance_s:
            return _VerificationResult(False, "stripe signature timestamp outside tolerance")
        signed_payload = f"{timestamp}.".encode("utf-8") + body
        for secret in self.secrets:
            expected = hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()
            for candidate in signatures:
                if hmac.compare_digest(candidate, expected):
                    return _VerificationResult(True)
        return _VerificationResult(False, "stripe signature mismatch")


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


class Knocker:
    def __init__(
        self,
        db_path: str,
        *,
        queue_name: str = "knocker.events",
        visibility_timeout_s: int = 60,
        max_attempts: int = 3,
        max_readers: int = 8,
    ):
        self.db_path = db_path
        self.db = _core_open(db_path, max_readers=max_readers)
        self.queue = _HonkerQueue(
            self.db,
            queue_name,
            visibility_timeout_s=visibility_timeout_s,
            max_attempts=max_attempts,
        )
        self.max_attempts = int(max_attempts)
        self._handlers: dict[tuple[str, Optional[str]], Handler] = {}
        self._endpoint_configs: dict[str, _EndpointConfig] = {}

    def add_endpoint(
        self,
        *,
        name: str,
        path: str,
        provider: Optional[str] = None,
        enabled: bool = True,
        verification: Any = _UNSET,
        delivery_key: Any = _UNSET,
        event_key: Any = _UNSET,
        secrets: Any = _UNSET,
    ) -> None:
        if verification is not _UNSET and secrets is not _UNSET:
            raise ValueError("pass either verification=... or secrets=..., not both")

        preset = _provider_preset(provider)
        if verification is _UNSET:
            if secrets is _UNSET:
                verifier = self._endpoint_configs.get(name, _EndpointConfig()).verifier
            elif secrets is None:
                verifier = None
            else:
                verifier = _build_provider_verifier(provider, secrets)
        else:
            verifier = _build_request_verifier(verification)

        config = _EndpointConfig(
            verifier=verifier,
            delivery_key=preset.delivery_key if delivery_key is _UNSET else _coerce_extractor(delivery_key, "delivery_key"),
            event_key=preset.event_key if event_key is _UNSET else _coerce_extractor(event_key, "event_key"),
            event_type=preset.event_type,
        )

        with self.db.transaction() as tx:
            tx.query(
                "SELECT knocker_endpoint_upsert(?, ?, ?, ?)",
                [name, path, provider, 1 if enabled else 0],
            )
        self._endpoint_configs[name] = config

    def endpoint(self, **kwargs: Any) -> None:
        self.add_endpoint(**kwargs)

    def add_handler(
        self,
        *,
        endpoint: str,
        handler: Handler,
        event_type: Optional[str] = None,
    ) -> None:
        self._handlers[(endpoint, event_type)] = handler

    def handle(self, *, endpoint: str, event_type: Optional[str] = None):
        def decorate(fn: Handler) -> Handler:
            self.add_handler(endpoint=endpoint, event_type=event_type, handler=fn)
            return fn

        return decorate

    def ingest(
        self,
        *,
        endpoint: str,
        body: bytes,
        headers: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, Any]] = None,
        method: str = "POST",
        event_type: Optional[str] = None,
        provider_event_id: Optional[str] = None,
        provider_delivery_id: Optional[str] = None,
        dedupe_key: Optional[str] = None,
        signature_valid: Optional[bool] = True,
        signature_error: Optional[str] = None,
    ) -> IngestResult:
        headers_json = json.dumps(headers or {}, sort_keys=True)
        query_json = json.dumps(query or {}, sort_keys=True)
        signature_valid_value = None if signature_valid is None else int(bool(signature_valid))
        with self.db.transaction() as tx:
            rows = tx.query(
                """
                SELECT knocker_ingest(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) AS result_json
                """,
                [
                    endpoint,
                    method,
                    headers_json,
                    body,
                    query_json,
                    signature_valid_value,
                    signature_error,
                    provider_event_id,
                    provider_delivery_id,
                    event_type,
                    dedupe_key,
                    self.queue.name,
                    self.max_attempts,
                ],
            )
        result = json.loads(rows[0]["result_json"])
        event_id = result["event_id"]
        return IngestResult(
            delivery_id=int(result["delivery_id"]),
            event_id=None if event_id is None else int(event_id),
            duplicate=bool(result["duplicate"]),
            status_code=int(result["status_code"]),
        )

    def receive(
        self,
        *,
        endpoint: str,
        body: bytes,
        headers: Optional[dict[str, Any]] = None,
        query: Optional[dict[str, Any]] = None,
        method: str = "POST",
        event_type: Optional[str] = None,
        provider_event_id: Optional[str] = None,
        provider_delivery_id: Optional[str] = None,
        dedupe_key: Optional[str] = None,
    ) -> IngestResult:
        headers = headers or {}
        query = query or {}
        config = self._endpoint_configs.get(endpoint, _EndpointConfig())
        request = _IngressRequest(method=method, headers=headers, query=query, body=body)
        verification = (
            _VerificationResult(True)
            if config.verifier is None
            else config.verifier.verify(body, headers)
        )
        resolved_provider_event_id = provider_event_id
        if resolved_provider_event_id is None:
            resolved_provider_event_id = _extract_optional(config.event_key, request)
        resolved_provider_delivery_id = provider_delivery_id
        if resolved_provider_delivery_id is None:
            resolved_provider_delivery_id = _extract_optional(config.delivery_key, request)
        resolved_event_type = event_type
        if resolved_event_type is None:
            resolved_event_type = _extract_optional(config.event_type, request)
        return self.ingest(
            endpoint=endpoint,
            body=body,
            headers=headers,
            query=query,
            method=method,
            event_type=resolved_event_type,
            provider_event_id=resolved_provider_event_id,
            provider_delivery_id=resolved_provider_delivery_id,
            dedupe_key=dedupe_key,
            signature_valid=verification.valid,
            signature_error=verification.error,
        )

    def get_event(self, event_id: int) -> Event:
        event = _get_event_or_none(self.db, event_id)
        if event is None:
            raise KeyError(f"unknown event id: {event_id}")
        return event

    def list_events(
        self,
        *,
        status: Optional[str] = None,
        endpoint: Optional[str] = None,
        event_type: Optional[str] = None,
        since: Optional[int] = None,
        limit: int = 100,
    ) -> list[Event]:
        sql = """
            SELECT
                e.id,
                ep.name AS endpoint,
                e.event_type,
                e.provider_event_id,
                e.provider_delivery_id,
                e.dedupe_key,
                e.status,
                e.attempt_count,
                e.headers_json,
                e.query_json,
                e.body_blob,
                e.received_at,
                e.handled_at,
                e.last_error
            FROM knocker_events e
            JOIN knocker_endpoints ep ON ep.id = e.endpoint_id
        """
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("e.status=?")
            params.append(status)
        if endpoint is not None:
            clauses.append("ep.name=?")
            params.append(endpoint)
        if event_type is not None:
            clauses.append("e.event_type=?")
            params.append(event_type)
        if since is not None:
            clauses.append("e.received_at>=?")
            params.append(_coerce_since(since))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY e.received_at DESC, e.id DESC LIMIT ?"
        params.append(_coerce_limit(limit))
        return [_event_from_row(row) for row in self.db.query(sql, params)]

    def get_delivery(self, delivery_id: int) -> Delivery:
        rows = self.db.query(
            """
            SELECT
                d.id,
                d.event_id,
                ep.name AS endpoint,
                d.event_type,
                d.provider_event_id,
                d.provider_delivery_id,
                d.dedupe_key,
                d.method,
                d.headers_json,
                d.query_json,
                d.body_blob,
                d.received_at,
                d.signature_valid,
                d.signature_error
            FROM knocker_deliveries d
            JOIN knocker_endpoints ep ON ep.id = d.endpoint_id
            WHERE d.id=?
            """,
            [int(delivery_id)],
        )
        if not rows:
            raise KeyError(f"unknown delivery id: {delivery_id}")
        return _delivery_from_row(rows[0])

    def list_deliveries(
        self,
        *,
        event_id: Optional[int] = None,
        endpoint: Optional[str] = None,
        signature_valid: Optional[bool] = None,
        orphaned: Optional[bool] = None,
        since: Optional[int] = None,
        limit: int = 100,
    ) -> list[Delivery]:
        sql = """
            SELECT
                d.id,
                d.event_id,
                ep.name AS endpoint,
                d.event_type,
                d.provider_event_id,
                d.provider_delivery_id,
                d.dedupe_key,
                d.method,
                d.headers_json,
                d.query_json,
                d.body_blob,
                d.received_at,
                d.signature_valid,
                d.signature_error
            FROM knocker_deliveries d
            JOIN knocker_endpoints ep ON ep.id = d.endpoint_id
        """
        clauses: list[str] = []
        params: list[Any] = []
        if event_id is not None:
            clauses.append("d.event_id=?")
            params.append(int(event_id))
        if endpoint is not None:
            clauses.append("ep.name=?")
            params.append(endpoint)
        if signature_valid is not None:
            if _coerce_bool_filter(signature_valid, "signature_valid"):
                clauses.append("d.signature_valid=1")
            else:
                clauses.append("(d.signature_valid=0 OR d.signature_valid IS NULL)")
        if orphaned is not None:
            orphaned_value = _coerce_bool_filter(orphaned, "orphaned")
            clauses.append("d.event_id IS NULL" if orphaned_value else "d.event_id IS NOT NULL")
        if since is not None:
            clauses.append("d.received_at>=?")
            params.append(_coerce_since(since))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY d.received_at DESC, d.id DESC LIMIT ?"
        params.append(_coerce_limit(limit))
        return [_delivery_from_row(row) for row in self.db.query(sql, params)]

    def ignore(self, event_id: int) -> None:
        with self.db.transaction() as tx:
            rows = tx.query("SELECT status FROM knocker_events WHERE id=?", [int(event_id)])
            if not rows:
                raise KeyError(f"unknown event id: {event_id}")
            status = str(rows[0]["status"])
            if status == "ignored":
                return
            if status not in {"received", "failed", "dead"}:
                raise ValueError(f"event {event_id} with status {status} cannot be ignored")
            tx.query("SELECT knocker_mark_ignored(?, ?)", [int(event_id), 0])

    def replay(self, event_id: int) -> None:
        with self.db.transaction() as tx:
            status = _event_status_or_raise(tx, int(event_id))
            if status not in {"handled", "failed", "dead", "ignored"}:
                raise ValueError(f"event {event_id} with status {status} cannot be replayed")
            tx.query(
                "SELECT knocker_replay(?, ?, ?)",
                [int(event_id), self.queue.name, self.max_attempts],
            )

    def requeue(self, event_id: int) -> None:
        with self.db.transaction() as tx:
            status = _event_status_or_raise(tx, int(event_id))
            if status not in {"failed", "dead", "ignored"}:
                raise ValueError(f"event {event_id} with status {status} cannot be requeued")
            tx.query(
                "SELECT knocker_requeue(?, ?, ?)",
                [int(event_id), self.queue.name, self.max_attempts],
            )

    def prune_events(
        self,
        *,
        statuses: list[str] | tuple[str, ...],
        older_than: int,
        limit: int,
    ) -> PruneEventsResult:
        resolved_statuses = _coerce_prune_statuses(statuses)
        older_than_value = _coerce_older_than(older_than)
        limit_value = _coerce_limit(limit)
        with self.db.transaction() as tx:
            event_ids = self._prune_event_candidate_ids(
                tx,
                statuses=resolved_statuses,
                older_than=older_than_value,
                limit=limit_value,
            )
            if not event_ids:
                return PruneEventsResult(0, 0, 0, 0)
            attempts_pruned = self._count_event_attempts(tx, event_ids)
            deliveries_pruned = self._count_event_deliveries(tx, event_ids)
            live_job_ids = self._stale_live_job_ids(tx, event_ids)
            self._delete_live_jobs_by_id(tx, live_job_ids)
            self._delete_deliveries_for_event_ids(tx, event_ids)
            self._delete_events_by_id(tx, event_ids)
        return PruneEventsResult(
            events_pruned=len(event_ids),
            attempts_pruned=attempts_pruned,
            deliveries_pruned=deliveries_pruned,
            live_jobs_pruned=len(live_job_ids),
        )

    def prune_orphan_deliveries(
        self,
        *,
        older_than: int,
        limit: int,
    ) -> PruneDeliveriesResult:
        older_than_value = _coerce_older_than(older_than)
        limit_value = _coerce_limit(limit)
        with self.db.transaction() as tx:
            delivery_ids = self._prune_orphan_delivery_candidate_ids(
                tx,
                older_than=older_than_value,
                limit=limit_value,
            )
            if not delivery_ids:
                return PruneDeliveriesResult(0)
            self._delete_deliveries_by_id(tx, delivery_ids)
        return PruneDeliveriesResult(deliveries_pruned=len(delivery_ids))

    def _prune_event_candidate_ids(
        self,
        tx: Any,
        *,
        statuses: tuple[str, ...],
        older_than: int,
        limit: int,
    ) -> list[int]:
        placeholders = _sql_placeholders(len(statuses))
        rows = tx.query(
            f"""
            SELECT id
            FROM knocker_events
            WHERE status IN ({placeholders})
              AND received_at < ?
            ORDER BY received_at ASC, id ASC
            LIMIT ?
            """,
            [*statuses, older_than, limit],
        )
        return [int(row["id"]) for row in rows]

    def _count_event_attempts(self, tx: Any, event_ids: list[int]) -> int:
        placeholders = _sql_placeholders(len(event_ids))
        rows = tx.query(
            f"""
            SELECT COUNT(*) AS c
            FROM knocker_attempts
            WHERE event_id IN ({placeholders})
            """,
            event_ids,
        )
        return int(rows[0]["c"])

    def _count_event_deliveries(self, tx: Any, event_ids: list[int]) -> int:
        placeholders = _sql_placeholders(len(event_ids))
        rows = tx.query(
            f"""
            SELECT COUNT(*) AS c
            FROM knocker_deliveries
            WHERE event_id IN ({placeholders})
            """,
            event_ids,
        )
        return int(rows[0]["c"])

    def _stale_live_job_ids(self, tx: Any, event_ids: list[int]) -> list[int]:
        candidate_event_ids = set(event_ids)
        rows = tx.query(
            """
            SELECT id, payload
            FROM _honker_live
            WHERE queue=?
            """,
            [self.queue.name],
        )
        job_ids: list[int] = []
        for row in rows:
            payload_event_id = _event_id_from_payload_json(row["payload"])
            if payload_event_id in candidate_event_ids:
                job_ids.append(int(row["id"]))
        return job_ids

    def _delete_live_jobs_by_id(self, tx: Any, job_ids: list[int]) -> None:
        if not job_ids:
            return
        placeholders = _sql_placeholders(len(job_ids))
        tx.query(f"DELETE FROM _honker_live WHERE id IN ({placeholders})", job_ids)

    def _delete_deliveries_for_event_ids(self, tx: Any, event_ids: list[int]) -> None:
        if not event_ids:
            return
        placeholders = _sql_placeholders(len(event_ids))
        tx.query(f"DELETE FROM knocker_deliveries WHERE event_id IN ({placeholders})", event_ids)

    def _delete_events_by_id(self, tx: Any, event_ids: list[int]) -> None:
        if not event_ids:
            return
        placeholders = _sql_placeholders(len(event_ids))
        tx.query(f"DELETE FROM knocker_events WHERE id IN ({placeholders})", event_ids)

    def _prune_orphan_delivery_candidate_ids(
        self,
        tx: Any,
        *,
        older_than: int,
        limit: int,
    ) -> list[int]:
        rows = tx.query(
            """
            SELECT id
            FROM knocker_deliveries
            WHERE event_id IS NULL
              AND received_at < ?
            ORDER BY received_at ASC, id ASC
            LIMIT ?
            """,
            [older_than, limit],
        )
        return [int(row["id"]) for row in rows]

    def _delete_deliveries_by_id(self, tx: Any, delivery_ids: list[int]) -> None:
        if not delivery_ids:
            return
        placeholders = _sql_placeholders(len(delivery_ids))
        tx.query(f"DELETE FROM knocker_deliveries WHERE id IN ({placeholders})", delivery_ids)

    async def run_worker(
        self,
        *,
        worker_id: Optional[str] = None,
        stop_event: Optional[asyncio.Event] = None,
        idle_poll_s: float = 0.1,
    ) -> None:
        worker_id = worker_id or f"knocker-{uuid.uuid4().hex[:8]}"
        claims = self.queue.claim(worker_id, idle_poll_s=idle_poll_s)
        while True:
            if stop_event is not None and stop_event.is_set():
                return
            try:
                job = await asyncio.wait_for(claims.__anext__(), timeout=idle_poll_s + 0.05)
            except asyncio.TimeoutError:
                continue
            await self._dispatch_job(job)

    async def _dispatch_job(self, job: _HonkerJob) -> None:
        start = time.perf_counter()
        event_id = int(job.payload["event_id"])
        try:
            with self.db.transaction() as tx:
                event = _get_event_or_none(tx, event_id)
                if event is None:
                    # Best-effort ack: a pruned event means this claim is stale retention
                    # residue, so we exit quietly even if the live row is already gone.
                    self.queue.ack(job.id, job.worker_id, tx=tx)
                    return
                if event.status == "ignored":
                    # Ignore is an operator override, so a lingering claim should be drained
                    # quietly rather than crashing the worker if the claim already expired.
                    self.queue.ack(job.id, job.worker_id, tx=tx)
                    return
                handler = self._resolve_handler(event)
                if handler is None:
                    duration_ms = _duration_ms(start)
                    tx.query(
                        "SELECT knocker_mark_failed(?, ?, ?, ?, ?)",
                        [
                            event_id,
                            int(job.attempts),
                            (
                                f"no handler registered for endpoint={event.endpoint!r} "
                                f"event_type={event.event_type!r}"
                            ),
                            1,
                            duration_ms,
                        ],
                    )
                    _require_queue_transition(
                        self.queue.fail(
                            job.id,
                            job.worker_id,
                            (
                                f"no handler registered for endpoint={event.endpoint!r} "
                                f"event_type={event.event_type!r}"
                            ),
                            tx=tx,
                        ),
                        action="fail",
                        job_id=job.id,
                        event_id=event_id,
                    )
                    return
                tx.query("SELECT knocker_mark_processing(?, ?)", [event_id, job.attempts])
                handler(event, tx)
                duration_ms = _duration_ms(start)
                tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, duration_ms])
                _require_queue_transition(
                    self.queue.ack(job.id, job.worker_id, tx=tx),
                    action="ack",
                    job_id=job.id,
                    event_id=event_id,
                )
        except _QueueTransitionError:
            raise
        except Exception as exc:
            await self._fail_job(job, event_id, exc, _duration_ms(start))

    async def _fail_job(
        self,
        job: _HonkerJob,
        event_id: int,
        exc: Exception,
        duration_ms: int,
        terminal: Optional[bool] = None,
    ) -> None:
        if terminal is None:
            terminal = int(job.attempts) >= self.max_attempts
        with self.db.transaction() as tx:
            tx.query(
                "SELECT knocker_mark_failed(?, ?, ?, ?, ?)",
                [event_id, int(job.attempts), str(exc), 1 if terminal else 0, duration_ms],
            )
            if terminal:
                _require_queue_transition(
                    self.queue.fail(job.id, job.worker_id, str(exc), tx=tx),
                    action="fail",
                    job_id=job.id,
                    event_id=event_id,
                )
            else:
                _require_queue_transition(
                    self.queue.retry(job.id, job.worker_id, 0, str(exc), tx=tx),
                    action="retry",
                    job_id=job.id,
                    event_id=event_id,
                )

    def _resolve_handler(self, event: Event) -> Optional[Handler]:
        return self._handlers.get((event.endpoint, event.event_type)) or self._handlers.get(
            (event.endpoint, None)
        )


def _event_from_row(row: dict[str, Any]) -> Event:
    return Event(
        id=int(row["id"]),
        endpoint=row["endpoint"],
        event_type=row["event_type"],
        provider_event_id=row["provider_event_id"],
        provider_delivery_id=row["provider_delivery_id"],
        dedupe_key=row["dedupe_key"],
        status=row["status"],
        attempt_count=int(row["attempt_count"]),
        headers=json.loads(row["headers_json"]),
        query=json.loads(row["query_json"]),
        body=bytes(row["body_blob"]),
        received_at=int(row["received_at"]),
        handled_at=row["handled_at"],
        last_error=row["last_error"],
    )


def _get_event_or_none(queryable: Any, event_id: int) -> Optional[Event]:
    rows = queryable.query(
        """
        SELECT
            e.id,
            ep.name AS endpoint,
            e.event_type,
            e.provider_event_id,
            e.provider_delivery_id,
            e.dedupe_key,
            e.status,
            e.attempt_count,
            e.headers_json,
            e.query_json,
            e.body_blob,
            e.received_at,
            e.handled_at,
            e.last_error
        FROM knocker_events e
        JOIN knocker_endpoints ep ON ep.id = e.endpoint_id
        WHERE e.id=?
        """,
        [int(event_id)],
    )
    if not rows:
        return None
    return _event_from_row(rows[0])


def _event_status_or_raise(queryable: Any, event_id: int) -> str:
    rows = queryable.query("SELECT status FROM knocker_events WHERE id=?", [int(event_id)])
    if not rows:
        raise KeyError(f"unknown event id: {event_id}")
    return str(rows[0]["status"])


def _delivery_from_row(row: dict[str, Any]) -> Delivery:
    signature_valid = row["signature_valid"]
    return Delivery(
        id=int(row["id"]),
        event_id=None if row["event_id"] is None else int(row["event_id"]),
        endpoint=row["endpoint"],
        event_type=row["event_type"],
        provider_event_id=row["provider_event_id"],
        provider_delivery_id=row["provider_delivery_id"],
        dedupe_key=row["dedupe_key"],
        method=row["method"],
        headers=json.loads(row["headers_json"]),
        query=json.loads(row["query_json"]),
        body=bytes(row["body_blob"]),
        received_at=int(row["received_at"]),
        signature_valid=None if signature_valid is None else bool(signature_valid),
        signature_error=row["signature_error"],
    )


def _coerce_limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer")
    limit = value
    if not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    return limit


def _coerce_older_than(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("older_than must be an integer timestamp")
    return value


def _coerce_since(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("since must be an integer timestamp")
    return value


def _coerce_bool_filter(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be a bool")
    return value


def _coerce_prune_statuses(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        raise TypeError("statuses must be a non-empty list or tuple of strings")
    if not isinstance(value, (list, tuple)):
        raise TypeError("statuses must be a non-empty list or tuple of strings")
    if not value:
        raise ValueError("statuses must be a non-empty list or tuple")
    allowed = {"handled", "ignored"}
    ordered: list[str] = []
    seen: set[str] = set()
    for status in value:
        if not isinstance(status, str):
            raise TypeError("statuses must contain only strings")
        if status not in allowed:
            raise ValueError("statuses must contain only 'handled' or 'ignored'")
        if status not in seen:
            ordered.append(status)
            seen.add(status)
    return tuple(ordered)


def _sql_placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _event_id_from_payload_json(payload: Any) -> Optional[int]:
    if isinstance(payload, bytes):
        try:
            payload_text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(payload, str):
        payload_text = payload
    else:
        return None
    try:
        value = json.loads(payload_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        # Safe by default: malformed payloads are left alone rather than
        # accidentally matched and pruned.
        return None
    if not isinstance(value, dict) or "event_id" not in value:
        return None
    event_id = value["event_id"]
    if isinstance(event_id, bool):
        return None
    try:
        return int(event_id)
    except (TypeError, ValueError):
        return None


def _duration_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _build_request_verifier(verification: Any) -> Optional[_RequestVerifier]:
    if verification is None:
        return None
    if not isinstance(verification, dict):
        raise TypeError("verification must be a dict or None")
    kind = str(verification.get("kind", "")).lower()
    secrets = _coerce_secrets_from_verification(verification)
    if kind in {"hmac-sha256", "generic-hmac-sha256", "hmac"}:
        header = verification.get("header")
        if not isinstance(header, str) or not header:
            raise ValueError("generic hmac verification requires a non-empty 'header'")
        prefix_value = verification.get("prefix", "sha256=")
        if prefix_value is not None and not isinstance(prefix_value, str):
            raise TypeError("verification 'prefix' must be a string or None")
        return _GenericHmacVerifier(header=header, secrets=secrets, prefix=prefix_value)
    if kind == "stripe":
        tolerance_value = verification.get("tolerance_s", 300)
        if isinstance(tolerance_value, bool) or not isinstance(tolerance_value, int):
            raise TypeError("stripe verification 'tolerance_s' must be an integer")
        if tolerance_value < 0:
            raise ValueError("stripe verification 'tolerance_s' must be non-negative")
        tolerance_s = tolerance_value
        return _StripeVerifier(secrets=secrets, tolerance_s=tolerance_s)
    raise ValueError(f"unsupported verification kind: {kind!r}")


def _build_provider_verifier(provider: Optional[str], secrets: Any) -> _RequestVerifier:
    secrets_tuple = _coerce_secret_values(secrets)
    provider_name = (provider or "").lower()
    if provider_name == "stripe":
        # Stripe's preset uses the same default timestamp window as explicit
        # Stripe verification config.
        return _StripeVerifier(secrets=secrets_tuple, tolerance_s=300)
    raise ValueError(
        f"provider preset verification is not supported for {provider!r}; use verification=..."
    )


def _provider_preset(provider: Optional[str]) -> _ProviderPreset:
    provider_name = (provider or "").lower()
    if provider_name == "stripe":
        return _ProviderPreset(
            event_key=lambda request: _json_string_field(request.body, "id"),
            event_type=lambda request: _json_string_field(request.body, "type"),
        )
    if provider_name == "github":
        return _ProviderPreset(
            delivery_key=lambda request: _get_header(request.headers, "x-github-delivery"),
            event_type=lambda request: _get_header(request.headers, "x-github-event"),
        )
    return _ProviderPreset()


def _coerce_extractor(value: Any, name: str) -> Optional[KeyExtractor]:
    if value is None:
        return None
    if not callable(value):
        raise TypeError(f"{name} must be callable or None")
    return value


def _extract_optional(extractor: Optional[KeyExtractor], request: _IngressRequest) -> Optional[str]:
    if extractor is None:
        return None
    value = extractor(request)
    if value is None:
        return None
    return str(value)


def _coerce_secrets_from_verification(verification: dict[str, Any]) -> tuple[bytes, ...]:
    if "secrets" in verification:
        raw = verification["secrets"]
        if not isinstance(raw, (list, tuple)):
            raise TypeError("verification 'secrets' must be a list or tuple")
        values = raw
    elif "secret" in verification:
        values = [verification["secret"]]
    else:
        raise ValueError("verification requires 'secret' or 'secrets'")
    return _coerce_secret_values(values)


def _coerce_secret_values(values: Any) -> tuple[bytes, ...]:
    if isinstance(values, (bytes, str)):
        iterable = [values]
    elif isinstance(values, (list, tuple)):
        iterable = list(values)
    else:
        raise TypeError("secrets must be a str, bytes, list, or tuple")
    secrets = tuple(_coerce_secret(value) for value in iterable)
    if not secrets:
        raise ValueError("verification requires at least one secret")
    return secrets


def _coerce_secret(value: Any) -> bytes:
    if isinstance(value, bytes):
        if not value:
            raise ValueError("verification secrets must be non-empty")
        return value
    if isinstance(value, str):
        if not value:
            raise ValueError("verification secrets must be non-empty")
        return value.encode("utf-8")
    raise TypeError("verification secrets must be str or bytes")


def _get_header(headers: dict[str, Any], name: str) -> Optional[str]:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _parse_stripe_signature(header_value: str) -> tuple[int, list[str]]:
    timestamp: Optional[int] = None
    signatures: list[str] = []
    for part in header_value.split(","):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "t":
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None:
        raise ValueError("stripe signature missing timestamp")
    if not signatures:
        raise ValueError("stripe signature missing v1 digest")
    return timestamp, signatures


def _json_string_field(body: bytes, field: str) -> Optional[str]:
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    result = value.get(field)
    if result is None:
        return None
    return str(result)


def _require_queue_transition(ok: bool, *, action: str, job_id: int, event_id: int) -> None:
    if not ok:
        raise _QueueTransitionError(
            f"honker {action} failed for job_id={job_id} event_id={event_id}; claim no longer valid"
        )


def open(path: str, **kwargs: Any) -> Knocker:
    return Knocker(path, **kwargs)

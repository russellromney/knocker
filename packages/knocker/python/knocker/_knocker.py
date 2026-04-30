from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Optional

from knocker._knocker_native import open as _core_open
from knocker.coercion import (
    _coerce_bool_filter,
    _coerce_limit,
    _coerce_older_than,
    _coerce_prune_statuses,
    _coerce_since,
    _duration_ms,
    _event_id_from_payload_json,
    _optional_payload_int,
    _required_payload_int,
    _sql_placeholders,
)
from knocker.models import (
    Delivery,
    ErrorHandler,
    Event,
    Handler,
    IngestResult,
    PruneDeliveriesResult,
    PruneEventsResult,
    WorkerState,
)
from knocker.providers import (
    Provider,
    ProviderRequest,
    ProviderResult,
    _builtin_provider_names,
    _builtin_providers,
)
from knocker.queue import (
    _HonkerJob,
    _HonkerQueue,
    _QueueTransitionError,
    _require_queue_transition,
)
from knocker.verifiers import (
    _EndpointConfig,
    _build_endpoint_config,
    _validate_provider_for_registration,
    _verify_and_extract,
)


_UNSET = object()


class Knocker:
    """Embeddable webhook inbox backed by SQLite and Honker.

    Handlers are synchronous callables that receive ``(event, tx)``. Writes
    performed through ``tx`` commit atomically with Knocker's event transition
    and queue acknowledgement, so handlers should keep that work short and
    DB-local.
    """

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
        self._worker_states: dict[str, WorkerState] = {}
        self._providers: dict[str, Provider] = {}
        for builtin in _builtin_providers():
            self._providers[builtin.name] = builtin
        self._builtin_provider_names = _builtin_provider_names()

    def register_provider(self, provider: Provider) -> None:
        """Register an app-local or community provider on this Knocker instance.

        Built-in provider names (``stripe``, ``github``) cannot be overridden;
        register a hotfix under a distinct name and point endpoints at it.
        Duplicate registrations fail rather than silently replacing.
        """

        _validate_provider_for_registration(
            provider, self._providers, self._builtin_provider_names
        )
        self._providers[provider.name] = provider

    def provider_versions(self) -> dict[str, str]:
        """Return a fresh ``{name: version}`` map of registered providers.

        Versions are provider implementation SemVer strings, not upstream
        provider API versions. Useful for inspection, debugging, and release
        notes; not part of any compatibility contract.
        """

        return {name: provider.version for name, provider in self._providers.items()}

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
        provider_options: Any = _UNSET,
    ) -> None:
        """Register or update an endpoint and its verification/extractor config.

        ``provider="name"`` resolves through the registered provider registry
        and fails immediately with ``ValueError`` for unknown names. Built-in
        provider endpoints that require secrets must pass a non-empty
        ``secrets=...`` value at registration time. Provider-specific tuning
        such as Stripe's ``tolerance_s`` goes in ``provider_options={...}``;
        unknown option keys are rejected by the provider with ``ValueError``.

        ``verification={...}`` remains the legacy explicit-config path for
        generic HMAC and pre-registry Stripe usage. Passing both
        ``verification=...`` and ``secrets=...`` is rejected.
        """

        config = _build_endpoint_config(
            providers=self._providers,
            previous_config=self._endpoint_configs.get(name),
            provider=provider,
            verification=verification,
            secrets=secrets,
            provider_options=provider_options,
            delivery_key=delivery_key,
            event_key=event_key,
            unset=_UNSET,
        )

        with self.db.transaction() as tx:
            tx.query(
                "SELECT knocker_endpoint_upsert(?, ?, ?, ?)",
                [name, path, provider, 1 if enabled else 0],
            )
        self._endpoint_configs[name] = config

    def add_handler(
        self,
        *,
        endpoint: str,
        handler: Handler,
        event_type: Optional[str] = None,
    ) -> None:
        """Register a synchronous ``handler(event, tx)`` for an endpoint."""

        self._handlers[(endpoint, event_type)] = handler

    def handle(self, *, endpoint: str, event_type: Optional[str] = None):
        """Decorate a synchronous ``handler(event, tx)`` for an endpoint."""

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
        """Store a trusted low-level delivery, bypassing binding-owned verification."""

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
        """Verify, extract metadata, store the delivery, and enqueue new events.

        Explicit ``provider_event_id``, ``provider_delivery_id``, and
        ``event_type`` arguments override provider-extracted metadata.
        Invalid receipts still surface extracted metadata on the orphan
        delivery row when the provider was able to read it before signature
        failure.
        """

        headers = dict(headers or {})
        query = dict(query or {})
        config = self._endpoint_configs.get(endpoint, _EndpointConfig())
        request = ProviderRequest(method=method, headers=headers, query=query, body=body)
        result = _verify_and_extract(config, request)
        return self.ingest(
            endpoint=endpoint,
            body=body,
            headers=headers,
            query=query,
            method=method,
            event_type=event_type if event_type is not None else result.event_type,
            provider_event_id=(
                provider_event_id
                if provider_event_id is not None
                else result.provider_event_id
            ),
            provider_delivery_id=(
                provider_delivery_id
                if provider_delivery_id is not None
                else result.provider_delivery_id
            ),
            dedupe_key=dedupe_key,
            signature_valid=result.valid,
            signature_error=result.signature_error,
        )

    def get_event(self, event_id: int) -> Event:
        """Return one deduped event row or raise ``KeyError``."""

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
        """List events newest-first using AND-composed filters."""

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
        """Return one append-only delivery row or raise ``KeyError``."""

        delivery = _get_delivery_or_none(self.db, delivery_id)
        if delivery is None:
            raise KeyError(f"unknown delivery id: {delivery_id}")
        return delivery

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
        """List delivery audit rows newest-first using AND-composed filters."""

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
        """Move a received, failed, or dead event to ``ignored`` explicitly."""

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
        """Replay a handled, failed, dead, or ignored event using its canonical payload.

        Resets ``attempt_count`` to ``0``; the dead-letter clock starts over.
        """

        with self.db.transaction() as tx:
            status = _event_status_or_raise(tx, int(event_id))
            if status not in {"handled", "failed", "dead", "ignored"}:
                raise ValueError(f"event {event_id} with status {status} cannot be replayed")
            tx.query(
                "SELECT knocker_replay(?, ?, ?)",
                [int(event_id), self.queue.name, self.max_attempts],
            )

    def requeue(self, event_id: int) -> None:
        """Requeue a failed, dead, or ignored event using its canonical payload.

        Resets ``attempt_count`` to ``0``; the dead-letter clock starts over.
        """

        with self.db.transaction() as tx:
            status = _event_status_or_raise(tx, int(event_id))
            if status not in {"failed", "dead", "ignored"}:
                raise ValueError(f"event {event_id} with status {status} cannot be requeued")
            tx.query(
                "SELECT knocker_requeue(?, ?, ?)",
                [int(event_id), self.queue.name, self.max_attempts],
            )

    def replay_delivery(self, delivery_id: int) -> None:
        """Replay one stored delivery body through its linked event's handler.

        This is an explicit operator action. It rejects unknown or orphan
        deliveries and never mutates the canonical event payload.

        The handler is resolved using the selected delivery's ``event_type``
        (and endpoint), not the canonical event's. If the delivery's event_type
        does not match a registered handler, the dispatch dead-letters the
        replay attempt.

        Resets ``attempt_count`` to ``0`` on the linked event; the dead-letter
        clock starts over.
        """

        with self.db.transaction() as tx:
            delivery = _get_delivery_or_none(tx, int(delivery_id))
            if delivery is None:
                raise KeyError(f"unknown delivery id: {delivery_id}")
            if delivery.event_id is None:
                raise ValueError(f"delivery {delivery_id} is not linked to an event")
            status = _event_status_or_raise(tx, delivery.event_id)
            if status not in {"handled", "failed", "dead", "ignored"}:
                raise ValueError(
                    f"event {delivery.event_id} with status {status} cannot replay a delivery"
                )
            live_job_ids = self._stale_live_job_ids(tx, [delivery.event_id])
            self._delete_live_jobs_by_id(tx, live_job_ids)
            tx.query(
                """
                UPDATE knocker_events
                SET status='received',
                    attempt_count=0,
                    handled_at=NULL,
                    last_error=NULL
                WHERE id=?
                """,
                [delivery.event_id],
            )
            tx.query(
                "SELECT honker_enqueue(?, ?, ?, ?, ?, ?, ?) AS job_id",
                [
                    self.queue.name,
                    json.dumps(
                        {"event_id": delivery.event_id, "delivery_id": delivery.id},
                        sort_keys=True,
                    ),
                    None,
                    None,
                    0,
                    self.max_attempts,
                    None,
                ],
            )

    def prune_events(
        self,
        *,
        statuses: list[str] | tuple[str, ...],
        older_than: int,
        limit: int,
    ) -> PruneEventsResult:
        """Prune old terminal events and their linked attempts, deliveries, and live jobs."""

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
        """Prune old orphan delivery rows using a strict ``received_at < older_than`` cutoff."""

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
        on_error: Optional[ErrorHandler] = None,
    ) -> None:
        """Claim and dispatch jobs until stopped.

        Handler exceptions use Knocker's retry/dead-letter path. Worker-loop
        exceptions outside normal handler handling update local worker state,
        call ``on_error`` when provided, and are re-raised for app-owned
        supervision.

        ``on_error`` runs *before* the re-raise, so an exception inside the
        callback shadows the original worker-loop exception. It may be sync
        or async; coroutines are awaited.
        """

        worker_id = worker_id or f"knocker-{uuid.uuid4().hex[:8]}"
        self._set_worker_state(worker_id, running=True, current_event_id=None)
        claims = self.queue.claim(worker_id, idle_poll_s=idle_poll_s)
        try:
            while True:
                if stop_event is not None and stop_event.is_set():
                    return
                claim_task = asyncio.create_task(claims.__anext__())
                if stop_event is not None:
                    stop_task = asyncio.create_task(stop_event.wait())
                    done, pending = await asyncio.wait(
                        {claim_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stop_task in done:
                        claim_task.cancel()
                        try:
                            await claim_task
                        except asyncio.CancelledError:
                            pass
                        return
                    stop_task.cancel()
                    for task in pending:
                        task.cancel()
                    job = claim_task.result()
                else:
                    job = await claim_task
                try:
                    await self._dispatch_job(job)
                except Exception as exc:
                    self._set_worker_state(
                        worker_id,
                        current_event_id=None,
                        last_error=str(exc),
                    )
                    if on_error is not None:
                        maybe_awaitable = on_error(exc)
                        if asyncio.iscoroutine(maybe_awaitable):
                            await maybe_awaitable
                    raise
        finally:
            self._set_worker_state(worker_id, running=False, current_event_id=None)

    def worker_states(self) -> list[WorkerState]:
        """Return local, non-durable snapshots for workers run by this process."""

        return [self._worker_states[key] for key in sorted(self._worker_states)]

    async def _dispatch_job(self, job: _HonkerJob) -> None:
        start = time.perf_counter()
        event_id = _required_payload_int(job.payload, "event_id")
        self._set_worker_state(job.worker_id, current_event_id=event_id)
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
                delivery_id = _optional_payload_int(job.payload, "delivery_id")
                handler_event = event
                if delivery_id is not None:
                    delivery = _get_delivery_or_none(tx, delivery_id)
                    if delivery is None:
                        raise RuntimeError(f"unknown delivery id in job payload: {delivery_id}")
                    if delivery.event_id != event_id:
                        raise RuntimeError(
                            f"delivery {delivery_id} is not linked to event {event_id}"
                        )
                    handler_event = _event_from_delivery(event, delivery)
                handler = self._resolve_handler(handler_event)
                if handler is None:
                    duration_ms = _duration_ms(start)
                    tx.query(
                        "SELECT knocker_mark_failed(?, ?, ?, ?, ?)",
                        [
                            event_id,
                            int(job.attempts),
                            (
                                f"no handler registered for endpoint={handler_event.endpoint!r} "
                                f"event_type={handler_event.event_type!r}"
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
                                f"no handler registered for endpoint={handler_event.endpoint!r} "
                                f"event_type={handler_event.event_type!r}"
                            ),
                            tx=tx,
                        ),
                        action="fail",
                        job_id=job.id,
                        event_id=event_id,
                    )
                    return
                tx.query("SELECT knocker_mark_processing(?, ?)", [event_id, job.attempts])
                handler(handler_event, tx)
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
        finally:
            self._set_worker_state(job.worker_id, current_event_id=None)

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

    def _set_worker_state(
        self,
        worker_id: str,
        *,
        running: Any = _UNSET,
        current_event_id: Any = _UNSET,
        last_error: Any = _UNSET,
    ) -> None:
        previous = self._worker_states.get(
            worker_id,
            WorkerState(worker_id=worker_id, running=False, current_event_id=None, last_error=None),
        )
        self._worker_states[worker_id] = WorkerState(
            worker_id=worker_id,
            running=previous.running if running is _UNSET else bool(running),
            current_event_id=(
                previous.current_event_id if current_event_id is _UNSET else current_event_id
            ),
            last_error=previous.last_error if last_error is _UNSET else last_error,
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


def _get_delivery_or_none(queryable: Any, delivery_id: int) -> Optional[Delivery]:
    rows = queryable.query(
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
        return None
    return _delivery_from_row(rows[0])


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


def _event_from_delivery(event: Event, delivery: Delivery) -> Event:
    # Synthesized handler input for replay_delivery: canonical event identity
    # and lifecycle fields (id, status, attempt_count, received_at, handled_at,
    # last_error) combined with the selected delivery's payload and metadata
    # (endpoint, event_type, provider_*, dedupe_key, headers, query, body).
    # The handler is resolved using the delivery's event_type, not the
    # canonical event's.
    return Event(
        id=event.id,
        endpoint=delivery.endpoint,
        event_type=delivery.event_type,
        provider_event_id=delivery.provider_event_id,
        provider_delivery_id=delivery.provider_delivery_id,
        dedupe_key=delivery.dedupe_key,
        status=event.status,
        attempt_count=event.attempt_count,
        headers=delivery.headers,
        query=delivery.query,
        body=delivery.body,
        received_at=event.received_at,
        handled_at=event.handled_at,
        last_error=event.last_error,
    )


def open(path: str, **kwargs: Any) -> Knocker:
    """Open or bootstrap a Knocker database at ``path``."""

    return Knocker(path, **kwargs)

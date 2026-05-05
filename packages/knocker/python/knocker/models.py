from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Result returned after Knocker durably stores an inbound receipt."""

    delivery_id: int
    event_id: Optional[int]
    duplicate: bool
    status_code: int


@dataclass(frozen=True, slots=True)
class Event:
    """Deduped processing unit passed to handlers and operator reads."""

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
    """Append-only HTTP receipt and verification audit row."""

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
    """Summary returned after pruning terminal events and linked rows."""

    events_pruned: int
    attempts_pruned: int
    deliveries_pruned: int
    live_jobs_pruned: int


@dataclass(frozen=True, slots=True)
class PruneDeliveriesResult:
    """Summary returned after pruning orphan delivery rows."""

    deliveries_pruned: int


@dataclass(frozen=True, slots=True)
class PruneAudit:
    """One durable audit row for an explicit prune operation."""

    id: int
    kind: str
    queue_name: str
    executed_at: int
    events_pruned: Optional[int]
    deliveries_pruned: int
    attempts_pruned: Optional[int]
    live_jobs_pruned: Optional[int]
    summary_json: str


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Python-first scheduled retention configuration."""

    interval_s: int
    event_statuses: tuple[str, ...] = ("handled", "ignored")
    event_older_than_s: Optional[int] = None
    event_limit: int = 100
    orphan_deliveries_older_than_s: Optional[int] = None
    orphan_deliveries_limit: int = 100


@dataclass(frozen=True, slots=True)
class WorkerState:
    """Local, non-durable snapshot of a running Knocker worker."""

    worker_id: str
    running: bool
    current_event_id: Optional[int]
    last_error: Optional[str]


Handler = Callable[[Event, Any], None]
ErrorHandler = Callable[[Exception], Any]

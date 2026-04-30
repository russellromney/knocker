import asyncio
import json
import sqlite3
import time

import knocker
import pytest

from knocker.queue import _HonkerJob

from tests.helpers import (
    generic_hmac_signature as _generic_hmac_signature,
    require_event_id as _require_event_id,
    stripe_signature as _stripe_signature,
    wait_for_status as _wait_for_status,
)


def _claim_one_for_test(app, worker_id: str) -> list:
    """Claim up to one Honker job via raw SQL for tests that exercise dispatch.

    Replacement for the removed semi-public ``app.queue.claim_batch(...)``
    escape hatch: tests that need to drive the worker dispatch path directly
    use the same SQL the internal claim helper uses, but without reaching
    into the underscored queue object.
    """

    with app.db.transaction() as tx:
        rows = tx.query(
            "SELECT honker_claim_batch(?, ?, ?, ?) AS rows_json",
            [app.queue_name, worker_id, 1, 60],
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


async def test_python_open_migrates_v1_database_to_delivery_rows(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE knocker_meta (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        INSERT INTO knocker_meta(key, value) VALUES ('schema_version', '1');

        CREATE TABLE knocker_endpoints (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL UNIQUE,
          path TEXT NOT NULL UNIQUE,
          provider TEXT,
          enabled INTEGER NOT NULL DEFAULT 1,
          created_at INTEGER NOT NULL DEFAULT (unixepoch())
        );

        CREATE TABLE knocker_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          endpoint_id INTEGER NOT NULL REFERENCES knocker_endpoints(id),
          received_at INTEGER NOT NULL DEFAULT (unixepoch()),
          provider_event_id TEXT,
          provider_delivery_id TEXT,
          event_type TEXT,
          method TEXT NOT NULL,
          headers_json TEXT NOT NULL,
          body_blob BLOB NOT NULL,
          query_json TEXT NOT NULL,
          signature_valid INTEGER,
          signature_error TEXT,
          dedupe_key TEXT,
          status TEXT NOT NULL CHECK (
            status IN ('received', 'processing', 'handled', 'failed', 'dead', 'ignored')
          ),
          attempt_count INTEGER NOT NULL DEFAULT 0,
          handled_at INTEGER,
          last_error TEXT
        );

        CREATE UNIQUE INDEX knocker_events_dedupe
          ON knocker_events(endpoint_id, dedupe_key)
          WHERE dedupe_key IS NOT NULL;

        CREATE TABLE knocker_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_id INTEGER NOT NULL REFERENCES knocker_events(id) ON DELETE CASCADE,
          attempted_at INTEGER NOT NULL DEFAULT (unixepoch()),
          outcome TEXT NOT NULL,
          error TEXT,
          duration_ms INTEGER NOT NULL
        );

        INSERT INTO knocker_endpoints (id, name, path, provider, enabled)
          VALUES (1, 'stripe', '/webhooks/stripe', 'stripe', 1);

        INSERT INTO knocker_events (
          id,
          endpoint_id,
          provider_event_id,
          provider_delivery_id,
          event_type,
          method,
          headers_json,
          body_blob,
          query_json,
          signature_valid,
          signature_error,
          dedupe_key,
          status
        )
        VALUES (
          1,
          1,
          'evt_legacy',
          'delivery_legacy',
          'checkout.session.completed',
          'POST',
          '{}',
          X'7B7D',
          '{}',
          1,
          NULL,
          'evt_legacy',
          'received'
        );
        """
    )
    conn.close()

    app = knocker.open(db_path)

    event = app.get_event(1)
    delivery = app.get_delivery(1)
    version = app.db.query("SELECT value FROM knocker_meta WHERE key='schema_version'")
    event_columns = {row["name"] for row in app.db.query("PRAGMA table_info(knocker_events)")}
    assert version[0]["value"] == "2"
    assert event.body == b"{}"
    assert delivery.event_id == event.id
    assert delivery.signature_valid is True
    assert "signature_valid" not in event_columns
    assert "signature_error" not in event_columns

async def test_prune_events_removes_old_terminal_events_and_linked_rows(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    first = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-prune-1"}',
        headers={},
        provider_event_id="evt-prune-1",
    )
    second = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-prune-2"}',
        headers={},
        provider_event_id="evt-prune-2",
    )
    third = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-prune-3"}',
        headers={},
        provider_event_id="evt-prune-3",
    )

    first_id = _require_event_id(first)
    second_id = _require_event_id(second)
    third_id = _require_event_id(third)

    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [first_id, 0])
    app.ignore(second_id)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [third_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, first_id])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [200, second_id])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [300, third_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [100, first_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [200, second_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [300, third_id])

    result = app.prune_events(statuses=["handled", "ignored"], older_than=250, limit=2)

    assert result == knocker.PruneEventsResult(
        events_pruned=2,
        attempts_pruned=2,
        deliveries_pruned=2,
        live_jobs_pruned=2,
    )

    with pytest.raises(KeyError):
        app.get_event(first_id)
    with pytest.raises(KeyError):
        app.get_event(second_id)
    assert app.get_event(third_id).status == "handled"
    assert [delivery.event_id for delivery in app.list_deliveries()] == [third_id]
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
    assert live_rows[0]["c"] == 1

async def test_prune_events_uses_strict_received_at_cutoff_and_oldest_first_limit(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    first = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-oldest"}',
        headers={},
        provider_event_id="evt-oldest",
    )
    second = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-edge"}',
        headers={},
        provider_event_id="evt-edge",
    )
    third = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-newer"}',
        headers={},
        provider_event_id="evt-newer",
    )
    first_id = _require_event_id(first)
    second_id = _require_event_id(second)
    third_id = _require_event_id(third)

    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [first_id, 0])
        tx.query("SELECT knocker_mark_handled(?, ?)", [second_id, 0])
        tx.query("SELECT knocker_mark_handled(?, ?)", [third_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, first_id])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [200, second_id])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [150, third_id])

    result = app.prune_events(statuses=("handled",), older_than=200, limit=1)
    assert result.events_pruned == 1
    with pytest.raises(KeyError):
        app.get_event(first_id)
    assert app.get_event(third_id).status == "handled"
    assert app.get_event(second_id).status == "handled"

async def test_prune_events_counts_multiple_deliveries_and_attempts(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    first = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-prune-count","n":1}',
        headers={},
        provider_event_id="evt-prune-count",
        provider_delivery_id="delivery-prune-count-1",
    )
    second = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-prune-count","n":2}',
        headers={},
        provider_event_id="evt-prune-count",
        provider_delivery_id="delivery-prune-count-2",
    )

    event_id = _require_event_id(first)
    assert second.event_id == event_id
    assert second.duplicate is True

    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_failed(?, ?, ?, ?, ?)", [event_id, 1, "boom", 0, 10])
        tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, 20])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, event_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [100, event_id])

    result = app.prune_events(statuses=["handled"], older_than=200, limit=10)

    assert result == knocker.PruneEventsResult(
        events_pruned=1,
        attempts_pruned=2,
        deliveries_pruned=2,
        live_jobs_pruned=1,
    )
    with pytest.raises(KeyError):
        app.get_event(event_id)
    with pytest.raises(KeyError):
        app.get_delivery(first.delivery_id)
    with pytest.raises(KeyError):
        app.get_delivery(second.delivery_id)

async def test_concurrent_prune_calls_serialize_without_double_counting(db_path):
    app_a = knocker.open(db_path)
    app_a.add_endpoint(name="stripe", path="/webhooks/stripe")
    event_ids = []
    for idx in range(10):
        result = app_a.ingest(
            endpoint="stripe",
            body=f'{{"id":"evt-prune-concurrent-{idx}"}}'.encode("utf-8"),
            headers={},
            provider_event_id=f"evt-prune-concurrent-{idx}",
        )
        event_ids.append(_require_event_id(result))
    with app_a.db.transaction() as tx:
        for event_id in event_ids:
            tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, 0])
            tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, event_id])
            tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [100, event_id])

    app_b = knocker.open(db_path)
    first, second = await asyncio.gather(
        asyncio.to_thread(
            lambda: app_a.prune_events(statuses=["handled"], older_than=200, limit=5)
        ),
        asyncio.to_thread(
            lambda: app_b.prune_events(statuses=["handled"], older_than=200, limit=5)
        ),
    )

    assert first.events_pruned + second.events_pruned == 10
    assert first.deliveries_pruned + second.deliveries_pruned == 10
    assert app_a.list_events() == []
    assert app_a.list_deliveries() == []

async def test_prune_events_rejects_invalid_status_inputs_and_types(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    with pytest.raises(TypeError, match="statuses must be a non-empty list or tuple of strings"):
        app.prune_events(statuses="handled", older_than=100, limit=1)
    with pytest.raises(ValueError, match="statuses must be a non-empty list or tuple"):
        app.prune_events(statuses=[], older_than=100, limit=1)
    with pytest.raises(ValueError, match="statuses must contain only 'handled' or 'ignored'"):
        app.prune_events(statuses=["received"], older_than=100, limit=1)
    with pytest.raises(TypeError, match="older_than must be an integer timestamp"):
        app.prune_events(statuses=["handled"], older_than=2.5, limit=1)
    with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
        app.prune_events(statuses=["handled"], older_than=100, limit=0)

async def test_prune_orphan_deliveries_only_removes_old_orphan_rows(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    linked = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-linked"}',
        headers={},
        provider_event_id="evt-linked",
    )
    orphan_old = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-orphan-old"}',
        headers={},
        provider_event_id="evt-orphan-old",
        signature_valid=False,
    )
    orphan_new = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-orphan-new"}',
        headers={},
        provider_event_id="evt-orphan-new",
        signature_valid=False,
    )

    linked_event_id = _require_event_id(linked)
    with app.db.transaction() as tx:
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE id=?", [100, orphan_old.delivery_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE id=?", [200, orphan_new.delivery_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE id=?", [50, linked.delivery_id])

    result = app.prune_orphan_deliveries(older_than=200, limit=10)
    assert result == knocker.PruneDeliveriesResult(deliveries_pruned=1)

    with pytest.raises(KeyError):
        app.get_delivery(orphan_old.delivery_id)
    assert app.get_delivery(orphan_new.delivery_id).event_id is None
    assert app.get_delivery(linked.delivery_id).event_id == linked_event_id

async def test_prune_events_only_cleans_live_jobs_for_this_knocker_queue(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-queue-scope"}',
        headers={},
        provider_event_id="evt-queue-scope",
    )
    event_id = _require_event_id(result)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, event_id])
        tx.query(
            "SELECT honker_enqueue(?, ?, ?, ?, ?, ?, ?) AS job_id",
            ["other.queue", json.dumps({"event_id": event_id}), None, None, 0, 1, None],
        )

    prune = app.prune_events(statuses=["handled"], older_than=200, limit=10)
    assert prune.live_jobs_pruned == 1

    other_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue='other.queue'")
    assert other_rows[0]["c"] == 1

async def test_prune_events_leaves_malformed_live_job_payloads_alone(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-malformed-live-payload"}',
        headers={},
        provider_event_id="evt-malformed-live-payload",
    )
    event_id = _require_event_id(result)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, event_id])
        tx.query(
            "SELECT honker_enqueue(?, ?, ?, ?, ?, ?, ?) AS job_id",
            [app.queue_name, "not-json", None, None, 0, 1, None],
        )

    prune = app.prune_events(statuses=["handled"], older_than=200, limit=10)

    assert prune.live_jobs_pruned == 1
    malformed_rows = app.db.query(
        "SELECT COUNT(*) AS c FROM _honker_live WHERE queue=? AND payload='not-json'",
        [app.queue_name],
    )
    assert malformed_rows[0]["c"] == 1

async def test_prune_events_missing_event_dispatch_exits_quietly_for_claimed_job(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-stale-claim"}',
        headers={},
        provider_event_id="evt-stale-claim",
    )
    event_id = _require_event_id(result)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, event_id])

    jobs = _claim_one_for_test(app, "worker-prune")
    assert len(jobs) == 1

    prune = app.prune_events(statuses=["handled"], older_than=200, limit=10)
    assert prune.events_pruned == 1
    assert prune.live_jobs_pruned == 1

    await app._dispatch_job(jobs[0])

    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
    assert live_rows[0]["c"] == 0

async def test_prune_events_rolls_back_when_delete_step_raises(db_path, monkeypatch):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-rollback"}',
        headers={},
        provider_event_id="evt-rollback",
    )
    event_id = _require_event_id(result)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, event_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [100, event_id])

    original = app._delete_deliveries_for_event_ids

    def boom(tx, event_ids):
        original(tx, event_ids)
        raise RuntimeError("boom")

    monkeypatch.setattr(app, "_delete_deliveries_for_event_ids", boom)

    with pytest.raises(RuntimeError, match="boom"):
        app.prune_events(statuses=["handled"], older_than=200, limit=10)

    assert app.get_event(event_id).status == "handled"
    assert app.get_delivery(result.delivery_id).event_id == event_id
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
    assert live_rows[0]["c"] == 1

async def test_prune_events_keeps_other_event_deliveries_intact(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    first = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-a"}',
        headers={},
        provider_event_id="evt-a",
    )
    second = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-b"}',
        headers={},
        provider_event_id="evt-b",
    )
    first_id = _require_event_id(first)
    second_id = _require_event_id(second)

    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [first_id, 0])
        tx.query("SELECT knocker_mark_handled(?, ?)", [second_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, first_id])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [300, second_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [100, first_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [300, second_id])

    app.prune_events(statuses=["handled"], older_than=200, limit=10)

    with pytest.raises(KeyError):
        app.get_event(first_id)
    assert app.get_event(second_id).status == "handled"
    assert app.get_delivery(second.delivery_id).event_id == second_id

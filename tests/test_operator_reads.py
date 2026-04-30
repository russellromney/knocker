import asyncio
import json
import sqlite3
import time

import knocker
import pytest

from tests.helpers import (
    generic_hmac_signature as _generic_hmac_signature,
    require_event_id as _require_event_id,
    stripe_signature as _stripe_signature,
    wait_for_status as _wait_for_status,
)


async def test_endpoint_alias_is_removed_before_public_release(db_path):
    app = knocker.open(db_path)
    assert not hasattr(app, "endpoint")

async def test_supported_python_surface_has_docstrings(db_path):
    public_objects = [
        knocker.IngestResult,
        knocker.Event,
        knocker.Delivery,
        knocker.PruneEventsResult,
        knocker.PruneDeliveriesResult,
        knocker.WorkerState,
        knocker.Knocker,
        knocker.open,
    ]
    public_methods = [
        "add_endpoint",
        "add_handler",
        "handle",
        "ingest",
        "receive",
        "get_event",
        "list_events",
        "get_delivery",
        "list_deliveries",
        "ignore",
        "replay",
        "requeue",
        "replay_delivery",
        "prune_events",
        "prune_orphan_deliveries",
        "run_worker",
        "worker_states",
    ]

    assert all(obj.__doc__ for obj in public_objects)
    assert all(getattr(knocker.Knocker, method).__doc__ for method in public_methods)

async def test_list_events_supports_filters_since_limit_and_stable_newest_first(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
    app.add_endpoint(name="github", path="/webhooks/github")

    first = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-1"}',
        headers={},
        event_type="checkout.session.completed",
        provider_event_id="evt-1",
    )
    second = app.ingest(
        endpoint="github",
        body=b'{"id":"evt-2"}',
        headers={},
        event_type="push",
        provider_event_id="evt-2",
    )
    third = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-3"}',
        headers={},
        event_type="checkout.session.completed",
        provider_event_id="evt-3",
    )

    first_id = _require_event_id(first)
    second_id = _require_event_id(second)
    third_id = _require_event_id(third)

    with app.db.transaction() as tx:
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, first_id])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [200, second_id])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [200, third_id])

    assert [event.id for event in app.list_events()] == [third_id, second_id, first_id]
    assert [event.id for event in app.list_events(since=200)] == [third_id, second_id]
    assert [event.id for event in app.list_events(endpoint="stripe")] == [third_id, first_id]
    assert [
        event.id
        for event in app.list_events(
            endpoint="stripe",
            event_type="checkout.session.completed",
            since=200,
        )
    ] == [third_id]
    assert app.list_events(event_type="CHECKOUT.SESSION.COMPLETED") == []
    assert [event.id for event in app.list_events(limit=1)] == [third_id]

    with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
        app.list_events(limit=0)
    with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
        app.list_events(limit=1001)

async def test_list_events_default_limit_caps_results(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    event_ids = []
    for idx in range(105):
        result = app.ingest(
            endpoint="stripe",
            body=f'{{"id":"evt-{idx}"}}'.encode("utf-8"),
            headers={},
            provider_event_id=f"evt-{idx}",
        )
        event_ids.append(_require_event_id(result))

    events = app.list_events()
    assert len(events) == 100
    assert [event.id for event in events] == list(reversed(event_ids[-100:]))

async def test_list_deliveries_supports_filters_since_limit_and_newest_first(db_path):
    app = knocker.open(db_path)
    body = b'{"ok":true}'
    app.add_endpoint(
        name="generic",
        path="/webhooks/generic",
        provider="generic",
        verification={
            "kind": "hmac-sha256",
            "secret": "topsecret",
            "header": "x-signature",
        },
    )
    app.add_endpoint(name="github", path="/webhooks/github")

    first = app.receive(
        endpoint="generic",
        body=body,
        headers={"X-Signature": _generic_hmac_signature("topsecret", body)},
        provider_delivery_id="delivery-1",
    )
    second = app.receive(
        endpoint="generic",
        body=body,
        headers={"X-Signature": "bad"},
        provider_delivery_id="delivery-2",
    )
    third = app.receive(
        endpoint="github",
        body=b'{"zen":"keep it logically awesome"}',
        headers={
            "X-GitHub-Delivery": "delivery-3",
            "X-GitHub-Event": "push",
        },
    )

    first_event_id = _require_event_id(first)
    with app.db.transaction() as tx:
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE id=?", [100, first.delivery_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE id=?", [200, second.delivery_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE id=?", [200, third.delivery_id])

    assert [delivery.id for delivery in app.list_deliveries()] == [
        third.delivery_id,
        second.delivery_id,
        first.delivery_id,
    ]
    assert [delivery.id for delivery in app.list_deliveries(event_id=first_event_id)] == [
        first.delivery_id
    ]
    assert [delivery.id for delivery in app.list_deliveries(endpoint="generic")] == [
        second.delivery_id,
        first.delivery_id,
    ]
    assert [delivery.id for delivery in app.list_deliveries(signature_valid=True)] == [
        third.delivery_id,
        first.delivery_id,
    ]
    assert [delivery.id for delivery in app.list_deliveries(signature_valid=False)] == [
        second.delivery_id
    ]
    assert [delivery.id for delivery in app.list_deliveries(orphaned=True)] == [
        second.delivery_id
    ]
    assert [delivery.id for delivery in app.list_deliveries(orphaned=False)] == [
        third.delivery_id,
        first.delivery_id,
    ]
    assert [delivery.id for delivery in app.list_deliveries(since=200)] == [
        third.delivery_id,
        second.delivery_id,
    ]
    assert [delivery.id for delivery in app.list_deliveries(limit=2)] == [
        third.delivery_id,
        second.delivery_id,
    ]

    with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
        app.list_deliveries(limit=1001)

async def test_ignore_public_surface_enforces_supported_statuses(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    received = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-received"}',
        headers={},
        provider_event_id="evt-received",
    )
    received_id = _require_event_id(received)
    app.ignore(received_id)
    assert app.get_event(received_id).status == "ignored"
    ignored_attempts = app.db.query(
        "SELECT COUNT(*) AS c FROM knocker_attempts WHERE event_id=? AND outcome='ignored'",
        [received_id],
    )
    assert ignored_attempts[0]["c"] == 1
    app.ignore(received_id)
    ignored_attempts = app.db.query(
        "SELECT COUNT(*) AS c FROM knocker_attempts WHERE event_id=? AND outcome='ignored'",
        [received_id],
    )
    assert ignored_attempts[0]["c"] == 1

    failed = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-failed"}',
        headers={},
        provider_event_id="evt-failed",
    )
    failed_id = _require_event_id(failed)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_failed(?, ?, ?, ?, ?)", [failed_id, 1, "boom", 0, 0])
    app.ignore(failed_id)
    assert app.get_event(failed_id).status == "ignored"

    dead = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-dead"}',
        headers={},
        provider_event_id="evt-dead",
    )
    dead_id = _require_event_id(dead)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_failed(?, ?, ?, ?, ?)", [dead_id, 1, "boom", 1, 0])
    app.ignore(dead_id)
    assert app.get_event(dead_id).status == "ignored"

    processing = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-processing"}',
        headers={},
        provider_event_id="evt-processing",
    )
    processing_id = _require_event_id(processing)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_processing(?, ?)", [processing_id, 1])
    with pytest.raises(ValueError, match="cannot be ignored"):
        app.ignore(processing_id)

    handled = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-handled"}',
        headers={},
        provider_event_id="evt-handled",
    )
    handled_id = _require_event_id(handled)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [handled_id, 0])
    with pytest.raises(ValueError, match="cannot be ignored"):
        app.ignore(handled_id)

async def test_ignore_received_event_prevents_later_worker_dispatch(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    seen = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        seen.append(event.id)

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-ignore"}',
        headers={},
        provider_event_id="evt-ignore",
    )

    event_id = _require_event_id(result)
    app.ignore(event_id)

    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop))

    deadline = asyncio.get_running_loop().time() + 1.0
    while asyncio.get_running_loop().time() < deadline:
        rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
        if rows[0]["c"] == 0:
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)

    event = app.get_event(event_id)
    assert event.status == "ignored"
    assert seen == []
    rows = app.db.query(
        "SELECT COUNT(*) AS c FROM knocker_attempts WHERE event_id=? AND outcome='ignored'",
        [event_id],
    )
    assert rows[0]["c"] == 1
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
    assert live_rows[0]["c"] == 0

async def test_signature_valid_false_filter_includes_null_rows(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    invalid = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-invalid"}',
        headers={},
        provider_event_id="evt-invalid",
        signature_valid=False,
    )
    unknown = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-unknown"}',
        headers={},
        provider_event_id="evt-unknown",
        signature_valid=None,
    )
    valid = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-valid"}',
        headers={},
        provider_event_id="evt-valid",
        signature_valid=True,
    )

    true_rows = app.list_deliveries(signature_valid=True)
    false_rows = app.list_deliveries(signature_valid=False)
    assert [delivery.id for delivery in true_rows] == [valid.delivery_id]
    assert [delivery.id for delivery in false_rows] == [unknown.delivery_id, invalid.delivery_id]

async def test_operator_filters_reject_non_integer_since_and_delivery_limit_zero(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
    app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-1"}',
        headers={},
        provider_event_id="evt-1",
    )

    with pytest.raises(TypeError, match="since must be an integer timestamp"):
        app.list_events(since=2.5)
    with pytest.raises(TypeError, match="since must be an integer timestamp"):
        app.list_deliveries(since=2.5)
    with pytest.raises(TypeError, match="limit must be an integer"):
        app.list_events(limit=2.5)
    with pytest.raises(TypeError, match="limit must be an integer"):
        app.list_deliveries(limit="50")
    with pytest.raises(ValueError, match="limit must be between 1 and 1000"):
        app.list_deliveries(limit=0)

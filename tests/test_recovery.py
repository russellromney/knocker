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


async def test_replay_requeues_stored_event(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    seen = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        seen.append(event.id)

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt_1"}',
        headers={"stripe-signature": "sig"},
        provider_event_id="evt_1",
    )

    event_id = _require_event_id(result)
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop))

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if app.get_event(event_id).status == "handled":
            break
        await asyncio.sleep(0.05)

    app.replay(event_id)

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if len(seen) == 2:
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)

    assert seen == [event_id, event_id]

async def test_requeue_moves_dead_event_back_to_received_and_runs_again(db_path):
    app = knocker.open(db_path, max_attempts=1)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    recovered = {"ready": False}
    seen = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        seen.append(event.id)
        if not recovered["ready"]:
            raise RuntimeError("boom")

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt_1"}',
        headers={"stripe-signature": "sig"},
        provider_event_id="evt_1",
    )

    event_id = _require_event_id(result)
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop))

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if app.get_event(event_id).status == "dead":
            break
        await asyncio.sleep(0.05)

    recovered["ready"] = True
    app.requeue(event_id)

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if app.get_event(event_id).status == "handled":
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)

    event = app.get_event(event_id)
    assert event.status == "handled"
    assert seen == [event_id, event_id]

async def test_replay_rejects_received_event_with_existing_live_job(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-replay-received"}',
        headers={},
        provider_event_id="evt-replay-received",
    )

    event_id = _require_event_id(result)
    with pytest.raises(ValueError, match="cannot be replayed"):
        app.replay(event_id)

    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert rows[0]["c"] == 1

async def test_requeue_failed_event_replaces_existing_live_job_not_duplicates(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-failed-requeue"}',
        headers={},
        provider_event_id="evt-failed-requeue",
    )

    event_id = _require_event_id(result)
    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_failed(?, ?, ?, ?, ?)", [event_id, 1, "boom", 0, 0])

    before = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert before[0]["c"] == 1

    app.requeue(event_id)

    after = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert after[0]["c"] == 1
    assert app.get_event(event_id).status == "received"

async def test_dead_redelivery_is_audit_only_until_explicit_requeue(db_path):
    app = knocker.open(db_path, max_attempts=1)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    recovered = {"ready": False}
    seen = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        seen.append(event.id)
        if not recovered["ready"]:
            raise RuntimeError("boom")

    first = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-dead-redelivery","n":1}',
        headers={},
        provider_event_id="evt-dead-redelivery",
        provider_delivery_id="delivery-1",
    )

    event_id = _require_event_id(first)
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop))

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if app.get_event(event_id).status == "dead":
            break
        await asyncio.sleep(0.05)

    recovered["ready"] = True
    second = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-dead-redelivery","n":2}',
        headers={},
        provider_event_id="evt-dead-redelivery",
        provider_delivery_id="delivery-2",
    )

    await asyncio.sleep(0.2)
    deliveries = app.list_deliveries(event_id=event_id)
    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert second.event_id == event_id
    assert second.duplicate is True
    assert app.get_event(event_id).status == "dead"
    assert app.get_event(event_id).body == b'{"id":"evt-dead-redelivery","n":1}'
    assert seen == [event_id]
    assert [delivery.provider_delivery_id for delivery in deliveries] == ["delivery-2", "delivery-1"]
    assert [delivery.body for delivery in deliveries] == [
        b'{"id":"evt-dead-redelivery","n":2}',
        b'{"id":"evt-dead-redelivery","n":1}',
    ]
    assert rows[0]["c"] == 0

    app.requeue(event_id)
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if app.get_event(event_id).status == "handled":
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)

    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert app.get_event(event_id).status == "handled"
    assert seen == [event_id, event_id]
    assert rows[0]["c"] == 0

async def test_replay_delivery_processes_specific_body_without_mutating_event_payload(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    seen = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        seen.append((event.body, event.provider_delivery_id))

    first = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-delivery-replay","n":1}',
        headers={},
        provider_event_id="evt-delivery-replay",
        provider_delivery_id="delivery-original",
    )
    event_id = _require_event_id(first)

    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop))
    await _wait_for_status(app, event_id, "handled")

    duplicate = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-delivery-replay","n":2}',
        headers={},
        provider_event_id="evt-delivery-replay",
        provider_delivery_id="delivery-specific",
    )
    assert duplicate.duplicate is True

    app.replay_delivery(duplicate.delivery_id)
    await _wait_for_status(app, event_id, "handled")

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)

    assert seen == [
        (b'{"id":"evt-delivery-replay","n":1}', "delivery-original"),
        (b'{"id":"evt-delivery-replay","n":2}', "delivery-specific"),
    ]
    assert app.get_event(event_id).body == b'{"id":"evt-delivery-replay","n":1}'
    assert app.get_event(event_id).provider_delivery_id == "delivery-original"

async def test_replay_delivery_rejects_unknown_orphan_and_live_events(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    with pytest.raises(KeyError):
        app.replay_delivery(404)

    orphan = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-orphan-replay"}',
        headers={},
        provider_event_id="evt-orphan-replay",
        signature_valid=False,
    )
    with pytest.raises(ValueError, match="not linked"):
        app.replay_delivery(orphan.delivery_id)

    live = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-live-replay"}',
        headers={},
        provider_event_id="evt-live-replay",
    )
    with pytest.raises(ValueError, match="cannot replay a delivery"):
        app.replay_delivery(live.delivery_id)

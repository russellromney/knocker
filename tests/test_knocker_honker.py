import asyncio
import hashlib
import hmac
import json
import time

import knocker
import pytest


def _generic_hmac_signature(secret: str, body: bytes, *, prefix: str = "sha256=") -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{prefix}{digest}"


def _stripe_signature(secret: str, body: bytes, *, timestamp: int | None = None) -> str:
    timestamp = int(time.time()) if timestamp is None else int(timestamp)
    signed_payload = f"{timestamp}.".encode("utf-8") + body
    digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def _require_event_id(result: knocker.IngestResult) -> int:
    assert result.event_id is not None
    return result.event_id


async def test_ingest_stores_event_before_worker_runs(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")
    with app.db.transaction() as tx:
        tx.execute("CREATE TABLE IF NOT EXISTS handled_events (event_id INTEGER PRIMARY KEY)")

    calls = []

    def handle_checkout(event, tx):
        calls.append(event.id)
        tx.query("INSERT INTO handled_events (event_id) VALUES (?)", [event.id])

    app.add_handler(
        endpoint="stripe",
        event_type="checkout.session.completed",
        handler=handle_checkout,
    )

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt_1"}',
        headers={"stripe-signature": "sig"},
        event_type="checkout.session.completed",
        provider_delivery_id="delivery-1",
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    delivery = app.get_delivery(result.delivery_id)
    assert result.duplicate is False
    assert event.status == "received"
    assert delivery.event_id == event_id
    assert calls == []
    rows = app.db.query("SELECT COUNT(*) AS c FROM handled_events")
    assert rows[0]["c"] == 0

    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop))

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        event = app.get_event(event_id)
        if event.status == "handled":
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)

    event = app.get_event(event_id)
    assert event.status == "handled"
    assert calls == [event_id]
    rows = app.db.query("SELECT COUNT(*) AS c FROM handled_events WHERE event_id=?", [event_id])
    assert rows[0]["c"] == 1


async def test_ingest_rolls_back_event_delivery_and_queue_when_enqueue_fails(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    with app.db.transaction() as tx:
        tx.execute(
            """
            CREATE TRIGGER fail_knocker_enqueue
            BEFORE INSERT ON _honker_live
            WHEN NEW.queue = 'knocker.events'
            BEGIN
                SELECT RAISE(ABORT, 'forced enqueue failure');
            END
            """
        )

    with pytest.raises(Exception, match="forced enqueue failure"):
        app.ingest(
            endpoint="stripe",
            body=b'{"id":"evt-rollback-ingest"}',
            headers={},
            provider_event_id="evt-rollback-ingest",
            provider_delivery_id="delivery-rollback-ingest",
        )

    event_rows = app.db.query(
        "SELECT COUNT(*) AS c FROM knocker_events WHERE provider_event_id='evt-rollback-ingest'"
    )
    delivery_rows = app.db.query(
        "SELECT COUNT(*) AS c FROM knocker_deliveries WHERE provider_event_id='evt-rollback-ingest'"
    )
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert event_rows[0]["c"] == 0
    assert delivery_rows[0]["c"] == 0
    assert live_rows[0]["c"] == 0


async def test_multiple_workers_process_each_event_once(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")
    with app.db.transaction() as tx:
        tx.execute("CREATE TABLE handled_once (event_id INTEGER PRIMARY KEY)")

    seen = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        tx.query("INSERT INTO handled_once (event_id) VALUES (?)", [event.id])
        seen.append(event.id)

    event_ids = []
    for idx in range(4):
        result = app.ingest(
            endpoint="stripe",
            body=f'{{"id":"evt-worker-{idx}"}}'.encode("utf-8"),
            headers={},
            provider_event_id=f"evt-worker-{idx}",
        )
        event_ids.append(_require_event_id(result))

    stop = asyncio.Event()
    workers = [
        asyncio.create_task(
            app.run_worker(worker_id=f"worker-{idx}", stop_event=stop, idle_poll_s=0.01)
        )
        for idx in range(2)
    ]

    try:
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            rows = app.db.query(
                "SELECT COUNT(*) AS c FROM knocker_events WHERE status='handled'"
            )
            if rows[0]["c"] == len(event_ids):
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(*workers), timeout=3.0)

    handled_rows = app.db.query("SELECT event_id FROM handled_once ORDER BY event_id")
    assert sorted(seen) == event_ids
    assert [int(row["event_id"]) for row in handled_rows] == event_ids
    assert all(app.get_event(event_id).status == "handled" for event_id in event_ids)


async def test_duplicate_valid_deliveries_are_auditable_without_mutating_event(db_path):
    app = knocker.open(db_path)
    app.endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    first_body = b'{"id":"evt_dup","n":1}'
    second_body = b'{"id":"evt_dup","n":2}'
    first = app.ingest(
        endpoint="stripe",
        body=first_body,
        headers={"stripe-signature": "sig-1"},
        event_type="checkout.session.completed",
        provider_event_id="evt_dup",
        provider_delivery_id="delivery-1",
    )
    second = app.ingest(
        endpoint="stripe",
        body=second_body,
        headers={"stripe-signature": "sig-2"},
        event_type="checkout.session.completed",
        provider_event_id="evt_dup",
        provider_delivery_id="delivery-2",
    )

    event_id = _require_event_id(first)
    assert second.event_id == event_id
    assert first.duplicate is False
    assert second.duplicate is True

    event = app.get_event(event_id)
    deliveries = app.list_deliveries(event_id=event_id)
    assert event.body == first_body
    assert event.provider_delivery_id == "delivery-1"
    assert [delivery.provider_delivery_id for delivery in deliveries] == ["delivery-2", "delivery-1"]
    assert [delivery.body for delivery in deliveries] == [second_body, first_body]

    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert rows[0]["c"] == 1


async def test_failures_retry_then_dead_letter(db_path):
    app = knocker.open(db_path, max_attempts=2)
    app.endpoint(name="slack", path="/webhooks/slack", provider="slack")

    attempts = []

    @app.handle(endpoint="slack")
    def always_fail(event, tx):
        attempts.append(event.attempt_count)
        raise RuntimeError("boom")

    result = app.ingest(
        endpoint="slack",
        body=b"{}",
        headers={"x-slack-request-timestamp": "1"},
    )

    event_id = _require_event_id(result)
    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop))

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        event = app.get_event(event_id)
        if event.status == "dead":
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)

    event = app.get_event(event_id)
    assert event.status == "dead"
    assert event.attempt_count == 2
    assert "boom" in (event.last_error or "")
    assert len(attempts) == 2

    attempt_rows = app.db.query(
        "SELECT outcome FROM knocker_attempts WHERE event_id=? ORDER BY id",
        [event_id],
    )
    assert [row["outcome"] for row in attempt_rows] == ["failed", "dead"]


async def test_replay_requeues_stored_event(db_path):
    app = knocker.open(db_path)
    app.endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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


async def test_missing_handler_is_dead_lettered_not_ignored(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
        event = app.get_event(event_id)
        if event.status == "dead":
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)

    event = app.get_event(event_id)
    assert event.status == "dead"
    assert "no handler registered" in (event.last_error or "")

    attempt_rows = app.db.query(
        "SELECT outcome FROM knocker_attempts WHERE event_id=? ORDER BY id",
        [event_id],
    )
    assert [row["outcome"] for row in attempt_rows] == ["dead"]


async def test_claim_expiry_rolls_back_handled_state(db_path):
    app = knocker.open(db_path, visibility_timeout_s=1, max_attempts=3)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

    @app.handle(endpoint="stripe")
    def slow_handle(event, tx):
        time.sleep(2)

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt_1"}',
        headers={"stripe-signature": "sig"},
        provider_event_id="evt_1",
    )

    event_id = _require_event_id(result)
    worker = asyncio.create_task(app.run_worker())

    with pytest.raises(RuntimeError, match="claim no longer valid"):
        await asyncio.wait_for(worker, timeout=5.0)

    event = app.get_event(event_id)
    assert event.status == "received"
    assert event.attempt_count == 0

    live_rows = app.db.query(
        "SELECT state FROM _honker_live WHERE queue=?",
        [app.queue.name],
    )
    assert live_rows[0]["state"] == "processing"


async def test_receive_valid_generic_hmac_is_stored_and_enqueued(db_path):
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

    result = app.receive(
        endpoint="generic",
        body=body,
        headers={"X-Signature": _generic_hmac_signature("topsecret", body)},
        provider_delivery_id="delivery-1",
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    delivery = app.get_delivery(result.delivery_id)
    assert result.status_code == 204
    assert delivery.signature_valid is True
    assert delivery.signature_error is None
    assert delivery.event_id == event_id
    assert event.status == "received"
    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert rows[0]["c"] == 1


async def test_receive_invalid_generic_hmac_creates_orphan_delivery(db_path):
    app = knocker.open(db_path)
    body = b'{"ok":false}'
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

    result = app.receive(
        endpoint="generic",
        body=body,
        headers={"X-Signature": _generic_hmac_signature("wrongsecret", body)},
        provider_delivery_id="delivery-2",
    )

    delivery = app.get_delivery(result.delivery_id)
    assert result.status_code == 401
    assert result.event_id is None
    assert delivery.event_id is None
    assert delivery.signature_valid is False
    assert "signature mismatch" in (delivery.signature_error or "")
    assert app.list_events() == []
    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert rows[0]["c"] == 0


async def test_receive_valid_stripe_signature_is_stored_and_enqueued(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_1","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": "whsec_test"},
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body)},
        provider_event_id="evt_1",
        provider_delivery_id="delivery-stripe-1",
        event_type="checkout.session.completed",
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    delivery = app.get_delivery(result.delivery_id)
    assert result.status_code == 204
    assert delivery.signature_valid is True
    assert event.status == "received"
    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert rows[0]["c"] == 1


async def test_receive_invalid_stripe_signature_creates_orphan_delivery(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_2","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": "whsec_test"},
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_wrong", body)},
        provider_event_id="evt_2",
        provider_delivery_id="delivery-stripe-2",
        event_type="checkout.session.completed",
    )

    delivery = app.get_delivery(result.delivery_id)
    assert result.status_code == 401
    assert result.event_id is None
    assert delivery.event_id is None
    assert delivery.signature_valid is False
    assert "stripe signature mismatch" in (delivery.signature_error or "")
    assert app.list_events() == []


async def test_secret_rotation_accepts_overlapping_stripe_secrets(db_path):
    app = knocker.open(db_path)
    old_secret = "whsec_old"
    new_secret = "whsec_new"
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secrets": [old_secret, new_secret]},
    )

    old_body = b'{"id":"evt_old","type":"checkout.session.completed"}'
    old_result = app.receive(
        endpoint="stripe",
        body=old_body,
        headers={"Stripe-Signature": _stripe_signature(old_secret, old_body)},
        provider_event_id="evt_old",
        provider_delivery_id="delivery-old",
    )
    new_body = b'{"id":"evt_new","type":"checkout.session.completed"}'
    new_result = app.receive(
        endpoint="stripe",
        body=new_body,
        headers={"Stripe-Signature": _stripe_signature(new_secret, new_body)},
        provider_event_id="evt_new",
        provider_delivery_id="delivery-new",
    )

    old_event = app.get_event(_require_event_id(old_result))
    new_event = app.get_event(_require_event_id(new_result))
    assert app.get_delivery(old_result.delivery_id).signature_valid is True
    assert app.get_delivery(new_result.delivery_id).signature_valid is True
    assert old_event.status == "received"
    assert new_event.status == "received"


async def test_stripe_verifier_rejects_expired_timestamps(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt-expired","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": "whsec_test", "tolerance_s": 300},
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body, timestamp=int(time.time()) - 601)},
        provider_event_id="evt-expired",
    )

    delivery = app.get_delivery(result.delivery_id)
    assert result.event_id is None
    assert result.status_code == 401
    assert delivery.signature_valid is False
    assert "outside tolerance" in (delivery.signature_error or "")


async def test_stripe_verifier_rejects_negative_tolerance_config(db_path):
    app = knocker.open(db_path)
    with pytest.raises(ValueError, match="must be non-negative"):
        app.add_endpoint(
            name="stripe",
            path="/webhooks/stripe",
            provider="stripe",
            verification={"kind": "stripe", "secret": "whsec_test", "tolerance_s": -1},
        )


async def test_secret_rotation_can_remove_old_secret(db_path):
    app = knocker.open(db_path)
    old_secret = "whsec_old"
    new_secret = "whsec_new"
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": new_secret},
    )

    old_body = b'{"id":"evt_old_removed","type":"checkout.session.completed"}'
    old_result = app.receive(
        endpoint="stripe",
        body=old_body,
        headers={"Stripe-Signature": _stripe_signature(old_secret, old_body)},
        provider_event_id="evt_old_removed",
        provider_delivery_id="delivery-old-removed",
    )
    new_body = b'{"id":"evt_new_only","type":"checkout.session.completed"}'
    new_result = app.receive(
        endpoint="stripe",
        body=new_body,
        headers={"Stripe-Signature": _stripe_signature(new_secret, new_body)},
        provider_event_id="evt_new_only",
        provider_delivery_id="delivery-new-only",
    )

    old_delivery = app.get_delivery(old_result.delivery_id)
    new_delivery = app.get_delivery(new_result.delivery_id)
    new_event = app.get_event(_require_event_id(new_result))
    assert old_result.event_id is None
    assert old_delivery.signature_valid is False
    assert new_delivery.signature_valid is True
    assert new_event.status == "received"


async def test_invalid_first_valid_later_creates_two_deliveries_and_one_event(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_rotate","type":"checkout.session.completed"}'
    old_secret = "whsec_old"
    new_secret = "whsec_new"
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": new_secret},
    )

    first = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature(old_secret, body)},
        provider_event_id="evt_rotate",
        provider_delivery_id="delivery-rotate-1",
    )
    second = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature(new_secret, body)},
        provider_event_id="evt_rotate",
        provider_delivery_id="delivery-rotate-2",
    )

    event_id = _require_event_id(second)
    deliveries = app.list_deliveries()
    linked = app.list_deliveries(event_id=event_id)
    event = app.get_event(event_id)
    assert first.event_id is None
    assert second.duplicate is False
    assert len(deliveries) == 2
    assert [delivery.event_id for delivery in deliveries] == [event_id, None]
    assert [delivery.signature_valid for delivery in deliveries] == [True, False]
    assert len(linked) == 1
    assert event.provider_delivery_id == "delivery-rotate-2"


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


async def test_stripe_provider_preset_can_fill_verifier_and_extract_metadata(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_preset","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        secrets=["whsec_test"],
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body)},
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_event_id == "evt_preset"
    assert event.event_type == "checkout.session.completed"


async def test_github_provider_preset_extracts_delivery_key_for_dedupe(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
    )

    first = app.receive(
        endpoint="github",
        body=b'{"zen":"keep it logically awesome"}',
        headers={
            "X-GitHub-Delivery": "delivery-123",
            "X-GitHub-Event": "push",
        },
    )
    second = app.receive(
        endpoint="github",
        body=b'{"zen":"keep it logically awesome"}',
        headers={
            "X-GitHub-Delivery": "delivery-123",
            "X-GitHub-Event": "push",
        },
    )

    event_id = _require_event_id(first)
    assert second.event_id == event_id
    assert second.duplicate is True
    event = app.get_event(event_id)
    deliveries = app.list_deliveries(event_id=event_id)
    assert event.event_type == "push"
    assert len(deliveries) == 2


async def test_list_events_supports_filters_since_limit_and_stable_newest_first(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")
    app.add_endpoint(name="github", path="/webhooks/github", provider="github")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    app.add_endpoint(name="github", path="/webhooks/github", provider="github")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
        rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
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
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert live_rows[0]["c"] == 0


async def test_signature_valid_false_filter_includes_null_rows(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")
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


async def test_prune_events_removes_old_terminal_events_and_linked_rows(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert live_rows[0]["c"] == 1


async def test_prune_events_uses_strict_received_at_cutoff_and_oldest_first_limit(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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


async def test_prune_events_rejects_invalid_status_inputs_and_types(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
            [app.queue.name, "not-json", None, None, 0, 1, None],
        )

    prune = app.prune_events(statuses=["handled"], older_than=200, limit=10)

    assert prune.live_jobs_pruned == 1
    malformed_rows = app.db.query(
        "SELECT COUNT(*) AS c FROM _honker_live WHERE queue=? AND payload='not-json'",
        [app.queue.name],
    )
    assert malformed_rows[0]["c"] == 1


async def test_prune_events_missing_event_dispatch_exits_quietly_for_claimed_job(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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

    jobs = app.queue.claim_batch("worker-prune", 1)
    assert len(jobs) == 1

    prune = app.prune_events(statuses=["handled"], older_than=200, limit=10)
    assert prune.events_pruned == 1
    assert prune.live_jobs_pruned == 1

    await app._dispatch_job(jobs[0])

    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert live_rows[0]["c"] == 0


async def test_prune_events_rolls_back_when_delete_step_raises(db_path, monkeypatch):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert live_rows[0]["c"] == 1


async def test_prune_events_keeps_other_event_deliveries_intact(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe", provider="stripe")

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

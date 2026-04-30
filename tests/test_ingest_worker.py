import asyncio
import json
import sqlite3
import time
from pathlib import Path

import knocker
import pytest

from tests.helpers import (
    generic_hmac_signature as _generic_hmac_signature,
    require_event_id as _require_event_id,
    stripe_signature as _stripe_signature,
    wait_for_status as _wait_for_status,
)


async def test_ingest_stores_event_before_worker_runs(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
    with app.db.transaction() as tx:
        tx.execute("CREATE TABLE IF NOT EXISTS handled_events (event_id INTEGER PRIMARY KEY)")

    calls = []
    states_seen = []

    def handle_checkout(event, tx):
        calls.append(event.id)
        states_seen.append(app.worker_states()[0])
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
    worker = asyncio.create_task(app.run_worker(worker_id="worker-state", stop_event=stop))

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
    assert states_seen == [
        knocker.WorkerState(
            worker_id="worker-state",
            running=True,
            current_event_id=event_id,
            last_error=None,
        )
    ]
    assert app.worker_states() == [
        knocker.WorkerState(
            worker_id="worker-state",
            running=False,
            current_event_id=None,
            last_error=None,
        )
    ]
    rows = app.db.query("SELECT COUNT(*) AS c FROM handled_events WHERE event_id=?", [event_id])
    assert rows[0]["c"] == 1

async def test_ingest_rolls_back_event_delivery_and_queue_when_enqueue_fails(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

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
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
    assert event_rows[0]["c"] == 0
    assert delivery_rows[0]["c"] == 0
    assert live_rows[0]["c"] == 0

async def test_concurrent_ingest_same_dedupe_key_creates_one_event_and_two_deliveries(db_path):
    app_a = knocker.open(db_path)
    app_a.add_endpoint(name="stripe", path="/webhooks/stripe")
    app_b = knocker.open(db_path)

    async def ingest(app, idx):
        return await asyncio.to_thread(
            lambda: app.ingest(
                endpoint="stripe",
                body=f'{{"id":"evt-concurrent","delivery":{idx}}}'.encode("utf-8"),
                headers={},
                provider_event_id="evt-concurrent",
                provider_delivery_id=f"delivery-concurrent-{idx}",
            )
        )

    first, second = await asyncio.gather(ingest(app_a, 1), ingest(app_b, 2))

    event_ids = {first.event_id, second.event_id}
    assert len(event_ids) == 1
    assert {first.duplicate, second.duplicate} == {False, True}
    event_id = _require_event_id(first if first.event_id is not None else second)
    assert len(app_a.list_deliveries(event_id=event_id)) == 2
    rows = app_a.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app_a.queue_name])
    assert rows[0]["c"] == 1

async def test_multiple_workers_process_each_event_once(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
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

async def test_idle_worker_wakes_on_wal_commit_without_waiting_for_poll_timeout(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
    handled = asyncio.Event()

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        handled.set()

    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop, idle_poll_s=5.0))
    await asyncio.sleep(0.2)

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-wal-wakeup"}',
        headers={},
        provider_event_id="evt-wal-wakeup",
    )
    event_id = _require_event_id(result)

    await asyncio.wait_for(handled.wait(), timeout=1.5)
    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)
    assert app.get_event(event_id).status == "handled"

async def test_burst_ingest_then_worker_drain_smoke(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        pass

    total = 1000
    for idx in range(total):
        app.ingest(
            endpoint="stripe",
            body=f'{{"id":"evt-burst-{idx}"}}'.encode("utf-8"),
            headers={},
            provider_event_id=f"evt-burst-{idx}",
        )

    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop, idle_poll_s=0.001))
    deadline = asyncio.get_running_loop().time() + 10.0
    while asyncio.get_running_loop().time() < deadline:
        rows = app.db.query("SELECT COUNT(*) AS c FROM knocker_events WHERE status='handled'")
        if rows[0]["c"] == total:
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(worker, timeout=3.0)
    rows = app.db.query("SELECT COUNT(*) AS c FROM knocker_events WHERE status='handled'")
    assert rows[0]["c"] == total

async def test_duplicate_valid_deliveries_are_auditable_without_mutating_event(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

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

    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
    assert rows[0]["c"] == 1

async def test_failures_retry_then_dead_letter(db_path):
    app = knocker.open(db_path, max_attempts=2)
    app.add_endpoint(name="slack", path="/webhooks/slack")

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

async def test_missing_handler_is_dead_lettered_not_ignored(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

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
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
    errors = []

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
    worker = asyncio.create_task(
        app.run_worker(worker_id="expiring-worker", on_error=lambda exc: errors.append(str(exc)))
    )

    with pytest.raises(RuntimeError, match="claim no longer valid"):
        await asyncio.wait_for(worker, timeout=5.0)

    event = app.get_event(event_id)
    state = app.worker_states()[0]
    assert event.status == "received"
    assert event.attempt_count == 0
    assert state == knocker.WorkerState(
        worker_id="expiring-worker",
        running=False,
        current_event_id=None,
        last_error=errors[0],
    )
    assert "claim no longer valid" in errors[0]

    live_rows = app.db.query(
        "SELECT state FROM _honker_live WHERE queue=?",
        [app.queue_name],
    )
    assert live_rows[0]["state"] == "processing"

async def test_expired_claim_can_be_reclaimed_and_handler_may_run_twice(db_path):
    app = knocker.open(db_path, visibility_timeout_s=1, max_attempts=3)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
    with app.db.transaction() as tx:
        tx.execute("CREATE TABLE handled_after_reclaim (event_id INTEGER PRIMARY KEY)")

    calls = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        calls.append(event.id)
        if len(calls) == 1:
            time.sleep(2)
        tx.query("INSERT OR IGNORE INTO handled_after_reclaim (event_id) VALUES (?)", [event.id])

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-reclaim"}',
        headers={},
        provider_event_id="evt-reclaim",
    )
    event_id = _require_event_id(result)

    first_worker = asyncio.create_task(app.run_worker(worker_id="slow-worker"))
    with pytest.raises(RuntimeError, match="claim no longer valid"):
        await asyncio.wait_for(first_worker, timeout=5.0)

    assert app.get_event(event_id).status == "received"
    stop = asyncio.Event()
    second_worker = asyncio.create_task(
        app.run_worker(worker_id="reclaim-worker", stop_event=stop, idle_poll_s=0.01)
    )
    await _wait_for_status(app, event_id, "handled")
    stop.set()
    await asyncio.wait_for(second_worker, timeout=3.0)

    rows = app.db.query("SELECT COUNT(*) AS c FROM handled_after_reclaim WHERE event_id=?", [event_id])
    assert calls == [event_id, event_id]
    assert rows[0]["c"] == 1

async def test_async_on_error_callback_is_awaited_and_user_raise_shadows_original(db_path):
    app = knocker.open(db_path, visibility_timeout_s=1, max_attempts=3)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
    awaited = []

    @app.handle(endpoint="stripe")
    def slow_handle(event, tx):
        time.sleep(2)

    async def on_error(exc):
        await asyncio.sleep(0)
        awaited.append(str(exc))

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-async-on-error"}',
        headers={},
        provider_event_id="evt-async-on-error",
    )
    _require_event_id(result)

    worker = asyncio.create_task(
        app.run_worker(worker_id="async-on-error-worker", on_error=on_error)
    )
    with pytest.raises(RuntimeError, match="claim no longer valid"):
        await asyncio.wait_for(worker, timeout=5.0)
    assert len(awaited) == 1
    assert "claim no longer valid" in awaited[0]
    state = app.worker_states()[0]
    assert state.worker_id == "async-on-error-worker"
    assert state.last_error is not None

    # Same setup, but the async on_error itself raises. The user's raise must
    # shadow the original worker exception.
    app2_db_path = str(Path(db_path).with_name("app2.db"))
    app2 = knocker.open(app2_db_path, visibility_timeout_s=1, max_attempts=3)
    app2.add_endpoint(name="stripe2", path="/webhooks/stripe2")

    @app2.handle(endpoint="stripe2")
    def slow_handle_2(event, tx):
        time.sleep(2)

    async def on_error_raises(exc):
        await asyncio.sleep(0)
        raise RuntimeError("on_error itself failed")

    app2.ingest(
        endpoint="stripe2",
        body=b'{"id":"evt-on-error-raises"}',
        headers={},
        provider_event_id="evt-on-error-raises",
    )
    worker2 = asyncio.create_task(
        app2.run_worker(worker_id="on-error-raises-worker", on_error=on_error_raises)
    )
    with pytest.raises(RuntimeError, match="on_error itself failed"):
        await asyncio.wait_for(worker2, timeout=5.0)

async def test_multiple_concurrent_workers_have_independent_state(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    handled = []
    errors_a = []
    errors_b = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        handled.append(event.id)

    event_ids = []
    for idx in range(6):
        result = app.ingest(
            endpoint="stripe",
            body=f'{{"id":"evt-iso-{idx}"}}'.encode("utf-8"),
            headers={},
            provider_event_id=f"evt-iso-{idx}",
        )
        event_ids.append(_require_event_id(result))

    stop = asyncio.Event()
    workers = [
        asyncio.create_task(
            app.run_worker(
                worker_id="worker-a",
                stop_event=stop,
                idle_poll_s=0.01,
                on_error=lambda exc: errors_a.append(str(exc)),
            )
        ),
        asyncio.create_task(
            app.run_worker(
                worker_id="worker-b",
                stop_event=stop,
                idle_poll_s=0.01,
                on_error=lambda exc: errors_b.append(str(exc)),
            )
        ),
    ]

    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if all(app.get_event(eid).status == "handled" for eid in event_ids):
            break
        await asyncio.sleep(0.05)

    stop.set()
    await asyncio.wait_for(asyncio.gather(*workers), timeout=3.0)

    states = {state.worker_id: state for state in app.worker_states()}
    assert set(states) == {"worker-a", "worker-b"}
    for worker_id, state in states.items():
        assert state.running is False, worker_id
        assert state.current_event_id is None, worker_id
        assert state.last_error is None, worker_id
    assert sorted(handled) == event_ids
    assert errors_a == []
    assert errors_b == []

async def test_one_worker_failing_does_not_taint_other_workers_state(db_path):
    app = knocker.open(db_path, visibility_timeout_s=1, max_attempts=3)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    calls = {"count": 0}

    @app.handle(endpoint="stripe")
    def first_call_slow(event, tx):
        calls["count"] += 1
        if calls["count"] == 1:
            time.sleep(2)

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-iso-mixed"}',
        headers={},
        provider_event_id="evt-iso-mixed",
    )
    event_id = _require_event_id(result)

    # Worker-a holds the loop past visibility_timeout and fails through on_error.
    errors_a = []
    worker_a = asyncio.create_task(
        app.run_worker(
            worker_id="worker-a",
            on_error=lambda exc: errors_a.append(str(exc)),
        )
    )
    with pytest.raises(RuntimeError, match="claim no longer valid"):
        await asyncio.wait_for(worker_a, timeout=5.0)

    states = {state.worker_id: state for state in app.worker_states()}
    assert states["worker-a"].running is False
    assert states["worker-a"].last_error is not None
    assert "claim no longer valid" in states["worker-a"].last_error
    assert errors_a and "claim no longer valid" in errors_a[0]

    # Worker-b reclaims the event, runs the handler successfully (no sleep on
    # second call), and finishes with a clean state independent of worker-a.
    stop_b = asyncio.Event()
    errors_b = []
    worker_b = asyncio.create_task(
        app.run_worker(
            worker_id="worker-b",
            stop_event=stop_b,
            idle_poll_s=0.01,
            on_error=lambda exc: errors_b.append(str(exc)),
        )
    )
    await _wait_for_status(app, event_id, "handled", timeout=5.0)
    stop_b.set()
    await asyncio.wait_for(worker_b, timeout=3.0)

    states = {state.worker_id: state for state in app.worker_states()}
    # Worker-a's terminal error stays pinned even after worker-b succeeded.
    assert states["worker-a"].running is False
    assert states["worker-a"].last_error is not None
    assert "claim no longer valid" in states["worker-a"].last_error
    # Worker-b's state is clean.
    assert states["worker-b"].running is False
    assert states["worker-b"].current_event_id is None
    assert states["worker-b"].last_error is None
    assert errors_b == []
    assert calls["count"] == 2

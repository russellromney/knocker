import asyncio

import knocker
import pytest

from tests.helpers import require_event_id as _require_event_id


async def _wait_for_audit_count(app: knocker.Knocker, expected: int, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if len(app.list_prune_audits(limit=50)) >= expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"prune audit count did not reach {expected}")


async def _wait_until_missing(getter, identifier: int, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            getter(identifier)
        except KeyError:
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"row {identifier} was not deleted in time")


async def _wait_for_scheduler_task(app: knocker.Knocker, name: str, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        rows = app.db.query(
            "SELECT COUNT(*) AS c FROM _honker_scheduler_tasks WHERE name=?",
            [name],
        )
        if int(rows[0]["c"]) == 1:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"scheduler task {name!r} was not registered")


async def test_run_retention_prunes_events_and_orphans_and_writes_audit_rows(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")
    app.add_endpoint(
        name="generic",
        path="/webhooks/generic",
        verification={
            "kind": "hmac-sha256",
            "secret": "topsecret",
            "header": "x-signature",
        },
    )

    handled = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-retention"}',
        headers={},
        provider_event_id="evt-retention",
    )
    event_id = _require_event_id(handled)
    orphan = app.receive(
        endpoint="generic",
        body=b'{"id":"orphan-retention"}',
        headers={"X-Signature": "bad"},
    )

    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, event_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [100, event_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE id=?", [100, orphan.delivery_id])

    stop = asyncio.Event()
    task = asyncio.create_task(
        app.run_retention(
            knocker.RetentionPolicy(
                interval_s=1,
                event_older_than_s=1,
                event_limit=10,
                orphan_deliveries_older_than_s=1,
                orphan_deliveries_limit=10,
            ),
            stop_event=stop,
        )
    )
    await _wait_for_scheduler_task(app, "knocker-retention:knocker.events")

    await _wait_until_missing(app.get_event, event_id)
    await _wait_until_missing(app.get_delivery, orphan.delivery_id)
    stop.set()
    await task

    assert app.list_deliveries() == []

    audits_by_kind = {audit.kind: audit for audit in app.list_prune_audits(limit=10)}
    assert set(audits_by_kind) == {"prune_events", "prune_orphan_deliveries"}
    assert audits_by_kind["prune_events"].events_pruned == 1
    assert audits_by_kind["prune_events"].deliveries_pruned == 1
    assert audits_by_kind["prune_orphan_deliveries"].deliveries_pruned == 1


async def test_run_retention_no_op_still_writes_audit_row(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    stop = asyncio.Event()
    task = asyncio.create_task(
        app.run_retention(
            knocker.RetentionPolicy(
                interval_s=1,
                event_older_than_s=1,
                event_limit=10,
            ),
            stop_event=stop,
        )
    )
    await _wait_for_audit_count(app, 1, timeout=2.0)
    stop.set()
    await task

    audits = app.list_prune_audits(limit=10)
    assert len(audits) == 1
    assert audits[0].kind == "prune_events"
    assert audits[0].events_pruned == 0
    assert audits[0].deliveries_pruned == 0
    assert audits[0].attempts_pruned == 0
    assert audits[0].live_jobs_pruned == 0


async def test_run_retention_stop_prevents_future_iterations(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    handled = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-stop"}',
        headers={},
        provider_event_id="evt-stop",
    )
    event_id = _require_event_id(handled)

    with app.db.transaction() as tx:
        tx.query("SELECT knocker_mark_handled(?, ?)", [event_id, 0])
        tx.query("UPDATE knocker_events SET received_at=? WHERE id=?", [100, event_id])
        tx.query("UPDATE knocker_deliveries SET received_at=? WHERE event_id=?", [100, event_id])

    stop = asyncio.Event()
    task = asyncio.create_task(
        app.run_retention(
            knocker.RetentionPolicy(
                interval_s=1,
                event_older_than_s=1,
                event_limit=10,
            ),
            stop_event=stop,
        )
    )

    await _wait_for_audit_count(app, 1)
    stop.set()
    await task
    await asyncio.sleep(0.05)

    audits = app.list_prune_audits(limit=10)
    assert len(audits) == 1


async def test_run_retention_requires_at_least_one_enabled_prune_path(db_path):
    app = knocker.open(db_path)

    stop = asyncio.Event()
    stop.set()
    with pytest.raises(ValueError, match="at least one automated prune path"):
        await app.run_retention(
            knocker.RetentionPolicy(
                interval_s=1,
                event_older_than_s=None,
                orphan_deliveries_older_than_s=None,
            ),
            stop_event=stop,
        )

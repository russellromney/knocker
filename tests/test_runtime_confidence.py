import asyncio
import gc
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time

import knocker
import pytest

from tests.helpers import require_event_id as _require_event_id, wait_for_status as _wait_for_status


pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGKILL-style crash tests are Unix-only.",
)


def _spawn_python(script: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def _wait_for_line(proc: subprocess.Popen, expected: str, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            err = proc.stderr.read() if proc.stderr else ""
            raise AssertionError(
                f"subprocess exited before emitting {expected!r}: "
                f"rc={proc.returncode}, stdout={out!r}, stderr={err!r}"
            )
        line = proc.stdout.readline() if proc.stdout else ""
        if not line:
            time.sleep(0.01)
            continue
        if line.strip() == expected:
            return
    raise AssertionError(f"timed out waiting for {expected!r}")


def _sigkill_and_reap(proc: subprocess.Popen) -> None:
    os.kill(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def _integrity_check(db_path: str) -> str:
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return row[0] if row else ""
    finally:
        conn.close()


async def test_sigkill_mid_ingest_tx_leaves_no_false_success_and_post_crash_flow_works(db_path):
    seed = knocker.open(db_path)
    seed.add_endpoint(name="stripe", path="/webhooks/stripe")
    del seed
    gc.collect()

    script = textwrap.dedent(
        f"""
        import json
        import time
        import knocker

        app = knocker.open({db_path!r})
        with app.db.transaction() as tx:
            tx.query(
                "SELECT knocker_ingest(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) AS result_json",
                [
                    "stripe",
                    "POST",
                    json.dumps({{}}, sort_keys=True),
                    b'{{"id":"evt-killed"}}',
                    json.dumps({{}}, sort_keys=True),
                    1,
                    None,
                    "evt-killed",
                    "delivery-killed",
                    "checkout.session.completed",
                    None,
                    "knocker.events",
                    3,
                ],
            )
            print("READY", flush=True)
            time.sleep(60)
        """
    )

    proc = _spawn_python(script)
    try:
        _wait_for_line(proc, "READY")
        _sigkill_and_reap(proc)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()

    assert _integrity_check(db_path) == "ok"

    app = knocker.open(db_path)
    event_rows = app.db.query(
        "SELECT COUNT(*) AS c FROM knocker_events WHERE provider_event_id='evt-killed'"
    )
    delivery_rows = app.db.query(
        "SELECT COUNT(*) AS c FROM knocker_deliveries WHERE provider_delivery_id='delivery-killed'"
    )
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
    assert event_rows[0]["c"] == 0
    assert delivery_rows[0]["c"] == 0
    assert live_rows[0]["c"] == 0

    seen = []

    @app.handle(endpoint="stripe")
    def handle(event, tx):
        seen.append(event.id)

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-post-crash"}',
        headers={},
        provider_event_id="evt-post-crash",
        provider_delivery_id="delivery-post-crash",
        event_type="checkout.session.completed",
    )
    event_id = _require_event_id(result)

    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop, idle_poll_s=0.01))
    try:
        await _wait_for_status(app, event_id, "handled")
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=3.0)

    assert seen == [event_id]


async def test_committed_ingress_survives_fresh_process_reopen_and_is_processable(db_path):
    seed = knocker.open(db_path)
    seed.add_endpoint(name="github", path="/webhooks/github")
    del seed
    gc.collect()

    script = textwrap.dedent(
        f"""
        import knocker

        app = knocker.open({db_path!r})
        result = app.ingest(
            endpoint="github",
            body=b'{{"id":"evt-committed"}}',
            headers={{}},
            provider_event_id="evt-committed",
            provider_delivery_id="delivery-committed",
            event_type="push",
        )
        print(result.event_id, flush=True)
        """
    )

    proc = _spawn_python(script)
    stdout, stderr = proc.communicate(timeout=5)
    assert proc.returncode == 0, stderr
    event_id = int(stdout.strip())

    app = knocker.open(db_path)
    event = app.get_event(event_id)
    deliveries = app.list_deliveries(event_id=event_id)
    live_rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue_name])
    assert event.status == "received"
    assert len(deliveries) == 1
    assert deliveries[0].provider_delivery_id == "delivery-committed"
    assert live_rows[0]["c"] == 1

    seen = []

    @app.handle(endpoint="github", event_type="push")
    def handle(event, tx):
        seen.append((event.id, event.provider_delivery_id))

    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop, idle_poll_s=0.01))
    try:
        await _wait_for_status(app, event_id, "handled")
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=3.0)

    assert seen == [(event_id, "delivery-committed")]


async def test_expired_claim_can_be_reclaimed_after_reopen(db_path):
    app = knocker.open(db_path, visibility_timeout_s=1)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    result = app.ingest(
        endpoint="stripe",
        body=b'{"id":"evt-reopen-claim"}',
        headers={},
        provider_event_id="evt-reopen-claim",
        provider_delivery_id="delivery-reopen-claim",
        event_type="checkout.session.completed",
    )
    event_id = _require_event_id(result)

    claimed = app._queue.claim_batch("claimer-a", 1)
    assert len(claimed) == 1

    del app
    gc.collect()
    time.sleep(1.2)

    reopened = knocker.open(db_path, visibility_timeout_s=1)
    seen = []

    @reopened.handle(endpoint="stripe", event_type="checkout.session.completed")
    def handle(event, tx):
        seen.append(event.id)

    stop = asyncio.Event()
    worker = asyncio.create_task(reopened.run_worker(stop_event=stop, idle_poll_s=0.01))
    try:
        await _wait_for_status(reopened, event_id, "handled")
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=3.0)

    assert seen == [event_id]

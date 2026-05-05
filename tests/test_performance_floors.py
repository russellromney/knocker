import asyncio
import time

import knocker


def test_ingest_throughput_floor(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    total = 1_000
    start = time.perf_counter()
    for idx in range(total):
        app.ingest(
            endpoint="stripe",
            body=f'{{"id":"evt-perf-{idx}"}}'.encode("utf-8"),
            headers={},
            provider_event_id=f"evt-perf-{idx}",
            provider_delivery_id=f"delivery-perf-{idx}",
            event_type="checkout.session.completed",
        )
    elapsed = time.perf_counter() - start

    assert elapsed < 6.0, (
        f"durable ingest of {total} events took {elapsed:.3f}s (floor: 6.0s). "
        f"Likely regression in knocker_ingest, endpoint lookup, or enqueue."
    )
    rows = app.db.query("SELECT COUNT(*) AS c FROM knocker_events")
    assert rows[0]["c"] == total


async def test_worker_drain_throughput_floor(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    @app.handle(endpoint="stripe", event_type="checkout.session.completed")
    def handle(event, tx):
        return None

    total = 1_000
    for idx in range(total):
        app.ingest(
            endpoint="stripe",
            body=f'{{"id":"evt-drain-{idx}"}}'.encode("utf-8"),
            headers={},
            provider_event_id=f"evt-drain-{idx}",
            provider_delivery_id=f"delivery-drain-{idx}",
            event_type="checkout.session.completed",
        )

    stop = asyncio.Event()
    worker = asyncio.create_task(app.run_worker(stop_event=stop, idle_poll_s=0.001))
    start = time.perf_counter()
    try:
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            rows = app.db.query("SELECT COUNT(*) AS c FROM knocker_events WHERE status='handled'")
            if rows[0]["c"] == total:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=3.0)
    elapsed = time.perf_counter() - start

    rows = app.db.query("SELECT COUNT(*) AS c FROM knocker_events WHERE status='handled'")
    assert rows[0]["c"] == total
    assert elapsed < 6.0, (
        f"worker drain of {total} no-op events took {elapsed:.3f}s (floor: 6.0s). "
        f"Likely regression in claim/dispatch/ack path."
    )


async def test_mixed_load_throughput_floor_one_producer_two_workers(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(name="stripe", path="/webhooks/stripe")

    @app.handle(endpoint="stripe", event_type="checkout.session.completed")
    def handle(event, tx):
        return None

    total = 1_000

    def produce() -> None:
        for idx in range(total):
            app.ingest(
                endpoint="stripe",
                body=f'{{"id":"evt-mixed-{idx}"}}'.encode("utf-8"),
                headers={},
                provider_event_id=f"evt-mixed-{idx}",
                provider_delivery_id=f"delivery-mixed-{idx}",
                event_type="checkout.session.completed",
            )

    stop = asyncio.Event()
    workers = [
        asyncio.create_task(app.run_worker(worker_id=f"worker-{idx}", stop_event=stop, idle_poll_s=0.001))
        for idx in range(2)
    ]
    start = time.perf_counter()
    try:
        await asyncio.to_thread(produce)
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            rows = app.db.query("SELECT COUNT(*) AS c FROM knocker_events WHERE status='handled'")
            if rows[0]["c"] == total:
                break
            await asyncio.sleep(0.05)
    finally:
        stop.set()
        await asyncio.wait_for(asyncio.gather(*workers), timeout=3.0)
    elapsed = time.perf_counter() - start

    rows = app.db.query("SELECT COUNT(*) AS c FROM knocker_events WHERE status='handled'")
    assert rows[0]["c"] == total
    assert elapsed < 8.0, (
        f"mixed load (1 producer / 2 workers) for {total} no-op events took {elapsed:.3f}s "
        f"(floor: 8.0s). Likely regression in batched claim or worker contention behavior."
    )

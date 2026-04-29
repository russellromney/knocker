<h1 align="center">
  <img src="./logo-transparent.png" alt="" width="180" /><br/>knocker
</h1>

`knocker` is a Python-first inbound webhook inbox on SQLite. It stores every HTTP receipt before returning success, dedupes provider retries into durable `Event` rows, and runs handlers later in the same process using [Honker](https://honker.dev), the SQLite-backed durable queue this project depends on.

Knocker is for apps that already have an HTTP server, a SQLite database, and local business logic. It is not a hosted webhook relay, not a broker, and not a framework adapter package.

> Pre-0.1.0. API may change.

Published package name: `knockerlite`. Python import name: `knocker`.

Webhooks look simple until you need to answer the boring production questions: did we store the request before returning `2xx`; did a provider retry create duplicate work; why did this event not run; can an operator replay it without guessing from logs?

Knocker takes the approach that if SQLite is already your app database, webhook ingress should live in the same file. Your route reads the raw request body and calls `receive(...)`. Knocker verifies, stores a `Delivery`, creates or correlates a deduped `Event`, enqueues durable work, and returns a status code. Later, a local worker dispatches the stored event to your handler.

Docs live at [knocker.dev](https://knocker.dev).

## At a glance

```bash
pip install knockerlite
```

```python
import asyncio
import knocker

app = knocker.open("app.db")

app.add_endpoint(
    name="stripe",
    path="/webhooks/stripe",
    provider="stripe",
    secrets=["whsec_123"],
)

@app.handle(endpoint="stripe", event_type="checkout.session.completed")
def handle_checkout(event, tx):
    # Business writes through tx commit atomically with Knocker's handled
    # transition and queue ack.
    tx.query("INSERT INTO handled_events (event_id) VALUES (?)", [event.id])

def webhook_route(raw_body_bytes, headers, query_params):
    result = app.receive(
        endpoint="stripe",
        body=raw_body_bytes,
        headers=headers,
        query=query_params,
    )
    # Return an HTTP response with this status using your framework.
    return response_with_status(result.status_code)

# Run app.run_worker(...) as a background task in your async app.
```

`receive(...)` is the binding-owned verified-ingress path. The lower-level `ingest(...)` method is trusted ingress for callers that already know the verification outcome.

## What you can use it for

- Store every inbound webhook receipt durably before returning provider success
- Deduplicate provider retries into one event-level processing unit
- Verify requests with Stripe or generic HMAC-SHA256, including overlapping active secrets for rotation
- Keep an audit trail of valid, invalid, duplicate, and orphaned deliveries
- Run synchronous handlers later with retries, dead-lettering, replay, and requeue
- Commit handler business writes atomically with Knocker's handled transition and queue ack
- Inspect events and deliveries from Python before building app-specific admin routes
- Prune handled, ignored, and orphan-delivery rows explicitly when you choose

## One route

Knocker intentionally does not ship framework adapters. Framework-specific code is small and should live in your app.

```python
from fastapi import Request, Response

@api.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    result = app.receive(
        endpoint="stripe",
        body=await request.body(),
        headers=dict(request.headers),
        query=dict(request.query_params),
    )
    return Response(status_code=result.status_code)
```

See [Framework integration](https://knocker.dev/guides/framework-integration/) for FastAPI, Starlette, Flask, and generic route recipes.

## Operator surface

```python
events = app.list_events(endpoint="stripe", since=1700000000, limit=50)
invalid = app.list_deliveries(signature_valid=False, orphaned=True, limit=50)

delivery = app.get_delivery(result.delivery_id)
event_deliveries = app.list_deliveries(event_id=result.event_id)

app.ignore(result.event_id)
app.replay(result.event_id)          # handled, failed, dead, ignored
app.requeue(result.event_id)         # failed, dead, ignored
app.replay_delivery(delivery.id)     # explicit: process this stored delivery body

summary = app.prune_events(statuses=["handled", "ignored"], older_than=1700000000, limit=100)
orphans = app.prune_orphan_deliveries(older_than=1700000000, limit=100)
```

Provider redelivery of an already-dead event is audit-only: Knocker stores the new `Delivery` but does not mutate or enqueue the existing `Event`. Recovery is explicit via `requeue(...)` or `replay_delivery(...)`.

## Deliberately not built

- Hosted webhook relay or control plane
- Framework adapters for FastAPI, Flask, Django, Rails, Express, etc.
- Generic queue API; Honker owns that layer
- Operator API server or HTML admin
- Exactly-once side effects
- Automatic retention jobs

## Repo layout

- `knocker-honker/`: Rust core for Knocker-owned SQLite semantics
- `knocker-extension/`: loadable SQLite extension for cross-language contract testing
- `packages/knocker/`: Python binding
- `packages/knocker-node/`: Node smoke-test binding for the shared SQLite contract
- `site/`: Astro/Starlight docs site for `knocker.dev`
- `SYSTEM.md`: current English model of the system
- `.intent/phases/`: spec diffs, plans, reviews, and commit records

## Development

Start with the local shell setup:

```bash
source ~/.zshrc
```

Useful commands:

```bash
make test
make test-rust
make test-python
make test-node
npm --prefix site run build
```

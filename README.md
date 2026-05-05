<h1 align="center">
  <img src="./logo-transparent.png" alt="" width="180" /><br/>knocker
</h1>

`knocker` is an embeddable inbound webhook inbox for apps that already run on
SQLite.

It stores every HTTP receipt before returning success, dedupes provider retries
into durable `Event` rows, and lets workers in the same process or another
process handle those events later using [Honker](https://honker.dev), the
SQLite-backed durable queue this project depends on.

Webhooks are cute until the provider retries, your handler half-succeeds, and
the only record of what happened is a log line from yesterday. Knocker gives
that mess a home: receipts, deduped events, durable work, retries,
dead-lettering, replay, and operator reads, all in the same SQLite file as your
app.

Knocker's durable semantics live in shared Rust/SQLite code. The repo ships
bindings over the same SQLite contract for Bun, Elixir, Go, Node, Python, and
Ruby.

Knocker is not a hosted webhook relay, broker, or framework adapter package. It
is the small durable layer between "HTTP request arrived" and "my app did the
business thing."

Docs live at [knocker.dev](https://knocker.dev).

## Python quickstart

Install the Python package:

```bash
pip install knockerlite
```

Open your app database, register an endpoint, and wire a handler. This example
uses FastAPI, but Knocker only needs the raw request body, headers, and query
params.

```python
import json

from fastapi import FastAPI, Request, Response

import knocker

api = FastAPI()
webhooks = knocker.open("app.db")

stripe = webhooks.endpoint(
    "stripe",
    path="/webhooks/stripe",
    provider="stripe",
    secrets=["whsec_123"],
)

@stripe.handle("checkout.session.completed")
def fulfill_checkout(event, tx):
    payload = json.loads(event.body)
    session_id = payload["data"]["object"]["id"]

    tx.query(
        """
        CREATE TABLE IF NOT EXISTS fulfilled_checkouts (
            session_id TEXT PRIMARY KEY,
            event_id INTEGER NOT NULL
        )
        """
    )
    tx.query(
        """
        INSERT OR IGNORE INTO fulfilled_checkouts (session_id, event_id)
        VALUES (?, ?)
        """,
        [session_id, event.id],
    )

@api.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    body = await request.body()
    result = stripe.receive(
        body=body,
        headers=dict(request.headers),
        query=dict(request.query_params),
    )
    return Response(status_code=result.status_code)
```

That route verifies the provider signature, stores an append-only `Delivery`,
creates or correlates a deduped `Event`, enqueues durable work, and gives you
the HTTP status to return. A duplicate retry stores another delivery row without
creating duplicate business work.

Run a worker against the same SQLite file to dispatch stored events:

```python
import asyncio

import knocker

webhooks = knocker.open("app.db")

@webhooks.handle(endpoint="stripe", event_type="checkout.session.completed")
def fulfill_checkout(event, tx):
    tx.query(
        "CREATE TABLE IF NOT EXISTS fulfilled_events (event_id INTEGER PRIMARY KEY)"
    )
    tx.query(
        "INSERT OR IGNORE INTO fulfilled_events (event_id) VALUES (?)",
        [event.id],
    )

asyncio.run(
    webhooks.run_worker(
        worker_id="webhooks-1",
        idle_poll_s=0.25,
    )
)
```

Handlers are synchronous by design. Database writes made through `tx` commit
atomically with Knocker's handled transition and queue ack, which is the whole
party trick.

Use the operator surface when production needs an answer:

```python
import knocker

webhooks = knocker.open("app.db")

for event in webhooks.list_events(status="dead", limit=20):
    print(event.id, event.endpoint, event.event_type, event.last_error)
    webhooks.requeue(event.id)

for delivery in webhooks.list_deliveries(signature_valid=False, limit=20):
    print(delivery.id, delivery.endpoint, delivery.signature_error)
```

Use `receive(...)` for normal verified ingress. Use `ingest(...)` only when a
trusted layer already knows the verification outcome.

## What you can use it for

- Store every inbound webhook receipt durably before returning provider success
- Deduplicate provider retries into one event-level processing unit
- Verify requests with curated built-ins for Stripe, GitHub, Shopify, Slack, Postmark, Resend, Paddle, Lemon Squeezy, Standard Webhooks/Svix, Clerk, Twilio, SendGrid, Linear, Meta, Discord, Zendesk, Intercom, HubSpot, and middleman-friendly token/basic/bearer auth shapes
- Keep an audit trail of valid, invalid, duplicate, and orphaned deliveries
- Run synchronous handlers later with retries, dead-lettering, replay, and requeue
- Commit handler business writes atomically with Knocker's handled transition and queue ack
- Inspect events and deliveries before building app-specific admin routes
- Prune handled, ignored, and orphan-delivery rows explicitly when you choose
- Run retention helpers/automation against the same SQLite file with core-owned prune semantics

## Bindings

Pick the binding for your runtime:

- [Bun](https://knocker.dev/reference/bun/)
- [Elixir](https://knocker.dev/reference/elixir/)
- [Go](https://knocker.dev/reference/go/)
- [Node](https://knocker.dev/reference/node/)
- [Python](https://knocker.dev/reference/python/)
- [Ruby](https://knocker.dev/reference/ruby/)

## Runtime packages

| Runtime | Package path | Shape |
| --- | --- | --- |
| Bun | `packages/knocker-bun` | Same shared SQLite contract as Node, adapted to Bun's SQLite runtime. |
| Elixir | `packages/knocker-elixir` | Same shared SQLite contract with Elixir handler and worker helpers. |
| Go | `packages/knocker-go` | Same shared SQLite contract with context-aware workers. |
| Node | `packages/knocker-node` | SQLite-extension binding with receive, typed handlers, workers, operators, retention, and provider verification through shared Rust code. |
| Python | `packages/knocker` | Python binding over the shared SQLite contract. |
| Ruby | `packages/knocker-ruby` | Same shared SQLite contract with Ruby-style handlers and operators. |

Bindings do not re-implement provider verification. Curated `receive(...)`
calls delegate to the shared Rust/SQLite path, so provider fixes land once and
are exercised across runtimes.

## Operator surface

Operator actions are durable SQLite operations exposed through the bindings:

Common operations include `list_events`, `list_deliveries`, `get_event`,
`get_delivery`, `ignore`, `replay`, `requeue`, `replay_delivery`,
`prune_events`, `prune_orphan_deliveries`, and prune-audit reads. See the
runtime binding pages for exact method names.

Provider redelivery of an already-dead event is audit-only: Knocker stores the new `Delivery` but does not mutate or enqueue the existing `Event`. Recovery is explicit via `requeue(...)` or `replay_delivery(...)`.

See the [Operator runbook](https://knocker.dev/guides/operator-runbook/) for the production checklist around dead events, invalid deliveries, worker failures, and pruning.

## Deliberately not built

- Hosted webhook relay or control plane
- Framework adapters for FastAPI, Flask, Django, Rails, Express, etc.
- Generic queue API; Honker owns that layer
- Operator API server or HTML admin
- Exactly-once side effects
- Hosted packages for every binding runtime

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

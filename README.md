<h1 align="center">
  <img src="./logo-transparent.png" alt="" width="180" /><br/>knocker
</h1>

`knocker` is a loadable SQLite extension plus language bindings for building an embeddable inbound webhook inbox. It stores every HTTP receipt before returning success, dedupes provider retries into durable `Event` rows, and lets workers in the same process or another process handle those events later using [Honker](https://honker.dev), the SQLite-backed durable queue this project depends on.

Knocker's durable semantics live in shared Rust/SQLite code. The repo ships bindings over the same SQLite contract for Python, Node, Bun, Ruby, Go, and Elixir.

Knocker is for apps that already have an HTTP server, a SQLite database, and local business logic. It is not a hosted webhook relay, not a broker, and not a framework adapter package.

Webhooks look simple until you need to answer the boring production questions: did we store the request before returning `2xx`; did a provider retry create duplicate work; why did this event not run; can an operator replay it without guessing from logs?

Knocker takes the approach that if SQLite is already your app database, webhook ingress should live in the same file. Your route reads the raw request body and calls `receive(...)`. Knocker verifies, stores a `Delivery`, creates or correlates a deduped `Event`, enqueues durable work, and returns a status code. Later, a worker in the same process or another process dispatches the stored event to your handler.

Docs live at [knocker.dev](https://knocker.dev).

## At a glance

Knocker ships as one SQLite extension contract with language bindings for Python, Node, Bun, Ruby, Go, and Elixir. This is the smallest Python-shaped example; the same durable receive/worker/operator contract is available from the other bindings too.

```bash
pip install knockerlite
```

```python
import asyncio
import knocker

webhooks = knocker.open("app.db")
stripe = webhooks.endpoint(
    "stripe",
    path="/webhooks/stripe",
    provider="stripe",
    secrets=["whsec_123"],
)

@stripe.handle("checkout.session.completed")
def handle_checkout(event, tx):
    # Business writes through tx commit atomically with Knocker's handled
    # transition and queue ack.
    tx.query("INSERT INTO handled_events (event_id) VALUES (?)", [event.id])
```

```python
result = stripe.receive(
    body=raw_body_bytes,
    headers=headers,
    query=query_params,
)

# Return an HTTP response with this status using your framework.
return response_with_status(result.status_code)
```

```python
await webhooks.run_worker()
```

`receive(...)` is the normal verified-ingress path. Curated provider verification is shared by the SQLite/Rust layer across bindings; Python also supports app-local provider instances. The lower-level `ingest(...)` method is trusted ingress for callers that already know the verification outcome. `webhooks.endpoint(...)` is the simpler Python endpoint-local helper; `add_endpoint(...)` and `@webhooks.handle(...)` remain available when you prefer the more explicit shape.

For Node, Bun, Ruby, Go, and Elixir examples, see [SQLite bindings](https://knocker.dev/reference/sqlite-bindings/).

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

| Runtime | Package path | Shape |
| --- | --- | --- |
| Python | `packages/knocker` | Full Python package with endpoint-local helpers, app-local providers, workers, operators, and retention automation. |
| Node | `packages/knocker-node` | SQLite-extension binding with receive, typed handlers, workers, operators, retention, and provider verification through shared Rust code. |
| Bun | `packages/knocker-bun` | Same shared SQLite contract as Node, adapted to Bun's SQLite runtime. |
| Ruby | `packages/knocker-ruby` | Same shared SQLite contract with Ruby-style handlers and operators. |
| Go | `packages/knocker-go` | Same shared SQLite contract with context-aware workers. |
| Elixir | `packages/knocker-elixir` | Same shared SQLite contract with Elixir handler and worker helpers. |

The non-Python bindings do not re-implement provider verification. Curated `receive(...)` calls delegate to the shared Rust/SQLite `knocker_receive(...)` path, so provider fixes land once and are exercised across runtimes.

## One route

Knocker intentionally does not ship framework adapters. Framework-specific code is small and should live in your app.

```python
from fastapi import Request, Response

@api.post("/webhooks/stripe")
async def stripe_webhook(request: Request):
    result = stripe.receive(
        body=await request.body(),
        headers=dict(request.headers),
        query=dict(request.query_params),
    )
    return Response(status_code=result.status_code)
```

See [Framework integration](https://knocker.dev/guides/framework-integration/) for FastAPI, Starlette, Flask, and generic route recipes.

## Operator surface

Operator actions are durable SQLite operations exposed through the bindings:

```python
events = webhooks.list_events(endpoint="stripe", since=1700000000, limit=50)
invalid = webhooks.list_deliveries(signature_valid=False, orphaned=True, limit=50)

delivery = webhooks.get_delivery(result.delivery_id)
event_deliveries = webhooks.list_deliveries(event_id=result.event_id)

webhooks.ignore(result.event_id)
webhooks.replay(result.event_id)          # handled, failed, dead, ignored
webhooks.requeue(result.event_id)         # failed, dead, ignored
webhooks.replay_delivery(delivery.id)     # explicit: process this stored delivery body

summary = webhooks.prune_events(statuses=["handled", "ignored"], older_than=1700000000, limit=100)
orphans = webhooks.prune_orphan_deliveries(older_than=1700000000, limit=100)
```

Provider redelivery of an already-dead event is audit-only: Knocker stores the new `Delivery` but does not mutate or enqueue the existing `Event`. Recovery is explicit via `requeue(...)` or `replay_delivery(...)`.

See the [Operator runbook](https://knocker.dev/guides/operator-runbook/) for the production checklist around dead events, invalid deliveries, worker failures, and pruning.

## Deliberately not built

- Hosted webhook relay or control plane
- Framework adapters for FastAPI, Flask, Django, Rails, Express, etc.
- Generic queue API; Honker owns that layer
- Operator API server or HTML admin
- Exactly-once side effects
- Hosted packages for every binding runtime

## Repo layout

- `knocker-core/`: Rust core for Knocker-owned SQLite semantics
- `knocker-extension/`: loadable SQLite extension for cross-language contract testing
- `packages/knocker/`: Python binding
- `packages/knocker-node/`: Node binding over the shared SQLite contract
- `packages/knocker-bun/`: Bun binding over the shared SQLite contract
- `packages/knocker-ruby/`: Ruby binding over the shared SQLite contract
- `packages/knocker-go/`: Go binding over the shared SQLite contract
- `packages/knocker-elixir/`: Elixir binding over the shared SQLite contract
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

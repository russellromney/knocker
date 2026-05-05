# knocker

Python bindings for Knocker, an embeddable inbound webhook inbox on SQLite.

Knocker stores every HTTP receipt before returning success, dedupes provider retries into durable `Event` rows, and runs handlers later in the same process or another process using [Honker](https://honker.dev), the SQLite-backed durable queue this project depends on.

Published package name: `knockerlite`. Python import name: `knocker`.

## At a glance

```bash
pip install knockerlite
```

```python
import knocker

webhooks = knocker.open("app.db")

webhooks.add_endpoint(
    name="stripe",
    path="/webhooks/stripe",
    provider="stripe",
    secrets=["whsec_123"],
)

@webhooks.handle(endpoint="stripe", event_type="checkout.session.completed")
def handle_checkout(event, tx):
    tx.query("INSERT INTO handled_events (event_id) VALUES (?)", [event.id])

result = webhooks.receive(
    endpoint="stripe",
    body=raw_body_bytes,
    headers=headers,
    query=query_params,
)
```

Use `receive(...)` for normal verified ingress. Use `ingest(...)` only when a trusted layer already knows the verification outcome.

Knocker intentionally does not ship framework adapters. Host apps own routes, read the raw request body and headers, then call `receive(...)`; the docs include copy-paste recipes for common frameworks.

## What you can use it for

- Durable receipt storage before provider success
- Curated verification for Stripe, GitHub, Shopify, Slack, Postmark, Resend, Paddle, Lemon Squeezy, Standard Webhooks/Svix, Clerk, Twilio, SendGrid, Linear, Meta, Discord, Zendesk, Intercom, HubSpot, and middleman-friendly token/basic/bearer auth shapes
- Deduped event processing over append-only delivery rows
- Event-level retries, dead-lettering, replay, requeue, and explicit delivery replay
- Operator reads for events, deliveries, worker state, and prune audits
- Explicit pruning for handled, ignored, and orphan-delivery rows
- Retention automation backed by the shared core retention primitive

Handlers are synchronous and should stay short and DB-local. Slow outbound work belongs in app-owned follow-up jobs, and production apps should wrap `run_worker(...)` in their own restart/supervision harness.

See [knocker.dev](https://knocker.dev) for guides and the repo-level [README](https://github.com/russellromney/knocker#development) for development commands.

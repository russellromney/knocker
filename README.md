<p align="center">
  <img src="./logo-transparent.png" alt="Knocker logo" width="260" />
</p>

# Knocker

Store first. Ack fast. Process later.

Knocker is an embeddable inbound webhook inbox for apps that already have:

- an HTTP server
- a SQLite database
- business logic that wants to react to inbound events

Docs live at [knocker.dev](https://knocker.dev).

Knocker is a library, not a service. The core promise is:

- one process
- one SQLite file
- durable ingress before success
- async processing later in the same process

## What Exists Today

- `knocker-honker` Rust core with idempotent bootstrap
- Rust-backed ingest, delivery/event correlation, replay/requeue, and event lifecycle transitions
- Append-only `Delivery` rows plus deduped `Event` rows in the shared SQLite contract
- Python binding with a thin wrapper for registration, verified ingress, audit reads, and handler dispatch
- Binding-owned verification for generic HMAC-SHA256 and Stripe, including overlapping active secrets for rotation
- Node contract smoke test via a loadable SQLite extension

Still intentionally missing:

- final publish/docs/performance gate work for `0.1.0`
- automatic retention jobs and richer retention policy

## Repo Layout

- `knocker-honker/`
  Rust core for Knocker-owned SQLite semantics
- `knocker-extension/`
  Loadable SQLite extension for cross-language contract testing
- `packages/knocker/`
  Python binding
- `packages/knocker-node/`
  Node smoke-test binding
- `site/`
  Astro/Starlight docs site for `knocker.dev`
- `SYSTEM.md`
  Human-owned English model of the system
- `.intent/phases/`
  Spec diffs, plans, reviews, and commit records for meaningful changes
- `ROADMAP.md`
  Remaining implementation work
- `CHANGELOG.md`
  Completed work summary

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
```

For the docs site:

```bash
cd site
npm install
npm run dev
npm run build
```

## Example

```python
import knocker

app = knocker.open("knocker.db")
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

result = app.receive(
    endpoint="stripe",
    body=b'{"id":"evt_1"}',
    headers={"stripe-signature": "..."},
    event_type="checkout.session.completed",
    provider_event_id="evt_1",
)

assert result.status_code == 204
assert result.event_id is not None
```

`receive(...)` is the binding-owned verified-ingress path. The lower-level `ingest(...)` method is trusted ingress: it bypasses binding-owned verification and is for callers that already know the verification outcome.

Every inbound HTTP receipt is stored as a `Delivery`. Valid receipts create or correlate to an `Event`, and the worker runs later in the same process against stored events from SQLite.

Handlers are synchronous and run while Knocker holds the work transaction. Keep them short and DB-local; put slow outbound work into app-owned follow-up jobs.

The Python operator surface can list and inspect stored events and deliveries:

```python
events = app.list_events(endpoint="stripe", since=1700000000, limit=50)
invalid = app.list_deliveries(signature_valid=False, orphaned=True, limit=50)
delivery = app.get_delivery(result.delivery_id)
event_deliveries = app.list_deliveries(event_id=result.event_id)
app.ignore(result.event_id)
app.replay(result.event_id)          # handled, failed, dead, ignored
app.requeue(result.event_id)         # failed, dead, ignored
app.replay_delivery(delivery.id)     # explicit: process this stored delivery body

# Preview before prune using the existing read surface.
handled = app.list_events(status="handled", since=0, limit=50)

# Explicit retention operations are Python-first and intentionally narrow.
summary = app.prune_events(statuses=["handled", "ignored"], older_than=1700000000, limit=100)
orphans = app.prune_orphan_deliveries(older_than=1700000000, limit=100)
```

Provider redelivery of an already-dead event is audit-only: Knocker stores the new `Delivery` but does not mutate or enqueue the existing `Event`. Recovery is explicit via `requeue(...)` or `replay_delivery(...)`.

`run_worker(...)` is intentionally small. It exposes local `worker_states()` and an optional `on_error` callback, but host apps own restart/supervision policy.

## Intent

Knocker now keeps the small intent artifacts described in your intent-driven-development post:

- `SYSTEM.md` is the current English model of the system.
- `.intent/phases/001-knocker-foundation/` captures the first meaningful change record.
- `ROADMAP.md` tracks what still needs to be built.
- `CHANGELOG.md` records what has landed.

## Status

Knocker is still pre-`0.1.0`. The durable core, verified-ingress path, Python operator reads, and an explicit minimal pruning surface are in place. Automated retention, admin UI, and broader operator ergonomics are still ahead.

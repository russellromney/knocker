# knocker

Python bindings for Knocker, an embeddable inbound webhook inbox on SQLite.

Knocker stores inbound webhook requests durably before returning success, then processes them asynchronously in the same process using Honker underneath.

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
    tx.query("INSERT INTO handled_events (event_id) VALUES (?)", [event.id])

result = app.receive(
    endpoint="stripe",
    body=b'{"id":"evt_1"}',
    headers={"stripe-signature": "..."},
)
```

## Development

See the repo-level docs for the current baseline and remaining work:

- [`README.md`](../../README.md)
- [`SYSTEM.md`](../../SYSTEM.md)
- [`ROADMAP.md`](../../ROADMAP.md)
- [`CHANGELOG.md`](../../CHANGELOG.md)

Knocker is the webhook product in the Honker family:

- `honker` owns durable async queue mechanics
- `knocker-honker` owns webhook-specific schema and SQLite operations
- `knocker` owns webhook semantics for single-machine SQLite apps

The point is simple:

- one app process
- one SQLite file
- store first
- ack fast
- process later

Knocker is not a hosted control plane and not a generic queue wrapper.

Use `receive(...)` for the binding-owned verified-ingress path. Use `ingest(...)` when you want the lower-level durable contract directly.

Every inbound request is stored as a `Delivery`, and valid requests create or correlate to a stored `Event`. The Python operator surface exposes stable audit/debug reads and event-level recovery actions:

```python
events = app.list_events(endpoint="stripe", since=1700000000, limit=50)
invalid = app.list_deliveries(signature_valid=False, orphaned=True, limit=50)
delivery = app.get_delivery(result.delivery_id)
deliveries = app.list_deliveries(event_id=result.event_id)
app.ignore(result.event_id)

# Preview candidates with the read surface, then prune explicitly.
handled = app.list_events(status="handled", since=0, limit=50)
summary = app.prune_events(statuses=["handled", "ignored"], older_than=1700000000, limit=100)
orphans = app.prune_orphan_deliveries(older_than=1700000000, limit=100)
```

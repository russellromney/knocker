<h1 align="center">
  <img src="./logo-transparent.png" alt="" width="180" /><br/>knocker
</h1>

`knocker` is a loadable SQLite extension plus language bindings for building an embeddable inbound webhook inbox. It stores every HTTP receipt before returning success, dedupes provider retries into durable `Event` rows, and lets workers in the same process or another process handle those events later using [Honker](https://honker.dev), the SQLite-backed durable queue this project depends on.

Knocker's durable semantics live in shared Rust/SQLite code. The repo ships bindings over the same SQLite contract for Bun, Elixir, Go, Node, Python, and Ruby.

Knocker is for apps that already have an HTTP server, a SQLite database, and local business logic. It is not a hosted webhook relay, not a broker, and not a framework adapter package.

Webhooks look simple until you need to answer the boring production questions: did we store the request before returning `2xx`; did a provider retry create duplicate work; why did this event not run; can an operator replay it without guessing from logs?

Knocker takes the approach that if SQLite is already your app database, webhook ingress should live in the same file. Your route reads the raw request body and calls `receive(...)`. Knocker verifies, stores a `Delivery`, creates or correlates a deduped `Event`, enqueues durable work, and returns a status code. Later, a worker in the same process or another process dispatches the stored event to your handler.

Docs live at [knocker.dev](https://knocker.dev).

## At a glance

Knocker ships as one SQLite extension contract with bindings for Bun,
Elixir, Go, Node, Python, and Ruby.

```text
open a SQLite database
register an endpoint with a curated provider and secrets
read the raw HTTP request body and headers in your route
call receive(...)
return result.status_code to the webhook sender
run a worker against the same SQLite file
handle the stored event inside Knocker's transaction
```

Pick the binding for your runtime:

- [Bun](https://knocker.dev/reference/bun/)
- [Elixir](https://knocker.dev/reference/elixir/)
- [Go](https://knocker.dev/reference/go/)
- [Node](https://knocker.dev/reference/node/)
- [Python](https://knocker.dev/reference/python/)
- [Ruby](https://knocker.dev/reference/ruby/)

`receive(...)` is the normal verified-ingress path. Curated provider
verification is shared by the SQLite/Rust layer across bindings. The
lower-level `ingest(...)` method is trusted ingress for callers that already
know the verification outcome.

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
| Bun | `packages/knocker-bun` | Same shared SQLite contract as Node, adapted to Bun's SQLite runtime. |
| Elixir | `packages/knocker-elixir` | Same shared SQLite contract with Elixir handler and worker helpers. |
| Go | `packages/knocker-go` | Same shared SQLite contract with context-aware workers. |
| Node | `packages/knocker-node` | SQLite-extension binding with receive, typed handlers, workers, operators, retention, and provider verification through shared Rust code. |
| Python | `packages/knocker` | Python binding over the shared SQLite contract. |
| Ruby | `packages/knocker-ruby` | Same shared SQLite contract with Ruby-style handlers and operators. |

Bindings do not re-implement provider verification. Curated `receive(...)`
calls delegate to the shared Rust/SQLite `knocker_receive(...)` path, so
provider fixes land once and are exercised across runtimes.

## One route

Knocker intentionally does not ship framework adapters. Framework-specific code is small and should live in your app.

```text
route /webhooks/provider:
  body = read_raw_request_body()
  headers = read_request_headers()
  query = read_query_params()
  result = webhooks.receive(endpoint, body, headers, query)
  return HTTP result.status_code
```

See [Framework integration](https://knocker.dev/guides/framework-integration/) for framework route recipes.

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

# Knocker Roadmap

Store first. Ack fast. Process later.

## Summary

Knocker is an embeddable inbound webhook inbox for apps that already have:

- an HTTP server
- a SQLite database
- business logic that wants to react to inbound events

Its core promise stays the same:

- one process
- one SQLite file
- store first
- ack fast
- process later

Knocker is a library, not a service. It should feel local, boring, and durable.

## Intent Artifacts

Knocker now keeps a small human-owned intent baseline alongside the roadmap:

- `SYSTEM.md` is the current English model of the system.
- `.intent/phases/001-knocker-foundation/` records the baseline foundation slice.
- `.intent/phases/002-append-only-delivery-rows/` records the delivery/event split that is now part of the baseline.
- `.intent/phases/003-operator-read-surface/` records the Python-first operator read/action surface that is now part of the baseline.
- `.intent/phases/004-minimal-retention-and-pruning/` records the minimal explicit pruning surface that is now part of the baseline.
- `.intent/phases/005-ship-readiness/` records the docs-site and final pre-release hardening pass.
- `.intent/phases/006-public-surface-and-reliability-hardening/` records the pre-`0.1.0` response to the intensive codebase review.
- `CHANGELOG.md` summarizes completed work after it lands.

## Current Status

Implemented in this repo today:

- `knocker-core` Rust core with idempotent bootstrap
- Rust-backed ingress contract with append-only `Delivery` rows, deduped `Event` rows, and Honker enqueue
- Rust-backed event lifecycle transitions
- Python binding built with PyO3 and a thin Python wrapper
- Python verified ingress for generic HMAC-SHA256 and Stripe
- Binding-owned active-secret rotation for supported verifiers
- Stable Python operator surface for `get_event`, `list_events`, `get_delivery`, `list_deliveries`, `ignore`, `replay`, and `requeue`
- Explicit `replay_delivery(delivery_id)` operator recovery for processing one stored receipt body without mutating the canonical event payload
- Local Python worker state snapshots and optional worker-loop `on_error` callbacks
- Minimal explicit Python pruning surface for `prune_events` and `prune_orphan_deliveries`
- Provider presets for Stripe and GitHub correlation metadata
- Node contract pressure-test via a loadable SQLite extension
- `knockerlite` published on PyPI with Linux and macOS wheels and a tag-driven GitHub Actions release workflow

Still intentionally not implemented:

- automatic retention jobs and richer retention policy
- cross-binding operator parity beyond the Python surface
- Windows wheels (blocked on a `honker-core` Windows file-identity fix)
- PyPI trusted publishing (currently uses an API token; OIDC migration is queued)

## Post-Release Backlog

The 006 follow-up queue (docstrings, code comments, and the four missing tests) closed in `0.1.1` polish. These remaining items came out of that same review and are deliberately deferred.

Code organization:

- Move Honker payload-shape helpers out of `coercion.py` into a better-named `payload.py` or `queue.py` home.
- Decide whether `app.queue` is intentionally public. If yes, remove misleading underscores from `_HonkerQueue` / `_HonkerJob` or expose a smaller documented queue inspection surface.
- Consider exposing a core `knocker_reset_event` UDF so Python `replay_delivery(...)` and Rust replay/requeue share exactly one reset implementation.

Trust polish (post-`0.1.0`):

- Restore Windows wheels once `honker-core` ships its Windows file-identity fix; re-add Windows to the release matrix and CI.
- Migrate the PyPI release workflow from API token auth to PyPI trusted publishing (OIDC).
- Add a second real provider verification (GitHub or Slack) to prove the custom-verifier shape without committing to maintaining a long provider catalog.
- Extend retention: a per-prune audit row, richer policy options, explicit answers to "what did we delete, when, and why."
- Publish honest ingress/worker throughput numbers.

## Product Direction

Knocker should be the webhook product. Honker should be the async substrate underneath it.

That means:

- Knocker owns webhook concepts and semantics.
- Honker owns generic queue mechanics.
- The host app owns business logic and framework integration.

This is not "Knocker as a thin queue wrapper." It is "Knocker as a real inbound webhook inbox that happens to use Honker internally for durable async work."

## Architecture

### Layering

The intended layering is:

- `honker`
  Generic queue / wake / retry / dead-letter substrate on SQLite
- `knocker-core`
  Rust crate that defines Knocker's schema and Knocker-specific SQLite operations, and uses Honker internally where appropriate
- `knocker`
  Thin language binding for ingress, documented framework recipes, handler registration, and worker dispatch

This should remain a one-way dependency:

- Knocker may depend on Honker
- Honker must not know about webhooks

### Honker Dependency Policy

Knocker should never float against an unspecified Honker version.

Policy:

- `knocker-core` pins an explicit compatible Honker crate range
- published language bindings pin compatible `knocker-core` artifacts
- 0.1.0 should publish a compatibility statement, not just code

Before 0.1.0, Honker and Knocker may co-evolve in the workspace. At 0.1.0, the dependency contract should become explicit.

### Boundary With Honker

Honker owns:

- durable job queue rows
- claim / lease
- retry scheduling
- dead-letter mechanics
- wakeup / polling behavior
- worker iteration primitives

Knocker owns:

- endpoint registration
- durable webhook event rows
- request metadata storage
- dedupe semantics
- verification result storage
- webhook event state machine
- attempt history
- replay / requeue / ignore behavior
- handler dispatch contract

If Knocker uses Honker underneath, Knocker should not duplicate:

- lease columns
- claim tokens
- retry timers
- a second queue state machine

Honker should be the only owner of queue mechanics.

## Core Design Principles

### 1. Registration First, Decorators Optional

The API should not be fundamentally decorator-shaped.

Decorators are fine as convenience sugar in Python, but the conceptual core should be explicit registration so the library fits:

- raw language usage
- host-framework route glue
- testing
- non-decorator coding styles
- future multi-language bindings

The center of gravity should look more like:

```python
knocker = Knocker(db)

knocker.add_endpoint(
    name="stripe",
    path="/webhooks/stripe",
    provider="stripe",
)

knocker.add_handler(
    endpoint="stripe",
    event_type="checkout.session.completed",
    handler=handle_checkout,
)
```

With optional sugar:

```python
@knocker.handle(endpoint="stripe", event_type="checkout.session.completed")
def handle_checkout(event, tx):
    ...
```

### 2. SQLite Contract First

The hard semantics should live at the SQLite boundary, authored once in Rust and exposed through a small set of Knocker-owned operations.

The binding should adapt host language concerns into that contract. Framework-specific route glue should live in docs/examples unless a future spec proves a package API is necessary.

### 3. Event Row Is The Source Of Truth

The durable webhook event row is the product.

The Honker job payload should be minimal and should only point at stored state, for example:

```json
{"event_id": 123}
```

Raw headers, body, provider IDs, verification results, and event status should live in `knocker_events`, not be duplicated into Honker payloads.

### 4. Bootstrap Should Be Idempotent

Knocker should eagerly bootstrap its schema and required SQLite objects.

This bootstrap must be safe to run:

- on every process start
- during app startup hooks
- before ingress
- before worker startup
- during tests

Bootstrap must use an idempotent migration story:

- `CREATE TABLE IF NOT EXISTS`
- `CREATE INDEX IF NOT EXISTS`
- schema-version-based migrations for changes that cannot be expressed that way

Calling bootstrap multiple times should never be surprising.

## Target API Shape

The public API should be structured around four concerns:

- endpoint registration
- ingress
- handler registration
- worker execution

Example shape:

```python
knocker = Knocker(db)

knocker.add_endpoint(
    name="stripe",
    path="/webhooks/stripe",
    provider="stripe",
    secret=os.environ["STRIPE_WEBHOOK_SECRET"],
)

knocker.add_handler(
    endpoint="stripe",
    event_type="checkout.session.completed",
    handler=handle_checkout,
)

result = knocker.receive(
    endpoint="stripe",
    body=raw_body,
    headers=headers,
    query=query_params,
    method="POST",
)
await knocker.run_worker()
```

Raw-language usage should also be first-class:

```python
result = knocker.ingest(
    endpoint="stripe",
    body=raw_body,
    headers=headers,
    query=query_params,
    method="POST",
    provider_delivery_id=delivery_id,
    provider_event_id=event_id,
    event_type=event_type,
)
```

Framework integration should stay as small documented route glue over this same ingress contract.

## SQLite Model

Knocker only needs three core tables.

### `knocker_endpoints`

Configured ingress endpoints.

Suggested columns:

- `id`
- `name`
- `path`
- `provider`
- `secret_ref` or equivalent configuration linkage
- `enabled`
- `created_at`

### `knocker_events`

The durable inbox.

Suggested columns:

- `id`
- `endpoint_id`
- `received_at`
- `provider_event_id`
- `provider_delivery_id`
- `event_type`
- `method`
- `headers_json`
- `body_blob`
- `query_json`
- `signature_valid`
- `signature_error`
- `dedupe_key`
- `status`
- `attempt_count`
- `handled_at`
- `last_error`

Status values:

- `received`
- `processing`
- `handled`
- `failed`
- `dead`
- `ignored`

### `knocker_attempts`

Processing history.

Suggested columns:

- `id`
- `event_id`
- `attempted_at`
- `outcome`
- `error`
- `duration_ms`

## Event Lifecycle

The intended lifecycle is:

1. Request hits host app
2. Knocker matches endpoint config
3. Binding verifies signature if configured
4. Binding extracts provider metadata if available
5. Knocker inserts the durable event row
6. Knocker enqueues a Honker job referencing `event_id`
7. Transaction commits
8. Host app returns fast success
9. Honker worker claims job
10. Binding loads stored event and invokes handler
11. Knocker marks handled / failed / dead / ignored and records the attempt

Important:

- handlers do not run inline with the inbound HTTP request in v1
- the event row must be durable before success is returned
- event state and Honker job disposition should move together in one SQLite transaction, except for the initial Honker claim which necessarily happens before handler dispatch

## Transaction Boundaries

These boundaries should be explicit and consistent across bindings.

### Ingress Transaction

This must always happen in one SQLite transaction:

- dedupe decision
- durable event insert if needed
- Honker enqueue if needed
- commit before HTTP success

There should be no binding-specific exceptions here.

### Claim Boundary

Honker job claim happens before Knocker handler dispatch and is owned by Honker.

That means the claim itself is a separate queue-level transaction. Knocker should not attempt to duplicate claim state in `knocker_events`.

### Handler Transaction

After a Honker job has been claimed, the binding should open one SQLite transaction for:

- loading the stored event
- marking event status for the current attempt
- host app business writes
- recording the Knocker attempt row
- Honker `ack` or `retry` or `fail`

This is the core operational invariant: webhook event state and Honker job disposition should commit together.

Named exception:

- the Honker claim itself happens before this transaction and is not duplicated in Knocker state

## Replay And Requeue Semantics

These operations should be distinct and documented.

### `requeue(event_id)`

Operational recovery for a stored event that is currently `failed`, `dead`, or `ignored`.

Behavior:

- does not create a new event row
- enqueues a fresh Honker job for the existing `event_id`
- preserves original request metadata and prior attempt history
- sets current event status back to `received`
- clears `handled_at` and `last_error` for the new processing cycle

### `replay(event_id)`

Deliberate reprocessing of any stored event, including one already marked `handled`.

Behavior:

- does not create a new event row in v1
- enqueues a fresh Honker job for the existing `event_id`
- preserves original request metadata and prior attempt history
- sets current event status back to `received`
- clears `handled_at` and `last_error` for the new processing cycle

Difference from `requeue`:

- `requeue` is an operational recovery action for terminal non-success states
- `replay` is a broader operator action allowed from any stored state, including `handled`

If this distinction proves unhelpful in practice, v1 should collapse toward one primitive rather than maintain two confusing ones.

## Deduping

Deduping should use the best available key:

- provider delivery ID if available
- provider event ID if that is the right semantic unit
- otherwise an app-supplied dedupe key

Knocker should be explicit about the difference between:

- duplicate HTTP delivery of the same upstream event
- genuinely repeated upstream events

Deduping belongs to Knocker, not Honker.

## Verification

The binding layer should own request verification and provider-specific request parsing.

0.1.0 should ship:

- generic HMAC
- one provider-specific reference preset

The next provider presets should likely be:

- Stripe
- GitHub
- Slack

Knocker should store:

- whether verification passed
- why verification failed
- relevant raw signature headers

Invalid requests should usually still be stored unless obviously malicious or outside size limits.

## `knocker-core` SQL Contract

The Rust core should expose a narrow, boring contract. Exact names may change, but the surface should be in this shape:

- `knocker_bootstrap()`
- `knocker_endpoint_upsert(...)`
- `knocker_ingest(...)`
- `knocker_get_event(id)`
- `knocker_list_events(...)`
- `knocker_mark_processing(...)`
- `knocker_mark_handled(...)`
- `knocker_mark_failed(...)`
- `knocker_mark_ignored(...)`
- `knocker_replay(id)`
- `knocker_requeue(id)`

The most important operation is `knocker_ingest(...)`.

Its job is to:

- apply dedupe semantics
- insert the durable event row if needed
- enqueue the Honker job if needed
- return enough information for the binding to choose the HTTP response

That should happen atomically in one transaction.

## Binding Responsibilities

Language bindings should stay thin and local.

Bindings own:

- documented framework route recipes
- reading request objects
- signature verification
- provider metadata extraction
- endpoint and handler registration API
- handler invocation
- convenience admin endpoints

Bindings should not own:

- event state machine semantics
- replay semantics
- dedupe write behavior
- retry scheduling semantics
- schema correctness

## Retention And Cleanup

Knocker needs an explicit retention story before 0.1.0.

At minimum, one of these must exist:

- Knocker-owned pruning operations such as `knocker_prune_events(...)`
- a clearly documented operator pattern with recommended SQL and vacuum guidance

The preferred direction is a Knocker-owned retention API for:

- handled events older than a threshold
- ignored events older than a threshold
- failed / dead events older than a threshold, optionally after operator review
- attempt rows associated with pruned events

Retention should be treated as an operational feature, not an afterthought.

## Secret Rotation

Secret rotation should be a first-class part of endpoint configuration.

V1 should support an overlap window where more than one secret may verify successfully for a given endpoint.

The minimal acceptable story is:

- endpoint config can reference multiple active secrets
- verification succeeds if any active secret matches
- operators can remove old secrets after the upstream provider has switched over

This does not require a global secret-management subsystem. It just needs a sane endpoint-level rotation story.

## Implementation Plan

### Phase 0: Python Smell Test Of The Layering

Goal:

- quickly test whether Knocker-specific tables plus a Honker job payload of `event_id` feels clean in real usage

This phase is intentionally not proof of the Rust contract. It is a product and ergonomics smell test.

Deliverables:

- small Python prototype
- end-to-end tests for ingest, async handling, dedupe, retry, replay

Exit criteria:

- we are convinced the `knocker` / `honker` split is promising enough to justify the Rust contract work
- we have identified any obvious product-shape mistakes before committing to native interfaces

### Phase 1: Rust Core Skeleton

Goal:

- create `knocker-core` crate and validate the actual Rust/SQLite layering

Deliverables:

- crate layout
- idempotent bootstrap
- schema versioning
- Knocker tables and indexes
- explicit Honker dependency pinning strategy

Exit criteria:

- bootstrap can be called repeatedly without side effects
- schema exists entirely from Rust-owned bootstrap
- the actual Knocker/Honker split looks clean at the contract level, not just in Python

### Phase 2: Ingest Contract

Goal:

- move durable ingress semantics into the Rust/SQLite layer

Deliverables:

- `knocker_ingest(...)`
- dedupe behavior
- event insert behavior
- Honker enqueue integration
- explicit ingress transaction invariant

Exit criteria:

- one SQLite call can durably store and schedule processing
- duplicate deliveries behave correctly
- HTTP success is never returned without a committed durable event row

### Phase 3: Event State Transitions

Goal:

- move event lifecycle bookkeeping into the Rust/SQLite layer

Deliverables:

- processing / handled / failed / dead / ignored operations
- attempt recording helpers
- replay semantics
- requeue semantics
- explicit handler-transaction invariant

Exit criteria:

- binding no longer hand-rolls state updates
- replay and requeue are behaviorally unambiguous

### Phase 4: Python Binding v1

Goal:

- deliver the first ergonomic, framework-neutral Python package

Deliverables:

- `Knocker` class
- explicit endpoint registration
- explicit handler registration
- optional decorator sugar
- raw `ingest(...)`
- `run_worker()` / worker helper
- framework integration docs/examples
- `uv`-based local development flow

Exit criteria:

- usable in raw Python and from documented host-framework routes without fighting the host app

### Phase 5: Contract Pressure-Test With A Second Binding

Goal:

- prove the contract is binding-friendly, not just Python-friendly

Deliverables:

- minimal Node binding stub
- raw ingest path
- endpoint registration path
- worker / handler dispatch spike

Exit criteria:

- a second language can hit the contract cleanly without reimplementing semantics
- any Python-shaped assumptions in the contract have been removed early

### Phase 6: Provider Verification And Secret Rotation

Goal:

- make common verification flows easy without overcommitting to a large vendor matrix

Deliverables:

- generic HMAC verifier
- Stripe preset as the first provider-specific reference implementation
- endpoint-level secret rotation support
- provider preset architecture that cleanly supports GitHub and Slack next

Non-goal for 0.1.0:

- committing to a large forever-maintained provider matrix before the core settles

Exit criteria:

- generic verification is solid
- at least one real provider preset proves the shape
- secret rotation is operationally sane

### Phase 7: Operations, Retention, And Admin Surface

Goal:

- give operators enough inspection and control to run Knocker safely in production

Deliverables:

- retention / pruning story
- list events
- filter by status / endpoint / event type
- inspect headers and body
- replay one event
- requeue failed or dead event
- mark ignored
- minimal JSON endpoints
- barebones read-only HTML table built directly on the same query surface, with no separate frontend stack

Exit criteria:

- operators can inspect, replay, requeue, ignore, and prune events without bespoke internal tooling

### Phase 8: Performance Characterization

Goal:

- turn "ack fast" into measured behavior, not just intent

Deliverables:

- ingress latency benchmarks
- worker lag benchmarks
- documented benchmark environment
- published p50 / p95 / p99 results for at least one representative workload

Exit criteria:

- 0.1.0 has published performance numbers
- obvious regressions are caught before release

### Phase 9: Docs And 0.1.0 Release Gate

Goal:

- make Knocker publishable, understandable, and supportable

Deliverables:

- README that explains the product clearly
- integration guide for raw Python and common host-framework routes
- operator runbook for retries, replay, dead events, and retention
- provider verification guide for the shipped presets
- explicit 0.1.0 compatibility story for Honker dependency versions
- PyPI publish gate and semver statement

Exit criteria:

- Knocker is ready to publish as `0.1.0`
- semver expectations are explicit
- operators have the docs needed to run it without reading source

### Phase 10: Additional Bindings

Goal:

- expand bindings after the contract and docs have proven stable

Likely order:

- Node beyond the initial stub
- Ruby
- Go

Exit criteria:

- bindings remain thin because the hard semantics already live in the SQLite contract

## Testing Plan

The first serious test suite should prove:

- inbound requests are durably stored before success is returned
- handlers run asynchronously after ingress
- dedupe works for provider-style delivery IDs
- retries and failure states work
- dead-letter behavior is visible
- replay reprocesses stored events
- bootstrap is idempotent
- documented framework recipes preserve the same `receive(...)` semantics
- retention operations behave safely
- secret rotation behaves predictably
- the second binding can hit the contract without semantic drift

Later, add:

- crash-recovery coverage
- migration tests
- provider verification tests
- performance regression checks once baseline benchmarks exist

## Repo Shape

Target repo shape:

- `knocker_v_1_design.md`
  Early product design notes
- `ROADMAP.md`
  Current implementation roadmap
- `knocker-core/`
  Rust core for Knocker's SQLite contract
- `knocker-extension/`
  SQLite loadable extension for cross-language contract access
- `packages/knocker/`
  Python binding
- `packages/knocker-node/`
  Minimal Node contract pressure-test
- `tests/`
  binding-level and cross-layer tests

This mirrors the good parts of the Honker shape without dragging in more packaging complexity than Knocker needs early on.

## Non-Goals

Knocker v1 should not become:

- a hosted SaaS
- a standalone webhook service
- a generic workflow engine
- a multi-backend abstraction
- a Kafka / Redis / Postgres control plane
- a distributed multi-node coordinator
- a multi-tenant SaaS control plane

Knocker may be used inside an application that itself serves many tenants, but tenanting remains the host app's concern. Knocker is not building tenant orchestration as a product feature.

SQLite-first is the feature, not a temporary compromise.

## Open Questions

These are real design questions. Some must be resolved before the later phases named below.

- whether `attempt_count` should represent total lifetime attempts or only the current processing cycle
  Must resolve before Phase 3 is complete
- how much provider-specific normalization should happen before `knocker_ingest(...)`
  Must resolve before Phase 6 is complete
- which framework recipes should be documented first
  Must resolve before Phase 9 is complete

## Current Recommendation

Build Knocker as:

- a real webhook inbox product
- backed by a Rust/SQLite contract
- using Honker privately for async queue mechanics
- with explicit registration-first bindings
- with decorators as optional sugar only

That keeps the product small, local, multi-language-friendly, and honest.

# Spec Diff: Ship Readiness

Phase:
- 005-ship-readiness

Session:
- A

## What changes

- Knocker adds the smallest remaining work needed to ship a credible `0.1.0`.
- This phase covers:
  - a public docs site for `knocker.dev`, built with Astro/Starlight
  - release-facing docs and operator guidance
  - explicit compatibility and publish criteria
  - minimal performance evidence
  - final pre-release hardening from the codebase review
- Duplicate ingest into an existing `Event` never mutates the event row and never enqueues work, including when the existing event is `dead`. It stores the new `Delivery`, returns the existing `event_id`, and reports `duplicate=true`.
- Operators recover dead events explicitly with `requeue(event_id)` rather than by relying on provider redelivery side effects.
- `replay(event_id)` is accepted only for `handled`, `failed`, `dead`, and `ignored` events. `requeue(event_id)` is accepted only for `failed`, `dead`, and `ignored` events.
- `replay(...)` and `requeue(...)` remove any stale live Honker jobs for this Knocker queue before enqueueing replacement work.
- Worker dispatch reads the event inside the work transaction. Stale jobs for pruned events and ignored events are drained with best-effort queue ack and do not invoke handlers.
- Public Python validation is tightened where the operator surface is already strict:
  - `limit`, `since`, and `older_than` must be integer-shaped, not float/string coercions
  - Stripe `tolerance_s` must be a non-negative integer
- Retention live-job cleanup treats malformed Honker payloads as non-matches rather than guessing through string coercion.

## What does not change

- This phase does not add a hosted control plane, operator HTTP API, frontend stack, or HTML admin UI.
- This phase does not redesign the underlying operator or pruning APIs.
- This phase does not add automatic retention jobs or richer retention policy.
- This phase does not add Node/admin parity beyond what is needed to document or smoke-test the baseline.
- This phase does not add `replay_delivery(delivery_id)` or any mode that processes a duplicate delivery body as the handler payload.
- This phase does not add automatic requeue-on-redelivery behavior.

## How we will verify it

- `site/` builds cleanly and publishes a docs-first `knocker.dev` surface that matches the shipped Python API.
- Knocker has a clear `0.1.0` release gate, current docs, and enough evidence that operators can run the shipped surface without reading source.
- `make test` passes across Rust, Python, and Node.
- Tests pin the review-driven hardening contracts:
  - ingest rollback leaves no partial event, delivery, or queue rows
  - multiple Python workers do not process the same event twice
  - dead duplicate redelivery is audit-only until explicit `requeue(...)`
  - replay/requeue do not create duplicate live jobs
  - pruning count math handles multiple deliveries and attempts
  - malformed live-job payloads are not pruned by accident

## Notes

- This phase stays deliberately downstream of `003` and `004`.
- App-owned admin or HTTP exposure remains the host application's job, not Knocker's.

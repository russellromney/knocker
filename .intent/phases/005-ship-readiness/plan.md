# Plan

Phase:
- 005-ship-readiness

Session:
- A

## Goal

- Ship the smallest honest `0.1.0` readiness pass: a public docs site, release-facing guidance, recorded evidence, and final pre-release hardening from the codebase review.

## Scope

1. Add `site/` as a lightweight Astro/Starlight docs site for `knocker.dev`.
2. Document the shipped Python-first surface:
   - verified ingress
   - operator reads and actions
   - minimal pruning
   - product boundary and roadmap
3. Keep the phase docs-only or evidence-only where possible:
   - no built-in JSON operator API
   - no HTML admin UI
   - no new control-plane semantics
4. Land narrow runtime hardening where review found real bug shapes:
   - strict integer validation for operator limits and Stripe tolerance windows
   - narrow missing-event dispatch handling
   - replay/requeue status gates and stale live-job cleanup
   - symmetric best-effort short-circuit ack for missing/pruned and ignored events
   - dispatch event read inside the worker transaction
5. Preserve the dedupe invariant:
   - duplicate ingest into an existing event stores a delivery only
   - duplicate ingest never mutates event state
   - duplicate ingest never enqueues work, including for `dead` events
   - dead-event recovery remains explicit through `requeue(event_id)`
6. Record release-facing evidence:
   - docs site builds
   - representative performance note
   - explicit `0.1.0` gate / compatibility statement

## Steps

1. Copy the working Astro/Starlight shape from `honker/site` into `knocker/site`, but keep only the minimal reusable skeleton:
   - package config
   - Starlight config
   - lightweight custom header/social components
   - docs page tree
2. Rewrite the content around Knocker's actual shipped surface rather than Honker's queue/stream feature set.
3. Update repo-level docs to point at the new site and its local build workflow.
4. Add any small supporting files needed for deployment to Cloudflare Pages.
5. Build the site locally and fix any MDX/config issues before touching release evidence.
6. Apply the hardening fixes without widening product scope:
   - `_coerce_limit(...)` rejects non-integers instead of coercing them
   - Stripe verifier config rejects bool, non-int, and negative `tolerance_s`
   - `_dispatch_job(...)` uses an explicit optional event lookup instead of broad `KeyError` swallowing
   - stale missing-event and ignored-event dispatch paths best-effort ack and return
   - `replay(...)` and `requeue(...)` validate accepted statuses before calling core functions
   - `knocker-honker` deletes stale live jobs for the same event and queue before explicit replay/requeue enqueue
7. Add regression tests for the codebase-review gaps:
   - ingest rollback on mid-transaction enqueue failure
   - multiple workers processing multiple events once each
   - dead duplicate redelivery staying audit-only until explicit requeue
   - multiple-delivery / multiple-attempt prune count math
   - malformed live-job payloads left alone by prune cleanup
   - Rust core duplicate-into-dead behavior
   - Rust FK cascade behavior under the real Honker connection opener
8. Record the remaining `0.1.0` gate items in the phase docs so the last release pass is explicit.

## Verification

- `make test`
- `cd site && npm run build`
- sanity-check the generated pages:
  - home
  - docs overview
  - getting started
  - verified ingress
  - operator surface
  - retention
  - Python reference
  - roadmap

## Traps

- Do not add a built-in operator HTTP API just because the docs site exists.
- Do not invent new runtime semantics while writing examples.
- Do not promise cross-binding parity the product does not ship.
- Do not turn the site into a frontend project; keep it docs-first and static.
- Do not make provider redelivery implicitly reactivate dead events.
- Do not process a duplicate delivery body as the handler payload in this phase.

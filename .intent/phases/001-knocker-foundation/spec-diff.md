# Spec Diff: Knocker Foundation

## What changes

- Knocker gets a Rust-backed core, `knocker-honker`, that owns bootstrap, durable ingress, replay/requeue, and event lifecycle transitions.
- The Python binding becomes thinner and delegates durable inbox semantics to the shared SQLite / Rust contract.
- A Node smoke test validates that the contract is not only Python-friendly.
- Worker correctness tightens so missing handlers dead-letter loudly and queue disposition failures roll back Knocker state.

## What does not change

- Knocker remains a library, not a service.
- The same-process and same-SQLite-file model stays intact.
- Event rows remain the source of truth and Honker payloads remain minimal.
- Handlers still run after durable ingress, not inline with the request.
- Provider adapters, retention, and admin surfaces remain out of scope for this change.

## How we will verify it

- `make test` passes.
- Rust tests prove bootstrap, ingest, dedupe, and replay/requeue behavior.
- Python tests prove async ingress, retry/dead-letter behavior, missing-handler failure, and claim-expiry rollback.
- Node tests prove bootstrap and dedupe through the shared SQLite contract.

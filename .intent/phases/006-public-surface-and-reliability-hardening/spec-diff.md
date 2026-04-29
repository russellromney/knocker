# Spec Diff: Public Surface And Reliability Hardening

Phase:
- 006-public-surface-and-reliability-hardening

Session:
- A

## What changes

- Knocker closes the full intensive code-review response before `0.1.0` rather than deferring known issues to a later release.
- The supported Python surface becomes documented in-process, not only on the docs site:
  - public dataclasses have concise docstrings
  - public `Knocker` methods have concise docstrings
  - docstrings describe status preconditions, transaction semantics, and important footguns
- The handler contract is documented as a first-class product feature:
  - handlers receive `(event, tx)`
  - business writes performed through `tx` commit atomically with Knocker's event transition and queue ack
  - handlers should stay synchronous, short, and DB-local; slow outbound work should be pushed into app-owned follow-up jobs
- The docs site and README explain:
  - the `(event, tx)` atomic-commit pattern
  - accepted `replay(...)` and `requeue(...)` statuses
  - dead redelivery is audit-only until explicit `requeue(...)`
  - `ingest(...)` is a trusted low-level path and bypasses binding-owned verification by default
  - production workers should be wrapped in an app-owned restart/supervision harness
- `Knocker.endpoint(...)` is removed before `0.1.0`; `add_endpoint(...)` is the single endpoint registration method.
- Core lifecycle UDFs fail fast when asked to mutate an unknown event id:
  - `knocker_mark_processing`
  - `knocker_mark_handled`
  - `knocker_mark_failed`
  - `knocker_mark_ignored`
- Knocker gains worker affordances without becoming a daemon or control plane:
  - a lightweight public worker state snapshot
  - current event id when a worker is actively dispatching
  - last worker error when dispatch fails outside normal handler retry/dead-letter flow
  - optional `on_error` callback for worker-loop failures
  - explicit guidance that host apps still own restart policy
- Knocker gains `replay_delivery(delivery_id)` as an explicit operator primitive for the body-vs-event identity case:
  - it is operator-only and Python-first
  - it does not change duplicate ingest behavior
  - it processes the specified stored `Delivery` body through the existing event's handler
  - it records attempt history and queue disposition consistently with normal dispatch
  - it does not mutate the canonical `Event` payload
- The Python binding and tests are split into smaller files that satisfy the project file-length standard:
  - no Python implementation file over 1000 lines
  - no test file over 1000 lines
  - the split is semantics-preserving except where this spec explicitly changes behavior
- The test suite adds the missing real-infrastructure coverage called out in review:
  - concurrent ingest with the same dedupe key
  - handler running longer than visibility timeout and being reclaimed by another worker
  - WAL wakeup of an idle worker after commit
  - replay racing with a mid-handler worker
  - Python-entrypoint v1-to-v2 migration
  - concurrent prune calls
  - burst ingest plus worker drain smoke
  - `replay_delivery(...)` success and rejection cases
  - unknown-event lifecycle UDF failures

## What does not change

- Knocker remains an embeddable library, not a daemon, hosted service, or admin server.
- Knocker still does not add a built-in operator HTTP API or HTML admin UI.
- Duplicate ingest still never mutates an existing `Event` and never enqueues work.
- `replay_delivery(...)` does not make provider redelivery automatic; it is an explicit operator action.
- `replay_delivery(...)` does not rewrite canonical `Event` payload.
- Worker state is local process state only; it is not a durable monitoring or orchestration system.
- Node remains a shared-contract smoke binding, not a parity operator binding.
- No new automatic retention scheduler or richer retention policy lands in this phase.

## How we will verify it

- `make test` passes across Rust, Python, and Node.
- `npm --prefix site run build` passes.
- Public Python docstrings are present for the supported API surface.
- File-length checks confirm split Python implementation and test files are below 1000 lines.
- Real-infrastructure tests pin the new and reviewed contracts without mocks.
- Existing phase 001-005 behavior remains covered by the reorganized tests.

## Notes

- This phase is deliberately larger than a release-cut phase because we decided to fix all known review findings before `0.1.0`.
- Implementation should prefer small commits by concern: docs/docstrings, fail-fast core hardening, worker affordances, `replay_delivery`, file split, tests.

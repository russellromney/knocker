# Reviews And Decisions

This file is append-only.

## Review 1 — Hardening Pass + Dead-Redelivery Reconsideration

Session B (Claude Opus 4.7), reviewing the pre-`0.1.0` hardening batch landed in `005`'s working tree alongside the docs/release scope.

### Code landed cleanly

- **B1** — `_coerce_limit` is int-only; matches `_coerce_since` / `_coerce_older_than`.
- **B2** — `_get_event_or_none` replaces the broad `except KeyError`, so real column-lookup `KeyError`s propagate instead of being swallowed.
- **B3** — `replay` / `requeue` gate on status sets and call `delete_live_jobs_for_event` before enqueue. Tests pin both the `received`-rejection and the no-duplicate-live-job invariant.
- **B5** — both short-circuit ack paths in `_dispatch_job` now best-effort and symmetric.
- **B6** — Stripe `tolerance_s` rejects bool / non-int / negative at config time. New runtime test confirms the expired-timestamp rejection that was previously untested.
- **Bonus** — `_dispatch_job` now reads the event inside the work transaction, which incidentally closes the prune-during-dispatch incoherence flagged in the codebase audit (the `B9` race).

### Dead-redelivery (B4) — recommend revert

Current implementation: a duplicate ingest into an existing `dead` event auto-resets the event, re-enqueues, and reports `duplicate=true`. The fix is correct in spirit but wrong as the v1 semantic endpoint.

**Why revert:**

- Breaks `002`'s implicit contract that "ingest of a duplicate never mutates event state." The asymmetry (`dead` mutates, `ignored` / `failed` / `handled` don't) makes the rule conditional on prior status — much harder to document.
- `duplicate=true` plus `should_enqueue=true` is incoherent at the result-type level. Either there's work to do (`duplicate=false`) or there isn't.
- Provider redeliveries can carry updated bodies (Stripe backfills `previous_attributes`, GitHub corrects timestamps). With auto-requeue the handler runs on the **original** event body, not the redelivery body. Operators reading the new redelivery body assume the handler processed it. Body-vs-event-identity confusion is a real bug shape, not a theoretical one.
- Forecloses a cleaner future API: `replay_delivery(delivery_id)` for "process this exact redelivery's body." Auto-requeue is a partial wrong version of that primitive.

**Recommended v1 behavior:**

- Dedupe of a duplicate into any prior status (including `dead`) never mutates the event row.
- Store the new `Delivery`, return `duplicate=true`, no enqueue.
- Operators recover `dead` events explicitly via `requeue(event_id)`. The `IngestResult` already carries `event_id`; observability is sufficient.
- If implicit reactivation proves useful in practice, ship later as opt-in: `add_endpoint(auto_requeue_dead=True)`.

**Concrete revert:**

- `knocker_ops.rs::ingest`: drop the dead branch's reset + enqueue. Revert `find_existing_event` to id-only.
- `tests/test_knocker_core.py::test_dead_redelivery_requeues_existing_event`: flip to assert `duplicate=true`, event stays `dead`, no new live job, new `Delivery` row stored. Add a follow-on assertion that `requeue(event_id)` is the explicit recovery path.

### Still open

- **Test gaps** from the codebase audit not yet addressed: ingest rollback / durability, multi-worker concurrency, prune count math with multi-delivery / multi-attempt events.
- **IDD artifact gap**: the `005` spec-diff and plan describe only the docs site, but six runtime contract changes landed in code. Before commit, the spec-diff needs invariants for: replay accepted-status set, requeue accepted-status set, strict `limit` / `since` / `older_than` / `tolerance_s` types. After the dead-redelivery revert, no change to `002`'s dedupe contract is needed.

## Review Response 1

Session:
- A

### Decisions

- **D1** — Accept B1, B2, B3, B5, B6, and the dispatcher transaction-unification bonus.
  Action: keep the landed hardening and pin it in `spec-diff.md`, `plan.md`, `SYSTEM.md`, and tests.

- **D2** — Accept B4 reconsideration.
  Action: revert dead auto-requeue. Duplicate ingest into any existing event, including `dead`, stores a new `Delivery` only and does not mutate event state or enqueue work.

- **D3** — Preserve explicit operator recovery.
  Action: `requeue(event_id)` remains the supported recovery path for dead events. `replay_delivery(delivery_id)` is left as future product design, not a v1 implication.

- **D4** — Close the test gaps before commit.
  Action: add tests for ingest rollback, multi-worker processing, multi-delivery / multi-attempt prune counts, malformed live-job payloads, Rust FK cascade, and Rust duplicate-into-dead semantics.

- **D5** — Close the IDD artifact gap.
  Action: update the `005` spec diff and plan so the runtime contracts appear in the intent layer rather than only in implementation.

### Result

- Dead duplicate redelivery is audit-only until explicit `requeue(...)`.
- Duplicate ingest never mutates existing event state.
- `make test` passes:
  - Rust: 10 passed
  - Python: 40 passed
  - Node: 2 passed

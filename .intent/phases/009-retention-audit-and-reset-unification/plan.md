# Plan

Phase:
- 009-retention-audit-and-reset-unification

Session:
- A

## Goal

- Make retention and recovery easier to trust by giving prune operations a durable audit trail and moving all event reset semantics onto one core primitive.

## Phase decisions

- This phase combines the two remaining operator-trust items from the roadmap:
  - per-prune audit rows
  - core `knocker_reset_event(...)`
- Audit is summary-oriented, not row-by-row tombstoning.
- Audit rows are append-only records.
- Audit rows live in Knocker-owned schema, not in application logs.
- Audit rows are never targeted by ordinary prune operations; they form a separate audit trail.
- The prune-audit table uses stable top-level columns plus a structured JSON summary field:
  - `id`, `kind`, `queue_name`, `executed_at`
  - `events_pruned`, `deliveries_pruned`, `attempts_pruned`, `live_jobs_pruned`
  - `summary_json` with minimum keys per prune kind (`statuses`/`older_than`/`limit` for events; `older_than`/`limit` for orphans)
- Counts include all rows removed as a consequence of the prune, whether by direct `DELETE` or `ON DELETE CASCADE`.
- No-op prunes (zero rows deleted) still write an audit row with zero counts.
- Reset semantics are defined once in Rust core and reused by Python recovery paths.
- `knocker_reset_event(...)` is a **low-level primitive** with no source-state validation. Callers (`knocker_replay`, `knocker_requeue`, Python `replay_delivery`) enforce their own allowed-source-state preconditions.
- `knocker_reset_event(...)` does **not** record attempt history. Attempt-history insertion remains the responsibility of handler dispatch (`mark_handled`, `mark_failed`, `mark_ignored`).
- The Python public surface gains a small `list_prune_audits(...)` read helper so operators do not drop to raw SQL.
- Automatic retention policies, timers, and scheduling remain out of scope.

## Build order

1. Add prune-audit storage in core:
   - add a Knocker-owned prune-audit table via idempotent bootstrap + migration
   - schema: stable top-level columns (`kind`, `queue_name`, `executed_at`, `events_pruned`, `deliveries_pruned`, `attempts_pruned`, `live_jobs_pruned`, `summary_json`) plus `id` primary key
   - keep audit rows append-only
2. Thread prune audit through existing prune operations:
   - `prune_events(...)` writes one audit row in the same transaction, even when zero rows are deleted
   - `prune_orphan_deliveries(...)` writes one audit row in the same transaction, even when zero rows are deleted
   - ensure rollback removes both data changes and audit row together
3. Add reset unification in core:
   - implement `knocker_reset_event(...)` as a low-level primitive (no status validation)
   - update Rust `replay(...)` and `requeue(...)` to use it instead of the private `reset_event(...)`
   - update Python `replay_delivery(...)` to call `knocker_reset_event(...)` instead of hand-rolling the same `UPDATE`
   - preserve current payload-selection differences between replay/requeue/replay-delivery
4. Add Python read helper:
   - add `list_prune_audits(kind=None, since=None, limit=50) -> list[PruneAudit]`
   - newest-first by default, with integer `since` and bounded `limit`
   - add a small `PruneAudit` dataclass
5. Update tests:
   - prune audit happy path (events and orphans)
   - prune audit no-op path (zero deletes still write a row)
   - prune audit rollback path
   - summary counts including cascade semantics
   - `list_prune_audits(...)` read surface
   - reset equivalence across replay/requeue/replay-delivery
   - `knocker_reset_event(...)` does not insert into `knocker_attempts`
   - existing recovery tests updated only as needed to pin the shared core primitive
6. Update docs:
   - operator runbook
   - retention guide
   - reference docs for `list_prune_audits(...)`
   - changelog / system / roadmap
7. Run verification and record evidence.

## Verification

- `make test`
- `npm --prefix site run build`
- New prune-audit and reset-unification tests are listed by name in the phase evidence.

## Traps

- Do not widen this into automatic retention scheduling.
- Do not turn audit rows into full deleted-row snapshots.
- Do not let replay/requeue/replay-delivery diverge again in reset behavior after adding `knocker_reset_event(...)`.
- Do not mutate canonical event payload as part of reset.
- Do not add cross-binding operator APIs beyond what is needed to preserve the current Python-first operator story.
- Do not let `knocker_reset_event(...)` accidentally validate source states — that would force the primitive to know caller-specific policy.
- Do not omit the no-op prune audit case; the operator trail must be unambiguous.

## Areas not to touch

- Provider conformance/catalog machinery from phase 008.
- Runtime plugin/provider loading questions.
- Release workflow, Windows wheels, or PyPI trusted publishing.
- New durable semantics unrelated to prune audit or reset unification.

## Assumptions and risks

- Summary-level prune audit is enough to solve the immediate “what got deleted, when, and why?” operator gap without introducing a heavy tombstone system.
- `knocker_reset_event(...)` should reduce drift and future bugs by making reset semantics impossible to fork accidentally, but only if callers continue to enforce their own preconditions.
- `list_prune_audits(...)` stays small and inspection-oriented, not a new admin subsystem.

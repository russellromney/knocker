# Reviews And Decisions

This file is append-only.

## Spec-Diff + Plan Review 1

Session A review of `spec-diff.md` and `plan.md`.

### Positive conformance review

- **P1 — The phase is coherent.** Retention audit and reset unification are both operator-trust work, so they belong together better than they would as two tiny unrelated slices.
- **P2 — The spec keeps the scope disciplined.** Automatic retention scheduling, richer cross-binding parity, and unrelated release-work are all explicitly out of scope.
- **P3 — The reset contract is pointed in the right direction.** Defining reset once in core and letting replay/requeue/replay-delivery differ only in payload choice is the right anti-drift move.
- **P4 — The prune-audit goal is user-shaped.** “What got deleted, when, and why?” is a real operator question, and the spec keeps that front and center.

### Negative conformance review

- **N1 — `knocker_reset_event(...)` leaves status gating undefined.** The spec pins what reset does to the row, but not which source states are legal or whether the core primitive enforces those preconditions itself. That is load-bearing because `replay(...)`, `requeue(...)`, and `replay_delivery(...)` intentionally accept different source states today. The phase should explicitly decide whether:
  - `knocker_reset_event(...)` is a low-level primitive that assumes callers already validated status, or
  - `knocker_reset_event(...)` enforces allowed source states and fails loudly on invalid ones.
  Without that, the implementer can accidentally widen or narrow recovery semantics at the core boundary.

- **N2 — The prune-audit summary shape is under-specified.** “filter summary / operator inputs” is directionally right, but the plan never pins how that summary is stored. This should be decided before implementation:
  - explicit columns only, or
  - a structured JSON summary column plus stable top-level counts/kind/timestamp columns.
  If JSON is used, the minimum keys for each prune kind should be named now (`statuses`, `older_than`, `limit`, maybe `queue_name`, etc.). Otherwise audit rows will be hard to query consistently and future readers will not know what is contract vs implementation detail.

- **N3 — No-op prune behavior is implied but not pinned.** The invariant says explicit prune operations are operator actions and must leave behind a durable audit row. That strongly suggests a successful prune that deletes zero rows should still record an audit row with zero counts, but the verification list does not name that case explicitly. Add a test/decision for no-op prune calls so the operator trail is not ambiguous.

- **N4 — The Python audit read surface is still optional in a way that may defer a product decision.** Plan step 4 says “if a helper is warranted.” That leaves the implementer deciding whether prune-audit inspection is a Python API surface or just a row mapping tested indirectly. Since Knocker is explicitly Python-first for operator surfaces, the phase should decide yes or no up front. My lean: add a very small read helper (`list_prune_audits(...)` or equivalent) so operators do not have to drop to raw SQL for the thing this phase is adding on purpose.

### Adversarial review

- **A1 — Audit rows need an explicit non-recursion rule.** Once prune audit rows exist, they should not themselves become part of ordinary prune targets in this phase. That probably sounds obvious, but future retention work gets weird fast unless this phase says prune audit rows are a separate audit trail and not part of `prune_events(...)` / `prune_orphan_deliveries(...)`.

- **A2 — `knocker_reset_event(...)` should not silently fork attempt-history semantics.** The spec pins `attempt_count`, `last_error`, and `handled_at`, but does not say whether reset records an attempt-history row itself or whether attempt-history remains the responsibility of the higher-level replay/requeue/replay-delivery paths. That should be explicit so the core primitive does not accidentally create a fourth variant of recovery bookkeeping.

- **A3 — Audit counts for mixed event/dependency deletion need to stay definitionally stable.** The plan correctly wants summary counts, but it should pin whether `attempts_pruned` and `live_jobs_pruned` are only directly removed rows or whether they can include rows implicitly removed by cascades. Operators will read those counts as facts, so the counting definition should be stated before code lands.

### Review verdict

The phase direction is good and the scope is healthy. Before implementation, I would pin:

- **N1** — status-gating contract for `knocker_reset_event(...)`
- **N2** — prune-audit summary storage shape
- **N3** — no-op prune audit behavior
- **N4** — whether a small Python audit read helper is in or out
- **A2/A3** — attempt-history responsibility and count semantics

Once those are decided, this looks like a strong 009.

## Spec-Diff + Plan Review 1 — Response

Session A response to Review 1 findings. All items below are now pinned in `spec-diff.md` and `plan.md`.

### N1 — Status-gating contract for `knocker_reset_event(...)`

**Decision:** `knocker_reset_event(...)` is a **low-level primitive** that does **not** validate source-event status. Callers are responsible for their own preconditions.

**Rationale:** The existing Rust `replay(...)` accepts `handled` | `failed` | `dead` | `ignored`; `requeue(...)` accepts `failed` | `dead` | `ignored`; Python `replay_delivery(...)` accepts `handled` | `failed` | `dead` | `ignored`. These are caller-specific policies. A single core primitive cannot enforce all of them without either (a) becoming a policy kitchen sink or (b) narrowing some callers incorrectly. The current private `reset_event(...)` in Rust already has no status gate; `knocker_reset_event(...)` simply exposes that same low-level contract as a named UDF so Python `replay_delivery(...)` stops hand-rolling the same `UPDATE`.

### N2 — Prune-audit summary storage shape

**Decision:** Stable top-level columns plus a structured JSON summary field.

**Schema:**
- `id` INTEGER PRIMARY KEY
- `kind` TEXT NOT NULL (`'prune_events'` or `'prune_orphan_deliveries'`)
- `queue_name` TEXT NOT NULL
- `executed_at` INTEGER NOT NULL DEFAULT (unixepoch())
- `events_pruned` INTEGER
- `deliveries_pruned` INTEGER
- `attempts_pruned` INTEGER
- `live_jobs_pruned` INTEGER
- `summary_json` TEXT NOT NULL

**JSON minimum keys:**
- `prune_events`: `{"statuses": [...], "older_than": <int>, "limit": <int>}`
- `prune_orphan_deliveries`: `{"older_than": <int>, "limit": <int>}`

**Rationale:** Stable columns make the common counts fast to query and sort. JSON captures the variable filter inputs without over-normalizing. This matches the shape of the existing `PruneEventsResult` / `PruneDeliveriesResult` return types.

### N3 — No-op prune behavior

**Decision:** A successful prune that deletes zero rows **still writes an audit row** with zero counts and the same `summary_json`.

**Rationale:** The invariant says explicit prune operations are operator actions and must leave a durable audit row. If the operator calls `prune_events(...)` and gets back `events_pruned=0`, the database should still record that the call happened, when it happened, and what filters were used. Otherwise the operator cannot distinguish "I haven't pruned yet" from "I pruned and found nothing." This case is now explicitly listed in verification.

### N4 — Python audit read helper

**Decision:** **Yes, add `list_prune_audits(...)`**.

**Signature:** `list_prune_audits(kind=None, since=None, limit=50) -> list[PruneAudit]`

**Rationale:** Knocker is Python-first for operators. Existing read surfaces (`list_events`, `list_deliveries`) follow the same pattern (newest-first, `since`, bounded `limit`). A small read helper keeps operators from dropping to raw SQL for the exact feature this phase adds on purpose.

### A1 — Audit-row non-recursion rule

**Decision:** Prune audit rows are **never** targeted by `prune_events(...)` or `prune_orphan_deliveries(...)`. They form a separate audit trail.

**Rationale:** Without this rule, a later phase could accidentally treat audit rows as ordinary data and prune them, destroying the operator history this phase creates. The spec now includes this as an explicit invariant.

### A2 — Attempt-history responsibility

**Decision:** `knocker_reset_event(...)` does **not** record attempt history. Attempt-history insertion remains the responsibility of handler dispatch (`mark_handled`, `mark_failed`, `mark_ignored`).

**Rationale:** The existing codebase only inserts into `knocker_attempts` from `mark_handled`, `mark_failed`, and `mark_ignored`. `replay(...)`, `requeue(...)`, and `replay_delivery(...)` do not create attempt rows. Reset is a state transition, not a processing attempt. Recording an attempt row on reset would create a fourth bookkeeping variant and mislead operators into thinking a handler run occurred.

### A3 — Count semantics (direct vs cascade)

**Decision:** Counts are defined as **all rows removed from each table as a consequence of the prune operation, whether by direct `DELETE` or by `ON DELETE CASCADE`**.

**Specific mapping:**
- `events_pruned` — direct event-row deletions.
- `deliveries_pruned` — direct delivery-row deletions.
- `attempts_pruned` — attempt rows removed (cascaded from event deletion, because `knocker_attempts` has `ON DELETE CASCADE`).
- `live_jobs_pruned` — explicit live-job deletions performed by the prune path.

**Rationale:** Operators read these counts as facts about the impact of a prune. What matters is "how many rows disappeared from each table," not "which SQL clause caused each disappearance." The current Python implementation already counts attempts before the event delete, which naturally captures cascaded rows. Pinning this definition prevents drift if future refactors change the order of deletions.

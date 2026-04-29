# Reviews And Decisions

This file is append-only.

- Review rounds are written in Session B.
- Decision rounds are written in Session A in response to findings.
- Do not rewrite earlier review text to make it look resolved.

## Intent Review 1

Target:
- spec-diff review

Session:
- B

Model family:
- Claude Opus 4.7

Artifacts reviewed:
- `spec-diff.md`

Context reviewed:
- `SYSTEM.md` (append-only `knocker_deliveries` invariant)
- `.intent/phases/003-operator-read-surface/spec-diff.md` (orphan vs invalid axis, integer-timestamp recency model)

### Positive conformance review

- **P1 — Status-bounding is the right safety posture.** Pinning `handled` / `ignored` as the only prunable terminal states and forbidding `received` / `processing` outright closes the obvious "operator deletes work-in-flight" footgun before plan time. The `failed` / `dead` defer is sensible for v1.
- **P2 — Explicit-only correctly defers scheduling.** Cron / vacuum automation is a separate concern with its own failure modes; keeping 004 to a manual operator surface and naming retention-jobs as a non-goal shrinks blast radius.
- **P3 — Re-uses 003's idioms.** Integer timestamps, conjunctive filters, typed return shapes, and Python-first match what 003 just stabilized. No new vocabulary.

### Negative conformance review

- **N1 — "Older than a caller-provided integer timestamp threshold" doesn't pin the time axis.** `received_at` or `handled_at`? For a `handled` event the operationally meaningful "age" is `handled_at`; for `ignored` it's whenever `mark_ignored` ran (no dedicated column today); for orphan deliveries it's `received_at`. The spec needs to pick a column per category or commit to "always `received_at`" with rationale. Otherwise the planner picks load-bearingly.
- **N2 — Cascade scope per call is unstated.** "Pruning one event removes its linked `Attempt` and `Delivery` rows in the same operation" reads per-event-atomic, but a bulk prune call deletes many events. Is the whole call one transaction (all-or-nothing), or one tx per event (partial-success), or batched? Different failure semantics; pin one.
- **N3 — Filter axes besides status+threshold aren't enumerated.** "All provided filters compose with `AND`" implies more than two, but the only ones named are status and timestamp. Does endpoint filtering exist? `event_type`? If only status+time, say so explicitly; if more, list them.
- **N4 — "Typed summary results" doesn't pin the summary shape.** Counts only? `(events_pruned, attempts_pruned, deliveries_pruned)`? Per-status breakdown? Time range actually pruned? This is a stable-API decision; the plan shouldn't be the one to invent it.

### Adversarial review

- **A1 — Pruning a `handled` event deletes its linked `Delivery` rows, which contradicts SYSTEM.md's "append-only source of truth for inbound HTTP receipt history."** The cascade is reasonable for retention, but it's the *only* deletion path the system has. The spec-diff should explicitly name pruning as the documented exception to the append-only `Delivery` invariant (and the SYSTEM.md note about "enforced by core code paths and tests rather than DDL triggers" needs to acknowledge the prune-cascade carve-out). Right now the two documents quietly disagree.
- **A2 — `failed` / `dead` defer leaves a real footgun.** Operators with high-volume webhook traffic accumulate `dead` events fastest; if 004 ships and 005/006 don't pick up `failed`/`dead` pruning, the deferred surface is the one operators will hit first. Either name the follow-up phase that will own this, or admit `failed`/`dead` pruning is the next-most-urgent gap.
- **A3 — Orphan-delivery pruning vs invalid-delivery pruning.** 003 pinned that orphan (`event_id IS NULL`) and invalid (`signature_valid=0` or NULL) are separate axes. 004 only adds orphan pruning. Today they coincide, so this works. But 003's spec invariant explicitly forbids conflating them. The 004 spec-diff should say "orphan-delivery pruning matches the 003 orphan-delivery filter axis, not the invalid axis," so it doesn't quietly re-conflate.
- **A4 — Method-shape direction unstated.** `Knocker.prune_events(...)` + `Knocker.prune_orphan_deliveries(...)` is one shape; `Knocker.prune(kind="events", ...)` is another. The 003 surface uses verbs (`list_events`, `ignore`), so a `prune_events` / `prune_orphan_deliveries` pair seems more consistent — but the spec-diff should commit to the high-level shape.
- **A5 — Pruning + honker_live consistency unstated.** If a stale honker job points at a pruned `event_id`, the worker hits `get_event` → `KeyError`. `handled` events should already be queue-acked, but the spec should explicitly pin the invariant: "after prune, no row in `_honker_live` references a deleted event," and decide whether prune is responsible for verifying or whether it's a precondition.

### Review verdict

- Spec-diff is well-scoped and well-motivated; the safety posture (status-bounded, explicit, integer-timestamp) is right. But four substantive intent gaps (N1 time-axis, N2 cascade scope, N3 filter enumeration, N4 summary shape) and five adversarial items (A1 append-only carve-out, A2 failed/dead defer rationale, A3 orphan vs invalid conflation, A4 method-shape direction, A5 honker_live consistency) are all load-bearing decisions the planner shouldn't be left to make.
- Highest-priority items to resolve in the spec-diff before the plan: **N1**, **N2**, **N4**, **A1**, **A5**.

## Intent Response 1

Responding to:
- Intent Review 1

Session:
- A

### Inputs

- P1
- P2
- P3
- N1
- N2
- N3
- N4
- A1
- A2
- A3
- A4
- A5

### Decisions

- D1 — Preserve P1, P2, and P3.
  Action: keep the narrow safety posture: explicit-only pruning, status-bounded event pruning, and reuse of `003` idioms.
  Targets: `spec-diff.md`

- D2 — Accept N1.
  Action: pin a single time axis for this minimal phase. Event pruning uses `knocker_events.received_at`; orphan-delivery pruning uses `knocker_deliveries.received_at`.
  Targets: `spec-diff.md`

- D3 — Accept N2.
  Action: make each public prune call all-or-nothing in one transaction, including candidate selection, linked-row deletion, stale live-job cleanup, and summary return.
  Targets: `spec-diff.md`

- D4 — Accept N3.
  Action: enumerate the supported filter axes explicitly. `prune_events(...)` supports `statuses`, `older_than`, and `limit`; `prune_orphan_deliveries(...)` supports `older_than` and `limit`.
  Targets: `spec-diff.md`

- D5 — Accept N4.
  Action: pin the summary result shapes as `PruneEventsResult(events_pruned, attempts_pruned, deliveries_pruned, live_jobs_pruned)` and `PruneDeliveriesResult(deliveries_pruned)`.
  Targets: `spec-diff.md`

- D6 — Accept A1.
  Action: explicitly document pruning as the retention carve-out to the append-only `Delivery` rule; the append-only invariant continues to apply to normal ingest and operator inspection flows.
  Targets: `spec-diff.md`

- D7 — Accept A2 as a defer with rationale.
  Action: state that `failed` / `dead` pruning remains the next retention gap after this minimal slice, but it stays out of `004` because operator-review semantics are not being defined here.
  Targets: `spec-diff.md`

- D8 — Accept A3.
  Action: pin orphan-delivery pruning to the orphan axis (`event_id IS NULL`), not the invalid-signature axis, while keeping the `003` distinction intact.
  Targets: `spec-diff.md`

- D9 — Accept A4.
  Action: commit to a small verb-shaped Python API: `prune_events(...)` and `prune_orphan_deliveries(...)`.
  Targets: `spec-diff.md`

- D10 — Accept A5.
  Action: pin `_honker_live` consistency in the spec diff: after a successful event-prune call, no live job may reference a deleted event id, and prune is responsible for removing any stale live-job rows for deleted events in the same transaction.
  Targets: `spec-diff.md`

### Verification

- `spec-diff.md` now pins the time axis, transaction scope, filter axes, summary shapes, append-only carve-out, orphan-vs-invalid distinction, method shape, and `_honker_live` cleanup responsibility.

### Decision verdict

- Accepted review. The spec diff is now tight enough to write a short plan without the plan making retention-policy decisions on its own.

## Plan Review 1

Target:
- plan review

Session:
- B

Model family:
- Claude Opus 4.7

Artifacts reviewed:
- `spec-diff.md` (post-Intent-Response-1)
- `plan.md`

### Positive conformance review

- **P1 — Plan reads the schema first.** Naming `knocker_attempts.event_id ON DELETE CASCADE`, the lack of cascade on `knocker_deliveries.event_id`, and `_honker_live` payload shape up front means the delete order in step 6–9 is grounded in actual schema, not guesswork. Saves a debugging round.
- **P2 — Each spec-diff invariant maps to a concrete step.** The Mapping-from-spec-diff section walks every `004` invariant (status bounding, transactional scope, cascade scope, orphan axis, typed summary, queue cleanup) to a specific implementation step. No drift between spec-diff and plan.
- **P3 — Traps are well-shaped.** "No pruning UDFs," "no `ignored_at` column," "no dry-run mode," "no failed/dead pruning," "no orphan/invalid conflation," "no JSON UDFs." Each closes a real drift surface that 005 / 006 might want to invent.

### Negative conformance review

- **N1 — `statuses` empty-list / type semantics aren't pinned.** What does `prune_events(statuses=[], older_than=..., limit=...)` do — reject as `ValueError` or no-op return zero counts? What about `statuses=("handled",)` (tuple)? `statuses="handled"` (bare string)? The plan says `list[str] | tuple[str, ...]` in the signature but doesn't say what's rejected. Pin one (recommended: empty list rejects, bare string rejects, tuple OK) so the implementer doesn't pick.
- **N2 — Error-type contract is unstated.** `003` uses `TypeError` for type mismatches and `ValueError` for range violations. The plan should commit to the same: `older_than=2.5` → `TypeError`; `limit=0` / `limit=1001` → `ValueError`; `statuses=["received"]` → `ValueError`. Otherwise the implementer picks per case and the surface drifts from `003`.
- **N3 — `attempts_pruned` count semantics.** Plan step 5 says "count linked attempts before deleting parent rows," then step 9 deletes events (cascade fires for attempts). Good. But the plan should pin: the count is `SELECT COUNT(*)` *before* the cascade, not the SQLite `changes()` rowcount of the cascade itself. Two reasonable approaches; pick one in the plan rather than leaving "count" ambiguous.
- **N4 — Transactionality test is described but the abort mechanism is fragile.** "Prefer a temporary SQLite trigger in test setup that aborts one delete step" — temporary triggers on internal tables coupled to the implementation's exact delete order make the test brittle. A cleaner approach: monkey-patch one of the helpers to raise mid-transaction, then assert the row counts didn't move. Worth specifying the test approach more concretely or accepting a less-strict transactionality test.

### Adversarial review

- **A1 — Worker-claims-during-prune race is unaddressed.** If a worker calls `claim_batch` (which presumably moves a job out of `_honker_live` into a claimed-but-not-acked state) while prune is selecting candidates, the worker holds a job for an `event_id` that prune is about to delete. After prune commits, the worker calls `get_event(event_id)` → `KeyError`. The plan only cleans `_honker_live`, not claimed-but-inflight jobs. Either (i) extend the cleanup to cover claimed jobs, (ii) make the dispatcher gracefully ack-and-skip on missing events, or (iii) document the race as acceptable because `handled` events are already acked in normal flow and `ignored` events go through the new dispatcher short-circuit. Pick one before implementation; this is the prune-equivalent of `003`'s ignore-then-worker race that we already fixed.
- **A2 — `prune_events` "all-or-nothing" claim is in tension with the per-call `limit`.** A 1000-row prune that fails on row 999 rolls back the whole transaction — fine, that's the contract. But operators with huge backlogs will need many prune calls. If each call is independent and atomic, but candidate selection happens inside the transaction, two concurrent prune calls (rare for a single operator surface, but possible in tooling) could pick overlapping candidate sets. SQLite's serialization saves correctness here, but the plan should note that prune is single-writer (no concurrent prune calls expected) so a future caller doesn't try to parallelize.
- **A3 — Dry-run defer is reasonable but the absence of a "preview" at the spec/plan level means operators committing to a 1000-row delete have no way to verify the candidate set first.** Acknowledged in traps. Worth one line of doc on "operators wanting a preview should call `list_events(status='handled', since=0, limit=...)` first," so users don't reach for a flag that doesn't exist.

### Review verdict

- Plan is well-grounded, schema-aware, and maps cleanly from the now-tight spec-diff. The substantive gaps are small and contained: edge-case input semantics (N1), error-type alignment with `003` (N2), `attempts_pruned` count source (N3), transactionality-test mechanism (N4), and the worker-claims-during-prune race (A1).
- Items to resolve before implementation: **N1**, **N2**, **A1**.
- Smaller items acceptable to defer to implementation as long as tests pin them: **N3**, **N4**, **A2**, **A3**.

## Plan Review 2

Target:
- spec-diff + plan re-review after Session A's update pass

Session:
- B

Model family:
- Claude Opus 4.7

Artifacts reviewed:
- `spec-diff.md` (current working tree)
- `plan.md` (current working tree)

Context: Session A reported updating the spec-diff and plan in response to Plan Review 1. This round re-reads both artifacts and reports what landed vs. what still appears open.

### What landed

Re-reading the current `spec-diff.md` and `plan.md`, I do not see substantive new pins for the Plan Review 1 findings. The current spec-diff invariants are identical in shape to the post-Intent-Response-1 version: time axis (line 59), `older_than` strict cutoff (line 60), AND composition (line 61), oldest-first selection (line 62), append-only carve-out (line 64), `_honker_live` cleanup (line 65). The plan's `Phase decisions` block is the same enumeration as before (lines 60–75). No new lines pin `statuses` empty/string/tuple semantics, the error-type contract, the `attempts_pruned` count source, the transactionality-test mechanism, the worker-claim-during-prune race, the single-writer assumption, or the preview-path doc.

If a save didn't go through, that's the easiest explanation. If the updates were cosmetic, Plan Review 1's findings are unaffected. Either way, the items below stand.

### Still open from Plan Review 1

- **N1** (statuses semantics) — spec-diff lists `statuses` as an axis without saying what `[]`, `("handled",)`, or `"handled"` do. Pin one.
- **N2** (error-type contract) — `003` uses `TypeError` for type mismatches, `ValueError` for range/membership violations. Neither spec nor plan commits 004 to the same. Add one line.
- **N3** (attempts_pruned source) — plan step 5 says "count linked attempts before deleting parent rows" without distinguishing pre-delete `COUNT(*)` from cascade `changes()`. Pick one.
- **N4** (transactionality test) — plan still recommends a temporary SQLite trigger. A monkey-patched helper raising mid-tx is less brittle and equally diagnostic.
- **A1** (worker-claim-during-prune race) — spec-diff still says only "no `_honker_live` row may reference a deleted event id." A worker that has already called `claim_batch` may hold a job for an `event_id` that prune is about to delete; on next dispatch `get_event(event_id)` raises `KeyError` and crashes the worker. Pick one of (i) extend cleanup to claimed-but-inflight jobs, (ii) make the dispatcher gracefully ack-and-skip on missing events, or (iii) explicitly accept the race because `handled` events are already acked and `ignored` events go through the dispatcher short-circuit added in 003.
- **A2** (single-writer assumption) — one sentence in `Assumptions and risks`.
- **A3** (preview path doc) — one line in plan or README pointing operators at `list_events(...)` for pre-prune candidate inspection.

### New items found in this round

- **N5 — `_honker_live` queue scoping is implicit.** Plan step 6 says "load `_honker_live` rows for the Knocker queue, parse payload JSON in Python, and collect live-job ids whose `event_id` is in the candidate set." Implicit scoping to `self.queue.name` is correct (Knocker uses one queue per instance per `SYSTEM.md`), but the spec-diff invariant on line 65 says "no `_honker_live` row may reference a deleted event id" — across all queues or just the Knocker queue? If a multi-Knocker setup ever lands and they share a database, prune from one Knocker shouldn't reach into another's queue rows. Pin queue scoping in the spec-diff invariant: "no `_honker_live` row *for this Knocker's queue* may reference a deleted event id."

- **A4 — Plan step 8 deletes `knocker_deliveries` for candidate event ids; this is more than "linked deliveries" if the event identity is reused.** In the current schema, `knocker_deliveries.event_id` is FK to `knocker_events.id` and event ids are unique, so this is fine in practice. But the wording in the plan ("delete `knocker_deliveries` rows for the candidate event ids") and in the spec-diff invariant ("Pruning one event removes its linked `Attempt` rows and linked `Delivery` rows in the same operation") are consistent. Worth adding one explicit acceptance test that confirms a prune of event A doesn't delete deliveries linked to event B with a coincidentally-similar `received_at`. Minor; defer.

### Review verdict

- Either the post-Plan-Review-1 update didn't land, or it was narrower than reported. Plan Review 1's six substantive items (N1–N4, A1, A2) and the small A3 doc note still apply, plus the new N5 (queue scoping) and A4 (cross-event prune-isolation test).
- **Highest priority before implementation: N1, N2, A1, N5.**
- **Acceptable to pin during implementation as long as tests cover them: N3, N4, A2, A3, A4.**

## Plan Response 1

Responding to:
- Plan Review 1
- Plan Review 2

Session:
- A

### Inputs

- P1
- P2
- P3
- N1
- N2
- N3
- N4
- N5
- A1
- A2
- A3
- A4

### Decisions

- D11 — Preserve P1, P2, and P3.
  Action: keep the current Python-first, schema-aware, narrow implementation approach.
  Targets: `plan.md`

- D12 — Accept N1.
  Action: pin `statuses` input semantics in the plan. `prune_events(statuses=...)` accepts a non-empty `list[str]` or `tuple[str, ...]`; a bare string is rejected; an empty collection is rejected.
  Targets: `plan.md`

- D13 — Accept N2.
  Action: align public validation errors with `003` conventions. Type mismatches raise `TypeError`; range or membership violations raise `ValueError`.
  Targets: `plan.md`

- D14 — Accept N3.
  Action: pin `attempts_pruned` as an explicit pre-delete `COUNT(*)` over candidate event ids rather than a cascade rowcount side effect.
  Targets: `plan.md`

- D15 — Accept N4.
  Action: replace the brittle trigger-based transactionality-test note with a monkey-patched mid-transaction failure approach.
  Targets: `plan.md`

- D16 — Accept N5.
  Action: scope `_honker_live` cleanup explicitly to `self.queue.name` in both the spec and the plan.
  Targets: `spec-diff.md`, `plan.md`

- D17 — Accept A1.
  Action: pin the prune/worker race behavior. If dispatch sees a claimed job whose event row was explicitly pruned, it attempts best-effort queue cleanup and exits quietly rather than crashing on `KeyError`.
  Targets: `spec-diff.md`, `plan.md`

- D18 — Accept A2 as a note, not a scope increase.
  Action: record the single-writer assumption in `Assumptions and risks`; do not broaden the slice to concurrent-prune coordination.
  Targets: `plan.md`

- D19 — Accept A3.
  Action: document the preview path as the existing `003` read surface (`list_events(...)` / `list_deliveries(...)`) rather than adding a dry-run feature.
  Targets: `plan.md`

- D20 — Accept A4.
  Action: add a cross-event isolation test expectation so pruning one event is explicitly shown not to touch another event's deliveries.
  Targets: `plan.md`

### Verification

- `spec-diff.md` now scopes `_honker_live` cleanup to this Knocker queue and pins missing-event dispatch after prune as a non-crashing path.
- `plan.md` now pins `statuses` semantics, error-type behavior, `attempts_pruned` count source, queue-scoped cleanup, preview-path docs, a less brittle transactionality test shape, and the cross-event isolation test.

### Decision verdict

- Accepted review. The plan is now tight enough to implement without the code deciding API semantics on its own.

## Implementation Review 1

Target:
- implementation review

Session:
- B

Model family:
- Claude Opus 4.7

Artifacts reviewed:
- `spec-diff.md` (working tree)
- `plan.md` (working tree)
- `commits.txt` (working tree)
- `packages/knocker/python/knocker/_knocker.py` (working-tree diff vs `HEAD`)
- `packages/knocker/python/knocker/__init__.py` (working tree)
- `tests/test_knocker_core.py` (working-tree diff vs `HEAD`)
- `README.md`, `packages/knocker/README.md` (working-tree diff vs `HEAD`)

The implementation is uncommitted in the working tree. `commits.txt` shows `- Pending`. No phase commit yet.

### Positive conformance review

- **P4 — Public surface and result types match the spec exactly.** `Knocker.prune_events(*, statuses, older_than, limit)` and `Knocker.prune_orphan_deliveries(*, older_than, limit)` are keyword-only with the signatures pinned in the plan. `PruneEventsResult(events_pruned, attempts_pruned, deliveries_pruned, live_jobs_pruned)` and `PruneDeliveriesResult(deliveries_pruned)` are frozen `slots=True` dataclasses, re-exported from [`__init__.py`](../../../packages/knocker/python/knocker/__init__.py), and asserted via `==` in the new tests.
- **P5 — D14 (`attempts_pruned` source) lands as written.** `_count_event_attempts` runs an explicit `SELECT COUNT(*) FROM knocker_attempts WHERE event_id IN (...)` before the events are deleted, so the value is the pre-cascade count, not a `changes()` side effect. Same for `deliveries_pruned`. ([_knocker.py:679-700](../../../packages/knocker/python/knocker/_knocker.py#L679-L700))
- **P6 — D16 (queue scoping) lands.** `_stale_live_job_ids` filters `WHERE queue=?` against `self.queue.name` only. `test_prune_events_only_cleans_live_jobs_for_this_knocker_queue` asserts a foreign queue's row survives the prune. ([_knocker.py:702-718](../../../packages/knocker/python/knocker/_knocker.py#L702-L718))
- **P7 — D17 (worker-claim race) lands as a graceful exit.** `_dispatch_job` now wraps `get_event` in `try/except KeyError` and on `KeyError` opens a fresh transaction, calls `queue.ack`, and returns without crashing. `test_prune_events_missing_event_dispatch_exits_quietly_for_claimed_job` exercises the full flow: claim → prune deletes event + live row → dispatch → ack-or-noop → no worker crash. ([_knocker.py:782-789](../../../packages/knocker/python/knocker/_knocker.py#L782-L789))
- **P8 — D12 / D13 (statuses + error contract) land verbatim.** `_coerce_prune_statuses` rejects bare strings (`TypeError`), non-list/tuple (`TypeError`), empty collections (`ValueError`), non-string elements (`TypeError`), and unsupported statuses (`ValueError`). `_coerce_older_than` rejects bools/non-ints (`TypeError`). `test_prune_events_rejects_invalid_status_inputs_and_types` asserts every distinct error path. ([_knocker.py:935-953](../../../packages/knocker/python/knocker/_knocker.py#L935-L953))
- **P9 — D15 (transactionality test) replaced with the cleaner monkey-patch shape.** `test_prune_events_rolls_back_when_delete_step_raises` patches `_delete_deliveries_for_event_ids` to delete then raise, then asserts event row, delivery row, and live-job row all survive. No fragile triggers. ([test_knocker_core.py:1166-1190](../../../tests/test_knocker_core.py#L1166-L1190))
- **P10 — D20 (cross-event isolation) test exists.** `test_prune_events_keeps_other_event_deliveries_intact` ingests two events, prunes only the older one, asserts the other event and its delivery are untouched.
- **P11 — D19 (preview path doc) landed in both READMEs.** Both `README.md` and `packages/knocker/README.md` show the read-surface preview pattern (`list_events(status="handled", since=0, limit=50)`) immediately above the prune calls, matching the plan's commitment that `003`'s read surface is the dry-run substitute.
- **P12 — Validation order is fail-fast.** `prune_events` runs `_coerce_prune_statuses → _coerce_older_than → _coerce_limit` before opening the transaction. Bad inputs never even start a tx.

### Negative conformance review

- **N6 — `_dispatch_job` missing-event ack is silent on `ack==False`; ignored-event ack is strict on `ack==False`. The two paths now diverge in failure handling without comment.** Look at [_knocker.py:782-799](../../../packages/knocker/python/knocker/_knocker.py#L782-L799): the missing-event branch calls `self.queue.ack(...)` and ignores the return value. The ignored-event branch calls `_require_queue_transition(self.queue.ack(...), ...)` which raises `_QueueTransitionError` on `False`. The asymmetry is defensible (a missing event is retention residue; ignored is normal flow with a held claim) but it isn't documented in code or in the spec-diff invariant. A one-line comment near the missing-event branch ("ack is best-effort here; the event row is gone, residue is acceptable") would close the readability gap.
- **N7 — Phase has no `make test` evidence.** `commits.txt` is `# 004-minimal-retention-and-pruning\n- Pending`. The plan's "Tests and evidence" section requires `make test` and the build order step 6 says "record evidence." No captured run yet. Same gap as 003 had at this stage of review — documenting "made it, didn't run it" isn't evidence.
- **N8 — `prune_events` doesn't observe its own audit trail.** Successful prune calls don't write any row to `knocker_attempts` or any other history table. After prune, there's no in-database trace of "operator pruned 50 events at timestamp T." For an operator surface that explicitly deletes data, even a single audit-log row per prune call would be useful for postmortems. Not in the spec-diff and not in the plan, so technically not a drift — but it's a real operability gap, worth either accepting explicitly ("prune does not write its own audit row in 004; operators rely on application logging") or flagging for a future slice.

### Adversarial review

- **A5 — `_stale_live_job_ids` loads every row in `_honker_live` for the queue.** For a Knocker instance with millions of pending events, prune's queue-scan cost grows with the queue, not with the prune limit. For a 1000-event prune against a 5M-row `_honker_live`, that's 5M rows pulled into Python before filtering. Acceptable for v1 (operator action, manual cadence) but worth noting in `Assumptions and risks`. A more targeted approach — `WHERE queue=? AND payload IN (...)` with constructed event-id payload literals, or a JSON-extract index — is a future optimization. Defer.
- **A6 — Catching `KeyError` broadly in `_dispatch_job` could mask non-pruning bugs.** The only `KeyError` in `get_event` is the explicit "unknown event id" raise, but `_event_from_row` does `dict[...]` lookups on the row dict; if a column is ever renamed or dropped, that would raise `KeyError` and silently get swallowed by the new ack-and-exit path. Tighter catch (e.g., a sentinel or an explicit `if not rows` check before constructing `_event_from_row`) would scope the silent-recovery path to "event was deleted" only. Defer; row-shape mismatches are a build-time bug that tests would catch first.
- **A7 — `_event_id_from_payload_json` returns `None` on any parse failure (TypeError, ValueError, JSONDecodeError, missing key, wrong type, non-int).** That's safe-by-default — a malformed live-job payload is left alone instead of accidentally pruned. Good defensive choice. Worth one comment line stating the safety property so a future cleanup doesn't tighten this into "raise on malformed payload" without thinking through the consequences. Minor.
- **A8 — Test `test_prune_events_missing_event_dispatch_exits_quietly_for_claimed_job` doesn't actually assert that prune itself found and removed the claimed-but-still-live job.** The test asserts `_honker_live` is empty after dispatch, which is consistent with both "prune removed the live row, dispatch's ack was a no-op" *and* "prune left the live row, dispatch's ack removed it." Adding `assert prune.live_jobs_pruned == 1` would pin the prune-side behavior unambiguously. Defer; the spec invariant (line 66) and the queue-scoping test together cover the intent.

### Review verdict

- All Plan-Response-1 decisions (D11–D20) are reflected in code and tests. Public surface, result shapes, error contract, count semantics, queue scoping, worker-claim handling, transactionality testing, cross-event isolation, and the preview path are each implemented and asserted by name.
- Two open items are housekeeping:
  - **N6** — one-line comment explaining the silent-ack asymmetry between missing-event and ignored-event paths
  - **N7** — capture `make test` evidence in `commits.txt` before the phase commits
- Three deferred:
  - **N8** — accept "no per-prune audit row in 004" explicitly or flag for a later slice
  - **A5** — note the full-queue scan cost in `Assumptions and risks`
  - **A8** — tighten the missing-event dispatch test to also assert `prune.live_jobs_pruned == 1`

With **N6** and **N7** addressed, the phase is ready to commit.

## Implementation Response 1

Responding to:
- Implementation Review 1

Session:
- A

### Inputs

- P4
- P5
- P6
- P7
- P8
- P9
- P10
- P11
- P12
- N6
- N7
- N8
- A5
- A6
- A7
- A8

### Decisions

- D21 — Preserve P4 through P12.
  Action: keep the implemented Python pruning surface, validation behavior, queue scoping, worker stale-claim handling, rollback test, isolation test, and preview-path docs as landed.
  Targets: implementation files

- D22 — Accept N6.
  Action: add a one-line code comment near the missing-event dispatch branch clarifying that ack is best-effort there because the event row is already gone and the job is stale retention residue.
  Targets: `packages/knocker/python/knocker/_knocker.py`

- D23 — Accept N7.
  Action: record the green `make test` run in `commits.txt` before any phase commit.
  Targets: `.intent/phases/004-minimal-retention-and-pruning/commits.txt`

- D24 — Accept N8 as an explicit defer.
  Action: `004` does not write a per-prune audit row. Operators rely on application logging for prune invocation history in this minimal slice; richer retention auditability can be considered later.
  Targets: `reviews_and_decisions.md`

- D25 — Accept A5.
  Action: note the full-queue scan cost of `_stale_live_job_ids(...)` in `Assumptions and risks`.
  Targets: `plan.md`

- D26 — Defer A6.
  Action: keep the current `KeyError` recovery path for this slice. Tightening it to distinguish missing-event from row-shape bugs is reasonable polish but not necessary to commit `004`.
  Targets: later follow-up if needed

- D27 — Accept A7.
  Action: add a short safe-by-default comment to `_event_id_from_payload_json(...)` so malformed payloads are intentionally left unmatched instead of causing accidental prune matches.
  Targets: `packages/knocker/python/knocker/_knocker.py`

- D28 — Accept A8.
  Action: strengthen the claimed-job race test by asserting `prune.live_jobs_pruned == 1`, so the test pins prune-side cleanup rather than only the post-dispatch end state.
  Targets: `tests/test_knocker_core.py`

### Verification

- Added the best-effort-ack comment in `_dispatch_job(...)`.
- Added the safe-by-default comment in `_event_id_from_payload_json(...)`.
- Strengthened the claimed-job race test with `assert prune.live_jobs_pruned == 1`.
- Recorded `make test` evidence in `commits.txt`.
- Re-ran `make test` successfully:
  - Rust: 8 passed
  - Python: 31 passed
  - Node: 2 passed

### Decision verdict

- Accepted review. `004` is now commit-ready.

## Implementation Review 2 (final)

Target:
- final implementation review (post-Implementation-Response-1)

Session:
- B

Model family:
- Claude Opus 4.7

Artifacts reviewed:
- `commits.txt`
- commit `a344242` (`packages/knocker/python/knocker/_knocker.py`, `tests/test_knocker_core.py`)
- `plan.md` (working tree)

### What landed

- **D22 / N6 — Best-effort ack comment.** `_dispatch_job` missing-event branch carries `# Best-effort ack: a pruned event means this claim is stale retention residue, so we exit quietly even if the live row is already gone.` Asymmetry with the ignored-event branch is now self-documenting.
- **D23 / N7 — Evidence captured.** [commits.txt](commits.txt) records phase commit `a344242` plus a dated `make test` run: Rust 8, Python 31, Node 2.
- **D24 / N8 — Explicit defer.** Recorded in Implementation Response 1 (D24): "operators rely on application logging for prune invocation history in this minimal slice."
- **D25 / A5 — Full-queue-scan note.** [plan.md:211](plan.md#L211) carries the assumption explicitly.
- **D27 / A7 — Safe-by-default comment.** `_event_id_from_payload_json` now states `# Safe by default: malformed payloads are left alone rather than accidentally matched and pruned.`
- **D28 / A8 — Test tightened.** `test_prune_events_missing_event_dispatch_exits_quietly_for_claimed_job` now asserts `prune.live_jobs_pruned == 1`, pinning the prune-side cleanup.
- **D26 / A6 — Explicitly deferred** as polish; acceptable per process.

### Verdict

- All blocking items (N6, N7) closed. Deferred items each carry an explicit decision (D24, D25, D26, D27).
- Phase `a344242` is on `main` with two follow-up commits (`19ddee0` recording 004 evidence, `4136635` folding 004 into the system baseline). Implementation, tests, evidence, and intent artifacts are coherent.
- **004 is shipped.** No further review needed.

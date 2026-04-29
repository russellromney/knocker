# Reviews and Decisions: 003 Operator Read Surface

## Round 1 — Spec Diff Review

Artifacts reviewed:

- `.intent/phases/003-operator-read-surface/spec-diff.md`

Context reviewed:

- `SYSTEM.md` (current baseline, post-002)
- `.intent/phases/002-append-only-delivery-rows/reviews_and_decisions.md` (decisions already absorbed)

### Positive conformance review

**P1 — Slice sits in the right place.** 002 introduced the Delivery/Event split. Without an operator-facing read surface, the new model is internally correct but practically opaque: ops staff would have to write SQL against `knocker_deliveries` and `knocker_events` directly. 003 turns the new nouns into something operable. Logical follow-on, correct sequencing.

**P2 — Non-goals are explicit and tight.** "No retention rules, HTML admin UI, or hosted control plane" and "no business idempotency helpers or per-delivery processing modes" are exactly the surfaces this slice could accidentally drift into. Naming them up front shrinks the slice's blast radius.

**P3 — Verification criteria map to user stories.** Each line in "How we will verify it" reads like something an operator would actually do — list events with filters, inspect one event and see its deliveries, list orphan deliveries from invalid receipts, etc. That's the right shape for verification: operator behavior, not internal machinery.

**P4 — The bottom note is self-aware.** "If planning uncovers that a stable read/query contract belongs in `knocker-honker` rather than only in the Python binding, that should be surfaced in the plan rather than silently decided in implementation." That's exactly the kind of preemptive trap that prevents architectural drift during implementation.

### Negative conformance review

**N1 — "Stable APIs" is asserted but not defined.** The spec-diff says the operator surface "exposes these queries as stable APIs rather than incidental helpers." Stable on what axis? Names that don't change? Semver promises? Backwards-compat shims? Without a definition, the planner picks, and the picked meaning becomes load-bearing for every future slice that touches this surface. Worth pinning: "stable means the public method names and return types do not change in subsequent slices without an explicit deprecation cycle."

**N2 — "Replay, requeue, and ignore are grouped with the operator surface as supported recovery actions" is ambiguous.** Those operations already exist on `Knocker` from earlier slices. Three possible interpretations:
- Doc change only: same methods, now described under an "operator surface" heading.
- Restructure: methods move under e.g. `Knocker.operator.replay(...)`.
- Reshape: same methods, but return types and parameters are aligned with the new list/get APIs.

The spec-diff should pick one. Otherwise this is a load-bearing planner decision masquerading as a definitional sentence.

**N3 — Filter axes are listed but not specified.** "By status, endpoint, event type, and recency" — recency in particular is underspecified. Possible meanings:
- `since` timestamp filter
- `limit` with default-sort-by-received_at
- Cursor-style `before`/`after` pagination
- Any combination

Each shape implies a different API surface. The spec-diff should commit to one or explicitly defer to the plan with named alternatives.

**N4 — Pagination is implicit.** "List recent events" without bounded result sets is a footgun for operators with months of webhook history. The spec-diff doesn't say whether listing returns all rows, a default-limited window, or paged results. Either commit ("default limit 100, opt-in unbounded") or list pagination as an open planning question.

**N5 — Filter-by-verification-outcome is in verification but not "What changes".** "Operators can filter deliveries by verification outcome without relying on handwritten SQL in tests" appears under "How we will verify it" — but it's a behavioral commitment, not just a test. The API must support that filter axis. Move it into "What changes" so it doesn't get lost.

**N6 — Relationship to 002's existing read helpers is unstated.** 002 already shipped `knocker_get_delivery` / `knocker_list_deliveries` (Rust SQL functions) plus Python `Knocker.list_events`, `list_deliveries`, `get_event`, `get_delivery`. Does 003 keep these as-is, expand them, replace them, or wrap them? Without a position, the planner could quietly redo all of them under a new namespace and call it "stabilization."

**N7 — "First supported operator surface" implies more.** "The Python binding becomes the first supported operator surface" — first implies subsequent. Node? Other languages? Currently out of scope per non-goals, but the "first" framing nudges the planner toward designing the Python API with future cross-language parity in mind. If the operator surface is intentionally Python-only for v1, say so. If future languages are anticipated, say that too. Don't leave it ambiguous.

### Adversarial review

I tried to find decisions an executor could resolve load-bearingly that the spec-diff doesn't pin.

**A1 — "Supported" isn't operationally defined.** What test would fail if the surface stops being "supported"? Documented? Tested? Versioned? Without an operational definition, the planner can claim "supported" by adding three docstrings and call it done.

**A2 — Read consistency under concurrent worker activity is unstated.** If an operator calls `list_events(status='processing')` while a worker is finishing a job, what guarantee holds? Snapshot consistency? Best-effort? Most operator surfaces accept best-effort, but the spec-diff should say so explicitly so a future user doesn't expect transactional reads.

**A3 — Where does the read contract actually live?** The bottom note flags this but doesn't resolve. If the plan decides "Python-only surface, no Rust contract changes," that's fine and small. If the plan decides "expose `knocker_list_events` etc. as SQL UDFs in `knocker-honker`," that's a bigger change with downstream implications for Node and any future bindings. The spec-diff should at least name the two paths and say which it leans toward.

**A4 — No trap forbidding raw-row leakage.** "Stable APIs rather than incidental helpers" implies polished return types. But nothing in the spec-diff forbids the planner from returning raw `dict[str, Any]` rows from SQL with column names like `headers_json`. If the operator API surface ends up shaped like SQL rows, "stable" becomes "stable wrapping of internal columns" — which is the opposite of what stable should mean. Worth an explicit trap: "operator-facing return types are typed objects (Event, Delivery), not row dicts."

**A5 — No trap forbidding new state machine transitions.** Replay, requeue, and ignore "grouped with the operator surface" could quietly become an excuse to add new operator-only state transitions (e.g., `force_handle`, `mark_dead_manually`). Worth a trap: "no new event state transitions land in this slice; replay/requeue/ignore are the existing set."

**A6 — Filtering by event_type relies on a free-form string column.** `event_type` on `knocker_events` is `TEXT`. Filtering by it works, but operators who try `event_type='checkout.session.completed'` and one provider sends `'CHECKOUT.SESSION.COMPLETED'` will be confused. Not necessarily a 003 problem, but the spec-diff should at least note that string-equality on `event_type` is the contract — no normalization, no case-insensitivity, no fuzzy match.

**A7 — Orphan deliveries vs invalid deliveries are conflated.** Verification line: "Operators can list orphan deliveries created by invalid signatures." But "orphan" (event_id IS NULL) and "invalid" (signature_valid=0) aren't the same predicate, even though they coincide today. Imagine a future where some deliveries are orphaned for non-verification reasons (out-of-scope routing, malformed body, whatever). The API should query on the predicate the operator cares about, not the one that happens to coincide today. Recommend the API exposes both axes (`event_id IS NULL` and `signature_valid = 0`) and the spec-diff acknowledges they are not the same.

### Overall

This is a coherent, well-scoped slice with explicit non-goals and a self-aware note about a known architectural question. The shape is right. The gaps are mostly underspecifications that would force the planner into load-bearing decisions the spec-diff hasn't taken a position on.

Items I'd ask Session A to resolve before writing the plan:

- **N1** — pin what "stable" means
- **N2** — pick a meaning for "grouped with the operator surface"
- **N3** — commit to a recency filter shape (or explicitly defer with named alternatives)
- **N6** — state the relationship to 002's existing read helpers
- **A3** — name the two architectural paths (Python-only vs Rust contract) and say which the slice leans toward

Smaller items (acceptable to defer to the plan):

- **N4** — pagination shape
- **N5** — move filter-by-verification-outcome into "What changes"
- **N7** — clarify whether "first" implies future cross-language parity
- **A1, A2** — define "supported"; state read-consistency expectations
- **A4, A5** — add traps forbidding raw-row leakage and new state transitions
- **A6, A7** — note the `event_type` matching contract; separate orphan from invalid

Once the five higher-priority items are pinned in the spec-diff, the plan can land cleanly. Without that pinning, the plan will be doing intent work that belongs in the spec-diff.

## Intent Response 1

Responding to:

- Round 1 — Spec Diff Review

### Inputs

- P1
- P2
- P3
- P4
- N1
- N2
- N3
- N4
- N5
- N6
- N7
- A1
- A2
- A3
- A4
- A5
- A6
- A7

### Decisions

- D1 — Accept N1.
  Action: define `stable` in the spec diff as documented, tested, typed public operator APIs whose method names and return shapes do not change without an explicit later deprecation or replacement decision.
  Targets: `spec-diff.md`

- D2 — Accept N2.
  Action: pin `replay`, `requeue`, and `ignore` as the existing event-level recovery actions. `003` documents and tests them as part of the supported operator surface; it does not move or reshape them.
  Targets: `spec-diff.md`

- D3 — Accept N3 and N4 together.
  Action: pin recency as newest-first ordering with optional `since`, default `limit=100`, and explicit caller-provided limit. Defer cursor pagination.
  Targets: `spec-diff.md`

- D4 — Accept N5.
  Action: move delivery verification-outcome filtering into `What changes` as a first-class operator behavior.
  Targets: `spec-diff.md`

- D5 — Accept N6.
  Action: state that `003` stabilizes the existing Python helpers from `002` (`list_events`, `get_event`, `list_deliveries`, `get_delivery`) rather than replacing them with a new namespace.
  Targets: `spec-diff.md`

- D6 — Accept N7 and A3 together.
  Action: choose the Python-first operator-surface path for v1. `knocker-honker` may gain helper queries only in support of that surface; `003` does not promise cross-binding operator parity or a second standalone operator API contract.
  Targets: `spec-diff.md`

- D7 — Accept A1 and A4 together.
  Action: define "supported" operationally and forbid raw-row leakage by pinning typed `Event` and `Delivery` return objects in the spec diff.
  Targets: `spec-diff.md`

- D8 — Accept A2.
  Action: state that operator reads are best-effort reads of stored state under concurrent worker activity, not a cross-call snapshot guarantee.
  Targets: `spec-diff.md`

- D9 — Accept A5.
  Action: add a no-new-state-transitions boundary to the spec diff.
  Targets: `spec-diff.md`

- D10 — Accept A6.
  Action: pin `event_type` filtering to exact string equality only.
  Targets: `spec-diff.md`

- D11 — Accept A7.
  Action: separate orphan-delivery and invalid-delivery concepts in the operator surface and keep both filter axes available.
  Targets: `spec-diff.md`

- D12 — Preserve P1, P2, P3, and P4.
  Action: keep the slice sequencing, non-goals, operator-shaped verification, and the explicit architectural note intact while tightening the spec.
  Targets: `spec-diff.md`

### Verification

- `spec-diff.md` now defines `stable`, recovery-action scope, recency shape, pagination boundary, verification-outcome filtering, the relationship to `002` helpers, the Python-first architectural lean, typed return shapes, best-effort read consistency, exact `event_type` matching, and the orphan-vs-invalid distinction.

### Decision verdict

- Accepted review. The spec diff is tightened enough that `plan.md` can now map intent to implementation without making upstream product decisions.

## Round 2 — Plan Review

Artifacts reviewed:

- `.intent/phases/003-operator-read-surface/spec-diff.md` (post-Intent-Response-1)
- `.intent/phases/003-operator-read-surface/plan.md`

### Positive conformance review

**P1 — Plan maps every spec-diff invariant to a concrete step.** The "Mapping from spec diff to implementation" section walks each spec-diff commitment (stabilize 002 helpers, expand filters, typed returns, Python-first, recovery actions) to a specific code change. That's the right shape — no intent decisions hiding inside implementation prose.

**P2 — Phase decisions section resolves implementation choices before code lands.** Newest-first ordering, `limit=100` default, integer-timestamp `since`, no DST math, exact `event_type` equality, no Rust UDFs by default, separate orphan and invalid delivery filters. Each is a specific commitment the planner is making explicit instead of leaving the implementer to pick.

**P3 — Function signatures are concrete enough to review.** `list_events(status=None, endpoint=None, event_type=None, since=None, limit=100)` and `list_deliveries(event_id=None, endpoint=None, signature_valid=_UNSET, orphaned=_UNSET, since=None, limit=100)` are reviewable as-is — most plans hand-wave at this level.

**P4 — Traps and "Areas not to touch" close off the obvious drift surfaces.** New `Knocker.operator.*` namespace forbidden, no separate cross-binding contract, no raw rows, no new state transitions, no cursor pagination, no DST math. Negative space explicit.

**P5 — Ambiguities section is honest.** "None that require a spec diff change before implementation." That's the right answer if it's true; making the planner say it explicitly forces them to actually check.

### Negative conformance review

**N1 — `_UNSET` sentinel for tri-state filtering is a load-bearing API choice the spec-diff doesn't pin.** The plan's `signature_valid=_UNSET` and `orphaned=_UNSET` resolve a tri-state question — filter-true / filter-false / no-filter — that `Optional[bool]` could also have resolved (with the convention `None` = no filter). The plan picks the sentinel approach silently. That's a planner-level API design call worth either pinning in the plan with rationale ("`Optional[bool]` is ambiguous because the column is nullable; sentinel disambiguates") or surfacing back to the spec-diff.

**N2 — `ignore(event_id)` is conditional in the plan; should be committed.** The plan says "Add `ignore(event_id)` to the Python public surface if it does not already exist." Reading [`packages/knocker/python/knocker/_knocker.py`](../knocker/packages/knocker/python/knocker/_knocker.py), there is no `Knocker.ignore()` method today — `mark_ignored` is internal. So this addition is net new. The plan should commit ("add `Knocker.ignore(event_id)`") rather than leaving it conditional. Conditional plan items hide implementation surprises.

**N3 — "Concrete need for Rust helper queries" is an unmeasurable escape hatch.** Plan: "Read queries remain implemented in Python SQL for now unless execution reveals a concrete need for Rust helper queries." What test or signal makes a need "concrete"? Without a definition, the planner could mid-implementation decide "we need it" and slide into a Rust UDF expansion that wasn't reviewed. Either define the bar ("concrete need = the Python query plan can't express it, OR a measured perf threshold is missed") or remove the escape hatch and commit to Python-only for this slice.

**N4 — Filter composition (AND vs OR) is unstated.** `list_events(status='received', endpoint='stripe', event_type='checkout.session.completed', since=1234567890, limit=50)` — almost certainly AND across all filters, but the plan doesn't say. Worth one sentence: "All non-default filter parameters compose with AND."

**N5 — "`get_event` and `get_delivery` are unchanged in spirit" is vague.** Plan says: "ensure their row selection and parsing remain consistent with the stabilized return types." That implies they might change. If they're staying byte-for-byte identical, say so. If something is being touched (e.g., consistent ordering of nullable fields, parsing tweaks), say what.

### Adversarial review

I tried to find places the plan punts decisions to implementation that should land before code.

**A1 — Newest-first ordering tie-break is unstated.** Two events at the same `received_at` (same Unix second under load) — what's the secondary sort? Almost certainly `id DESC` for stability, but the plan doesn't say. Tests asserting order could be flaky if the implementer picks differently.

**A2 — `since` inclusivity is unstated.** Inclusive (`received_at >= since`) or exclusive (`received_at > since`)? Both are reasonable. Pin one. Otherwise the test `since=event.received_at` either returns the event or doesn't, depending on the implementer's call.

**A3 — Empty-filter behavior is unstated.** `list_events()` with all defaults — returns the 100 most recent events across all endpoints/statuses, or requires at least one filter? Almost certainly the former, but worth confirming.

**A4 — `limit` has no upper bound.** A caller passing `limit=1000000` would attempt to materialize a million rows in Python. For an operator surface, that's a footgun. Either set an upper bound (`max_limit=1000`) or explicitly say "no cap; caller responsible."

**A5 — `ignore()` valid prior states are unstated.** SYSTEM.md says `ignored` means "intentionally not processed by operator or application policy." From which event statuses is `ignore()` accepted? Probably `received`, `processing`, `failed`, `dead`. From `handled` it's nonsensical. The plan should commit, ideally referencing the existing `mark_ignored` constraints.

**A6 — Test plan doesn't include "002 helper call sites continue to work."** The spec-diff says: "Existing 002 helper call sites continue to work through the stabilized API surface rather than being replaced by a new namespace." This is verifiable by leaving 002's test cases unchanged and watching them pass. Implicit in `make test`, but worth being explicit so the reviewer can grep the test diff and confirm no 002 tests were modified.

**A7 — Best-effort read consistency is a documented invariant with no test.** The spec-diff says reads are best-effort under concurrent worker activity. The plan doesn't include a test for this. Hard to test directly (would need a controlled race), and probably acceptable as a documented contract — but the plan should acknowledge "no test pins this; it lives as documented behavior."

### Overall

The plan is in good shape and reflects the tightened spec-diff. The implementation sequence is reasonable, function signatures are concrete enough to review, and the traps cover the obvious drift surfaces.

Items I'd ask Session A to resolve before implementation:

- **N1** — pin the sentinel-vs-Optional[bool] choice with rationale, or surface back to spec-diff
- **N2** — commit unconditionally to adding `Knocker.ignore(event_id)`
- **N3** — define "concrete need" for Rust UDFs or drop the escape hatch
- **A2** — pin `since` inclusivity (one sentence)
- **A4** — pin `limit` upper bound or explicitly state "no cap"
- **A5** — list valid prior statuses for `ignore()`

Smaller items (acceptable to defer to implementation as long as tests assert):

- **N4** — name AND composition explicitly
- **N5** — clarify what changes for `get_event` / `get_delivery`
- **A1** — name the tie-break (`id DESC`)
- **A3** — confirm empty-filter returns latest 100 across the board
- **A6** — add an explicit "002 tests still pass without modification" line to the test plan
- **A7** — document that best-effort consistency is a contract, not a tested invariant

Once the six higher-priority items above are pinned, the plan is publishable to the executor.

## Plan Response 1

Responding to:

- Round 2 — Plan Review

### Inputs

- P1
- P2
- P3
- P4
- P5
- N1
- N2
- N3
- N4
- N5
- A1
- A2
- A3
- A4
- A5
- A6
- A7

### Decisions

- D13 — Preserve P1, P2, P3, P4, and P5.
  Action: keep the current plan structure, concrete signatures, traps, and honest ambiguity check.
  Targets: `plan.md`

- D14 — Accept N1.
  Action: drop the `_UNSET` sentinel and use `Optional[bool]` filter parameters for `signature_valid` and `orphaned`, with explicit plan rationale.
  Targets: `plan.md`

- D15 — Accept N2.
  Action: commit unconditionally to adding `Knocker.ignore(event_id)` in the Python public surface.
  Targets: `plan.md`

- D16 — Accept N3.
  Action: remove the Rust-helper escape hatch and commit to Python SQL reads for this slice.
  Targets: `plan.md`, `spec-diff.md`

- D17 — Accept N4.
  Action: pin list-filter composition to `AND`.
  Targets: `plan.md`

- D18 — Accept N5.
  Action: clarify that `get_event(...)` and `get_delivery(...)` stay unchanged in signature and typed return shape.
  Targets: `plan.md`

- D19 — Accept A1 and A2 together.
  Action: pin newest-first ordering as `received_at DESC, id DESC` and make `since` inclusive.
  Targets: `plan.md`, `spec-diff.md`

- D20 — Accept A3.
  Action: state that empty-filter calls return the latest bounded rows across the full set.
  Targets: `plan.md`, `spec-diff.md`

- D21 — Accept A4.
  Action: cap `limit` at `1000` and reject values outside `1..1000`.
  Targets: `plan.md`, `spec-diff.md`

- D22 — Accept A5.
  Action: pin public `ignore(event_id)` semantics: accept `received`, `failed`, and `dead`; no-op on `ignored`; reject `processing` and `handled`.
  Targets: `plan.md`

- D23 — Accept A6.
  Action: make continued `002` helper-call-site compatibility an explicit evidence item in the test plan.
  Targets: `plan.md`

- D24 — Accept A7.
  Action: keep best-effort read consistency as a documented contract and state plainly that this slice does not race-test it.
  Targets: `plan.md`

- D25 — Record the DST defer rationale explicitly.
  Action: note that DST-aware / local-day operator windows are deferred to a later deterministic simulation testing slice.
  Targets: `plan.md`, `spec-diff.md`

### Verification

- `spec-diff.md` and `plan.md` now pin the remaining API edges before implementation:
  - Python-only reads
  - inclusive `since`
  - newest-first stable ordering
  - bounded `limit`
  - empty-filter behavior
  - unconditional `ignore(...)`
  - valid prior statuses for `ignore(...)`
  - DST-aware filtering deferred to deterministic simulation testing later

### Decision verdict

- Accepted review. The plan is tightened enough to implement without leaving API-shape decisions for the executor.

## Round 3 — Implementation Review

Artifacts reviewed:

- `.intent/phases/003-operator-read-surface/spec-diff.md`
- `.intent/phases/003-operator-read-surface/plan.md`
- `packages/knocker/python/knocker/_knocker.py` (working-tree diff vs `HEAD`)
- `tests/test_knocker_honker.py` (working-tree diff vs `HEAD`)
- `README.md`, `packages/knocker/README.md` (working-tree diff vs `HEAD`)
- `knocker-honker/src/knocker_ops.rs` (read-only context for `mark_ignored`, `replay`, `requeue`, `mark_processing`)
- `SYSTEM.md`

The implementation is uncommitted in the working tree. There is no `commits.txt` for this phase yet, so this review is against the pending diff.

### Positive conformance review

**P1 — Public surface matches the pinned plan.** `Knocker.list_events(status, endpoint, event_type, since, limit=100)` and `Knocker.list_deliveries(event_id, endpoint, signature_valid, orphaned, since, limit=100)` use the exact `Optional[bool]` shape and parameter names committed in D14–D24. No new namespace, no `_UNSET` sentinel for the bool filters, no Rust UDF additions, no cursor params. ([_knocker.py:435-481](../../../packages/knocker/python/knocker/_knocker.py#L435-L481), [_knocker.py:511-561](../../../packages/knocker/python/knocker/_knocker.py#L511-L561))

**P2 — Ordering and recency invariants land verbatim.** Both list methods emit `ORDER BY ... received_at DESC, ... id DESC LIMIT ?`, and `since` is rendered as `received_at>=?` (inclusive). The `test_list_events_supports_filters_since_limit_and_stable_newest_first` case explicitly forces `second_id` and `third_id` to share `received_at=200` and asserts `[third_id, second_id]`, pinning the `id DESC` tie-break called for in D19. ([_knocker.py:479](../../../packages/knocker/python/knocker/_knocker.py#L479), [_knocker.py:559](../../../packages/knocker/python/knocker/_knocker.py#L559), [test_knocker_honker.py:618-665](../../../tests/test_knocker_honker.py#L618-L665))

**P3 — Limit bounds are enforced and tested.** `_coerce_limit` rejects bools, non-1..1000 ints, with a clear error. `list_events(limit=0)` and `list_events(limit=1001)` both raise the pinned `ValueError`. ([_knocker.py:718-724](../../../packages/knocker/python/knocker/_knocker.py#L718-L724), [test_knocker_honker.py:660-663](../../../tests/test_knocker_honker.py#L660-L663))

**P4 — Typed returns are preserved.** `list_events` and `list_deliveries` continue to flow through `_event_from_row` / `_delivery_from_row`, returning the existing `Event` and `Delivery` dataclasses. No raw SQL row dicts leak into the public surface, conforming to D7. ([_knocker.py:679-715](../../../packages/knocker/python/knocker/_knocker.py#L679-L715))

**P5 — `ignore()` semantics match D22.** Public `Knocker.ignore(event_id)`: unknown id → `KeyError`; status `ignored` → no-op (test asserts the `knocker_attempts` row count stays at 1 across two calls); `received`, `failed`, `dead` accepted; `processing` and `handled` rejected with a clear `ValueError`. ([_knocker.py:563-573](../../../packages/knocker/python/knocker/_knocker.py#L563-L573), [test_knocker_honker.py:715-816](../../../tests/test_knocker_honker.py#L715-L816))

**P6 — Exact `event_type` matching is asserted.** The event-type test asserts `list_events(event_type="CHECKOUT.SESSION.COMPLETED") == []` against `checkout.session.completed`-typed rows. That pins D10 (no normalization, no case folding) as a behavioral test, not just prose. ([test_knocker_honker.py:653](../../../tests/test_knocker_honker.py#L653))

**P7 — Orphaned vs invalid stay separate filter axes.** `orphaned=True` maps to `event_id IS NULL`, `orphaned=False` to `event_id IS NOT NULL`, and `signature_valid` maps independently to the `signature_valid` column. The delivery-list test exercises both axes and confirms different result sets, even though they coincide today (D11). ([_knocker.py:548-553](../../../packages/knocker/python/knocker/_knocker.py#L548-L553), [test_knocker_honker.py:695-705](../../../tests/test_knocker_honker.py#L695-L705))

**P8 — Doc surface was updated.** Both READMEs now show `list_events`, `list_deliveries(signature_valid=..., orphaned=...)`, and `ignore(...)` in the audit/operator example block, removing the "minimal audit reads" framing. That matches D7's "documented" leg of "supported." ([README.md:99-110](../../../README.md#L99-L110), [packages/knocker/README.md:55-66](../../../packages/knocker/README.md#L55-L66))

### Negative conformance review

**N1 — `ignore()` on a `received` event leaves the Honker job live; the spec-diff and plan don't acknowledge that.** `mark_ignored` updates `knocker_events.status='ignored'` and inserts an `ignored` attempt row, but it does not touch `_honker_live`. The Python `ignore()` wrapper does not call `queue.fail` / `queue.ack` either. So if `ignore()` is called on a `received` event that still has a queued Honker job, a worker can later claim that job, run `knocker_mark_processing` (which unconditionally rewrites `status` to `processing`), and call the handler — silently undoing the ignore. The acceptance criterion in the plan ("`ignore(...)` accepts `received`, `failed`, and `dead`") was pinned without a follow-on rule for "and revokes any pending Honker work." Either the implementation needs to ack/fail the Honker job inside `ignore()`, the dispatcher needs to short-circuit on `status='ignored'` before `mark_processing`, or the spec-diff needs to explicitly accept the race. ([_knocker.py:563-573](../../../packages/knocker/python/knocker/_knocker.py#L563-L573), [_knocker.py:607-641](../../../packages/knocker/python/knocker/_knocker.py#L607-L641), [knocker-honker/src/knocker_ops.rs:459-477](../../../knocker-honker/src/knocker_ops.rs#L459-L477), [knocker-honker/src/knocker_ops.rs:391-407](../../../knocker-honker/src/knocker_ops.rs#L391-L407))

**N2 — `signature_valid=False` excludes `NULL` rows; not pinned anywhere.** The filter renders as `d.signature_valid=0` (parameterized), so SQL three-valued logic drops `signature_valid IS NULL` deliveries. In normal `receive()` flow `signature_valid` is always `True`/`False`, but `Knocker.ingest(..., signature_valid=None)` is a public path that can produce `NULL`. The plan defined the filter as "filter for non-matching rows" without specifying NULL semantics. Operators asking "show me deliveries where the signature did not pass" would reasonably expect `NULL` deliveries to appear, since they certainly did not pass. Either commit to "False filters for `signature_valid = 0` only; `NULL` requires a separate axis" in the spec-diff and add a test, or change the SQL to `(d.signature_valid = 0 OR d.signature_valid IS NULL)`. ([_knocker.py:548-550](../../../packages/knocker/python/knocker/_knocker.py#L548-L550), [_knocker.py:326-360](../../../packages/knocker/python/knocker/_knocker.py#L326-L360))

**N3 — D23's "002 helper call sites continue to work" stretched into "002 tests had their assertions updated."** The diff modifies two preexisting 002 tests (`test_duplicate_valid_deliveries_are_auditable_without_mutating_event`, `test_invalid_first_valid_later_creates_two_deliveries_and_one_event`) to flip the asserted delivery order from `[delivery-1, delivery-2]` to `[delivery-2, delivery-1]`, because the default ordering changed from `ORDER BY d.id` to `ORDER BY d.received_at DESC, d.id DESC`. The methods and signatures still work, which is the narrow contract. The wider intent in D23 was an explicit evidence item that 002 tests still pass; modifying them to match a new ordering is effectively a soft break. Worth either calling out in the spec-diff that ordering changed and prior asserts had to be updated, or keeping a parallel small test that fixes the older ordering as a "002 behavior preserved through the same method names" anchor. ([test_knocker_honker.py:115-116](../../../tests/test_knocker_honker.py#L115-L116), [test_knocker_honker.py:551-552](../../../tests/test_knocker_honker.py#L551-L552))

**N4 — `commits.txt` and a recorded `make test` evidence run are missing.** The build order in `plan.md` includes "Run the full test suite and capture evidence." 001 and 002 each have a `commits.txt`; 003 does not, and there's no captured pass/fail of `make test` anywhere in the phase folder. The plan's own acceptance trail is incomplete. Without that, "tests pass" is a claim, not evidence.

**N5 — `_coerce_bool_filter` is unreachable for `None` and its error message lies.** Both `signature_valid is not None` and `orphaned is not None` checks gate the call, so `_coerce_bool_filter` only ever sees a non-`None` value. The body still says `if not isinstance(value, bool): raise TypeError(f"{name} must be a bool or None")`. The "or None" is misleading — passing `None` reaches the `is not None` gate first, never the helper. Either drop the helper and inline the `isinstance` check, or rewrite the message to "must be a bool". Cosmetic but it's a public-facing error string. ([_knocker.py:733-736](../../../packages/knocker/python/knocker/_knocker.py#L733-L736))

### Adversarial review

**A1 — `_coerce_since` accepts arbitrary integers, including negatives and very large values.** `since=-1` passes through as `received_at>=-1` (matches everything; harmless). `since=10**18` is fine. But `since=2.5` becomes `int(2.5)=2` silently, because `_coerce_since` only blocks `bool`, then `int(value)`. A float-typed timestamp gets truncated without a warning. For an operator surface that pins integer-only filters in the spec-diff, this is a quiet coercion of a wrong-typed input. Worth either rejecting non-`int` inputs explicitly or documenting the coercion. ([_knocker.py:727-730](../../../packages/knocker/python/knocker/_knocker.py#L727-L730))

**A2 — No race-tested invariant for "best-effort reads under concurrent worker activity" (acknowledged in D24, but worth re-flagging).** The plan explicitly stated this would be documented but not race-tested. The implementation neither breaks nor proves the contract. Revisit in a later slice if/when concurrent worker tests are added; for now, acknowledged.

**A3 — `list_events()` and `list_deliveries()` empty-filter behavior is exercised but the test could be tighter.** The first assertion in `test_list_events_supports_filters_since_limit_and_stable_newest_first` is `list_events()` returns `[third_id, second_id, first_id]` — three rows under the default `limit=100`. There is no test that exercises the "default limit caps" edge — e.g., insert 110 events and assert `len(list_events()) == 100`. Without that, the default limit is asserted by docstring only. Acceptable for v1, but worth a one-line note.

**A4 — `list_deliveries(orphaned=True, signature_valid=True)` returns rows that cannot exist today.** The two filters compose with `AND`. In the current model, an orphan delivery means `event_id IS NULL`, which only happens when `signature_valid=0`. So `orphaned=True AND signature_valid=True` always returns `[]`. That's not a bug — it's a correct empty answer for an impossible-by-construction predicate — but operators reading the API might expect a hint or doc note that this combination is intentionally vacuous today. Defer; future slice may make this combination non-empty.

**A5 — Error coverage for delivery-list `limit=0` is missing.** The events-list test asserts both `limit=0` and `limit=1001` raise. The deliveries-list test only asserts `limit=1001`. The validation logic is shared, so this is not a code gap, but it is a test-symmetry gap. Trivially addable. ([test_knocker_honker.py:706-707](../../../tests/test_knocker_honker.py#L706-L707))

**A6 — `ignore()` has no test that asserts a worker run after ignore on a `received` event leaves the event in `ignored`.** This is the testable form of N1. A test that calls `ignore(received_id)` and then runs the worker for one tick should either (a) confirm the event stays `ignored` (which would fail today), or (b) be omitted with an explicit doc note that ignore-on-received does not revoke pending work.

### Overall

The shape is right. The Python operator surface, typed returns, recency model, exact `event_type` matching, orphan-vs-invalid axis, `ignore()` status gating, and limit bounds all map cleanly from the post-Round-2 plan to the diff. Doc updates landed in both READMEs.

The substantive gap is N1: `ignore()` accepts `received`, but the implementation does not revoke the pending Honker job, and a worker run after `ignore()` would silently re-process. The spec-diff/plan committed to accepting `received` without committing to "and pending work is canceled." This is the kind of state-divergence drift that is easier to fix now than after a user hits it.

Items I'd ask Session A to resolve before declaring this phase done:

- **N1** — decide between (i) revoking the Honker job inside `ignore()`, (ii) short-circuiting in `_dispatch_job` on `status='ignored'`, or (iii) explicit spec-diff text that ignore-on-received is a soft signal subject to worker race
- **N2** — pin `signature_valid=False` NULL semantics (and add a test) or change the SQL to include NULL
- **N3** — note the 002 ordering change in the spec-diff or add a small "002-shape" test that pins the old assertions through the new method names
- **N4** — record `commits.txt` and a captured `make test` evidence line for the phase
- **A6** — add a worker-run-after-ignore test that pins whichever resolution N1 takes

Smaller items (acceptable to defer):

- **N5** — fix the `_coerce_bool_filter` error string or inline the check

## Implementation Response 1

Responding to:

- Round 3 — Implementation Review

### Inputs

- P1
- P2
- P3
- P4
- P5
- P6
- P7
- P8
- N1
- N2
- N3
- N4
- N5
- A1
- A2
- A3
- A4
- A5
- A6

### Decisions

- D26 — Preserve P1 through P8.
  Action: keep the Python-first operator surface, typed returns, ordering, limit bounds, recovery actions, and README updates intact.
  Targets: code, docs

- D27 — Accept N1.
  Action: make ignored events non-dispatchable by short-circuiting `_dispatch_job(...)` to acknowledge claimed Honker jobs without moving ignored events back to `processing` or invoking handlers.
  Targets: `_knocker.py`, tests, spec-diff.md, plan.md

- D28 — Accept N2.
  Action: define `signature_valid=False` as "not true" in the operator surface and include `NULL` verification-state rows in that filter.
  Targets: `_knocker.py`, tests, spec-diff.md, plan.md

- D29 — Reject N3 as a correctness bug; record it as an intentional `003` behavior change.
  Action: keep newest-first ordering. The stabilized helper names from `002` remain the same, but default list ordering is intentionally changed by `003` and the updated tests now pin that behavior.
  Targets: `reviews_and_decisions.md`

- D30 — Accept N4.
  Action: add `commits.txt` for the phase and record the `make test` evidence after the fix pass.
  Targets: `commits.txt`, `reviews_and_decisions.md`

- D31 — Accept N5.
  Action: tighten `_coerce_bool_filter` / `_coerce_since` validation and public error messages.
  Targets: `_knocker.py`, tests

- D32 — Accept A1.
  Action: reject float `since` values instead of truncating them.
  Targets: `_knocker.py`, tests

- D33 — Accept A3 and A5.
  Action: add tighter tests for default event-list limit capping and for delivery-list `limit=0`.
  Targets: tests

- D34 — Accept A6.
  Action: add a worker-run-after-ignore test that pins the chosen N1 resolution.
  Targets: tests

- D35 — Defer A2 and A4.
  Action: keep best-effort concurrent-read behavior and impossible filter combinations as documented/operator-level semantics, not new implementation work in this slice.
  Targets: none

### Verification

- 2026-04-25: `make test` passed after the post-review fix pass.
  - Rust: 8 passed
  - Python: 23 passed
  - Node: 2 passed

### Decision verdict

- Accepted the substantive implementation findings and tightened the slice so `ignore()` cannot be silently undone, verification-outcome filtering has explicit NULL semantics, and the phase has a proper evidence trail.
### Preserved smaller-items note from Round 3

- **A1** — tighten `_coerce_since` to reject non-`int` (or document the coercion)
- **A3** — add a default-limit-caps test (insert > 100, assert exactly 100)
- **A4** — short doc note on the vacuous `orphaned=True AND signature_valid=True` combination
- **A5** — add the `limit=0` rejection test for `list_deliveries`

## Round 4 — Implementation Review (follow-up)

Artifacts reviewed:

- `.intent/phases/003-operator-read-surface/spec-diff.md` (post-Implementation-Response-1)
- `.intent/phases/003-operator-read-surface/plan.md` (post-Implementation-Response-1)
- `.intent/phases/003-operator-read-surface/commits.txt`
- `packages/knocker/python/knocker/_knocker.py` (working-tree diff vs `HEAD`)
- `tests/test_knocker_honker.py` (working-tree diff vs `HEAD`)

This round only re-reviews the items addressed in Implementation Response 1 (D26–D35). Phase still has no commit; review is against the working tree.

### Positive conformance review

**P9 — D27 / N1 fix is correctly placed.** `_dispatch_job` now reads `event.status` before opening the work transaction and short-circuits with `queue.ack` when the event is already `ignored`. Importantly the short-circuit lives *before* `mark_processing` runs, so the previously documented silent-override path is closed. ([_knocker.py:612-621](../../../packages/knocker/python/knocker/_knocker.py#L612-L621))

**P10 — D27 / A6 has a real worker test, not a docstring claim.** `test_ignore_received_event_prevents_later_worker_dispatch` ingests a `received` event, calls `ignore()`, runs `app.run_worker(...)` until `_honker_live` drains, and asserts: status stays `ignored`, no handler invocation, exactly one `ignored` attempt row, and `_honker_live = 0`. The test exercises the full path (claim → short-circuit → ack), not just a status read. ([test_knocker_honker.py:868-908](../../../tests/test_knocker_honker.py#L868-L908))

**P11 — D28 / N2 SQL change is exactly the spec text.** Filter renders as `(d.signature_valid=0 OR d.signature_valid IS NULL)` for `signature_valid=False`. The new spec-diff invariant ("`signature_valid=False` means 'not true' ... `signature_valid = 0` and `signature_valid IS NULL` both match") matches the SQL one-to-one. `test_signature_valid_false_filter_includes_null_rows` asserts both rows return, in newest-first order. ([_knocker.py:548-553](../../../packages/knocker/python/knocker/_knocker.py#L548-L553), [spec-diff.md:35](spec-diff.md#L35), [test_knocker_honker.py:911-930](../../../tests/test_knocker_honker.py#L911-L930))

**P12 — D32 / A1 tightening is genuine.** `_coerce_since` now rejects bools and non-`int` types up front instead of truncating floats. Test `test_operator_filters_reject_non_integer_since_and_delivery_limit_zero` asserts `since=2.5` raises `TypeError` for both `list_events` and `list_deliveries`. ([_knocker.py:728-731](../../../packages/knocker/python/knocker/_knocker.py#L728-L731), [test_knocker_honker.py:933-948](../../../tests/test_knocker_honker.py#L933-L948))

**P13 — D31 / N5 error string is honest now.** `_coerce_bool_filter` raises `f"{name} must be a bool"` (no longer the misleading "or None"). Cosmetic but it was a public-facing string. ([_knocker.py:734-737](../../../packages/knocker/python/knocker/_knocker.py#L734-L737))

**P14 — D33 / A3 default-limit cap is now a behavioral test.** `test_list_events_default_limit_caps_results` ingests 105 events and asserts `len(list_events()) == 100` plus order is `reversed(event_ids[-100:])`. Pins both the cap and newest-first ordering at the cap boundary. ([test_knocker_honker.py:668-685](../../../tests/test_knocker_honker.py#L668-L685))

**P15 — D33 / A5 delivery `limit=0` rejection is asserted.** `list_deliveries(limit=0)` now raises in `test_operator_filters_reject_non_integer_since_and_delivery_limit_zero`. Test-symmetry gap closed.

**P16 — D30 / N4 evidence is recorded.** `commits.txt` exists with a `make test` evidence block dated `2026-04-25` (Rust 8 / Python 23 / Node 2 passing). The "No phase commit yet." line is the right honest framing — the implementation is not yet committed. ([commits.txt:1-9](commits.txt#L1-L9))

**P17 — Spec-diff and plan absorb the new contracts.** Two new spec-diff invariants pin `signature_valid=False` NULL semantics and the ignore-before-dispatch contract. Plan acceptance now includes "An ignored event is not later dispatched by a worker, even if a previously queued Honker job is claimed after the ignore." Intent and implementation are aligned. ([spec-diff.md:34-35](spec-diff.md#L34-L35), [plan.md:127](plan.md#L127))

### Negative conformance review

**N6 — Trailing fragment from Round 3 was orphaned by the new section insertion.** The original Round 3 review ended with a "Smaller items (acceptable to defer)" list. After Implementation Response 1 was inserted, lines 502–505 of [reviews_and_decisions.md](reviews_and_decisions.md) now contain the tail of that list (A1, A3, A4, A5 bullets) sitting *below* the "Decision verdict" of Implementation Response 1, dangling under no header. It reads like leftover content. Either fold the four bullets back up into the Round 3 "Smaller items" block (where the surviving N5 bullet still lives) or drop them — they no longer index a live finding now that A1/A3/A5 are accepted in D32/D33. Append-only review history is fine; orphaned bullets in the middle of a section are not. ([reviews_and_decisions.md:502-505](reviews_and_decisions.md#L502-L505))

**N7 — D29's rejection of N3 is not reflected in the spec-diff.** D29 declared the 002 ordering change "intentional `003` behavior change" — fair as a decision, but the spec-diff/plan never call out that `list_deliveries()` and `list_events()` default ordering changed from `id ASC` (the implicit 002 contract) to `received_at DESC, id DESC`. A reader of the spec-diff alone would not know this is a behavior change for any 002 caller that depended on insertion-order returns. One-line note under "What changes" would close it: "Default list ordering changes from insertion-order to newest-first; 002 tests that asserted insertion-order on delivery lists are updated accordingly."

### Adversarial review

**A7 — TOCTOU window between `get_event` and `mark_processing` in `_dispatch_job` is theoretical, not real, given Knocker's same-process model — worth a one-line note.** The new short-circuit reads status, branches, and (in the success path) opens the work transaction. There are no `await` points between the status read and `with self.db.transaction()`, so an `ignore()` call from the same event loop cannot interleave between them. SQLite's `BEGIN IMMEDIATE` semantics serialize the worker's `mark_processing` against any concurrent `ignore()` tx. So the "ignore lands after status read but before tx" race is a non-issue under the documented single-process / single-event-loop model. Worth one explicit comment line near the short-circuit so a future reader doesn't add an `await` between those two statements and silently reopen the race. Defer; not blocking. ([_knocker.py:612-621](../../../packages/knocker/python/knocker/_knocker.py#L612-L621))

**A8 — `test_ignore_received_event_prevents_later_worker_dispatch` is timing-coupled.** The test polls `_honker_live` for up to 1.0s and asserts it reaches 0. On a heavily loaded CI machine the worker tick might not run within that budget; the test then fails with `live_rows[0]["c"] != 0`. The 1s budget is generous in normal conditions but is the kind of test that can flake under load. Acceptable for v1; consider a longer cap or an explicit one-shot worker variant if it ever flakes. Defer. ([test_knocker_honker.py:888-895](../../../tests/test_knocker_honker.py#L888-L895))

**A9 — `_dispatch_job`'s short-circuit doesn't write a fresh `ignored` attempt row, which is the right call but unstated.** The original `ignore()` already wrote one `knocker_attempts` row with outcome `ignored`. The dispatcher's short-circuit acks the queue and returns without inserting a second attempt row. The test asserts attempt count stays at 1, so the behavior is pinned. Worth one line in the spec-diff or plan that the dispatcher short-circuit is queue-only and does not record an additional attempt. Tiny; defer. ([test_knocker_honker.py:903-907](../../../tests/test_knocker_honker.py#L903-L907))

### Overall

The substantive findings from Round 3 are all addressed. N1 has both a real fix and a worker-level test that exercises the fix end to end. N2 has matching SQL, an explicit invariant, and a NULL-row test. N4 has a `commits.txt` with `make test` evidence. N5, A1, A3, and A5 are each tightened with a corresponding test. D29's rejection of N3 is a defensible choice for an unshipped pre-1.0 surface.

What's left is small:

- **N6** — clean up the orphaned bullet fragment under "Decision verdict"
- **N7** — add a one-line note in the spec-diff under "What changes" about the 002 default-ordering behavior change

Smaller items (acceptable to defer):

- **A7** — single comment near the dispatcher short-circuit warning future authors not to add an `await` between status read and the work transaction
- **A8** — note the timing-coupled nature of `test_ignore_received_event_prevents_later_worker_dispatch`; consider a longer poll cap if it flakes
- **A9** — one line stating the dispatcher short-circuit is queue-only and does not write a second `ignored` attempt row

With N6 and N7 cleaned up, the phase is ready to commit. The decision verdict on this round: implementation conforms; only documentation/review-housekeeping items remain.

## Round 5 — Final Implementation Review

Artifacts reviewed:

- `.intent/phases/003-operator-read-surface/spec-diff.md`
- `.intent/phases/003-operator-read-surface/plan.md`
- `.intent/phases/003-operator-read-surface/commits.txt`
- `packages/knocker/python/knocker/_knocker.py` (working tree)
- `tests/test_knocker_honker.py` (working tree, 23 test functions counted)
- `README.md`, `packages/knocker/README.md` (working tree)
- `knocker-honker/src/knocker_ops.rs` (re-read for the ignore short-circuit context)
- `SYSTEM.md`

This is a final pass before the phase commits. Working-tree state is unchanged since Round 4 (no further code or test edits). The two open Round 4 items (**N6**, **N7**) are still open in the docs.

### Positive conformance review

**P18 — Acceptance criteria are all met by tests, not just prose.** Every line in `plan.md` "Acceptance" maps to at least one assertion in `tests/test_knocker_honker.py`:
- `list_events` filter axes → `test_list_events_supports_filters_since_limit_and_stable_newest_first`
- default-limit cap → `test_list_events_default_limit_caps_results`
- `list_deliveries` filter axes (incl. orphaned vs invalid) → `test_list_deliveries_supports_filters_since_limit_and_newest_first`
- `signature_valid=False` includes NULL → `test_signature_valid_false_filter_includes_null_rows`
- `ignore` status gating + idempotency → `test_ignore_public_surface_enforces_supported_statuses`
- ignored-then-worker contract → `test_ignore_received_event_prevents_later_worker_dispatch`
- `since`/`limit` validation → `test_operator_filters_reject_non_integer_since_and_delivery_limit_zero`

The ratio of plan invariants to executable assertions is 1:≥1 across the slice.

**P19 — Test count matches recorded evidence.** `tests/test_knocker_honker.py` contains 23 test functions; `commits.txt` evidence reports `Python: 23 passed`. The numbers line up — evidence is for *this* test file, not a stale run.

**P20 — Spec-diff invariants are airtight against the implementation.** Eight invariants ([spec-diff.md:30-37](spec-diff.md#L30-L37)) — "supported," typed returns, Python-first, exact event_type, orphan ≠ invalid, best-effort reads, signature_valid=False NULL semantics, ignore-prevents-dispatch — each have a corresponding test that would fail if the invariant broke. None of them are documentation-only.

**P21 — No drift in non-goals.** No `Knocker.operator.*` namespace, no Rust read UDFs, no cursor pagination, no DST/calendar filters, no new state transitions, no Node-binding parity work. The negative space committed in spec-diff and plan held through implementation.

**P22 — `commits.txt` is honest pre-commit.** The "No phase commit yet." line + dated `make test` evidence is the right shape for a phase that's been implemented but not yet committed. When the commit lands, the hash gets appended above the evidence block — same shape as `001-knocker-foundation/commits.txt` and `002-append-only-delivery-rows/commits.txt`.

### Negative conformance review

**N6 (still open) — Orphaned bullet fragment under "Decision verdict" remains.** Lines 502–505 are still dangling. As Session B the standing rule is "do not silently rewrite Session A artifacts," so the cleanup remains Session A's call. Not a blocker for ship.

**N7 (still open) — Spec-diff still does not note the default-ordering behavior change for `list_events()` / `list_deliveries()`.** D29 declared keeping the new newest-first ordering as intentional behavior, and updated 002 tests reflect that, but the spec-diff body never says "default ordering is newest-first; this is a behavior change for any 002 caller that depended on insertion-order." A future reader of the spec-diff alone would miss the soft-break. One sentence in "What changes" closes it. Not a blocker for ship; a polish item.

### Adversarial review

I tried one final pass to find anything that survived four review rounds.

**A10 — Replay-after-ignore can leave a stale honker job lingering, but this is pre-existing 002 behavior, not 003-introduced.** Walking it: `ignore()` sets `status='ignored'` without acking honker; later `replay()` resets to `received` and enqueues a *new* honker job; the *original* honker job (which the dispatcher will short-circuit on next claim) is still in `_honker_live`. So a replayed event has up to two live jobs. The new short-circuit handles only the ignored case; once `replay()` flips status back to `received`, both jobs become normal-dispatch eligible, and the same event can be processed twice. This is the same shape of state-divergence as the pre-existing replay/requeue gap — out of scope per the slice's "no new state transitions" trap, but worth a roadmap note. Defer to a future phase that owns honker-event consistency for recovery actions.

**A11 — `_dispatch_job` short-circuit's `queue.ack` can raise on an expired claim.** If the claim has already expired by the time the short-circuit's ack runs, `_require_queue_transition` raises `_QueueTransitionError` and the exception propagates out of `run_worker`, killing the worker task. For an ignored-event short-circuit, a more graceful path might be a no-op fallback when the claim is gone. Low impact (rare), but worth one line in a future hardening pass. Defer.

**A12 — No test confirms `ignore()` + worker race when both run truly concurrently in the same loop.** The current test serializes: `ignore()` first, then `run_worker(...)`. There's no test that interleaves `ignore()` and a running worker via `asyncio.gather` or similar. Per the same-process / single-event-loop architecture and the lack of `await` points between status-read and `mark_processing`, the race is structurally impossible — but no test pins that structural property. Acceptable per A2 (best-effort reads, defer). Defer.

### Overall

Four rounds in. The shape is right and stays right.

**Substantive findings (Rounds 1–3): all closed.** Stable IDs P1–P17 / N1–N5 / A1–A6 are either accepted-and-implemented or accepted-and-deferred with explicit rationale.

**Round 4 housekeeping (N6, N7): still open.** Both are documentation-shape items, not behavior:
- **N6** — orphaned bullet fragment at [reviews_and_decisions.md:502-505](reviews_and_decisions.md#L502-L505)
- **N7** — spec-diff doesn't note the 002 → 003 default-ordering behavior change

**Round 5 adversarial (A10–A12): all deferred.** A10 is pre-existing 002 behavior outside this slice's scope; A11 is rare-edge graceful-degradation polish; A12 is structurally bounded by the same-process model and consistent with D24's defer of race testing.

### Ship verdict

**The phase is ship-ready.** All behavior contracts in the spec-diff are implemented and tested. Evidence is recorded. Code, tests, docs, and intent artifacts are coherent.

The two open items (N6, N7) are documentation cleanup that can land in the same commit as the phase or a follow-up — neither blocks the work.

Recommended next step: Session A folds N6 and N7 into the same commit (5-line edit to `reviews_and_decisions.md` for N6, one-sentence add to `spec-diff.md` "What changes" for N7), then commits the phase and appends the hash to `commits.txt`.

## Implementation Response 2

Responding to:

- Round 4 — Implementation Review (follow-up)
- Round 5 — Final Implementation Review

### Inputs

- N6
- N7
- A7
- A8
- A9
- A10
- A11
- A12

### Decisions

- D36 — Accept N6.
  Action: keep the preserved Round 3 bullets but give them an explicit header so the review history stays append-only without leaving dangling bullets under a verdict.
  Targets: `reviews_and_decisions.md`

- D37 — Accept N7.
  Action: add a one-line note to the spec diff that `003` intentionally changes default list ordering from the earlier insertion-order shape to newest-first.
  Targets: `spec-diff.md`

- D38 — Defer A7, A8, and A9.
  Action: keep these as non-blocking readability/hardening notes for a later cleanup pass.
  Targets: none

- D39 — Defer A10, A11, and A12.
  Action: treat these as future hardening or recovery-semantics work outside `003`'s accepted scope.
  Targets: roadmap/later phase

### Verification

- `reviews_and_decisions.md` no longer has orphaned bullets under a verdict.
- `spec-diff.md` now explicitly records the `002` -> `003` default-ordering behavior change.

### Decision verdict

- Accepted the final documentation-only cleanup. `003` is ready to commit.

# Reviews and Decisions: 002 Append-Only Delivery Rows

## Round 1 — Implementation Review

Artifacts reviewed:

- `.intent/phases/002-append-only-delivery-rows/spec-diff.md`
- `.intent/phases/002-append-only-delivery-rows/plan.md`
- `knocker-honker/src/lib.rs`
- `knocker-honker/src/knocker_ops.rs`
- `packages/knocker/python/knocker/_knocker.py`
- `tests/test_knocker_honker.py`

Verification reviewed:

- `make test-rust`: 8 tests pass
- `make test-python`: 16 tests pass
- `make test-node`: 2 tests pass

### Positive conformance review

**P1 — Schema split lands cleanly.** `knocker_deliveries` is created with the right shape: receipt-level data plus an optional `event_id` foreign key. Indexes cover both expected query patterns — per-event audit (`event_id, id`) and per-endpoint chronology (`endpoint_id, received_at, id`).

**P2 — Three ingress cases match the spec.** `knocker_ops.rs:195-269` implements the documented branches exactly:
- invalid → orphan delivery (`event_id = NULL`) + 401, no enqueue
- first-valid → event + linked delivery + one enqueue
- duplicate-valid → linked delivery only, no enqueue, `duplicate=1` returned

Each path returns the documented response shape (`delivery_id`, nullable `event_id`, `duplicate`, `status_code`).

**P3 — Append-only invariant is honored in code.** `insert_delivery` is the only writer of delivery rows. There is no `UPDATE knocker_deliveries` anywhere in `knocker_ops.rs`. The old "recover invalid by mutating the event in place" path is gone — nothing in the new code mutates `body_blob`, `headers_json`, `method`, or `query_json` on event rows.

**P4 — `ignored` is no longer overloaded.** The only writer of `status='ignored'` post-migration is `mark_ignored`. The old branch that set initial status to `'ignored'` for verification failures is removed.

**P5 — Migration is correct and idempotent.** `migrate_v1_to_v2` backfills with `INSERT ... WHERE NOT EXISTS (SELECT 1 FROM knocker_deliveries d WHERE d.event_id = e.id)`, so a partial migration can be resumed safely. Column drops are guarded by `column_exists`. Re-running `bootstrap_knocker_schema` on a v2 DB is a no-op.

**P6 — Python types reflect the schema split.** `Event` has no `signature_valid`/`signature_error`; `Delivery` carries them. `list_deliveries` and `get_delivery` exist on `Knocker`. Handlers receive `Event`, not `Delivery` — the trap "do not expose `delivery_id` as the main handler primitive" is held.

**P7 — Test coverage matches the spec's verification plan.** Each verification line in the spec-diff has a corresponding test: valid HMAC, invalid HMAC, valid Stripe, invalid Stripe, duplicate-deliveries-one-event, invalid-first-valid-later. The migration test exercises a real v1 schema rolling forward to v2.

### Negative conformance review

**N1 — GitHub verification is implemented but listed as out-of-scope.** `_build_provider_verifier` in `_knocker.py:692-697` ships an HMAC-SHA256 verifier for GitHub (`x-hub-signature-256`, `sha256=` prefix). The plan's Out of scope section explicitly says "GitHub and Slack verification." The provider preset for GitHub (extractors only) is fine and is in scope. The verifier is not.

Two paths to resolve:
- Remove the GitHub branch from `_build_provider_verifier` for this slice; users who need GitHub verification can pass `verification={"kind": "hmac-sha256", ...}` explicitly.
- Or update the spec-diff/plan to acknowledge GitHub verification landed.

I'd lean toward the first — the slice should match its own out-of-scope list.

**N2 — Append-only is enforced by code discipline, not schema.** `knocker_deliveries` has no `BEFORE UPDATE` trigger or other DDL-level guard. The invariant rests on no future code adding an UPDATE. That's fine for v1 but should be called out in `SYSTEM.md` so a future contributor doesn't add an UPDATE thinking it's allowed.

**N3 — `commits.txt` is empty.** The file still says "No phase-specific commit exists yet for `002`." Either commit the slice and record hashes, or note explicitly that this is local-only work.

**N4 — Doc updates need a confirmation pass.** `SYSTEM.md`, `README.md`, and `CHANGELOG.md` show as modified in `git status` (good — plan step 7), but I haven't verified the content describes the new Delivery/Event split or that `signature_valid`/`signature_error` are no longer mentioned as event-level fields. Quick pass to confirm before merge.

### Adversarial review

I tried to break the main invariants.

**A1 — Migration leaves legacy `ignored` rows ambiguous.** Pre-v2 rows where `status='ignored'` could have come from operator action (legitimate) or from invalid-signature ingress (legacy semantics). The migration preserves the status as-is and backfills a delivery row with `signature_valid=0` for legacy verification-failed events. Post-migration, `event.status='ignored'` no longer has a single meaning for historical data — it has whatever it had pre-migration.

This is probably acceptable but should be acknowledged. Either:
- Add an invariant carve-out: "`ignored` has a single meaning for events created in v2; v1-migrated events may carry the legacy verification-failed sense."
- Or migrate legacy: detect events with `status='ignored'` plus a backfilled delivery with `signature_valid=0` and... reclassify how? Deleting events is destructive. Better to acknowledge the boundary.

**A2 — No Rust test pinning that Event is unchanged after a duplicate.** The Python test `test_duplicate_valid_deliveries_are_auditable_without_mutating_event` asserts `event.body == first_body` — so the semantic is covered. But Rust-level tests don't pin all four canonical fields (body, headers, method, query). Adding a Rust test would lock the invariant at the layer that owns the schema. Not blocking; small follow-up.

**A3 — `ingest()` doesn't manage its own transaction.** The Rust function runs multiple statements (insert event, insert delivery, enqueue Honker job). The Python binding correctly wraps the whole thing in `with self.db.transaction()`. But if a future binding calls `knocker_ingest(...)` without an outer transaction, a partial failure could leave an event without a linked delivery. Worth a comment in the function header: "caller must run inside a transaction; the function does not begin one."

**A4 — Race between concurrent first-valid for the same dedupe_key.** Two parallel valid deliveries with the same `dedupe_key`: both `find_existing_event_id` calls return None, both attempt `INSERT INTO knocker_events`, the second fails on the unique index. SQLite serializes writes in WAL mode, so one transaction commits and the other gets a constraint violation that bubbles up. Not silent corruption, but callers should know to retry. Not a slice 002 problem; inherent to webhook ingestion under concurrency. Worth a sentence in user-facing docs eventually.

**A5 — `ALTER TABLE DROP COLUMN` requires SQLite ≥ 3.35.** The migration uses `DROP COLUMN`, which is SQLite 3.35+ (March 2021). Most modern installs have it, but very old SQLite would fail the migration with an unhelpful error. Either pin a minimum SQLite version in `Cargo.toml` and `pyproject.toml`, or document the requirement in the README.

### Overall

The implementation conforms to the spec-diff and the plan. Tests pass cleanly across Rust, Python, and Node. The three load-bearing invariants — append-only deliveries, invalid signatures don't create events, event rows aren't mutated by later deliveries — are upheld in code and proven by tests.

Drifts I'd ask Session A to address before final acceptance:

- **N1** — pick one path on GitHub verification (drop or relabel)
- **N3** — populate `commits.txt`
- **A1** — pick one path on legacy `ignored` ambiguity (carve-out or document)
- **A3** — add a comment to `ingest()` about transaction expectations

Smaller follow-ups (acceptable to defer):

- **N2** — `SYSTEM.md` note about append-only being a code-level invariant
- **N4** — confirm doc content
- **A2** — Rust-level test for "Event unchanged after duplicate"
- **A5** — minimum SQLite version note

Nothing here is structural. Once the four blockers above are resolved, the slice is done.

## Decision Round 1 — Response To Implementation Review

Responding to:

- Round 1 — Implementation Review

Session:
- A

### Inputs

- [N1]
- [N2]
- [N3]
- [N4]
- [A1]
- [A2]
- [A3]
- [A5]

### Decisions

- [D1] Accept [N1]
  Action: remove the out-of-scope GitHub verifier shortcut from the Python provider preset path. Keep GitHub only as an in-scope metadata extractor preset; explicit generic HMAC verification remains available through `verification=...`.
  Targets: `packages/knocker/python/knocker/_knocker.py`

- [D2] Accept [A1]
  Action: document the historical carve-out for migrated v1 `ignored` rows rather than attempting destructive reclassification.
  Targets: `SYSTEM.md`

- [D3] Accept [A3]
  Action: document the `knocker_ingest(...)` transaction requirement at the Rust contract boundary and make the Node smoke binding honor it explicitly by wrapping calls in a transaction.
  Targets: `knocker-honker/src/knocker_ops.rs`, `packages/knocker-node/index.mjs`

- [D4] Accept [N2]
  Action: add an explicit `SYSTEM.md` note that append-only deliveries are currently enforced by code paths and tests rather than DDL guards.
  Targets: `SYSTEM.md`

- [D5] Accept [N4] as resolved on confirmation
  Action: confirm that `README.md`, `SYSTEM.md`, and `CHANGELOG.md` describe the Delivery/Event split and no longer present verification fields as event-level API.
  Targets: docs confirmation only

- [D6] Defer [A2]
  Action: keep the existing Python-level duplicate-payload immutability test for this slice. Add a Rust-level canonical-payload pin only if follow-up review shows the Python-layer proof is not enough.
  Targets: deferred follow-up

- [D7] Defer [A5]
  Action: keep SQLite minimum-version pinning/documentation out of this slice. Revisit if deployment targets or packaging work reveal an actual version mismatch risk.
  Targets: deferred follow-up

- [D8] Accept [N3]
  Action: make a real commit for `002` and record the hashes in `commits.txt`.
  Targets: `commits.txt`

### Verification

- `make test`
  - `make test-rust`: 8 tests pass
  - `make test-python`: 16 tests pass
  - `make test-node`: 2 tests pass

### Decision verdict

- Accepted fixes landed for [N1], [A1], [A3], [N2], and [N4].
- [A2] and [A5] are deferred by explicit choice, not oversight.
- Slice is ready for commit and evidence recording.

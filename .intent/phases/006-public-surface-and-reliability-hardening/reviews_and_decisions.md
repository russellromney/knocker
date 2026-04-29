# Reviews And Decisions

This file is append-only.

## Implementation Response 1

- Implemented the full code-review response captured by the spec and plan: public docstrings, docs for `(event, tx)`, endpoint alias removal, lifecycle UDF fail-fast, worker state / `on_error`, `replay_delivery(...)`, file split, and reliability tests.
- Preserved the v1 dead-redelivery decision: duplicate ingest never mutates or enqueues, including `dead`; operators recover explicitly with `requeue(...)` or `replay_delivery(...)`.
- Added real-infrastructure tests for concurrent dedupe ingest, WAL wakeup, burst drain, claim-expiry reclaim, Python-entrypoint v1 migration, concurrent pruning, delivery replay, docstrings, and unknown-event lifecycle UDF failures.
- Verification passed locally: `make test`, `npm --prefix site run build`, docstring smoke, and line-count check.

## Implementation Review 1

Session B (Claude Opus 4.7), reviewing commit `616e165` plus evidence in `c50c011` against the 006 spec-diff and plan.

### What landed cleanly

- **All 22 plan-required public docstrings present**, plus `worker_states` and `WorkerState`. The meta-test `test_supported_python_surface_has_docstrings` enforces this as a regression check — clever way to keep the surface "documented" leg of "supported" tied to the test suite.
- **`Knocker.endpoint` removed.** [test_operator_reads.py:17-19](../../../tests/test_operator_reads.py#L17-L19) asserts `not hasattr(app, "endpoint")`. Clean removal.
- **Lifecycle UDFs fail fast.** Each `mark_*` and `reset_event` now checks `changed == 0` via `ensure_event_updated` and returns `InvalidParameterName` on unknown event id. The change is uniform across `mark_processing`, `mark_handled`, `mark_failed`, `mark_ignored`, and `reset_event`. ([knocker_ops.rs:590-598](../../../knocker-core/src/knocker_ops.rs#L590-L598))
- **Worker affordances are real, not cosmetic.** `WorkerState(worker_id, running, current_event_id, last_error)` is updated at every transition: start, dispatch enter, dispatch exit, error, finally-running=False. `on_error` is called before the re-raise so the user can log/alert before the worker dies. ([_knocker.py:612-669](../../../packages/knocker/python/knocker/_knocker.py#L612-L669))
- **`run_worker` stop responsiveness fixed.** The new `asyncio.wait` over `{claim_task, stop_task}` means a stop event interrupts the idle wait immediately instead of waiting for `idle_poll_s + 0.05`. The previous shape could hang up to 5 seconds on user-set idle polls.
- **`replay_delivery` is implemented as a synthetic replay job** with payload `{"event_id": ..., "delivery_id": ...}`. The dispatcher reads the delivery, synthesizes a handler-event with the delivery's body/headers/event_type, and dispatches normally. The canonical event row is never mutated. The replay-delivery test confirms `event.body` stays at the original delivery's body even after the replay handler ran with the new delivery body.
- **File split lands under the 1000-line standard.** `_knocker.py` 935, `verifiers.py` 233, `queue.py` 106, `coercion.py` 101, `models.py` 85. Test files: `test_ingest_worker.py` 454, `test_migration_pruning.py` 494, `test_operator_reads.py` 375, `test_recovery.py` 284, `test_verification.py` 330. All under 1000.
- **Test additions match the plan list one-to-one.** Concurrent ingest dedupe, claim-expiry reclaim with `at-least-once` assertion, WAL wakeup with idle_poll_s=5.0, burst (1000 events) drain, Python v1→v2 migration, concurrent prune, replay_delivery happy + reject, on_error callback, lifecycle UDF unknown-id failures (Rust-side).
- **Docs site updated.** [getting-started.mdx:46-48](../../../site/src/content/docs/getting-started.mdx#L46-L48) explicitly walks through the `(event, tx)` atomic-commit pattern and the "keep handlers synchronous, short, and DB-local" guidance. The worker section adds `worker_states()` + `on_error` + restart-harness expectation.
- **CHANGELOG updated** with the 006-specific lines: `replay_delivery`, worker state + `on_error`, lifecycle UDF fail-fast, prompt stop responsiveness, file split, docstrings.

### Subtle behaviors the review surfaced

These are not bugs — they're contracts that should be visible to operators reading the code or docs:

- **`replay_delivery` resolves the handler using the delivery's `event_type`, not the canonical event's.** [_knocker.py:703-704](../../../packages/knocker/python/knocker/_knocker.py#L703-L704) constructs `handler_event = _event_from_delivery(event, delivery)`, which uses delivery metadata for endpoint/event_type/body. If a redelivery carried a different `event_type` than the original event (provider quirk), the replay routes to a handler keyed by the delivery's type. Intended — replay_delivery means "process this body" — but worth one line in the operators doc.
- **`replay_delivery` resets `attempt_count=0` on the event.** A replay restarts the dead-letter clock. If the operator was using `attempt_count` for any reasoning (e.g., "this event has failed N times"), the count resets after replay_delivery. The docstring on `replay_delivery` doesn't mention this side effect; the `replay`/`requeue` docstrings don't either. Pre-existing for replay/requeue.
- **`replay_delivery`'s synthesized `handler_event` is a hybrid.** It carries the canonical event's id/status/received_at/handled_at/last_error and the delivery's endpoint/event_type/headers/query/body/provider_*/dedupe_key. Handlers see "an Event" but it's a Frankenstein-row at construction time. The model is consistent (event identity + delivery payload) but a one-line comment in `_event_from_delivery` would help future readers.
- **`on_error` runs *before* the re-raise.** If the user's `on_error` itself raises, the original worker exception is replaced. Acceptable — that's the user's bug — but worth an in-docstring note.
- **`replay_delivery` uses an inline `UPDATE knocker_events SET status='received'...` in Python rather than calling the existing Rust `reset_event`.** That's because `reset_event` is private (not exposed as a UDF). Two pieces of code now reset events to `received` (Rust `reset_event` and Python `replay_delivery`). If the canonical reset semantics ever drift, these can disagree. Cosmetic; consider exposing `knocker_reset_event` as a UDF later.

### Module organization quibbles

- **`coercion.py` carries payload-shape validators that aren't really "coercion."** `_event_id_from_payload_json`, `_optional_payload_int`, `_required_payload_int` are payload contract checks. A future `payload.py` (or merging into `queue.py` since these are honker-payload concerns) would name the module more precisely. Defer.
- **`_HonkerQueue` / `_HonkerJob` / `_WorkerQueueIter` keep their leading underscores but are reachable through `app.queue.*`.** The 006 plan didn't ask for renaming; the test suite uses `app.queue.name` and `app.queue.claim_batch(...)` directly. The convention says "private" but the access pattern says "public." Pick one in a future hardening pass.

### Test gaps still worth closing in 0.2

- **`replay_delivery` racing with a worker mid-handler on the same event.** The replay path resets status and enqueues a new job. If a worker is mid-`mark_handled` when replay_delivery's tx commits, the worker's ack will succeed (claim still valid), the worker's tx commits, replay_delivery's tx already committed status='received'. End state depends on commit order and is acceptable per "no exactly-once" — but no test pins the resolution.
- **`on_error` returning a coroutine that itself raises.** The current test passes a sync lambda. The dual-mode handling (`if asyncio.iscoroutine(maybe_awaitable): await maybe_awaitable`) is exercised only via the sync path.
- **`replay_delivery` for a delivery whose original event_type doesn't match any registered handler.** Today this would dead-letter on the new attempt. Worth pinning.
- **Multi-worker plus `on_error`.** When two workers run concurrently and one hits a `_QueueTransitionError`, does the other keep running? `on_error` is per-worker-task; the other worker's state isn't affected. Untested.

### Verification check

- `commits.txt` records 12 Rust + 50 Python + 2 Node tests, matching the test counts grep returns (50 Python `test_*` definitions across the five test files).
- `npm --prefix site run build` recorded as 9 pages built — matches the Astro sidebar config.

### Verdict

The full code-review response landed faithfully. Spec-diff invariants → plan steps → code → tests is 1:1:1:1 across every plan item. The IDD trail is consistent: spec defined the contracts, plan named the steps, implementation matched, evidence is captured in `commits.txt`.

The remaining items above are documentation-shape and test-coverage refinements for 0.2, not 0.1.0 blockers. **006 is shipped and the codebase is ready for `0.1.0` cut.**

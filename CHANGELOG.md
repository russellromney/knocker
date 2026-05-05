# Changelog

## Unreleased

### Added

- Added `RetentionPolicy` plus `run_retention(...)`, a Python-first retention surface backed by Honker Scheduler and the shared core retention-pass primitive.
- Added production worker claim batching with a fixed internal batch size of `10`.
- Added runtime-confidence coverage for SQLite-shaped guarantees, including a subprocess-kill ingest test, fresh-process reopen coverage for committed ingress, and claim-expiry recovery after reopen.
- Added `bench/knocker_bench.py` with two documented local workloads: durable ingress-only and no-op-handler worker drain.
- Added loose CI-facing performance-floor tests for durable ingest throughput and no-op-handler worker drain throughput.
- Added phase-011 throughput exploration scripts for handler cost, lifecycle cost, claim batching, mixed-load topology, writer-handle topology, lock contention, and JSON serialization cost.
- Expanded the Node contract pressure-test client with shared-contract reads (`getDelivery`, `listDeliveriesForEvent`) plus a minimal lifecycle/recovery path (`claimOne`, `ack`, `replay`).
- Added a curated provider pack for `shopify`, `slack`, `postmark`, `resend`, `paddle`, and `lemon-squeezy`, plus repo-owned metadata and binding-neutral conformance fixtures for every curated provider.
- Expanded the curated provider catalog with `standard-webhooks`, `clerk`, `twilio`, `sendgrid`, `linear`, `meta`, `discord`, `zendesk`, `intercom`, `hubspot`, `token-header`, `bearer-token`, and `basic-auth`, with shared Rust/SQLite receive coverage and Python mirror fixtures.
- Added minimal shared-contract bindings for Bun, Ruby, Go, and Elixir, each with its own runtime-level end-to-end smoke test alongside the existing Node binding.
- Added `knocker_prune_audits` table with stable top-level columns plus `summary_json`, as part of the single supported Knocker schema.
- Added `knocker_reset_event(...)` as a core UDF: low-level primitive that resets event status to `received`, `attempt_count` to `0`, clears `last_error` and `handled_at`, without validating source-event status or recording attempt history.
- Added `list_prune_audits(kind=None, since=None, limit=50)` operator read helper returning `PruneAudit` rows newest-first.
- Added `PruneAudit` dataclass to the public Python model surface (`knocker.PruneAudit`).
- Python `prune_events(...)` and `prune_orphan_deliveries(...)` now write one audit row per call in the same transaction, including for no-op prunes with zero counts.
- Python `replay_delivery(...)` now calls `knocker_reset_event(...)` instead of hand-rolling the same `UPDATE`.

### Changed

- The production worker now drains already-claimed local buffered jobs before honoring a stop signal, so batched claims do not strand work until lease expiry.
- Updated the representative local worker baseline on an Apple M1 Pro, Python 3.13.5, SQLite 3.49.1 after shipping production claim batching:
  - durable ingress-only: `5,000` events in `1.445s` (`3,460/s`, `0.289 ms/event`)
  - no-op-handler worker drain: `5,000` events in `1.836s` (`2,723/s`, `0.367 ms/event`)
- The Python docs now explicitly recommend one long-lived `knocker.open(...)` per process for hot paths; multiple independent same-file opens remain supported but are documented as a degraded contention mode.
- The README and docs now present Knocker as a loadable SQLite extension plus multi-runtime bindings instead of a Python-first package, and the binding reference documents the shared provider/worker/operator baseline.
- The retention guide and Python API reference now document Honker-backed automated retention: multiple runners are safe on one SQLite file, while one process/instance should still own retention configuration.
- Knocker now documents and tests SQLite-shaped crash/restart guarantees explicitly: committed transaction state survives reopen, never-committed state does not appear after reopen, and fresh post-crash operations continue to work.
- The benchmark/evidence surface now distinguishes stable local baselines from heavier multi-handle contention probes; `bench/run_all_experiments.py` skips the degraded multi-handle experiments unless explicitly asked to include them.
- Phase-011 evidence replaces the earlier mixed-load intuition from Phase 010: the event-loop-starved harness had overstated steady-state throughput, while the corrected exploration shows worker-side claim/dispatch contention dominates before JSON marshalling does.
- Phase-011 corrected baseline numbers before production claim batching were:
  - durable ingress-only: `5,000` events in `1.329s` (`3,763/s`, `0.266 ms/event`)
  - no-op-handler worker drain: `5,000` events in `2.136s` (`2,341/s`, `0.427 ms/event`)
- Knocker now treats the current schema as the only supported schema shape; pre-release legacy layouts are rejected instead of migrated forward.
- Prune audit rows are never targeted by ordinary prune operations; they form a separate audit trail.
- Counts in prune audits include all rows removed as a consequence, whether by direct `DELETE` or `ON DELETE CASCADE`.
- The operator runbook now documents `list_prune_audits(...)` instead of suggesting application-level logging.
- The retention guide now documents the prune audit trail, the `PruneAudit` shape, and the no-op audit invariant.
- The Python API reference now includes `list_prune_audits(...)` and `PruneAudit`.

### Fixed

- `run_worker(stop_event=...)` no longer exits between locally buffered claimed jobs and leave already-claimed work waiting for lease expiry.
- The previous operator runbook told operators to log prune calls themselves because Knocker did not write a durable audit row. That is no longer true; prune audit is built-in.
- `_WorkerQueueIter` now buffers extra claimed jobs instead of dropping everything after `jobs[0]`, so batched-claim experiments and any future `claim_batch(..., n>1)` worker path do not strand claimed live jobs.

## Released

## Unreleased (cont'd)

### Added

- Added `knocker_prune_audits` table with stable top-level columns plus `summary_json`, as part of the single supported Knocker schema.
- Added `knocker_reset_event(...)` as a core UDF: low-level primitive that resets event status to `received`, `attempt_count` to `0`, clears `last_error` and `handled_at`, without validating source-event status or recording attempt history.
- Added `list_prune_audits(kind=None, since=None, limit=50)` operator read helper returning `PruneAudit` rows newest-first.
- Added `PruneAudit` dataclass to the public Python model surface (`knocker.PruneAudit`).
- Python `prune_events(...)` and `prune_orphan_deliveries(...)` now write one audit row per call in the same transaction, including for no-op prunes with zero counts.
- Python `replay_delivery(...)` now calls `knocker_reset_event(...)` instead of hand-rolling the same `UPDATE`.

### Changed

- Knocker now treats the current schema as the only supported schema shape; pre-release legacy layouts are rejected instead of migrated forward.
- Prune audit rows are never targeted by ordinary prune operations; they form a separate audit trail.
- Counts in prune audits include all rows removed as a consequence, whether by direct `DELETE` or `ON DELETE CASCADE`.
- The operator runbook now documents `list_prune_audits(...)` instead of suggesting application-level logging.
- The retention guide now documents the prune audit trail, the `PruneAudit` shape, and the no-op audit invariant.
- The Python API reference now includes `list_prune_audits(...)` and `PruneAudit`.

### Fixed

- The previous operator runbook told operators to log prune calls themselves because Knocker did not write a durable audit row. That is no longer true; prune audit is built-in.

## Released

## Unreleased (cont'd)

### Added

- Added a repo-level curated provider catalog under `providers/<name>/` with `metadata.json` plus binding-neutral JSON `fixtures/`. Stripe and GitHub each ship valid, invalid-signature, missing-required-header, and (for Stripe) timestamp-tolerance fixtures. The catalog is repo-only conformance material in this phase; runtime plugin loading remains intentionally deferred.
- Added `tests/test_provider_conformance.py` which loads every fixture under `providers/<name>/fixtures/` and asserts the bundled Python provider produces the expected verification outcome and extracted metadata. Stripe fixtures use explicit clock injection (`request.now_s`) so absolute timestamps stay valid forever.
- Added a `Provider`-instance path to `add_endpoint(provider=AcmeProvider(), secrets=[...])` for app-local and community providers. Curated string names (`"stripe"`, `"github"`) remain reserved for built-ins; an instance whose `.name` collides with a curated name is rejected.
- Added `Knocker.queue_name` as a read-only string property for tests and operator queries that need the configured Honker queue name. The underlying queue object is no longer publicly accessible as `app.queue`.
- Added a curated-provider contribution guide (`/guides/contributing-providers/`) documenting the catalog directory layout, `metadata.json` schema, fixture JSON schema, support promise, and the deliberate-review rule for fixture changes.
- Added an import-path stability regression test pinning `knocker.Provider`, `knocker.ProviderRequest`, `knocker.ProviderResult`, `Knocker.provider_versions`, and `Knocker.queue_name` as the public surface, and asserting `Knocker.register_provider` is no longer present.
- Added two regression tests for the Phase 008 Implementation Review N1 finding: a fresh `Provider` instance with the same `.name` as the previous one must rebuild the verifier (instance equality is not by name), and a re-`add_endpoint(...)` with a new instance but no new secrets must fail loudly rather than silently reusing the previous verifier.
- Added a public Python provider plugin surface: `Provider`, `ProviderRequest`, `ProviderResult`, and `Knocker.provider_versions(...)`. App-local and community providers pass a `Provider` instance directly to `add_endpoint(...)`; there is no global string-lookup registry for non-curated providers.
- Added a built-in curated `github` provider that verifies `X-Hub-Signature-256` (`sha256=<hex hmac>`), extracts `X-GitHub-Delivery` and `X-GitHub-Event`, and uses the delivery id as the dedupe identity (stable across GitHub dashboard redelivery).
- Added `add_endpoint(..., provider_options={...})` for provider-specific tuning such as Stripe `tolerance_s`. Unknown option keys are rejected with `ValueError`.
- Added a comprehensive `tests/test_providers.py` covering the registry surface, GitHub valid/invalid/missing-header/missing-delivery cases, app-local providers, orphan-delivery-with-metadata invariants, provider exception safety, and compatibility paths.
- Added a no-matching-handler regression test for `replay_delivery(...)` so a delivery whose `event_type` does not match a registered handler dead-letters predictably.
- Added a `replay_delivery(...)` test that exercises an actively-running worker so synthetic replay jobs are picked up without restarting the worker loop.
- Added an async `on_error` callback test for `run_worker(...)` that also pins the user-raise-shadows-original-exception path.
- Added two multi-worker isolation tests confirming concurrent workers maintain independent `WorkerState` and that one worker's failure does not contaminate another's terminal state.
- Added an operator runbook covering dead events, invalid/orphan deliveries, worker failures, replay/requeue/replay-delivery recovery, and pruning.

### Removed

- Removed `Knocker.register_provider(...)`. String provider names are now reserved exclusively for curated built-ins; app-local and community providers pass a `Provider` instance directly to `add_endpoint(provider=AcmeProvider(), ...)`. Removing the string registry eliminates the upgrade-collision failure mode where a future curated built-in name could clash with a community provider registered under the same string.

### Fixed

- Re-`add_endpoint(...)` with a fresh `Provider` instance whose `.name` matches the previous one no longer silently reuses the old verifier. Instance equality is not by name; a different `Provider` object is treated as a different implementation, and the verifier is rebuilt (or the call rejects when secrets are missing). The same-name verifier-reuse shortcut is now gated to the curated string-name re-registration path only.

### Changed

- Built-in provider implementations now live in sibling internal modules (`knocker._builtin_stripe`, `knocker._builtin_github`); `knocker.providers` stays the public facade with `Provider`, `ProviderRequest`, `ProviderResult`, and shared helpers. The public import paths for `knocker.Provider`, `knocker.ProviderRequest`, and `knocker.ProviderResult` are unchanged.
- `_StripeProvider` now accepts an internal `clock` constructor argument so the conformance fixture loader can evaluate timestamp tolerance against an absolute, fixture-frozen point in time. Production usage retains wall-clock semantics; the seam is not part of the public `Provider` contract.
- Honker job payload helpers moved out of `knocker.coercion` into a new `knocker.job_payload` module. `coercion.py` is now scoped to truly generic argument coercion (limit, since, older_than, bool filter, prune statuses, duration_ms).
- `app.queue` is no longer a public attribute. The raw Honker queue object is internal (`app._queue`); the small public inspection surface is `app.queue_name`. Tests that previously called `app.queue.claim_batch(...)` for low-level worker-dispatch scenarios use raw SQL through `app.db` instead, so the queue boundary stays explicit.
- Documentation now teaches app-local and community providers exclusively through the `Provider`-instance path; the legacy `register_provider(...)` string-name registry has been retired (see "Removed" below).
- The Stripe verified-ingress path now routes through the new provider registry without any intentional behavior change. Existing `provider="stripe", secrets=[...]` and `verification={"kind": "stripe", ...}` configurations continue to work, including secret rotation and timestamp tolerance.
- `provider="github"` is now a curated built-in provider rather than a metadata-only preset. Endpoints using it must pass `secrets=[...]`; verification, delivery id extraction, and event type extraction now all come from the provider implementation.
- `add_endpoint(provider="name", ...)` resolves only curated built-in names (`stripe`, `github`). Unknown string names fail at registration. App-local and community providers must use the instance path. Built-in providers that require secrets reject missing/`None`/empty `secrets=...` at registration so misconfigured endpoints can no longer silently accept all deliveries.
- Adding a new built-in provider name is no longer a compatibility-affecting collision risk for community providers, because community providers no longer compete in the same string namespace.
- Provider implementations return both verification outcome and extracted metadata in one `ProviderResult`. Invalid receipts retain extracted provider metadata on the orphan delivery row when the provider was able to read it before signature failure.
- An unexpected exception inside `Provider.verify(...)` is now turned into a verification failure with a useful `signature_error` and an orphan delivery, rather than crashing the caller.
- `ProviderRequest.json()` raises `ValueError` on non-JSON bodies and caches its parsed result; provider authors should guard with `try/except` for endpoints that may receive non-JSON payloads.
- Documented that `replay(...)`, `requeue(...)`, and `replay_delivery(...)` reset `attempt_count` to `0` and restart the dead-letter clock.
- Documented that `replay_delivery(...)` resolves handlers using the selected delivery's `event_type`, not the canonical event's.
- Documented that `run_worker(on_error=...)` runs the callback before re-raising, so a user raise in `on_error` shadows the original worker-loop exception; coroutines are awaited.
- Added an explanatory comment in `_event_from_delivery` describing the canonical-event-identity plus delivery-payload synthesized handler input.
- Updated `ROADMAP.md` post-release backlog to reflect that PyPI publication, Linux/macOS wheels, and the tag-driven release workflow are in place; Windows wheels and PyPI trusted publishing remain queued.

## 0.1.0 - 2026-04-29

### Added

- Added the `knocker-core` Rust core for idempotent bootstrap, durable ingest, dedupe, replay/requeue, and event lifecycle transitions.
- Added the Python binding with a thin runtime wrapper over the shared Rust / SQLite contract.
- Added a loadable SQLite extension and a small Node smoke-test binding to pressure-test the cross-language contract.
- Added `SYSTEM.md` plus the initial `.intent/` change record for the current Knocker baseline.
- Added binding-owned verified ingress for generic HMAC-SHA256 and Stripe.
- Added overlapping active-secret support in endpoint configuration for rotation.
- Added append-only `Delivery` rows to preserve every inbound HTTP receipt.
- Added Python `Delivery` reads via `get_delivery(...)` and `list_deliveries(...)`.
- Added provider-preset metadata extraction for Stripe and GitHub in the Python binding.
- Added a stable Python operator surface for `list_events(...)`, `get_event(...)`, `list_deliveries(...)`, `get_delivery(...)`, and `ignore(...)`.
- Added a minimal Python-first pruning surface with `prune_events(...)`, `prune_orphan_deliveries(...)`, and typed prune summary results.
- Added an Astro/Starlight docs site for `knocker.dev`.
- Added the Knocker logo assets to the repo and docs site.
- Added `replay_delivery(delivery_id)` for explicit operator replay of one stored delivery body without mutating the canonical event payload.
- Added local Python worker state snapshots and an optional `on_error` callback for worker-loop failures outside normal handler retry/dead-letter handling.
- Added GitHub Actions CI plus a tag-driven Python release workflow for `knockerlite` wheels and source distributions.

### Changed

- Renamed the planned Python distribution to `knockerlite` while keeping the import package as `knocker`.
- Moved durable Knocker semantics out of the Python wrapper and into the shared `knocker-core` crate.
- Clarified the project docs around the Knocker / Honker boundary, current implementation status, and remaining work.
- Added a high-level `receive(...)` path while preserving the lower-level `ingest(...)` primitive.
- Split the durable ingress model into append-only `Delivery` rows plus deduped `Event` rows.
- Invalid verified ingress now stores orphan deliveries instead of creating ignored events.
- The canonical event payload now comes from the first valid delivery that created the event and is not mutated by later duplicate deliveries.
- The shared schema now bootstraps and migrates from version `1` to `2`, backfilling one synthetic delivery per existing event.
- Stabilized the Python operator helpers in place instead of replacing them with a new namespace.
- Changed default event and delivery list ordering to newest-first (`received_at DESC, id DESC`) with inclusive `since` filtering and bounded `limit` values.
- `ignore(...)` now prevents later worker dispatch for the ignored event, even if a previously queued Honker job is claimed after the ignore.
- Explicit retention pruning now deletes old `handled` / `ignored` events plus their linked deliveries and attempts, and can separately prune old orphan deliveries.
- `replay(...)` and `requeue(...)` now validate accepted event statuses and remove stale live jobs for the same event and queue before enqueueing replacement work.
- Duplicate ingest into an existing event, including a `dead` event, remains audit-only: it stores the new delivery and does not mutate event state or enqueue work.
- Public Python classes and methods now carry concise docstrings, and the README/docs explain the `(event, tx)` atomic handler contract.
- Split the Python binding and test suite into smaller files under the project line-count standard.
- Reframed framework integration as documented host-app route glue instead of a shipped adapter package.

### Fixed

- Missing handlers now fail loudly and dead-letter the stored event instead of silently marking it `ignored`.
- Honker `ack` / `retry` / `fail` transition failures now abort the worker transaction so Knocker event state cannot commit out of sync with queue state.
- `signature_valid=False` delivery filters now include rows whose verification state is `NULL`, not only rows with `signature_valid = 0`.
- Claimed jobs for explicitly pruned events now exit quietly instead of crashing worker dispatch on missing-event lookups.
- Worker dispatch now reads the event inside the work transaction and no longer swallows unrelated `KeyError` bugs as missing events.
- Python operator `limit` validation now rejects floats and strings instead of coercing them.
- Stripe verification config now rejects bool, non-integer, and negative `tolerance_s` values.
- Retention live-job cleanup now leaves malformed Honker payloads alone instead of attempting broad string coercion.
- Lifecycle UDFs now fail fast when asked to mutate an unknown event id.
- Worker stop now responds promptly while idle instead of waiting for a long poll timeout.

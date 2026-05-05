# Plan

Phase:
- 013-retention-automation-providers-bindings-and-perf-polish

Session:
- A

## What we are building

- Knocker will add opt-in retention automation so old handled/ignored events and old orphan deliveries can be pruned on a schedule instead of only by manual operator calls.
- Knocker will add a curated provider pack for the next obvious webhook sources:
  - `shopify`
  - `slack`
  - `postmark`
  - `resend`
  - `paddle`
  - `lemon-squeezy`
- Knocker will add minimal additional bindings for:
  - Node
  - Ruby
  - Go
  - Bun
  - Elixir
- Knocker will do one more bounded performance polish pass after the new product surface lands.

This is intentionally a large phase, but it should still read as one coherent “make Knocker feel complete enough to show people” slice rather than a bag of unrelated work.

## What will not change

- Knocker stays SQLite-first and one-file-first.
- Knocker does not add framework adapters or framework-owned route glue in this phase.
- Knocker does not add distributed coordination, a service mode, or a non-SQLite backend.
- Knocker does not stop being Python-first for the richest operator/admin surface.
- Knocker does not promise equal ergonomics across all bindings in this phase.
- Knocker does not add native/WASM/plugin-runtime provider loading.
- Knocker does not add a rich retention policy engine with endpoint-specific rules, DST-aware windows, or a full cron system.
- Knocker does not weaken the existing durable invariants around ingest, event lifecycle, replay/requeue/replay-delivery, or prune audit rows.

## Key decisions before coding

- Retention automation is Python-first in this phase.
  - Python gets the full opt-in retention-automation surface.
  - Other bindings only need the minimal shared-contract surface, not feature parity for retention automation on day one.
- Retention automation stays intentionally small:
  - explicit config
  - no external daemon
  - no calendar semantics
  - Honker-backed recurring maintenance job rather than a binding-local sleep loop
- Retention runtime ownership is multi-runner-safe in this phase.
  - multiple processes may run retention workers against the same SQLite file without duplicate prune runs
  - one process/instance should still own retention configuration for a given SQLite file
- Automatic retention still uses the existing explicit prune primitives underneath so audit rows and pruning semantics stay exactly one-source-of-truth.
- The curated provider pack is limited to the six providers listed above. If one turns out to be materially more complex than expected, it can be deferred without blocking the rest.
- Additional bindings are intentionally thin wrappers over the shared Rust/SQLite contract.
  - no framework sugar
  - no giant language-idiomatic convenience layers
  - Node and Bun may share the same JS-facing implementation core, but both still need proof from their runtime perspective
- The perf polish pass happens after the feature surface lands and should be bounded:
  - benchmark refresh
  - one or two high-signal worker/ingest improvements if needed
  - no new open-ended performance investigation phase hiding inside this one

## How we will build it

### 1. Add retention automation

- Design and implement a small Python retention surface backed by Honker recurring work.
- Keep the surface explicit and small.
- Reuse the existing prune audit trail rather than inventing a parallel logging system.
- Make the schedule and thresholds obvious in code and docs.

### 2. Add the curated provider pack

- For each provider:
  - add `providers/<name>/metadata.json`
  - add binding-neutral fixture coverage
  - add the Python implementation
  - add docs/examples if the provider has any gotchas worth calling out
- Keep the provider contract fixture-driven so later bindings have a stable target.

### 3. Add the minimal bindings

- Node:
  - move from pressure-test shape toward a minimal real binding
- Ruby:
  - minimal open/bootstrap, endpoint registration, ingest/receive, worker, basic reads/actions
- Go:
  - same minimal shape
- Bun:
  - runtime-facing minimal shape using the JS binding core where sensible, but still proved from the Bun runtime perspective
- Elixir:
  - same minimal shape

The minimum honest binding surface for this phase is:

- open/bootstrap
- endpoint registration
- ingest
- basic worker loop
- `get_event`
- `list_events`
- `replay`

Bindings may add `receive`, `requeue`, or other extras if they come cheaply, but the phase does not count a binding as done unless it supports exactly the baseline above.

### 4. Do one bounded perf polish pass

- Re-run the benchmark surface after the above lands.
- Fix one or two remaining obvious regressions or low-risk hot-path issues if they matter.
- Refresh the published benchmark numbers and any loose performance floors.

### 5. Update docs and baseline truth

- Update docs for retention automation.
- Update docs for the new provider pack.
- Add quickstart/reference pages for the new bindings in their minimal shapes.
- Update `SYSTEM.md` only after the changed behavior is directly proved.

## Direct proof

### Retention automation

- e2e/integration proof that scheduled retention actually invokes the existing prune primitives
- proof that scheduled prunes still write durable audit rows
- proof that no-op scheduled prunes still write audit rows just like manual prunes
- proof that disabling/stopping the retention runner stops future automated prune activity

### Provider pack

- fixture-driven conformance proof for each curated provider
- at least one user-shaped ingress path per provider in Python proving:
  - valid signature accepted
  - invalid signature rejected into delivery-only/orphan path
  - extracted delivery/event metadata behaves as promised

### Bindings

Each binding needs at least one end-to-end proof from that binding perspective that hits the shared contract through its real public shape:

- bootstrap/open
- endpoint registration
- ingest or receive
- worker processes stored work
- a read/action path works after processing

For Bun, the proof must run from the Bun runtime perspective even if it shares the JS core with Node.

### Perf polish

- rerun the stable benchmark surface
- if a perf change is shipped, prove it with the corrected benchmark shape, not a synthetic artifact

## Blast-radius proof

Old intended behavior that still needs re-proof:

- Python operator/admin surface still works
- replay/requeue/replay-delivery semantics still hold
- prune audit semantics still hold under automation
- runtime-confidence tests still pass
- worker contention/throughput baseline does not collapse
- existing Node/shared-contract behavior still holds after expanding bindings/providers

The minimum blast-radius suite is:

- `make test`
- `npm --prefix site run build`
- binding-specific e2e tests for each new binding surface
- provider conformance tests for all curated providers

## Surrogate proof that can help but does not close the claim

- microbenchmarks
- contract-only tests that bypass the real binding wrapper
- fixture-only provider tests without one user-shaped ingress path

These help, but they do not close the “this binding/provider/automation surface really works” claim by themselves.

## Missing proof risks to watch for

- A provider fixture suite can still miss request-adaptation bugs in a real binding entry path.
- A thin binding can pass one smoke test and still be missing basic lifecycle ergonomics or action correctness.
- A retention scheduler can appear to work while silently skipping audit rows if it bypasses the existing prune surface.
- Perf polish can look good in a single benchmark while degrading the ordinary embedded shape if we do not rerun the broader baseline.

## Traps

- Do not let the binding slice turn into five mini-framework projects.
- Do not let provider count become more important than provider correctness and fixture quality.
- Do not build a fancy retention rules engine in this phase.
- Do not claim cross-binding parity that we are not actually proving.
- Do not hide launch-polish work inside vague “perf polish” language; keep it concrete.

## Likely outcome

- If this phase goes well, Knocker comes out looking much more complete:
  - automatic cleanup exists
  - the obvious providers are supported
  - multiple basic bindings exist
  - the benchmark story is refreshed
  - the project feels ready for a broader public audience rather than “Python webhook experiment with one or two sharp edges”

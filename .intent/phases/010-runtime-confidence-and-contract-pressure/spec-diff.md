# Spec Diff: Runtime Confidence And Contract Pressure

Phase:
- 010-runtime-confidence-and-contract-pressure

Session:
- A

## What changes

- Knocker adds a focused runtime-confidence slice covering crash/restart behavior, measured performance, and second-client contract pressure.
- Knocker gains explicit tests for restart-sensitive scenarios that matter to its real product promise:
  - ingress durability before success
  - restart/reopen against existing durable state
  - claimed/live job residue after interrupted work
  - retry/recovery behavior after process interruption
- Knocker adds a small performance harness and published baseline numbers for representative workloads.
- Knocker strengthens non-Python contract pressure so the shared SQLite contract is exercised from more than one client, without claiming full operator/API parity across languages.

## What does not change

- Knocker does not add framework integrations or framework adapters in this phase.
- Knocker does not turn Node into a full public binding in this phase.
- Knocker does not add a broad second-language operator surface.
- Knocker does not add automatic retention scheduling or richer retention policy in this phase.
- Knocker does not widen provider surface or runtime provider loading in this phase.
- Knocker does not change the current public Python API surface unless a bug in the shared runtime contract forces a narrow compatibility-safe fix.

## Invariants

- Runtime confidence work must test the actual durable contract, not mocks.
- Knocker should claim SQLite-shaped guarantees, not stronger bespoke ones:
  - if the relevant SQLite transaction committed, Knocker state committed
  - if the relevant SQLite transaction rolled back or never committed, Knocker state did not durably change
  - reopening the same SQLite file should reveal that same durable truth
- At least one crash-recovery proof in this phase must be a real subprocess/process-boundary kill test, not only an in-process exception or rollback simulation.
- A crash/restart test is only meaningful if it exercises a realistic interruption boundary:
  - before ingress commit
  - after ingress commit but before worker handling
  - after Honker claim but before Knocker handler transaction commit
  - after terminal state is written and queue disposition is attempted
- Knocker's core promise remains: durable stored state is the source of truth after restart, not in-memory worker state.
- Restart/reopen against an existing current-schema database must be boring: Knocker should reopen it, preserve durable data, and continue operating without special recovery steps by the host app.
- For this phase, "restart/reopen" means at least one fresh-process reopen path against the same SQLite file, not only closing and reopening connections in-process.
- Restart-sensitive tests must distinguish:
  - state that must survive process death
  - state that is intentionally local/transient and may be rebuilt
- Performance characterization in this phase is honest baseline evidence, not a marketing benchmark suite.
- Performance work in this phase follows a two-layer model:
  - local benchmark scripts under `bench/` for richer baseline numbers
  - small CI-facing floor tests that only catch order-of-magnitude regressions
- Published performance observations must name the benchmark shape and environment clearly enough that a reader understands the numbers are representative, not universal.
- Second-client contract pressure means:
  - a non-Python client can hit the shared SQLite contract cleanly
  - shared semantics do not rely on Python-only assumptions
  - the test surface stays narrow and contract-oriented
- Second-client pressure should look like interop/contract agreement, not API parity:
  - shared durable schema
  - shared write/read semantics
  - shared lifecycle/recovery semantics for the small named surface under test
- Second-client contract pressure in this phase does **not** imply:
  - full multi-language feature parity
  - multiple maintained public bindings
  - multiple operator/admin surfaces
- Runtime-confidence and second-client tests should continue using the single supported current schema only. This phase does not resurrect schema-version ladders or migration support.

## How we will verify it

- Tests cover at least:
  - reopen/restart against a database with existing events/deliveries/attempts/live jobs
  - interrupted ingress does not produce false-success durable state
  - committed ingress survives reopen and is still processable
  - interrupted worker handling leaves state consistent with Knocker/Honker invariants after reopen
  - claim-expiry / stale-claim behavior remains correct after reopen
  - second-client contract tests exercise the shared contract beyond the current minimal smoke path
  - performance harness has a documented local invocation path, and CI may run only a smoke-sized non-gating execution to prove the harness still works
- The crash/restart minimum proof shape in this phase is:
  - one subprocess opens the DB and begins a realistic Knocker write path
  - the subprocess is killed before commit
  - a fresh process reopens the same DB file
  - integrity and durable-state expectations are checked
  - fresh post-crash Knocker operations still work
- The minimum second-client contract scope in this phase is:
  - bootstrap/open the current schema
  - endpoint registration
  - first ingest happy path
  - duplicate ingest / dedupe
  - stored-state visibility over the shared contract
  - at least one recovery/lifecycle check
- The minimum published performance workloads in this phase are:
  - durable ingress-only throughput/latency
  - no-op-handler worker-drain throughput/latency
- The minimum CI-facing performance floor shape in this phase is:
  - loose thresholds
  - explicitly regression-catching, not benchmark-authoritative
  - few enough cases that flakiness teaches nothing dangerous
- Docs publish:
  - what restart/crash scenarios Knocker explicitly tests
  - one or more baseline throughput/latency observations
  - clear wording that Node remains a contract pressure-test client, not a full operator binding

## Notes

- This phase is about proving Knocker's real promise under ugly but realistic conditions.
- The goal is not distributed-systems heroics. The goal is confidence that one-process, one-SQLite-file Knocker behaves predictably after interruption and under ordinary load.

# Spec-Diff + Plan Review 1

Verdict: the phase direction is right and more important than any remaining roadmap item, but a few load-bearing choices are still soft enough that implementation could satisfy the words without delivering the intended confidence.

## Positive

- Combining crash/restart confidence, honest performance characterization, and second-client contract pressure into one confidence phase is the right cut.
- The spec correctly refuses to conflate “second-client pressure” with “full second binding.”
- The phase keeps framework concerns out of scope, which matches the actual product boundary.
- The current-schema-only rule is preserved, which avoids reopening the fake pre-release migration ladder.

## Findings

### P1 — Crash/interruption proof method is still undefined

The spec names realistic interruption boundaries, but it does not pin what kind of harness is acceptable for proving them. That matters because “interrupted ingress does not produce false-success durable state” can be weakly satisfied with rollback-style exception tests, while “process died at a bad boundary” usually needs a stronger mechanism. Before implementation, decide whether these scenarios must be exercised via:

- subprocess/process-boundary harnesses
- SQLite-trigger/abort injection
- explicit connection-close / reopen fault injection
- or a named mix of the above

Without that, the executor can produce green tests that do not really increase restart confidence.

### P1 — Performance execution path is still a product decision, not an implementation detail

The spec says the harness runs “in CI or a documented local path,” and the plan says “a light regression check where practical.” That still leaves the most important decision open: is performance evidence a CI-gated signal, a local/manual signal, or both? Those are materially different promises. Pin one:

- documented local benchmark only
- CI non-gating benchmark/report
- CI regression threshold on a narrow benchmark

Right now implementation has to choose the governance model, not just the code.

### P2 — Second-client minimum contract scope should be enumerated

“Ingest, dedupe, stored-state visibility, and one or two lifecycle/recovery checks” is the right shape, but still too fuzzy for a confidence phase. Pin the minimum cases the Node pressure client must prove so the phase cannot land with a too-small expansion. For example, enumerate whether the minimum set includes:

- bootstrap/open current-schema DB
- endpoint registration
- first ingest happy path
- duplicate ingest / dedupe
- invalid-signature / delivery-only path, if applicable from the shared contract surface
- one recovery/reset path such as replay or requeue
- one read-back visibility check over stored state

The point is not to widen Node, just to make “contract pressure” concrete enough to review.

### P2 — Restart/reopen should pin whether same-process reopen is enough

The spec says “restart/reopen against existing durable state,” but there is a real difference between:

- closing and reopening connections in the same process
- starting a fresh process against the same DB

If same-process reopen is enough for this phase, say so explicitly. If at least one fresh-process path is required, say that instead. Leaving it open makes the word “restart” stronger than the test harness may actually be.

### P2 — Baseline performance workload(s) should be named up front

The plan calls for representative workload(s), but does not pin which ones matter most. That makes future numbers harder to interpret and review. A small named set would keep the phase honest, for example:

- durable ingress-only throughput/latency
- no-op handler worker drain throughput
- maybe one failure/retry workload if that is considered essential

Without named workloads, the phase can publish numbers that are technically true but not clearly tied to Knocker’s central claims.

# Response 1

Pinned before implementation:

- Crash/restart confidence is now explicitly SQLite-shaped:
  - committed transaction state must survive reopen
  - rolled-back or never-committed state must not appear after reopen
  - reopening the same SQLite file is the confidence target, not a stronger bespoke guarantee
- "Restart/reopen" now requires at least one fresh-process reopen path against the same SQLite file.
- Performance execution is now pinned as:
  - documented local benchmark path
  - optional CI smoke execution only
  - no fragile timing gate in this phase
- Second-client minimum scope is now pinned:
  - bootstrap/open
  - endpoint registration
  - ingest happy path
  - duplicate ingest / dedupe
  - stored-state visibility
  - one lifecycle/recovery check
- Baseline workloads are now pinned:
  - durable ingress-only
  - no-op-handler worker drain

Net: 010 remains the same phase direction, but the confidence claims are now constrained to SQLite-native guarantees and a small, reviewable benchmark/second-client surface.

# Response 2

Honker comparison tightened the phase further:

- Crash/restart is now explicitly required to include a real subprocess-kill path, not just rollback/fault-injection style tests.
- Performance is now explicitly split like Honker:
  - local `bench/` scripts for richer baseline numbers
  - small CI-facing floor tests for regression catching
- Second-client pressure is now framed as contract/interop agreement, not vague extra-client exercise and not full parity work.

This keeps 010 very close to the proven Honker model:
- brutal crash test where it matters
- simple benchmark scripts
- cheap CI regression guards
- concrete cross-client contract checks

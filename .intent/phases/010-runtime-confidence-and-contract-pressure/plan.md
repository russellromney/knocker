# Plan

Phase:
- 010-runtime-confidence-and-contract-pressure

Session:
- A

## Goal

- Increase trust in Knocker's actual runtime promise by testing interruption boundaries, publishing honest baseline performance numbers, and pressure-testing the shared contract from a second client without pretending that client is a full public binding.

## Phase decisions

- This phase combines three closely related confidence items:
  - crash/restart confidence
  - performance characterization/regression detection
  - second-client contract pressure
- The center of gravity is the shared durable contract, not framework behavior.
- Python remains the only full public operator surface in this phase.
- Node remains a contract pressure-test client, not a feature-parity target.
- Performance work is baseline-oriented and honest:
  - two named workloads:
    - durable ingress-only
    - no-op-handler worker drain
  - documented environment
  - reproducible local command(s)
  - optional CI smoke execution that proves the harness still runs, but no fragile timing gate yet
- Crash/restart work should prefer realistic durable-state setups over synthetic mocks.
- Crash/restart proof should follow SQLite-shaped guarantees:
  - committed transaction state must survive reopen
  - rolled-back or never-committed state must not appear after reopen
- At least one restart-sensitive path in this phase must use a fresh-process reopen against the same SQLite file, not only an in-process reconnect.
- At least one restart-sensitive path must use a real subprocess kill harness, mirroring Honker's crash-recovery style rather than relying only on rollback-style exception tests.
- Current-schema-only rule remains in force. Legacy schema support and migration ladders stay out.

## Build order

1. Pin runtime-confidence scenarios in tests:
   - reopen current-schema DB with existing durable state
   - prove committed ingress survives reopen
   - prove interrupted/rolled-back work does not masquerade as committed durable state
   - exercise restart-sensitive worker/claim scenarios using real SQLite + Honker state
   - include at least one fresh-process reopen path
   - include at least one subprocess-kill crash path
2. Strengthen second-client contract pressure:
   - expand the Node contract pressure-test beyond the current minimal happy path
   - keep it contract-shaped with this minimum set:
     - bootstrap/open
     - endpoint registration
     - ingest happy path
     - duplicate ingest / dedupe
     - stored-state visibility
     - one lifecycle/recovery check
   - prefer explicit interop/contract-agreement assertions over building Node-only convenience surface
   - do not widen Node into a second full binding surface
3. Add performance characterization:
   - add local `bench/` scripts or equivalent for the two named workloads:
     - durable ingress-only
     - no-op-handler worker drain
   - record baseline observations
   - make the local invocation path explicit and repeatable
   - add small CI-facing performance-floor tests with loose thresholds that only catch order-of-magnitude regressions
   - if CI runs the richer benchmark scripts at all, keep that execution non-gating for timing
4. Update docs and evidence:
   - current confidence story
   - what restart/crash conditions are actually covered
   - published performance notes
   - explicit wording that Node is still a contract pressure-test client
5. Run verification and record evidence.

## Verification

- `make test`
- `npm --prefix site run build`
- Any new runtime-confidence / benchmark / second-client tests are listed by name in the phase evidence.

## Traps

- Do not drift into framework integration testing; that is outside Knocker's product boundary.
- Do not mistake second-client contract pressure for a promise of full multi-language parity.
- Do not build an elaborate benchmark lab before we have simple, trustworthy baseline numbers.
- Do not make CI performance checks so noisy that they teach everyone to ignore them.
- Do not widen this into Windows wheel / release-plumbing work.
- Do not reintroduce schema-version/migration support while adding restart coverage.
- Do not let "second-client pressure" collapse into only smoke-open tests; it needs real contract agreement assertions.

## Areas not to touch

- Provider loading architecture questions.
- Automatic retention scheduling.
- Full Node operator/admin surface.
- Framework-specific route adapters.
- Release automation beyond what is required to run the new confidence checks.

## Assumptions and risks

- Crash/restart confidence is more important to Knocker's truthfulness than additional surface-area expansion.
- A second client can expose Python-shaped assumptions early even if it remains a thin pressure-test.
- Honest baseline numbers are enough for this phase; we do not need exhaustive p50/p95/p99 matrix work before getting useful signal.
- Honker's split is the model:
  - subprocess crash tests for restart truth
  - local benchmark scripts for baseline numbers
  - CI floor tests for regression catching

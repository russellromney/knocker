# Plan

Phase:
- 012-worker-throughput-and-contention-reduction

Session:
- A

## What we are building

- Knocker will ship real worker-throughput improvements based on the Phase-011 evidence instead of doing more open-ended exploration.
- The production worker will intentionally claim multiple jobs per claim transaction instead of permanently hard-coding one-job claims.
- The worker path will reduce avoidable contention and write-lane churn where that can be done without weakening truthful lifecycle semantics.
- Knocker will document the intended hot-path topology more plainly: one `knocker.open(...)` per process is the normal fast path; many independent opens against one SQLite file remain supported but are a degraded contention mode.
- The benchmark and performance-floor evidence will be refreshed to match the shipped worker path.

## What will not change

- Knocker stays SQLite-first and one-file-first.
- Knocker does not add distributed coordination, a non-SQLite backend, or a multi-node worker design.
- Knocker does not add framework integrations.
- Knocker does not add a public performance-tuning API in this phase.
- Knocker does not change replay, requeue, replay-delivery, or attempt-history semantics unless the plan says so explicitly.
- Knocker does not weaken the invariant that event-state transitions and Honker queue disposition commit together.
- Knocker does not add a process-global connection registry or singleton handle manager in this phase.
- Knocker does not turn multi-open same-file contention into a promised “good scaling” story.
- Knocker does not spend time on JSON micro-optimizations in this phase.

## Key decisions before coding

- Production claim batching is in scope and is the center of gravity for the phase.
- The worker claim batch size will be a fixed internal default of `10` for this phase.
  - no public knob
  - no per-endpoint tuning
  - future tuning can happen in a later phase if needed
- The worker will buffer and drain already-claimed jobs safely. Claimed jobs must never be stranded in live rows because local iteration only dispatched `jobs[0]`.
- Worker stop semantics with batches are pinned:
  - stop is still checked promptly between jobs
  - if a worker already holds a local claimed-job buffer, it may finish draining that buffer before exiting
  - this phase does not intentionally abandon locally buffered claimed jobs just to exit faster
  - `run_worker()` itself must honor this rule; changing claim size and iterator buffering alone is not sufficient
- Ack batching is out of scope for 012 unless a design falls out that keeps the existing atomic “event transition + queue disposition” contract obviously intact. The default assumption for this phase is per-job queue disposition stays in the same transaction as the event transition.
- If a lock-retry/fairness improvement ships, it should be small and local:
  - it applies to claim/acquisition pressure, not to weakening event/queue atomicity
  - it does not add cross-process coordination
  - it does not promise stronger guarantees than SQLite already gives

## How we will build it

### 1. Ship production claim batching

- Change the real worker path to claim up to `10` jobs at a time.
- Keep the claimed-job buffer local to the worker iterator so already-claimed jobs are dispatched safely.
- Recheck idle-poll and stop behavior with the batched path so the worker does not become sluggish when the queue is mostly empty.
- Change `run_worker()` as needed so a stop signal received between buffered jobs still honors the pinned “drain already-claimed local buffer before exit” rule.

### 2. Reduce worker-path churn if the semantics stay clean

- Inspect the claim/dispatch path for avoidable write-lane churn.
- Candidate targets:
  - the extra lifecycle work around `mark_processing`
  - unnecessary queue/database round-trips between claim and dispatch
  - small lock-retry/fairness improvements around claim pressure
- Reject any change that muddies the atomic event/queue contract, and write that rejection down in the phase evidence if it happens.

### 3. Refresh the benchmark surface around the shipped path

- Re-run the corrected Phase-011 benchmark shape after the worker changes land.
- Keep the stable experiment set cheap to rerun.
- Keep the heavy multi-handle contention probes explicit opt-ins rather than the default “truth” story.

### 4. Update docs and baseline truth

- Update bench docs and phase evidence with the new baseline.
- Update user-facing guidance so the intended hot-path process shape is explicit.
- Update `SYSTEM.md` only after the new worker behavior is directly proved.

## Direct proof

The changed behavior needs proof that hits the real public path, not just helper tests.

- Integration/e2e proof for real batched claims:
  - ingest multiple events through the public path
  - run the real worker path
  - prove every claimed job is processed
  - prove no live rows are stranded
- Integration proof for worker stop behavior with batches:
  - queue more than one event
  - run worker with the batched path
  - trigger stop while work is buffered
  - prove the pinned stop semantics actually happen
- Integration/e2e proof for throughput improvement:
  - rerun the corrected local benchmark harness
  - show materially better worker throughput than the Phase-011 corrected baseline
- Integration proof for contention-sensitive behavior:
  - run at least one bounded mixed-load shape such as `1 producer / 2 workers` or `2 producers / 1 worker`
  - compare it against the Phase-011 corrected evidence for the same shape
  - prove the phase did not buy a low-contention benchmark win by quietly regressing the ordinary contended path

## Blast-radius proof

Old intended behavior that still needs re-proof:

- replay/requeue/replay-delivery still behave the same
- attempt history is still correct
- worker failure/dead-letter behavior is still correct
- queue disposition still commits atomically with event-state transition
- runtime-confidence and operator-surface tests still pass
- the Node contract pressure tests still pass

The minimum blast-radius suite is:

- `make test`
- `npm --prefix site run build`
- targeted recovery/worker tests covering replay, requeue, replay-delivery, and failure paths

## Surrogate proof that can help but does not close the claim

- microbenchmarks for serialization
- synthetic lock-contention probes
- isolated monkeypatch measurements for `mark_processing`

These can guide decisions, but they do not by themselves prove the shipped worker path is correct.

## Missing proof risks to watch for

- A faster benchmark alone does not prove event/queue atomicity still holds.
- A local batched-claim unit test alone does not prove replay/requeue/replay-delivery blast radius is safe.
- Same-process shared-handle success does not prove multi-open same-file behavior is good; it only proves the lower-contention comparison point.

## Traps

- Do not drift back into open-ended performance exploration. Phase 011 already built the map.
- Do not optimize JSON or other tiny costs while the worker hot path is still the obvious bottleneck.
- Do not add user-facing tuning knobs unless the fixed internal default is clearly inadequate.
- Do not silently redesign lifecycle semantics in the name of speed.
- Do not update `SYSTEM.md` before the direct proof is real.

## Likely outcome

- If this phase goes well, Knocker should come out with a materially faster real worker path, cleaner evidence for “a few workers can keep up” in the ordinary embedded case, and clearer guidance about the degraded nature of same-file multi-open contention.

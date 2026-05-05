# Plan

Phase:
- 011-throughput-and-lock-contention-exploration

Session:
- A

## Goal

- Make Knocker's bottlenecks measurable and attributable so that future speed work is grounded in evidence, not guesswork.
- Distinguish benchmark-shape artifacts from real architectural limits.
- Explore candidate optimizations in controlled experiments without prematurely committing to ship them.
- Preserve Knocker's SQLite-shaped model and one-file simplicity throughout.

## Phase decisions

- This phase is pure measurement and experiment. It does not ship architectural changes to the public path unless a bug fix (e.g., benchmark-shape artifact) is proven to corrupt existing test signal.
- The center of gravity is honest, reproducible numbers.
- We address the known anomalies from Phase 010:
  - The earlier mixed-load benchmark accidentally starved the asyncio event loop because synchronous `receive(...)` calls ran in a tight async loop.
  - A corrected threaded-producer probe exposed a real `database is locked` failure during worker `BEGIN IMMEDIATE` / claim.
  - Worker throughput lags far behind ingress even with a no-op handler.
- Likely hotspots are treated as hypotheses to be tested, not as targets to be fixed:
  - one-job-at-a-time `claim_batch(..., 1)`
  - two write transactions per handled event (claim tx + dispatch/ack tx)
  - multiple `knocker.open(...)` handles contending at SQLite file level rather than through a shared in-process writer slot
  - no higher-level retry/fairness around `BEGIN IMMEDIATE`
  - extra event-row churn (`mark_processing`, then `mark_handled` / `mark_failed`)
  - Python/Rust JSON marshalling on claim/ingest hot paths
- Prototype experiments may modify code temporarily to isolate costs (e.g., a measurement-only bypass of `mark_processing`), but these modifications must be clearly labeled and gated so they do not affect production tests.
- Performance work follows a three-layer model:
  - rich local benchmark scripts under `bench/` for deep-dive numbers
  - small CI-facing performance-floor tests that only catch order-of-magnitude regressions
  - experiment-specific scripts with documented invocation paths
- Conclusions are written separately from the numbers: numbers are evidence, conclusions are interpretation.

## Build order

### 1. Fix benchmark-shape artifacts in existing harness

- Correct `bench/knocker_bench.py` so that ingest load does not starve the asyncio event loop.
- Ensure producer threads/processes run independently of the worker loop, producing realistic mixed-load conditions.
- Re-run the corrected baseline and record new phase-011 reference numbers.

### 2. Build the corrected mixed-load benchmark

- Shape: one or more producer threads/processes, one or more worker tasks, shared SQLite file.
- Producers should use `knocker.ingest(...)` or `knocker.receive(...)` off the main event loop.
- Workers should use the real `run_worker()` loop.
- Record:
  - producer throughput
  - worker throughput
  - lock failure rate / retry counts
  - latency distributions if feasible without heavy infrastructure

### 3. Run the experiment matrix

#### 3a. Producer / worker topology experiments

| Topology | What we learn |
|---|---|
| 1 producer / 1 worker | Lowest-contention mixed-load baseline for this architecture. |
| 1 producer / N workers | Whether worker scaling is bottlenecked by file-level writer contention. |
| N producers / 1 worker | Whether ingress floods the single writer slot and starves the worker's claim tx. |
| N producers / N workers | Realistic mixed load; where does throughput collapse? |

#### 3b. Handler cost experiments

| Handler | What we learn |
|---|---|
| no-op | Theoretical max worker throughput if DB work inside handler were free. |
| tiny DB-write (one INSERT/UPDATE) | Cost of a realistic minimal handler. |
| slow synthetic (sleep 1ms) | Whether worker throughput is compute-bound or I/O-bound. |

#### 3c. Writer-handle topology experiments

| Handle pattern | What we learn |
|---|---|
| Single shared `knocker.open(...)` | Same-process idealization: one writer slot, all readers shared, useful as a lower-contention comparison point rather than a deployment assumption. |
| Multiple independent `knocker.open(...)` | The realistic multi-module case: multiple writer connections contending at SQLite file level. |
| Honker's single writer + reader pool | Where the binding's current architecture sits on this spectrum. |

#### 3d. Claim-path experiments

| Claim variant | What we learn |
|---|---|
| `claim_batch(..., 1)` (current default) | Baseline claim cost and transaction frequency. |
| `claim_batch(..., k)` for k > 1 | Whether batching claims amortizes SQLite tx overhead. |
| Ack batching prototype (measurement only) | Whether deferring acks into a periodic batch tx reduces writer contention. |

#### 3e. Lifecycle write experiments

| Lifecycle variant | What we learn |
|---|---|
| Current: `mark_processing` then `mark_handled`/`mark_failed` | Baseline with two event-row writes per handled event. |
| Temporary bypass of `mark_processing` (measurement only) | Isolate cost of the extra `UPDATE` that sets `processing`. |
| Temporary bypass of `mark_handled` attempt recording (measurement only) | Isolate cost of the `knocker_attempts` INSERT. |

#### 3f. Transaction / locking experiments

| Locking variant | What we learn |
|---|---|
| Default `BEGIN IMMEDIATE` | Baseline contention failure rate. |
| Small retry loop around `BEGIN IMMEDIATE` | Whether transient lock-busy resolves with a few retries. |
| Fairness strategy (e.g., exponential backoff or writer queue) | Whether lock starvation between producers and workers materially changes throughput. |

#### 3g. Serialization experiments

| Serialization variant | What we learn |
|---|---|
| Current: JSON result from `knocker_ingest(...)` | Baseline Python/Rust JSON cost on hot path. |
| Binary / simpler encoding prototype (measurement only) | Whether JSON parsing is a meaningful slice of ingest latency. |
| Current: JSON payload on Honker enqueue/dequeue | Baseline for queue payload cost. |

### 4. Record measurement tables and write conclusions

- For each experiment, record:
  - throughput (events/second)
  - latency (avg, p95 if sampled)
  - lock failures (count, rate)
  - CPU and I/O profiles if available (e.g., `py-spy`, `perf`)
- Write a short conclusion per hypothesis:
  - confirmed / disproven / inconclusive
  - whether the bottleneck matters at expected production scale
  - whether an optimization is worth pursuing in a future phase
- List recommended next architectural changes as phase outputs only, not as committed roadmap items.

### 5. Update docs and evidence

- Document the corrected benchmark invocation in `README` or `bench/README`.
- Record environment details for reproducibility.
- Update any CI performance-floor tests if the corrected baseline shifts the floor.

## Verification

- `make test`
- `npm --prefix site run build`
- Each experiment script runs with a documented invocation and produces stable output.
- Measurement tables exist and are checked into phase evidence.

## Traps

- Do not drift into shipping optimizations before the measurement map is complete.
- Do not let temporary measurement-only code leak into the production path or tests.
- Do not build elaborate profiling infrastructure before simple timers and counters tell the story.
- Do not make CI performance checks so noisy that they teach everyone to ignore them.
- Do not mistake "could be faster with X" for "should build X now."
- Do not let this phase become a distributed-systems redesign. SQLite-first is the feature.

## Areas not to touch

- Framework integrations.
- Provider loading architecture.
- Automatic retention scheduling.
- Full Node operator/admin surface.
- Multi-language binding parity.
- Non-SQLite backend work.
- Public Python API surface expansion.

## Assumptions and risks

- We assume the hotspots listed are the right top candidates, but experiments may reveal surprises.
- We assume the corrected benchmark shape finally gives honest signal; if it does not, we iterate the harness before drawing conclusions.
- We assume temporary measurement modifications (schema bypasses, ack batching prototypes) do not destabilize the production test suite.
- Honker's single-writer architecture is a deliberate SQLite design choice. We measure its limits but do not plan to redesign it in this phase.

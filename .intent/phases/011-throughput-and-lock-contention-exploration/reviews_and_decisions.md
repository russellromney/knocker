# Reviews and Decisions

Phase:
- 011-throughput-and-lock-contention-exploration

Session:
- A

## Implementation Notes 1

What the implementation and rerun evidence established:

- The earlier Phase-010 mixed-load intuition was polluted by benchmark shape. Synchronous `receive(...)` calls in a tight async producer loop starved the event loop and overstated steady-state throughput.
- Once the benchmark shape was corrected, the important stable results came from the bounded local scripts, not the pathological high-contention runs.
- Worker throughput is constrained primarily by claim/dispatch lifecycle work and file-level writer contention, not by Python JSON marshalling.
- The `mark_processing` write is measurable, but it is not the whole story. The larger cost center remains the worker-side lifecycle path as a whole.
- Multiple independent `knocker.open(...)` handles against one SQLite file are a degraded contention mode. They are useful to probe architectural limits, but they are not the baseline to present as ordinary same-process performance.

## Implementation Response 1

Changes made in response to the exploration:

- Fixed a real correctness bug in the worker iterator: `_WorkerQueueIter` now buffers extra claimed jobs instead of dispatching only `jobs[0]`. This makes `claim_batch(..., n > 1)` safe for experiments and future worker work.
- Added a regression test proving that a monkey-patched batched claim of two jobs drains both jobs and leaves no stranded live rows.
- Kept the stable experiment set cheap to rerun and moved the heavier multi-handle contention probes behind an explicit opt-in in `bench/run_all_experiments.py`.
- Clarified the bench docs so the shared-handle topology is framed as a same-process lower-contention comparison point, while multiple independent opens are explicitly documented as degraded-mode contention probes.

## Final conclusions

- The stable local baseline is now the corrected one from Phase 011, not the earlier event-loop-starved mixed-load intuition from Phase 010.
- Claim batching is the highest-signal improvement candidate uncovered by this phase. Once the buffering bug was fixed, larger batch sizes materially improved worker throughput.
- JSON encode/decode cost is negligible relative to lifecycle and lock contention costs.
- Future throughput work should target worker claim/dispatch architecture and writer-handle contention rather than serialization micro-optimizations.

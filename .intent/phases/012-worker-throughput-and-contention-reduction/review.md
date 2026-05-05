# Review

Phase:
- 012-worker-throughput-and-contention-reduction

Session:
- A

## Plan Review 1

### Finding 1

The pinned stop semantics and the stated build steps do not line up tightly enough yet.

The plan says that once a worker has a local claimed-job buffer, it may drain that buffer before exiting. But the current `run_worker()` shape checks `stop_event` at the top of the loop before asking the iterator for the next job. If implementation only changes claim size plus iterator buffering, a stop signal that arrives between buffered jobs can still make the worker exit early and leave locally buffered claimed jobs undispatched until lease expiry. The plan should explicitly call out that `run_worker()` itself must honor the buffered-drain rule, not just `_WorkerQueueIter`.

### Finding 2

The direct proof for throughput improvement is still too isolated for a phase that claims contention reduction.

Right now the direct proof only requires rerunning the corrected local benchmark and showing materially better worker throughput than the Phase-011 corrected baseline. That proves the 1-producer/1-worker path got faster, but it does not prove the chosen production batch size and any lock-retry/fairness work actually help under the ordinary contended shape that motivated the phase. The plan should require at least one bounded contention-sensitive proof, such as `1 producer / 2 workers` or `2 producers / 1 worker`, so we do not ship a benchmark win that quietly regresses the claimed contention story.

## Response 1

Both plan-review findings were folded back into `plan.md` before implementation:

- the plan now says plainly that `run_worker()` itself must honor the buffered-drain-on-stop rule
- the direct proof now requires one bounded contention-sensitive proof in addition to the low-contention benchmark rerun

## Implementation Notes 1

What landed:

- the production worker now claims up to `10` jobs per claim transaction
- `run_worker(stop_event=...)` drains already-claimed local buffered jobs before exiting
- the Python/API docs now explicitly recommend one long-lived `knocker.open(...)` per process for hot paths
- the local benchmark/evidence surface was refreshed after the worker-path change

What did not land:

- no public worker tuning knob
- no ack batching redesign
- no distributed or cross-process coordination layer

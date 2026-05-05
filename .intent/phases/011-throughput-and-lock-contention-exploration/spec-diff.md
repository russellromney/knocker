# Spec Diff: Throughput And Lock Contention Exploration

Phase:
- 011-throughput-and-lock-contention-exploration

Session:
- A

## What changes

- Knocker adds a disciplined measurement and experimentation phase to understand throughput limits and lock-contention behavior under realistic mixed load.
- The existing benchmark harness (`bench/knocker_bench.py`) is corrected to avoid benchmark-shape artifacts that previously produced misleading signal.
- New benchmark and experiment scripts are added to `bench/` to explore specific bottlenecks.
- Controlled prototype experiments are run to isolate the cost and impact of known potential optimization targets.
- Measurement tables and honest baseline numbers are published as phase evidence.

## What does not change

- Knocker does not commit to shipping all optimizations explored in this phase.
- Knocker does not abandon its SQLite-shaped model or one-file simplicity in this phase.
- Knocker does not begin multi-language parity work (Node binding remains a contract pressure-test client, not a feature target).
- Knocker does not add a non-SQLite backend, distributed coordination, or multi-node support.
- Knocker does not add framework integrations or framework adapters.
- Knocker does not add new public Python API surface.
- Knocker does not redesign the event lifecycle or queue mechanics.
- Knocker does not add automatic retention scheduling or richer retention policy.

## Invariants

- The SQLite-shaped model remains the only durable store. Any experiment that requires a second store or non-local durability is out of scope.
- One-process, one-SQLite-file simplicity is preserved. Experiments that require multiple processes on the same file purposely test contention boundaries, not a product endorsement of multi-process use.
- Measurement must distinguish benchmark-shape artifacts from real architectural limits. If a benchmark design flaw produces a misleading bottleneck, the flaw is fixed before conclusions are drawn.
- Experiments are measurement-only or prototype-only. They do not ship into the public API or become permanent infrastructure unless a subsequent phase explicitly authorizes it.
- Published numbers must name the benchmark shape, environment, and workload clearly enough that a reader understands they are representative, not universal.
- If an experiment modifies the database schema or SQL contract for measurement purposes only (e.g., a temporary `processing` state bypass), the modification must be clearly labeled as temporary and must not affect production path tests.
- The Python binding remains the only full public operator surface.
- Performance floor tests in CI remain loose and regression-catching only, not authoritative benchmarks.

## How we will verify it

- Corrected mixed-load benchmark with producer work off the asyncio event loop avoids benchmark-shape starvation artifacts and produces stable numbers.
- If `database is locked` failures still occur under corrected mixed load, they are treated as real measured outcomes to quantify and attribute, not as automatic harness bugs.
- Lock-contention repros produce quantified failure rates (e.g., `BEGIN IMMEDIATE` retry count) for each experiment variant.
- Measurement tables exist for:
  - through-ingress throughput (one producer, two producers, N producers)
  - worker drain throughput (one worker, N workers)
  - handler cost comparison (no-op vs tiny DB-write)
  - claim cost comparison (`claim_batch(..., 1)` vs larger batches)
  - writer-handle contention comparison (shared in-process `knocker.open(...)` vs multiple independent opens)
  - lifecycle write cost comparison (current `mark_processing`/`mark_handled` vs temporary measurement-only bypass)
  - serialization cost on hot paths (JSON marshalling benchmark)
- Conclusions document which bottlenecks are real architectural limits vs. benchmark artifacts.
- Any recommended next architectural changes are explicitly distinguished from the measurement itself and listed as phase outputs only.
- Build passes: `make test`, `npm --prefix site run build`.

## Notes

- This phase is about measurement and decision support, not about shipping a faster Knocker.
- The goal is to give future implementation work a clear, quantified map of where speed lives and where it does not.
- If an experiment shows that a bottleneck is not where we expected, we record that honestly and adjust our intuition.
- The experiment scripts are part of the evidence. They must be clean enough to rerun in six months without heroic reconstruction.

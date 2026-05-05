# Phase 011 Benchmarks: Throughput and Lock Contention Exploration

All scripts run from the repo root (`/Users/russellromney/Documents/Github/knocker`) via `uv run --group dev python bench/<script>`. Results are printed as text tables suitable for pasting into `commits.txt` evidence.

## Scripts

| Script | Purpose |
|---|---|
| `bench_shared.py` | Common infrastructure (DB setup, threaded producers, workers, metrics). Not run directly. |
| `knocker_bench.py` | Corrected replacement for Phase 010 baseline. 1 producer thread + 1 worker. |
| `experiment_mixed_load.py` | Topology matrix for the default multi-handle producer shape: 1/1, 1/4, 4/1, 4/4 producer/worker. |
| `experiment_handler_cost.py` | No-op vs tiny-DB-write vs 1ms-sleep handler. |
| `experiment_writer_topology.py` | Same-process shared-handle idealization vs multiple independent opens on one file. |
| `experiment_lock_contention.py` | Quantify `database is locked` failures at 1, 2, and 4 producers. |
| `experiment_claim_batch.py` | Monkey-patch `claim_batch` to batch sizes 1, 2, 5, 10 and measure buffered end-to-end drain. |
| `experiment_lifecycle_cost.py` | Temporarily bypass `mark_processing` to isolate its cost. |
| `experiment_serialization_cost.py` | Micro-benchmark JSON parse/encode in Python. |
| `run_all_experiments.py` | Run the stable experiment set by default; pass `--include-contention` to add the heavier multi-handle probes. |

## Notes

- Producers always run in dedicated threads so they do not starve the asyncio event loop.
- Each experiment uses a fresh temp database.
- The production worker currently claims up to `10` jobs per claim transaction.
- Lock failures (`database is locked`) are counted and displayed; they are treated as real measured outcomes, not harness bugs.
- Scripts that open multiple `knocker.open(...)` handles are deliberately measuring the degraded multi-handle contention mode, not claiming that shape is ideal.
- The shared-handle topology is a same-process lower-contention comparison point, not a deployment assumption.

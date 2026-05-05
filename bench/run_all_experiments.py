#!/usr/bin/env python3
"""Orchestrator that runs all Phase 011 experiments and writes evidence.

Invocation:
    uv run --group dev python bench/run_all_experiments.py
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
DEFAULT_EXPERIMENTS = [
    "knocker_bench",
    "experiment_handler_cost",
    "experiment_claim_batch",
    "experiment_lifecycle_cost",
    "experiment_serialization_cost",
]
CONTENTION_EXPERIMENTS = [
    "experiment_mixed_load",
    "experiment_writer_topology",
    "experiment_lock_contention",
]


def _run_experiment(name: str) -> tuple[str, str, int]:
    script = SCRIPT_DIR / f"{name}.py"
    print(f"\n{'='*60}")
    print(f"Running: {name}")
    print("=" * 60)
    start = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        cwd=str(SCRIPT_DIR.parent),
    )
    elapsed = time.perf_counter() - start
    print(proc.stdout, end="")
    if proc.stderr:
        print("[stderr]", proc.stderr, file=sys.stderr)
    print(f"Completed in {elapsed:.2f}s (rc={proc.returncode})")
    return proc.stdout, proc.stderr, proc.returncode


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all Phase 011 experiments.")
    parser.add_argument("--skip", nargs="+", default=[], help="experiments to skip")
    parser.add_argument("--only", nargs="+", default=[], help="only run these experiments")
    parser.add_argument(
        "--include-contention",
        action="store_true",
        help="also run the heavier multi-handle contention probes",
    )
    args = parser.parse_args()

    to_run = list(DEFAULT_EXPERIMENTS)
    if args.include_contention:
        to_run.extend(CONTENTION_EXPERIMENTS)
    if args.only:
        all_experiments = DEFAULT_EXPERIMENTS + CONTENTION_EXPERIMENTS
        to_run = [e for e in all_experiments if e in args.only]
    if args.skip:
        to_run = [e for e in to_run if e not in args.skip]

    if not args.only and not args.include_contention:
        print(
            "Skipping the heavy multi-handle contention probes by default. "
            "Use --include-contention to run them."
        )

    results: list[tuple[str, str, int]] = []
    for name in to_run:
        out, err, rc = _run_experiment(name)
        results.append((out, err, rc))

    all_ok = all(rc == 0 for _, _, rc in results)
    print(f"\n{'='*60}")
    print("ALL EXPERIMENTS COMPLETE")
    print("=" * 60)
    print(f"Passed: {sum(1 for _, _, rc in results if rc == 0)}/{len(results)}")
    if not all_ok:
        for name, (_, _, rc) in zip(to_run, results):
            if rc != 0:
                print(f"  FAILED: {name} (rc={rc})")
        sys.exit(1)


if __name__ == "__main__":
    main()

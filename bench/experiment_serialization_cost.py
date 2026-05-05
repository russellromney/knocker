#!/usr/bin/env python3
"""Serialization cost on hot paths.

Directly measures the cost of JSON marshalling/unmarshalling for:
- ingest result (Rust -> Python JSON string -> dict)
- claim payload (Rust -> Python JSON string -> dict)
- Small vs large payload comparison

Invocation:
    uv run --group dev python bench/experiment_serialization_cost.py
"""
from __future__ import annotations

import argparse
import json
import time


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serialization cost experiments.")
    parser.add_argument("--samples", type=int, default=100_000)
    return parser.parse_args()


def _bench_json_parse(samples: int, payload_size: str) -> float:
    if payload_size == "small":
        raw = json.dumps({"event_id": 12345, "duplicate": 0, "status_code": 204, "delivery_id": 99999})
    elif payload_size == "large":
        raw = json.dumps({
            "event_id": 12345,
            "duplicate": 0,
            "status_code": 204,
            "delivery_id": 99999,
            "metadata": {
                "endpoint": "stripe",
                "provider_event_id": "evt_xxxxxxxxxxxxxxxxxxxx",
                "headers": {"content-type": "application/json", "stripe-signature": "t=123,v1=abc" * 20},
            },
        })
    elif payload_size == "payload":
        raw = json.dumps({"id": 1, "payload": json.dumps({"event_id": 12345}), "event_id": 12345})

    start = time.perf_counter()
    for _ in range(samples):
        result = json.loads(raw)
        _ = result["event_id"]
    return time.perf_counter() - start


def _bench_json_encode(samples: int) -> float:
    data = {"event_id": 12345, "duplicate": 0}
    start = time.perf_counter()
    for _ in range(samples):
        raw = json.dumps(data)
        _ = len(raw)
    return time.perf_counter() - start


def main() -> None:
    args = _parse_args()
    print("Serialization cost benchmark")
    print(f"samples={args.samples}")
    print()

    for size in ["small", "large", "payload"]:
        elapsed = _bench_json_parse(args.samples, size)
        print(
            f"json.parse ({size}): {elapsed:.3f}s total, "
            f"{args.samples / elapsed:,.0f}/s, "
            f"{elapsed / args.samples * 1_000_000:.2f} us/op"
        )

    elapsed = _bench_json_encode(args.samples)
    print(
        f"json.encode: {elapsed:.3f}s total, "
        f"{args.samples / elapsed:,.0f}/s, "
        f"{elapsed / args.samples * 1_000_000:.2f} us/op"
    )


if __name__ == "__main__":
    main()

# Plan: Knocker Foundation

1. Build `knocker-core` as the Rust core for Knocker-owned SQLite semantics.
2. Move ingest, dedupe, replay/requeue, and event lifecycle transitions into that shared contract.
3. Refactor the Python binding so it owns registration and handler dispatch, not durable semantics.
4. Add a Node smoke test early to pressure-test the contract boundary.
5. Write regression tests for worker correctness around missing handlers and expired Honker claims.
6. Update the baseline docs after the implementation proves the intended invariants.

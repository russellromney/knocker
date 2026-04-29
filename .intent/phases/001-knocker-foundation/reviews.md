# Reviews: Knocker Foundation

## Positive conformance review

- The Rust core now owns the Knocker semantics that should not drift across languages.
- The Python binding is materially thinner and focuses on registration, dispatch, and runtime adaptation.
- The Node smoke test exercises the shared SQLite contract instead of duplicating behavior in JavaScript.
- The worker fixes preserve the core invariant that Knocker event state and Honker job disposition do not commit independently.

## Negative conformance review

- No HTTP framework abstraction was pushed into the Rust core.
- No second queue state machine or lease mechanism was added to Knocker.
- Knocker still does not depend on any storage backend other than the host app's SQLite file.
- Provider adapters, retention, and admin surface were not quietly added under the umbrella of this change.

## Adversarial review

- A missing handler used to look like an explicit `ignored` decision. That is now treated as a real failure so accepted events are not silently dropped.
- Honker `ack`, `retry`, and `fail` used to be trusted blindly. They are now part of the transaction boundary, so claim expiry cannot leave misleading Knocker state behind.
- The remaining gaps are explicit: provider verification, secret rotation, retention, admin endpoints, docs/release gate, and performance characterization still need to be built.

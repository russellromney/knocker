# Plan

Phase:
- 014-honker-boundary-and-substrate-convergence

Session:
- A

## What we are building

- Knocker will define and then execute a cleaner architectural boundary with Honker.
- Generic queue, scheduling, locking, and recurring-work orchestration should move toward Honker wherever Honker already has, or should have, the right abstraction.
- Webhook-domain semantics should remain in Knocker core.
- Where Honker is still missing a generic capability that Knocker needs, this phase will pin that as Honker upgrade work first rather than letting Knocker grow more private substrate code.

This is a boundary-and-convergence phase, not a feature-marketing phase. The goal is to stop Knocker from becoming “half a webhook product, half a shadow Honker binding.”

## What will not change

- Knocker stays a webhook inbox/operator product, not a generic queue library.
- Knocker does not collapse its event/delivery schema into Honker’s generic queue rows.
- Knocker does not replace its replay/requeue/replay-delivery semantics with generic Honker queue operations.
- Knocker does not move provider verification/extraction logic into Honker.
- Honker does not become webhook-aware in this phase.
- This phase does not widen scope into framework adapters, more providers, or more bindings by itself.

## Key decisions before coding

- Knocker should use Honker for as much generic substrate behavior as possible.
- The current custom retention recurrence path in Knocker is transitional and should be replaced by Honker Scheduler rather than remaining a Knocker-owned recurring-job loop.
- Knocker should not rebase its primary worker semantics onto Honker outbox as-is.
  - Honker outbox is generic transactional side-effect delivery.
  - Knocker is an inbound webhook inbox with delivery/event/operator semantics that are not reducible to a generic outbox.
- The existence of a current batching gap in Honker outbox/worker APIs is not a reason for Knocker to keep reimplementing generic queue-worker machinery long-term.
  - Instead, the generic batching capability should be added to Honker and then consumed by Knocker.
- Knocker-specific retention semantics stay in Knocker core even if recurrence/orchestration moves fully onto Honker.
  - Knocker retention prunes webhook events, deliveries, attempts, and linked live jobs, and writes Knocker-specific prune audit rows.
  - That is not the same thing as generic Honker job/result/notification retention.

## The boundary we want

### Knocker should use Honker directly for

- queue claim/ack/retry/fail/cancel mechanics
- delayed enqueue and generic run-at scheduling primitives
- named locks
- scheduler registration, ticking, leader election, and wake behavior
- cross-binding thin-wrapper patterns for generic substrate APIs
- generic buffered/batched worker mechanics once Honker exposes them as first-class APIs

### Knocker should keep owning

- endpoint registration semantics
- provider verification and metadata extraction
- event/delivery append-only model
- dedupe semantics
- event lifecycle semantics and attempt-history semantics
- replay
- requeue
- replay-delivery
- prune-event and prune-orphan-delivery semantics
- prune audit rows and Knocker-specific retention reporting

### Honker should be upgraded first where needed

- a first-class buffered/batched queue worker surface
  - today Honker queue has `claim_batch(...)` and `ack_batch(...)`
  - but the default claim iterator / worker path is still single-row oriented
- a batch-aware outbox worker surface
  - enough that Knocker is not forced to keep private generic worker batching code just because Honker outbox still drives one row at a time
- scheduler usage patterns that Knocker can consume directly for recurring maintenance work
  - Knocker should register recurring retention work through Honker Scheduler rather than managing a custom maintenance queue recurrence loop

## How we will build it

### 1. Replace private architectural ambiguity with an explicit boundary

- Update Knocker phase docs and baseline truth so the Honker boundary is stated plainly.
- Call out which current Knocker-owned helpers are transitional substrate code rather than desired end-state architecture.

### 2. Move recurring retention orchestration onto Honker Scheduler

- Replace Knocker’s custom recurring retention-job reseeding/locking loop with Honker Scheduler registrations plus a retention worker path.
- Keep `knocker_run_retention_pass(...)` in Knocker core as the semantic unit of work.
- Make recurrence generic Honker behavior; keep retention-pass semantics Knocker behavior.

### 3. Eliminate private generic queue-worker code in Knocker where Honker should own it

- Identify the minimum private queue-worker shim still living in Knocker.
- Upstream the generic pieces into Honker:
  - buffered claim iteration
  - batch-friendly worker loop shape
  - any queue transition helpers that are truly generic
- Refactor Knocker to consume the Honker surface instead of keeping a long-lived private `_HonkerQueue` substrate.

### 4. Upgrade Honker where Knocker is currently blocked

- Add a batch-aware outbox/worker path in Honker.
- Keep the improvement generic and cross-binding rather than Knocker-specific.
- Do not force Knocker’s webhook semantics into Honker outbox; only close the generic worker-surface gap.

### 5. Reconfirm the Knocker-specific layer after convergence

- Make sure replay/requeue/replay-delivery still behave exactly as Knocker intends.
- Make sure retention audit behavior is unchanged except for the improved scheduling substrate underneath.

## Direct proof

### Boundary proof

- proof that recurring retention orchestration is now driven by Honker Scheduler rather than Knocker-owned reseeding logic
- proof that Knocker no longer depends on a long-lived private generic queue-worker implementation where the same behavior now exists in Honker

### Honker-upgrade proof

- proof in Honker that the new buffered/batched worker surface is generic and cross-binding rather than a Knocker special case
- proof in Honker that the upgraded outbox/worker surface can claim more than one row at a time when configured to do so

### Knocker blast-radius proof

- replay still works
- requeue still works
- replay-delivery still works
- retention pass still writes the same Knocker prune audit rows
- worker stop/drain semantics still hold
- throughput does not regress on the corrected benchmark shape

## Blast-radius proof

- `make test`
- `npm --prefix site run build`
- Knocker retention automation tests
- Knocker replay/requeue/replay-delivery tests
- Honker scheduler tests
- Honker queue/outbox worker tests covering the new generic batching path

## Surrogate proof that can help but does not close the claim

- docs-only architectural diagrams
- Knocker tests passing while still relying on private substrate code
- Honker microbench wins without a binding-level proof that the upgraded worker APIs are actually consumable

## Missing proof risks to watch for

- It is easy to “use Honker more” cosmetically while still keeping the real batching/orchestration logic private inside Knocker.
- It is easy to overcorrect and try to force webhook-specific semantics into Honker’s generic outbox/task model.
- It is easy to move recurrence to Honker Scheduler while accidentally changing Knocker retention timing, no-op audit behavior, or failure handling.
- It is easy to add a generic Honker batch-worker API that helps Rust only but leaves other bindings behind.

## Traps

- Do not use “Honker should own more” as an excuse to blur the webhook/product boundary.
- Do not treat Knocker retention as if it were just generic queue retention.
- Do not leave Knocker on a custom recurring maintenance queue once Honker Scheduler is available and proven.
- Do not keep private Knocker queue-worker substrate code just because Honker needs a straightforward upgrade.
- Do not ship a Knocker-first workaround when the real fix belongs in Honker.

## Likely outcome

- Honker becomes the clear owner of generic queue/lock/scheduler/worker substrate behavior.
- Knocker becomes thinner at the substrate layer and clearer at the product layer.
- Future bindings get easier because they rely on more shared Honker behavior and less binding-local or Knocker-local orchestration glue.
- The project pair reads more coherently:
  - Honker is the generic SQLite substrate
  - Knocker is the webhook inbox/operator product built over that substrate

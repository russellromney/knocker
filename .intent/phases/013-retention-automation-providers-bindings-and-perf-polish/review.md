# Review

Phase:
- 013-retention-automation-providers-bindings-and-perf-polish

Session:
- A

## Plan Review 1

### Finding 1

Retention-automation ownership is still undefined for multi-process or
multi-handle deployments.

The plan says retention automation is a small in-process scheduled
loop/task, but it never decides what happens when multiple app
processes or multiple independently configured Knocker instances point
at the same SQLite file with automation enabled. That is load-bearing:
without an explicit rule, implementation could silently create
duplicate prune runs, unexpected aggregate deletion under shared
limits, and noisy audit rows that look like separate operator intent.
The plan should decide whether automated retention is:

- single-runner-by-convention and documented that way, or
- explicitly safe/acceptable to run from multiple processes with the
  resulting duplicate/noisy audit behavior treated as intended.

Right now that ownership model is still soft.

### Finding 2

The minimal binding contract is still too under-specified for a phase
that wants five new bindings at once.

The plan says each binding needs open/bootstrap, endpoint registration,
ingest or receive, a basic worker loop, and basic reads/actions with
`get_event` / `list_events` / replay or requeue “at minimum.” That is
not pinned tightly enough. If each binding gets to choose a different
minimum action surface, the phase can “complete” with five subtly
different products and no clear cross-binding baseline. The plan should
name one exact minimum contract that every new binding must support in
this phase, even if small. For example:

- open/bootstrap
- endpoint registration
- ingest
- worker loop
- `get_event`
- `list_events`
- `replay`

or whatever set you actually want. Right now the “or replay or requeue
at minimum” wording still defers a product decision to implementation
time.

### Verdict

The direction is good, but these two choices should be pinned before
implementation starts. The first is about avoiding surprising automated
deletion behavior; the second is about avoiding five different
"minimal" bindings by accident.

## Response 1

Both findings were pinned back into `plan.md` before implementation:

- retention automation runtime ownership is now multi-runner-safe on a shared SQLite file, while configuration ownership stays singular
- the exact minimum binding contract is now fixed to:
  - open/bootstrap
  - endpoint registration
  - ingest
  - basic worker loop
  - `get_event`
  - `list_events`
  - `replay`

## Implementation Note 1

The first implementation slice landed on retention automation:

- Python now has a small `RetentionPolicy` + `run_retention(...)` surface
- recurrence is Honker-backed via Honker Scheduler rather than a
  binding-local sleep loop
- automated runs call the shared core retention-pass primitive and therefore
  write the same durable prune-audit rows as manual calls
- multiple runners on one SQLite file are now safe; docs treat configuration
  ownership, not runtime duplication, as the remaining operator concern
- direct proof now covers:
  - scheduled event/orphan pruning
  - no-op automated prune audit rows
  - stop semantics preventing future automated iterations

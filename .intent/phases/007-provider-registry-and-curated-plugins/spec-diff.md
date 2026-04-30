# Spec Diff: Provider Registry And Curated Plugins

Phase:
- 007-provider-registry-and-curated-plugins

Session:
- A

## What changes

- Knocker adds a public provider plugin shape for binding-owned verification and metadata extraction.
- Providers are plugins at the source/API/contribution level, not native runtime extensions:
  - built-in providers are bundled into the language package
  - app-local providers are ordinary host-language objects/callables
  - community providers may be ordinary host-language packages that expose a provider object
  - accepted upstream providers live under a repo-level `providers/<name>/` catalog and are bundled into language packages during release/build work
- The Python binding gains a provider registry:
  - built-in providers are registered by default
  - `Knocker.register_provider(provider)` registers an app-local or community provider by name
  - `Knocker.provider_versions()` reports the registered provider names and semantic versions
  - `add_endpoint(provider="name", secrets=[...])` resolves provider names through the registry
  - `add_endpoint(..., provider_options={...})` passes provider-specific options such as Stripe timestamp tolerance
- Provider implementations return both verification outcome and extracted metadata:
  - whether the signature is valid
  - why verification failed, when it failed
  - provider delivery id
  - provider event id
  - event type
- Stripe is migrated onto the provider registry with no intentional behavior change.
- GitHub becomes a curated built-in provider, not just a metadata extractor:
  - verifies `X-Hub-Signature-256`
  - extracts delivery id from `X-GitHub-Delivery`
  - extracts event type from `X-GitHub-Event`
  - uses the delivery id as the default dedupe key when no event id is available
- Generic HMAC verification remains available as a low-level helper/config path, but it is not the provider ecosystem model by itself. Generic HMAC has too many per-provider knobs to be a curated provider without diluting the curated-provider support promise.
- Provider docs distinguish three support tiers:
  - built-in curated provider: shipped, tested, documented, and supported by Knocker
  - app-local provider: owned by the host application
  - community provider: installed/registered by the application and maintained outside Knocker unless accepted upstream

## What does not change

- `knocker-core` stays provider-neutral and continues to own durable SQLite/event semantics only.
- `knocker-extension` does not become a verifier/plugin loading surface.
- Knocker does not add native dynamic provider loading, WASM provider loading, or a provider package manager.
- Knocker does not add one crate/package per provider in this phase.
- Knocker does not promise provider parity across Node/Ruby/future bindings in this phase.
- Existing public Python ingress APIs remain:
  - `receive(...)` is the verified ingress path
  - `ingest(...)` is the trusted low-level path
  - explicit `receive(...)` metadata arguments still override provider-extracted metadata
- Existing `provider="stripe"` endpoint configuration continues to work.
- Existing explicit `verification={...}` configuration continues to work for compatibility.

## Invariants

- Built-in provider names are support promises. If Knocker accepts `provider="stripe"` or `provider="github"`, the bundled implementation must be tested with valid, invalid, missing-header, and rotation/timestamp cases where applicable.
- Provider registration is explicit and local to a `Knocker` instance. There is no process-wide provider mutation and no automatic import/discovery in this phase.
- Built-in providers cannot be overridden with `register_provider(...)`. Adding an explicit override knob is deferred.
- Adding a new built-in provider name is a compatibility-affecting release because it can collide with an app-local/community provider of the same name. New built-in provider names must be called out in the changelog.
- `add_endpoint(provider="name", ...)` fails immediately if `name` is not already registered on that `Knocker` instance.
- `add_endpoint(provider="name", secrets=None)` is rejected for providers that require secrets. A named provider with required verification secrets should fail loudly at config time rather than silently accepting or rejecting all deliveries.
- Provider names are stable lowercase identifiers. Duplicate provider registration fails rather than silently replacing an existing provider.
- Provider-specific options are schema-checked by that provider. Unknown option keys fail with `ValueError`; typo'd options must not silently no-op.
- Provider versions are implementation semantic-version strings surfaced for inspection and release/debugging. They are not upstream provider API versions and do not create endpoint-level provider-version pinning in this phase.
- Invalid provider verification still stores a `Delivery` row and does not create or enqueue an `Event`.
- If provider verification raises an unexpected exception, Knocker treats it as verification failure, stores an orphan `Delivery`, and records a useful `signature_error` rather than crashing the caller.
- Provider-extracted metadata must not bypass the existing durable ingest contract. It is input to `ingest(...)`, not a second storage path.
- Provider-extracted metadata is still applied on verification failure when the provider was able to extract it; invalid/orphan deliveries should be useful for debugging.
- App-local and community providers use the same public provider interface as built-ins.
- GitHub's dedupe identity is `X-GitHub-Delivery`, which is expected to be stable across GitHub dashboard redelivery of the same webhook delivery.

## How we will verify it

- Existing Stripe verified-ingress tests pass through the new provider registry path.
- GitHub verified-ingress tests cover:
  - valid signature creates a linked event/delivery
  - invalid signature creates an orphan delivery
  - invalid signature still preserves extracted delivery id and event type on the orphan delivery
  - missing signature is rejected with a useful error
  - missing delivery id is rejected or produces a clear invalid delivery error, not accidental duplicate behavior
  - event type extraction from `X-GitHub-Event`
- App-local provider tests cover:
  - registering a provider by name
  - duplicate registration rejection
  - successful verification and metadata extraction
  - failed verification storing an orphan delivery
  - `provider_versions()` output
- Compatibility tests cover:
  - existing `verification={"kind": "hmac-sha256", ...}` usage
  - existing `verification={"kind": "stripe", ...}` usage
  - explicit `receive(...)` metadata overriding provider-extracted metadata
- Docs explain built-in vs app-local vs community providers and include one custom provider example.

## Notes

- This phase is about making the verifier/provider seam safe and pleasant, not about maximizing provider count.
- The "random Nebraska package props up every Stripe user" failure mode is avoided by keeping important providers curated and bundled.
- Runtime plugin loading can be reconsidered later only if ordinary host-language provider registration proves insufficient.
- The repo-level `providers/<name>/` catalog mentioned above is forward-looking; this phase defines the provider API and Python built-ins, not the catalog/generator system.

# Spec Diff: Provider Conformance And Internal Cleanup

Phase:
- 008-provider-conformance-and-internal-cleanup

Session:
- A

## What changes

- Knocker adds a source-level provider conformance story for curated providers:
  - the repo gains a minimal `providers/<name>/` catalog for curated provider metadata, fixtures, and contribution notes
  - curated-provider behavior is pinned by shared request/expected-outcome fixtures rather than only ad hoc binding tests
  - the first entries are the already-shipped curated providers: `stripe` and `github`
  - fixture files are JSON, one fixture per file under `providers/<name>/fixtures/`
- Knocker documents three provider contribution paths more explicitly:
  - app-local provider: implemented directly in the host app
  - community provider: packaged in the host language and passed into endpoint configuration as a provider object/value using that language's normal semantics
  - curated provider: accepted into Knocker's repo-level provider catalog and bundled into the language package
- The Python provider surface becomes easier to maintain internally:
  - public provider API remains stable
  - built-in provider implementations move behind a cleaner internal module split
  - provider fixture loading/test helpers become an explicit maintained path
- Python queue and payload internals are cleaned up:
  - Honker job payload parsing helpers move out of `coercion.py` into a better-named payload-focused module
  - raw Honker queue mechanics stop looking like public Knocker API surface
  - Knocker exposes a small explicit queue-inspection value where needed, rather than a raw queue object

## What does not change

- `knocker-core` still does not own provider verification logic.
- `knocker-extension` still does not become a provider/plugin loading surface.
- Knocker still does not add native dynamic provider loading, WASM provider loading, or automatic provider discovery.
- Knocker still does not promise that every curated provider exists in every language binding.
- The current curated built-ins remain `stripe` and `github`; this phase is about conformance and authoring shape, not maximizing provider count.
- String provider names remain reserved for curated built-ins. This phase changes the custom-provider path so app-local/community providers no longer compete in the same string namespace as future bundled providers.
- Existing public ingress/operator semantics do not change:
  - `receive(...)`, `ingest(...)`
  - `ignore(...)`, `replay(...)`, `requeue(...)`, `replay_delivery(...)`
  - append-only `Delivery` behavior
  - duplicate ingest does not mutate existing event state

## Invariants

- Curated provider names are Knocker support promises. If a provider is curated and bundled, its expected verification and extraction behavior must live in repo-owned fixtures and docs, not only in code.
- Curated provider fixtures are binding-neutral source material. They describe requests and expected outcomes, not Python-specific object behavior.
- Curated provider catalog entries use a small explicit `metadata.json` schema:
  - `name`
  - `version`
  - `support_tier`
  - optional `description`
  - optional `upstream_docs_url`
- Curated provider fixture JSON uses a small explicit schema:
  - `request.method`
  - `request.headers`
  - `request.query`
  - `request.body`
  - `request.secrets`
  - optional `request.provider_options`
  - `expected.signature_valid`
  - optional `expected.signature_error_contains`
  - optional `expected.provider_delivery_id`
  - optional `expected.provider_event_id`
  - optional `expected.event_type`
- Fixture `request.secrets` is a list of UTF-8 strings. The conformance loader encodes those strings into the byte-oriented secrets tuple used by the runtime provider interface.
- Fixture `request.provider_options` uses the same dict shape and provider-specific validation rules as the runtime `provider_options=...` surface.
- Curated provider fixtures are repo-owned conformance material only. They are loaded by repo tests and docs/contribution workflows; they are not part of the published wheel/package surface in this phase.
- The repo-level `providers/<name>/` catalog is a source/conformance catalog, not a runtime plugin loading system.
- Curated built-ins are addressed by stable string names such as `provider="stripe"`.
- App-local and community providers are supplied to endpoint configuration as provider objects/values, not by claiming a global string name.
- Because custom providers no longer claim global string names, a future Knocker release adding a new curated built-in should not break an app-local provider that was passed directly as an object/value.
- The public Python provider API is:
  - `Provider`
  - `ProviderRequest`
  - `ProviderResult`
  - `Knocker.provider_versions()`
- `Knocker.register_provider(...)` is removed (Implementation Review 1 finding N2). String provider names are reserved for curated built-ins; app-local and community providers pass a `Provider` instance directly to `add_endpoint(...)`. There is no global string-lookup registry for non-curated providers.
- Internal module cleanup must not change the above public names or their supported behavior.
- Raw Honker queue objects are not part of Knocker's intended long-term public API. Queue mechanics are an implementation detail behind Knocker's worker surface.
- If queue-level inspection is needed, it should be exposed through a small explicit Knocker-owned surface, not by encouraging host apps to call Honker queue helpers directly.
- Job payload parsing/validation helpers should live with payload semantics, not generic argument coercion.
- Fixture changes require deliberate review with rationale. Updating fixtures to match code drift is a contract change, not routine churn.
- Passing curated-provider fixtures does not mean the implementation has no other bugs. Binding-specific edge-case tests remain required alongside the fixture suite.
- This phase does not define a curated-provider deprecation/sunset lifecycle. If a curated provider ever needs to be removed or deprecated, that policy will be handled in a later phase and called out explicitly in release notes.
- Stripe conformance uses explicit clock injection for timestamp/tolerance checks. Curated Stripe fixtures must remain valid independent of wall-clock time by supplying the verification clock explicitly in the conformance test path.

## How we will verify it

- Docs include:
  - a provider author guide for app-local/community providers using provider objects/values directly
  - a curated-provider contribution guide explaining the repo catalog and fixture expectations
  - clear language that curated providers are bundled/support promises while community providers are explicit opt-ins passed directly into endpoint configuration
- Tests load curated-provider fixtures from the repo-level catalog and assert current Python built-ins satisfy them.
- Fixture coverage for `stripe` and `github` includes at least:
  - valid verification
  - invalid signature
  - required-header missing case
  - metadata extraction outcome
  - any provider-specific replay/timestamp case already promised by Knocker docs
- Internal cleanup verification includes:
  - payload helper tests still pass after the module split
  - no public docs or examples encourage direct `app.queue` usage
  - any remaining queue-name inspection in tests/docs uses the explicit Knocker-owned surface

## Notes

- This phase is intentionally about trust and maintainability around the provider seam we just added.
- The goal is to avoid both extremes:
  - "random docs snippet verification" that causes users to drop webhooks
  - "runtime plugin operating system" architecture that is too heavy for Knocker's current stage
- A future cross-language provider generator/catalog can build on this source-level fixture catalog later, but that generator is not part of this phase.

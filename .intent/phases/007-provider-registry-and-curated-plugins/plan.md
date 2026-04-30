# Plan

Phase:
- 007-provider-registry-and-curated-plugins

Session:
- A

## Goal

- Make provider verification/extensibility boring: built-in curated providers stay easy and supported, while private/community providers can register through the same small public interface without native plugin loading, WASM, or provider code in `knocker-core`.

## Phase decisions

- Provider code remains in the language binding/package layer for this phase.
- Provider plugins are host-language objects, not dynamic native libraries.
- Built-in providers are bundled and registered automatically by the Python package.
- `knocker-core` and `knocker-extension` remain provider-neutral.
- Python is the only full implementation target for this phase; Node remains a smoke/shared-contract binding.
- Provider result combines verification and extraction so provider code parses headers/body once and can return useful metadata even for invalid deliveries.
- Public provider API names for Python:
  - `ProviderRequest`
  - `ProviderResult`
  - `Provider`
  - `Knocker.register_provider(...)`
  - `Knocker.provider_versions()`
- `ProviderRequest` is immutable enough for provider authors and exposes helper methods:
  - `header(name, default=None)` with case-insensitive lookup
  - `json()` with cached body parsing; raises `ValueError` on non-JSON bodies
  - direct `method`, `headers`, `query`, and `body` attributes
- `ProviderResult` has helper constructors:
  - `ProviderResult.accept(...)`
  - `ProviderResult.reject(error, ...)`
- Provider objects expose:
  - `name`
  - `version`
  - `verify(request, *, secrets, options) -> ProviderResult`
- `add_endpoint(...)` gains `provider_options=None` for provider-specific knobs such as Stripe tolerance.
- `provider_options` defaults to `{}` and must be a dict when provided.
- Providers reject unknown option keys with `ValueError`.
- Providers that require secrets reject missing/`None`/empty secrets at endpoint registration time.
- Explicit metadata passed to `receive(...)` wins over provider-extracted metadata.
- `provider_versions()` returns a plain `dict[str, str]` copy mapping provider name to implementation semantic version.
- Built-in curated providers in this phase:
  - `stripe`
  - `github`
- Generic HMAC stays supported through existing explicit `verification={...}` configuration and may internally reuse provider helpers, but it is not presented as a named curated provider.
- Module split:
  - `providers.py` is the public provider API plus built-in provider registry helpers
  - `verifiers.py` remains an internal compatibility/helper module only if needed for legacy `verification={...}` adapters

## Build order

1. Add public provider models/protocols:
   - create `packages/knocker/python/knocker/providers.py`
   - define `ProviderRequest`, `ProviderResult`, and `Provider`
   - document `ProviderRequest.json()` raising `ValueError` for non-JSON bodies
   - export them from `knocker.__init__`
   - keep file sizes under the project line-count standard
2. Convert the current private request/result types:
   - replace private `_IngressRequest` / `_VerificationResult` usage where appropriate
   - preserve the existing `receive(...)` and `ingest(...)` public behavior
3. Add a provider registry to `Knocker`:
   - initialize built-ins during `Knocker.__init__`
   - implement `register_provider(provider)`
   - validate provider name/version shape
   - reject duplicate names
   - reject attempts to override built-ins
   - make `add_endpoint(provider="unknown")` fail at endpoint-registration time
   - implement `provider_versions()`
4. Move Stripe onto the registry:
   - keep existing header parsing, timestamp tolerance, secret rotation, and error strings unless tests require clearer wording
   - route `provider="stripe", secrets=[...]` through the registered provider
   - support `provider_options={"tolerance_s": 300}`
   - reject unknown Stripe provider options
   - reject missing/empty secrets for Stripe provider endpoints
   - keep legacy `verification={"kind": "stripe", ...}` compatibility by adapting it to the same implementation
5. Add GitHub as a curated provider:
   - verify `X-Hub-Signature-256` using `sha256=<hex hmac>`
   - extract `X-GitHub-Delivery`
   - extract `X-GitHub-Event`
   - use delivery id as event/dedupe identity when no provider event id exists
   - preserve extracted delivery id and event type even on invalid signatures
   - reject unknown GitHub provider options
   - reject missing/empty secrets for GitHub provider endpoints
   - return clear errors for missing signature, missing delivery id, and mismatch
6. Preserve compatibility paths:
   - `verification={"kind": "hmac-sha256", ...}` still works
   - endpoint `delivery_key` / `event_key` callables still override or supplement provider defaults as they do today
   - explicit `receive(...)` metadata overrides provider output
7. Add tests:
   - provider registry success/failure/version cases
   - built-in override rejection
   - unknown provider rejection during `add_endpoint(...)`
   - provider exception becomes invalid/orphan delivery rather than a crash
   - `provider_options` unknown-key rejection
   - provider missing/empty secrets rejection
   - `ProviderRequest.json()` raises on non-JSON body
   - app-local provider accept/reject cases
   - Stripe regression cases through the registry
   - GitHub valid/invalid/missing-header/missing-delivery cases
   - GitHub invalid signature preserves extracted delivery metadata on the orphan delivery
   - legacy verification dict compatibility
   - explicit receive metadata override
8. Update docs:
   - verified-ingress guide explains built-in curated, app-local, and community provider tiers
   - Python reference lists the provider API
   - getting-started remains simple and does not expose registry machinery before users need it
   - roadmap notes that runtime provider loading and cross-language provider bundling are intentionally deferred
9. Run verification and record evidence when implemented.

## Verification

- `make test`
- `npm --prefix site run build`
- Public docstring smoke includes new provider classes/methods.
- Test inventory includes the provider-registry and GitHub verifier cases by name.

## Traps

- Do not add provider logic to `knocker-core`.
- Do not add provider verification UDFs to `knocker-extension`.
- Do not add WASM, native dynamic loading, entry-point auto-discovery, or plugin package loading in this phase.
- Do not silently replace providers with duplicate names.
- Do not turn provider support into a large provider catalog; this phase proves the interface with Stripe and GitHub only.
- Do not break existing `receive(...)`, `ingest(...)`, or `verification={...}` call sites.
- Do not make generic HMAC look as safe as provider-specific timestamped verification where it is not.
- Do not add cross-binding provider parity promises before another binding actually implements the provider API.

## Areas not to touch

- `knocker-core` schema, dedupe, lifecycle, replay/requeue, and pruning semantics.
- Honker queue mechanics.
- Node API beyond any smoke-test adjustment required by shared build changes.
- Release workflow and package naming.

## Assumptions and risks

- App-local provider registration is enough for private providers in the near term.
- Community providers can be ordinary language packages that expose a provider object; Knocker does not need to load them automatically yet.
- Provider version reporting is useful for debugging and release notes, but endpoint-level version pinning would add complexity that this phase does not need.
- GitHub does not expose a separate event id for every webhook kind; using delivery id as the default dedupe identity is acceptable for GitHub's delivery/redelivery model and must be documented.

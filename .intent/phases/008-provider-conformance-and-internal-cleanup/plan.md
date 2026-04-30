# Plan

Phase:
- 008-provider-conformance-and-internal-cleanup

Session:
- A

## Goal

- Make Knocker's new provider seam trustworthy to extend and easier to maintain: curated providers get repo-owned conformance fixtures and contribution docs, while Python internals get the cleanup needed to keep the provider/queue boundary boring.

## Phase decisions

- This phase does not add runtime provider loading.
- This phase does not add a new curated provider unless implementation reveals a gap that cannot be proven with `stripe` and `github`.
- The repo-level `providers/<name>/` catalog starts now as source/conformance material only:
  - provider metadata
  - fixture files
  - contribution notes
  - no runtime loading hooks
- Curated provider fixtures are JSON, one fixture per file under `providers/<name>/fixtures/`.
- Curated provider fixture loaders are test-only helpers in this phase, not public package surface.
- Public Python provider API stays unchanged.
- `add_endpoint(provider=...)` evolves to accept either:
  - a curated built-in string name such as `"stripe"` or `"github"`
  - a `Provider` object/value for app-local or community providers
- Internal Python module organization may change freely as long as the current public imports remain stable.
- `app.queue` is treated as internal leakage, not a public feature:
  - Knocker should expose `queue_name` as a read-only string property for tests/docs that need queue identity
  - raw queue helpers stay internal
- Payload helpers move out of `coercion.py` into `job_payload.py`.
- A repo/provider author should have two documented paths:
  - "I only need this in my app"
  - "I want this to become curated/bundled"
- Custom/community providers should not need to reserve a global string name. The preferred path is to pass the provider object/value directly to `add_endpoint(...)`.
- Built-in provider module split target:
  - keep `providers.py` as the public facade
  - move built-in implementations to sibling internal modules such as `_builtin_stripe.py` and `_builtin_github.py`
- Shared built-in helper functions should live in a clearly-labeled internal helper home rather than being duplicated across sibling modules. Prefer `_provider_internals.py` if the split needs a shared helper module.
- `coercion.py` remains the home for generic argument coercion after payload helpers move; this phase does not widen into a full coercion-module redesign.
- Queue leakage fix is at the `Knocker` attribute boundary, not a `queue.py` rename exercise.

## Build order

1. Add source-level provider catalog scaffolding:
   - create `providers/stripe/` and `providers/github/`
   - add `metadata.json` files using the pinned schema (`name`, `version`, `support_tier`, optional `description`, optional `upstream_docs_url`)
   - add JSON fixture files for valid/invalid/missing-header/extraction cases
   - keep the shape boring and human-readable; no generator machinery yet
2. Add provider conformance test helpers:
   - add tests-only fixture loader utilities
   - encode fixture `request.secrets` from UTF-8 strings into the runtime byte-oriented secrets tuple
   - validate that fixture `request.provider_options` matches the runtime provider-options shape
   - shared assertions that map fixture input to current Python provider behavior
   - put curated-provider conformance coverage in a new `tests/test_provider_conformance.py`
   - verify current curated providers from fixtures rather than only handwritten tests
   - implement explicit clock injection for Stripe tolerance fixtures
3. Write provider author/contribution docs:
   - extend the existing verified-ingress guide with the app-local/community story where useful
   - add a separate curated-provider contribution page for the repo catalog + fixture workflow
   - app-local provider recipe using a direct provider object/value
   - community provider packaging guidance showing the package exposing `provider()` (or equivalent) and the host app passing that object/value into `add_endpoint(...)`
   - curated-provider contribution path using the repo catalog + fixtures
   - explicitly distinguish support tiers and support promises
4. Clean up provider module organization:
   - keep `providers.py` as the public entry surface
   - move built-in provider implementations behind `_builtin_stripe.py` / `_builtin_github.py`
   - keep shared built-in helper functions in a clearly-labeled internal helper home such as `_provider_internals.py`
   - keep public docstrings/import paths stable
   - add an import-path stability regression test for `knocker.Provider`, `knocker.ProviderRequest`, `knocker.ProviderResult`, and related public symbols
   - add regression coverage that built-in strings still work while custom/community providers are accepted as direct provider objects/values
5. Clean up payload helpers:
   - move Honker payload parsing/validation helpers out of `coercion.py`
   - move them specifically into `job_payload.py`
   - update tests/imports accordingly
6. Clean up queue leakage:
   - stop treating the raw Honker queue object as public Knocker API
   - replace `app.queue.name` usage in tests/docs with `app.queue_name`
   - replace direct queue-mechanic test usage such as `app.queue.claim_batch(...)` with Knocker-owned test paths or direct DB setup where needed; do not preserve raw queue access as a semi-public escape hatch
   - move raw queue classes/helpers behind a more obviously internal module only if needed to support the attribute-boundary cleanup
7. Update docs and roadmap language:
   - explain the curated-provider fixture catalog
   - explain that runtime/native/WASM plugin loading remains deferred
   - explain that string provider names are for curated built-ins only
   - explain that app-local/community providers are passed directly as provider objects/values, which avoids future collisions with newly bundled built-ins
   - clarify the queue surface decision so future contributors do not re-expose it casually
8. Fixture regeneration tooling:
   - defer a dedicated `tools/regenerate_fixtures.py` script in this phase
   - instead, keep curated fixtures small and hand-reviewed; if regeneration pain appears during implementation, spin it into a later focused phase rather than widening this one
9. Run verification and record evidence.

## Verification

- `make test`
- `npm --prefix site run build`
- Curated provider fixture/conformance tests are listed by name in the phase evidence.
- Public docstring smoke still passes after any provider module split.

## Traps

- Do not add runtime plugin loading, native plugin ABI work, WASM, or auto-discovery.
- Do not preserve string-based custom/community provider lookup as the preferred model; that leaves the upgrade-collision problem in place.
- Do not silently change current Stripe or GitHub verification behavior without updating fixtures/docs intentionally.
- Do not let the new provider catalog turn into a generator framework in this phase.
- Do not leave `app.queue` half-public: either it is internal with a tiny replacement surface, or the phase has failed to make the boundary clearer.
- Do not mix generic coercion helpers and queue-payload semantics in a way that recreates today's module confusion under a new filename.
- Do not widen this into new durable semantics work in `knocker-core`.

## Areas not to touch

- Rust ingest/lifecycle/pruning semantics unless a purely internal helper rename is absolutely necessary.
- Node beyond any smoke/build adjustment required by file moves.
- Release workflow, PyPI, or Windows wheel work.
- New provider count beyond what is needed to prove the conformance shape.

## Assumptions and risks

- A small source-level provider catalog is enough to make future curated/community work less ad hoc without committing to a cross-language generator yet.
- Hiding raw queue mechanics may require touching several tests; that is cleanup, not product regression.
- The best time to separate payload helpers from coercion is now, before more provider/worker glue accumulates around the wrong module boundary.

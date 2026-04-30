# Reviews And Decisions

This file is append-only.

## Spec-Diff + Plan Review 1

Session B (Claude Opus 4.7, 1M context), reviewing `spec-diff.md` and `plan.md` together since they were authored as a pair.

### Positive conformance review

- **P1 — This phase consumes named items from the post-`0.1.0` backlog.** ROADMAP.md's "Code organization" list explicitly calls out "Move Honker payload-shape helpers out of `coercion.py`" and "Decide whether `app.queue` is intentionally public." Phase 008 picks both up. The phase is not inventing new ambition; it is closing existing follow-up. That keeps the scope honest.
- **P2 — The `providers/<name>/` catalog is correctly framed as source/conformance, not runtime.** Spec invariant "The repo-level providers/<name>/ catalog is a source/conformance catalog, not a runtime plugin loading system" pins exactly the line that 007 deferred under A7. The "Notes" section also reiterates that runtime/native/WASM remain deferred. Two redundant guardrails against scope creep here is the right amount given how easy it is to drift toward a plugin OS.
- **P3 — Binding-neutral fixture format is the right shape.** "Curated provider fixtures are binding-neutral source material. They describe requests and expected outcomes, not Python-specific object behavior." That is the only choice that lets a future Node/Ruby binding ever assert "we implement Stripe the same way Knocker says Stripe should work." If fixtures were Python-shaped they would freeze the catalog into one binding's idioms.
- **P4 — Three-tier doc taxonomy refines, not contradicts, 007.** Phase 007 introduced built-in / app-local / community. Phase 008 splits "built-in" into "curated provider" with an explicit catalog/contribution path. That is a refinement, not a re-architecture; existing 007 docs do not need to be rewritten, only extended.
- **P5 — Public Python provider API is locked.** Spec invariant lists `Provider`, `ProviderRequest`, `ProviderResult`, `register_provider`, `provider_versions`. The plan pins "Internal Python module organization may change freely as long as the current public imports remain stable." That is the right risk posture for this kind of cleanup phase: high willingness to move private code, zero tolerance for moving public symbols.
- **P6 — `app.queue` leakage is treated as the design bug it is.** Plan trap "Do not leave `app.queue` half-public: either it is internal with a tiny replacement surface, or the phase has failed to make the boundary clearer" forces a binary outcome. That is much better than vague "tighten this up later."
- **P7 — Build-order step 5 (payload helpers) and step 6 (queue leakage) are sequenced correctly.** Payload helpers move first into their own module, then queue cleanup happens. That ordering means the queue rename does not have to drag along payload helpers, which would re-couple them to the thing being made internal. If these were swapped, the executor might land payload helpers in queue.py (because that is "where queue stuff lives now") and then have to undo it.

### Negative conformance review

- **N1 — Fixture file format is unspecified.** Plan step 1 says "small provider metadata files" and "fixture files for valid/invalid/missing-header/extraction cases." That could be JSON, YAML, TOML, Python dicts, fixture-per-file, or fixture-bundle-per-provider. Pin format before code lands. Recommendation: JSON with one fixture-per-file under `providers/<name>/fixtures/`, because (a) every binding can read it, (b) version control diffs cleanly, (c) no external library needed for parsing in CI. JSON is the convention `knocker_ingest` already uses for serialized core results.
- **N2 — Fixture schema is unpinned.** "describes requests and expected outcomes" is the goal; the fields are not enumerated. Curated-provider authors will need to know: request method, headers (including signature header), query, body, secret(s), provider_options, and on the expected side: valid/invalid, signature_error substring, provider_delivery_id, provider_event_id, event_type. Pin a minimal schema in spec invariants so a future contributor does not invent a different shape per provider.
- **N3 — Stripe timestamp tolerance becomes time-coupled in fixtures.** Stripe verification compares `header_t` against `int(time.time())` with a `tolerance_s` window. A fixture that bakes in `t=1714000000` as a "valid signature" will be invalid in three years because the test machine's clock will be outside tolerance. Spec/plan must commit to one of: (a) freeze "now" by injecting a clock at verify time, (b) express timestamps as offsets from test execution and recompute the signature at fixture-load, or (c) only assert verification outcomes that are clock-independent (signature math correct, but "expired-because-old-timestamp" remains a code-only test). Without this decision, the conformance suite rots silently.
- **N4 — `app.queue` replacement surface shape is unstated.** Plan says "Knocker should expose `queue_name` (or equivalent tiny inspection data) if tests/docs genuinely need it" — that is two different shapes (a string property vs. a typed inspection dataclass). Convention from 003/004/006 is typed dataclasses for stable surfaces. But for "what queue name does this Knocker use," a string property is clearly enough and matches the "tiny" framing. Pick one before the executor flips a coin. My lean: `app.queue_name -> str` as a read-only property, not a dataclass; if richer inspection is needed later, add a dataclass-returning method then.
- **N5 — Payload module name is left as a coin flip.** Plan says "`payload.py`, `job_payload.py`, or equivalent." Knocker has at least three distinct payload-shaped concepts: HTTP request body, canonical Event payload, and Honker job payload. A bare `payload.py` would be ambiguous; `job_payload.py` is clearer. Pin one. Recommend `job_payload.py`.
- **N6 — Fixture loader location is unspecified.** Plan step 2 "fixture loader utilities" — does this live in `tests/conftest.py`? `tests/_provider_fixtures.py`? Or in the shipped Python package as `knocker._conformance` so community providers can use the same loader to assert their own fixtures? Pin before code lands. If the answer is "tests-only for now," say so explicitly so a community provider author does not assume the loader is supported public surface.
- **N7 — Fixture distribution in the wheel is unstated.** Curated providers are bundled into `knockerlite`. The fixture catalog lives at `providers/<name>/`, which is repo-relative. Three plausible answers: (a) fixtures stay repo-only and the wheel does not contain them, (b) fixtures are copied into the package at build time so wheel users can run conformance against the shipped impl, (c) fixtures live in the repo and are referenced from tests via the repo path. (a) is the simplest answer and is consistent with "this phase is about repo-internal trust"; (b) is invasive and overcommits; (c) breaks for downstream tests. Spec should pick.
- **N8 — Module split target for built-in providers is unpinned.** Spec says "built-in provider implementations move behind a cleaner internal module split." Today `providers.py` (~380 lines) holds public API + `_StripeProvider` + `_GitHubProvider` + helpers. Plausible splits: keep one file (status quo), split into `providers/__init__.py` + `providers/_stripe.py` + `providers/_github.py` (sub-package), or `providers.py` + `_builtin_stripe.py` + `_builtin_github.py` (siblings). Pin before plan lands. The sub-package option is the obvious place to grow if more curated providers ever land; the sibling-modules option keeps `import knocker.providers` semantically unchanged. My lean: sibling-modules to avoid an `__init__.py` re-export dance.

### Adversarial review

- **A1 — Fixture/implementation drift.** Curated conformance tests assert that "this fixture's expected outcome is what the implementation produces." A maintainer who fixes a bug in StripeProvider that changes the wording of `signature_error` (say "stripe signature mismatch" → "stripe signature digest mismatch") breaks all the fixtures that asserted the old wording. That is the right behavior — the fixture is a contract — but the plan should commit to "fixture changes require deliberate review, with rationale in the PR description." Otherwise fixtures will get rubber-stamp-updated to match drift, which silently weakens the contract. One line in spec invariants suffices.
- **A2 — Fixture re-derivability.** A subtler version of A1: if Knocker ships `providers/stripe/fixtures/valid_checkout.json` with header `Stripe-Signature: t=1714000000,v1=ABC...` claiming it is a valid signature for body `{"id":"evt_1",...}` with secret `whsec_test`, that signature must actually be a correct HMAC of the timestamped body with that secret. If the fixture author got the math wrong but happened to test against an implementation with the same wrong math, fixtures and impl drift together silently. Mitigation: a one-shot `tools/regenerate_fixtures.py` that regenerates signatures from the canonical inputs, used to author fixtures and to spot-check that they are not byte-rotted. Cheap to write, catches a real failure mode. Phase decisions should commit to including this script or explicitly defer it with rationale.
- **A3 — Conformance does not mean "exhaustive."** The fixture coverage list ("valid verification, invalid signature, required-header missing case, metadata extraction outcome, any provider-specific replay/timestamp case") is good, but it is intentionally not exhaustive. A reader could come away thinking "if it passes the fixtures, it is correct." Spec should add: "Curated provider fixtures pin behavior the catalog has agreed to; passing them does not mean the implementation has no other bugs. Provider-specific edge-case tests still belong in the binding." Otherwise this phase accidentally re-frames the meaning of "we tested it."
- **A4 — `app.queue_name` migration friction in tests.** ~17 test sites currently use `app.queue.name`. Migration is mechanical, but adversarial question: does the rename break in any test that depended on `app.queue.claim_batch(...)`? `tests/test_migration_pruning.py:418` calls `app.queue.claim_batch(...)`. That is queue-mechanic usage, not just name inspection. Spec/plan should address: do those tests need a Knocker-owned alternative (e.g., `app.run_worker(...)` already covers this in normal flow), or do they keep `app.queue` as an explicitly internal escape hatch under a leading underscore? Pin before code lands.
- **A5 — Curated-provider deprecation/sunset is undefined.** Spec calls curated providers "support promises." What happens when GitHub deprecates `X-Hub-Signature-256` in 2030? Is removing a curated provider a breaking change? A SemVer minor? An entry in CHANGELOG only? Probably overengineering this phase, but a one-sentence pin in spec invariants prevents future agents from reinventing the answer: "this phase does not add curated-provider deprecation lifecycle; sunset, when needed, will be handled per-release in CHANGELOG with a deprecation window."
- **A6 — Conformance test file location vs. line-count standard.** Existing `tests/test_providers.py` is 765 lines. Phase 007 evidence already pinned the standard. Adding a fixture-driven conformance test there pushes toward 1000 lines fast. Plan should pre-pick: new `tests/test_provider_conformance.py` file. Otherwise the executor adds tests to the existing file and a follow-up has to split it.
- **A7 — Provider author docs vs. existing verified-ingress doc.** Phase 007 already wrote provider author content into `site/src/content/docs/guides/verified-ingress.mdx` (built-in/app-local/community tiers, custom-provider example). Plan step 3 says "Write provider author/contribution docs" — does that *replace* the 007 doc, *extend* it, or *split it into multiple files* (e.g. a separate "Curated provider contribution" page)? Pin. The cleanest answer is probably extension: keep the verified-ingress guide as the user-facing reference, add a `contributing-providers.mdx` (or similar) for the catalog/fixture path.
- **A8 — Internal module split + import path stability test gap.** Plan says "keep public docstrings/import paths stable." Phase 007's `tests/test_providers.py::test_public_provider_classes_have_docstrings` is one half of that contract; what about the import path half? Add a small test that imports `knocker.Provider`, `knocker.ProviderRequest`, `knocker.ProviderResult`, etc., from `knocker` (not just `knocker.providers`) and asserts the symbols are reachable. That regression-tests the cleanup against accidental over-eager hiding. Should be ~10 lines.

### Module organization (small)

- **M1 — `coercion.py` after the move.** If `_event_id_from_payload_json`, `_optional_payload_int`, and `_required_payload_int` move out, `coercion.py` is left with the truly-generic int/bool/limit coercers. That file becomes much more honest about its name. Spec should commit to NOT also moving those (i.e., the cleanup is targeted to payload helpers, not a full rename of coercion).
- **M2 — `queue.py` is currently `_HonkerQueue`/`_HonkerJob`/`_QueueTransitionError`/`_WorkerQueueIter` with leading underscores already. The leading underscores are correct internal markers. The remaining leakage is `app.queue = _HonkerQueue(...)` exposing a `_HonkerQueue` as a public attribute. The fix is on the `Knocker.__init__` side, not in `queue.py`. Spec should not require renaming inside `queue.py`; it should require not exposing the queue object as a public attribute.

### Review verdict

The spec-diff and plan are aligned and the architectural calls (binding-neutral fixtures, no runtime loading, internal cleanup of `coercion.py` / `app.queue`, public API frozen) are right. The phase is correctly scoped: it consumes existing post-release backlog items rather than inventing new ambition.

Items to resolve in the spec-diff before the plan lands:

- **N1** — fixture file format (recommend JSON, one fixture per file under `providers/<name>/fixtures/`)
- **N2** — fixture schema fields enumerated
- **N3** — Stripe timestamp tolerance handling in fixtures (clock injection vs. offset vs. clock-independent only)
- **N7** — fixture distribution in the wheel (recommend repo-only, not shipped)
- **A1** — fixture changes require deliberate review with rationale
- **A3** — "passes fixtures" does not mean "no bugs"; per-binding edge tests still expected
- **A5** — curated-provider sunset is out of scope for this phase

Items to resolve in the plan:

- **N4** — replacement queue inspection surface shape (recommend `app.queue_name -> str`)
- **N5** — payload module name (recommend `job_payload.py`)
- **N6** — fixture loader location (tests-only or shipped public)
- **N8** — built-in provider module split target (recommend sibling `_builtin_stripe.py` / `_builtin_github.py` modules)
- **A2** — fixture regeneration tooling (commit to `tools/regenerate_fixtures.py` or defer with reason)
- **A4** — what to do with `app.queue.claim_batch(...)` test usage
- **A6** — new `tests/test_provider_conformance.py` file pre-picked
- **A7** — provider docs split: extend existing verified-ingress vs. new contribution page
- **A8** — add an import-path stability test for the public provider symbols
- **M1** — keep `coercion.py` minimal post-move; do not also rename it
- **M2** — leakage is at the `Knocker.__init__` boundary, not inside `queue.py`

Once those land, the implementation can proceed without leaving load-bearing decisions to the executor. None of these blocks the architectural direction; they are precision items.

## Spec-Diff + Plan Response 1

Session A response to Review 1.

Accepted and pinned:

- **D1** — Curated-provider fixtures are JSON, one fixture per file under `providers/<name>/fixtures/`.
- **D2** — The fixture schema is now explicit in `spec-diff.md`: request method/headers/query/body/secrets/provider_options plus expected verification/error/metadata fields.
- **D3** — Curated-provider fixtures are repo-only conformance material in this phase; they are not shipped in the published wheel/package.
- **D4** — Stripe timestamp/tolerance fixtures must use a pinned non-wall-clock strategy; the spec names this explicitly so the implementer cannot let fixtures rot.
- **D5** — Fixture changes now require deliberate review with rationale; fixture conformance is a contract, not a rubber stamp.
- **D6** — The spec now says fixture pass != bug-free; binding-specific edge-case tests still belong beside the fixture suite.
- **D7** — Curated-provider deprecation/sunset is explicitly out of scope for this phase.
- **D8** — The queue inspection replacement surface is pinned as `app.queue_name -> str`.
- **D9** — Payload helpers move specifically to `job_payload.py`.
- **D10** — Fixture loaders are test-only helpers in this phase, not public package surface.
- **D11** — Built-in provider split target is pinned: `providers.py` public facade with sibling internal modules such as `_builtin_stripe.py` / `_builtin_github.py`.
- **D12** — Conformance coverage gets its own `tests/test_provider_conformance.py` file.
- **D13** — Provider docs split is pinned: extend the existing verified-ingress guide where useful, and add a separate curated-provider contribution page.
- **D14** — Add an import-path stability regression test for the public provider symbols.
- **D15** — `coercion.py` remains the generic coercion module after payload helpers move; the cleanup does not widen into a full rename/redesign.
- **D16** — Queue leakage is fixed at the `Knocker` attribute boundary; `queue.py` itself does not need a conceptual rename to satisfy this phase.

Deferred deliberately:

- **D17** — A dedicated fixture regeneration tool is deferred. The plan now says to keep fixtures small and hand-reviewed in 008 rather than widening this phase into catalog tooling.

Net:

- The architectural direction stands.
- The implementer now has the missing precision on fixture shape, clock coupling, queue surface, module targets, doc split, and regression-test layout.

## Spec-Diff + Plan Review 2

Session B (Claude Opus 4.7, 1M context), reviewing the updated `spec-diff.md` and `plan.md` plus Response 1.

### Items closed cleanly

- **D1 / N1** — JSON, one-per-file under `providers/<name>/fixtures/`. Pinned in spec ("What changes" + "Invariants") and plan step 1.
- **D2 / N2** — Fixture schema enumerated in spec invariants with concrete field names (`request.method`, `request.headers`, `request.query`, `request.body`, `request.secrets`, optional `request.provider_options`, `expected.signature_valid`, optional `expected.signature_error_contains` / `provider_delivery_id` / `provider_event_id` / `event_type`). A future contributor cannot invent a per-provider shape without violating an invariant.
- **D3 / N7** — Spec invariant "Curated provider fixtures are repo-owned conformance material only. They are loaded by repo tests and docs/contribution workflows; they are not part of the published wheel/package surface in this phase." Wheel scope is no longer ambiguous.
- **D5 / A1** — Spec invariant "Fixture changes require deliberate review with rationale. Updating fixtures to match code drift is a contract change, not routine churn." Closes the rubber-stamp drift risk.
- **D6 / A3** — Spec invariant "Passing curated-provider fixtures does not mean the implementation has no other bugs. Binding-specific edge-case tests remain required alongside the fixture suite." Prevents the fixture catalog from re-framing what "tested" means.
- **D7 / A5** — Spec invariant "This phase does not define a curated-provider deprecation/sunset lifecycle. ... will be handled in a later phase and called out explicitly in release notes." Future agents will not reinvent this answer.
- **D8 / N4** — Plan pins `app.queue_name` as a read-only string property. Decisive enough.
- **D9 / N5** — Plan pins `job_payload.py`. Disambiguates from request body and canonical event payload.
- **D10 / N6** — Spec + plan agree fixture loaders are test-only in this phase. Community-provider authors will not assume the loader is supported public surface.
- **D11 / N8** — Plan pins sibling `_builtin_stripe.py` / `_builtin_github.py` modules with `providers.py` as the public facade. Avoids the `__init__.py` re-export dance.
- **D12 / A6** — Plan pins new `tests/test_provider_conformance.py`. Existing `tests/test_providers.py` does not get pushed past the line-count standard by a fixture suite.
- **D13 / A7** — Plan splits provider docs: extend `verified-ingress.mdx` for app-local/community, add a new curated-provider contribution page. Existing 007 doc is preserved.
- **D14 / A8** — Plan step 4 commits to an import-path stability regression test for the public provider symbols. Closes the cleanup-without-regression-test gap.
- **D15 / M1** — Plan: "`coercion.py` remains the home for generic argument coercion after payload helpers move; this phase does not widen into a full coercion-module redesign." Targeted move, no over-reach.
- **D16 / M2** — Plan: "Queue leakage fix is at the `Knocker` attribute boundary, not a `queue.py` rename exercise." Right surgery on the right boundary.
- **A4 (folded into plan step 6)** — Plan: "replace direct queue-mechanic test usage such as `app.queue.claim_batch(...)` with Knocker-owned test paths or direct DB setup where needed; do not preserve raw queue access as a semi-public escape hatch." Closes the test-mechanic question. The single `claim_batch` site in `tests/test_migration_pruning.py:418` becomes a one-shot rewrite, not a preserved escape hatch.

### Partially resolved

- **D4 / N3 — Stripe timestamp strategy is named but not picked.** Spec line 88 (in "How we will verify it") says fixtures must use "either explicit clock injection, explicit offset-based fixture material, or another pinned strategy that keeps the fixture valid over time." That is still three options plus an open-ended fourth ("another pinned strategy"). Compared to D8/D9/D11 — which name one concrete answer — this one defers the choice to the implementer. Two consequences:
  - The implementer will pick at code time. That picks a load-bearing decision under implementation pressure rather than under review pressure. The pattern this review pinned for everything else was "decide before code."
  - The clause lives in "How we will verify it" rather than "Invariants." Verification clauses describe what the reviewer checks; invariants describe what the system guarantees. The strategy is a system guarantee (about how Knocker's conformance suite is built), so it belongs in Invariants.
  - Recommendation: pin "explicit clock injection" as the default. Concretely, add a small test-only `clock` argument or thread `time.time()` through `_StripeProvider.verify` so the conformance test can supply a fixed `now`. That keeps fixture timestamps absolute and human-readable, which is what makes JSON fixtures valuable in the first place.

### New items surfaced by the response

- **A9 — Provider metadata file schema is unpinned.** Spec line 12 mentions "provider metadata" and plan line 42 says "small provider metadata files," but unlike fixture JSON, the metadata file schema is not enumerated. Two curated providers will diverge organically (one has `support_tier`, the other has `tier`; one has `upstream_docs_url`, the other doesn't). Pin a minimal `metadata.json` schema in spec invariants: at least `name`, `version`, `support_tier` ("curated"), and optional `description` / `upstream_docs_url`. Cheap to specify; expensive to retrofit once two files exist with different shapes.
- **A10 — `request.secrets` encoding is unpinned.** Spec invariant lists `request.secrets` as a fixture field, but `Provider.verify(..., secrets: tuple[bytes, ...])` takes bytes. JSON has no bytes type. Pin: fixture `request.secrets` is a list of UTF-8 strings; the loader UTF-8-encodes them into `tuple[bytes, ...]` before invoking the provider. Otherwise fixture authors will reach for base64 or hex and the loader will need a discriminator.
- **A11 — Fixture provider_options shape parity.** Spec invariant says "optional `request.provider_options`." The fixture's `provider_options` should be the same dict shape `add_endpoint(..., provider_options=...)` accepts and the same the provider's `validate_options(...)` schema-checks. Add a one-line invariant pinning that parity, so fixtures cannot accidentally encode options the runtime would reject.

### Module organization (small)

- **M3 — Sibling-module split affects internal imports.** Once `_StripeProvider` and `_GitHubProvider` move into `_builtin_stripe.py` and `_builtin_github.py`, `providers.py`'s `_builtin_providers()` factory function needs to import from the new siblings. That is straightforward, but: the helpers `_parse_stripe_signature`, `_json_string_field`, `_coerce_secrets`, etc., currently sit in `providers.py`. After the split, do they stay there as a shared internals module, move into `verifiers.py`, or get duplicated? Pin in plan step 4: shared provider internals stay in `providers.py` (or a clearly-labelled `_provider_internals.py`); built-in modules import from there. Otherwise the sibling files will pull helpers out via shotgun moves and the helper home will be load-bearing-but-undocumented.

### Review verdict

Response 1 closes nearly everything from Review 1 with concrete, single-answer pins. The remaining gaps are small and additive:

- **D4 / N3** — pick one of clock injection / offsets / clock-independent-only and move the clause from "How we will verify it" into "Invariants."
- **A9** — pin a minimal `metadata.json` schema for curated provider catalog entries.
- **A10** — pin `request.secrets` as a list of UTF-8 strings in fixtures; loader encodes to bytes.
- **A11** — pin that fixture `request.provider_options` matches the runtime `provider_options` shape that providers schema-check.
- **M3** — pin where shared provider internals live after the sibling-module split.

None of these blocks the architectural direction. With those pinned, the implementation can proceed without leaving load-bearing decisions to the executor. If Session A wants to land 008 with D4 / N3 still soft, that is a defensible call as long as the implementer is explicitly told to commit to one strategy in the implementation PR rather than write code that tolerates all three.

## Spec-Diff + Plan Response 2

Session A response to Review 2.

Accepted and pinned:

- **D18** — Stripe timestamp/tolerance conformance uses explicit clock injection. This is now a spec invariant, not an implementation-time choice among several options.
- **D19** — Curated provider catalog entries use a pinned `metadata.json` schema: `name`, `version`, `support_tier`, plus optional `description` and `upstream_docs_url`.
- **D20** — Fixture `request.secrets` is encoded as a list of UTF-8 strings; the tests-only loader encodes those strings into the runtime byte secrets tuple.
- **D21** — Fixture `request.provider_options` uses the same dict shape and validation rules as the runtime `provider_options=...` surface.
- **D22** — Shared built-in provider helper functions get an explicitly-labeled internal home during the sibling-module split; the plan now prefers `_provider_internals.py` rather than leaving helper placement implicit.

Net:

- The last soft implementation choices from Review 1/2 are now pinned in the intent docs.
- Phase 008 should no longer require the implementer to invent fixture/catalog formats, clock strategy, secret encoding, provider-options parity, or built-in-helper placement during coding.

## Spec-Diff + Plan Response 3

Session A design refinement after additional product discussion.

Accepted and pinned:

- **D23** — String provider names are reserved for curated built-ins only.
- **D24** — `add_endpoint(provider=...)` should accept either a curated built-in string name or a provider object/value for app-local/community providers.
- **D25** — App-local/community providers should no longer compete in the same global string namespace as curated built-ins, which removes the "I upgraded Knocker and a new bundled provider name broke my custom provider" failure mode.
- **D26** — Community packages should expose `provider()` (or language-equivalent) and the host app should pass that object/value directly into endpoint configuration.
- **D27** — `register_provider(...)` is no longer the primary custom-provider path. It may remain temporarily for compatibility/introspection during the transition, but string-based custom-provider lookup is not the intended steady-state model.

Net:

- The provider model is now simpler and safer:
  - strings for curated built-ins
  - provider objects/values for app-local and community providers
- This keeps the common built-in path pleasant while removing upgrade collisions for custom providers.

## Implementation Pre-Decisions

Session A (Claude Opus 4.7, 1M context), implementing 008. Pinning the soft items from Reviews 1 and 2 before code lands so the implementation is principled, not improvised.

- **PD1 — Stripe clock injection (D4 / N3 final).** `_StripeProvider.__init__(*, clock=lambda: int(time.time()))`. Constructor injection. Default keeps wall-clock semantics for normal use; the conformance loader constructs its own `_StripeProvider(clock=lambda: <fixed>)` for fixtures with absolute timestamps. Public `Provider.verify(...)` signature stays unchanged; the clock is a built-in implementation detail, not a new public API surface. The clause moves from "How we will verify it" into the implicit invariants by being represented in code.
- **PD2 — Provider-instance path in `add_endpoint`.** Implementing the user-stated intent: app-local and community providers pass a `Provider` instance directly, e.g. `app.add_endpoint(provider=AcmeProvider(), secrets=[...])`. String resolution stays for curated names. Rules:
  - If `provider` is a `Provider` instance, use it directly. Skip registry lookup. Validate `instance.name` is non-empty lowercase, `instance.version` is SemVer, and `instance.name` is NOT a built-in name (so a community provider cannot impersonate a curated one).
  - If `provider` is a string, registry lookup as today (007 behavior preserved).
  - The endpoint row's stored `provider` tag is `instance.name` for instance path, the lowercased string for string path.
  - `register_provider(...)` stays in the public surface (007 contract). Documentation now steers app-local/community toward the instance path; `register_provider` is presented as the legacy string-name path.
  - `provider_versions()` still reflects the registry. Instance-path providers do not show up there because they are per-endpoint, not global. Documentation calls this out.
- **PD3 — `metadata.json` schema (A9).** Each `providers/<name>/metadata.json` carries `name`, `version`, `support_tier` ("curated"), `description`, optional `upstream_docs_url`. Schema is small and human-readable; future fields are additive.
- **PD4 — Fixture `request.secrets` encoding (A10).** Fixture JSON uses a list of UTF-8 strings for `request.secrets`. The conformance loader UTF-8 encodes them into `tuple[bytes, ...]` before passing to `Provider.verify(...)`. No base64/hex discriminator.
- **PD5 — Fixture `request.provider_options` parity (A11).** Fixture `request.provider_options` is the same dict shape `add_endpoint(..., provider_options=...)` accepts. Loader calls `provider.validate_options(options)` before invoking `verify(...)`.
- **PD6 — Shared provider internals home (M3).** `_parse_stripe_signature`, `_json_string_field`, `_coerce_secrets`, `_coerce_secret`, `_coerce_provider_options` stay in `providers.py`. `_builtin_stripe.py` and `_builtin_github.py` import from `knocker.providers`. `providers.py` remains the public facade and the canonical home for shared provider helpers.
- **PD7 — Fixture body encoding.** Bodies are UTF-8 strings in JSON, encoded to bytes by the loader. Same model as secrets. Non-UTF-8 webhook bodies are out of scope for the curated catalog in this phase.
- **PD8 — Fixture timestamp control.** Each Stripe fixture file declares `request.now_s` (integer Unix seconds). The conformance loader constructs `_StripeProvider(clock=lambda: fixture["request"]["now_s"])` per fixture, so `t=...` in the signature is absolute and the fixture stays valid forever. Fixtures without a declared `now_s` use the current wall clock (only useful for clock-independent behavior). This is the concrete shape PD1 unlocks.

## Implementation Review 1

Session A review of the completed 008 implementation.

### Findings

- **N1 — Provider-instance updates can silently keep the old implementation when the provider name is unchanged.** In `packages/knocker/python/knocker/verifiers.py`, `_resolve_endpoint_verifier(...)` still reuses `previous_config.verifier` whenever `previous_config.provider_name == provider_name` and no new `secrets=...` are passed. That is correct for curated built-ins like `"stripe"` where "same name" means "same bundled implementation, reuse stored secrets." It is not correct for the new instance path. Two distinct provider objects with the same `.name` but different implementation/version should not collapse to the old verifier. Reproduction in chat confirmed: `add_endpoint(provider=P1(name="acme"), secrets=["s"])` followed by `add_endpoint(provider=P2(name="acme"))` still processes requests through `P1`. The instance path needs to rebuild the verifier from the new provider object rather than taking the string-name reuse shortcut.

- **N2 — String-based custom/community provider lookup is still live, so the upgrade-collision problem is not actually removed.** Phase Response 3 (D23-D27) pinned the intended steady-state model: strings reserved for curated built-ins, provider objects/values for app-local and community providers. The implementation still supports `register_provider(...)` + `add_endpoint(provider="acme", ...)` as an active, documented, and tested path. That behavior appears in `packages/knocker/python/knocker/_knocker.py` docstrings, in docs (`reference/python.mdx`, `verified-ingress.mdx`), and in `tests/test_providers.py`. Because string-based custom lookup still works, a future curated built-in name can still collide with an existing community/custom provider using the legacy path. If we want the 008 product decision to hold, string-based custom/community provider lookup needs to be removed or clearly quarantined behind a deprecated compatibility path that no longer participates in the recommended/steady-state model.

### Verdict

The curated catalog, conformance fixtures, queue cleanup, and payload-helper split all look good. The two remaining issues are both on the new provider-instance / naming-boundary seam:

- one behavioral bug in instance-path endpoint updates
- one architectural mismatch where the old string-based custom-provider path still exists strongly enough to undermine the new collision-avoidance goal

Those should be fixed before calling 008 done.

## Implementation Response 1

Session A response to Implementation Review 1.

Both findings closed.

### N1 — Instance-path verifier reuse on same-name re-add_endpoint

Fixed in `packages/knocker/python/knocker/verifiers.py` `_resolve_endpoint_verifier(...)`: the "previous_config.verifier reuse on no-secrets re-registration" branch now requires `instance is None`. The branch only fires for the curated string-name path, where `provider="stripe"` followed by `provider="stripe"` legitimately points at the same bundled implementation. The instance path always rebuilds: a fresh `Provider` object is treated as a different implementation, even when its `.name` matches the previous one.

Two regression tests added in `tests/test_provider_surface.py`:

- `test_re_add_endpoint_with_new_instance_rebuilds_verifier_with_new_secrets` — P1 then P2 (same `.name`, different verify behavior, fresh secrets) routes subsequent receipts through P2 and rejects requests targeting P1.
- `test_re_add_endpoint_with_new_instance_no_secrets_is_rejected` — re-adding the endpoint with a new instance but no new secrets fails loudly with "requires non-empty secrets" rather than silently reusing the previous verifier.

### N2 — String-based custom-provider lookup retired

`Knocker.register_provider(...)` removed. The registry on each `Knocker` instance is now fixed to the curated built-ins (`stripe`, `github`) and never grows. `add_endpoint(provider="name", ...)` only resolves curated names; app-local and community providers must pass a `Provider` instance directly. There is no global string namespace shared between curated built-ins and non-curated providers, so the upgrade-collision failure mode that motivated D23–D27 is now structurally impossible — not just discouraged.

Concrete changes:

- `Knocker.register_provider` removed from the public class.
- `_validate_provider_for_registration` removed from `verifiers.py` (it only had one caller, the now-deleted `register_provider`). `_validate_instance_provider` remains and continues to enforce name/version/builtin-collision rules at `add_endpoint(...)` time.
- `provider_versions()` now documents itself as the curated-built-ins inspection surface; instance-path providers are per-endpoint and intentionally do not appear there.
- `tests/test_providers.py`: five `register_provider`-shaped unit tests deleted (their behavior is covered by the equivalent instance-path tests in `tests/test_provider_surface.py`); the four end-to-end app-local-provider tests migrated from the `register_provider("acme") + add_endpoint(provider="acme")` pattern to `add_endpoint(provider=AcmeProvider())`. New positive test: `test_register_provider_method_is_no_longer_present` plus `test_provider_versions_only_lists_curated_builtins`. The import-path stability test in `tests/test_provider_surface.py` was updated to assert `register_provider` is *not* present, regression-locking the removal.
- Docs: `verified-ingress.mdx` and `reference/python.mdx` rewritten to drop the legacy string-name path. The intended steady-state model is the only model documented.
- `CHANGELOG.md`: explicit `### Removed` section for `register_provider`, `### Fixed` section for N1, `### Changed` section updated to reflect that `add_endpoint(provider="...")` only resolves curated built-ins.
- `SYSTEM.md`: current-baseline bullets updated to drop the registry framing and emphasize the instance path as the only path for non-curated providers.

### Net

The 008 product decision (D23–D27) is now actually true in code, not just in docs. There is no string-based custom-provider lookup left to undermine the collision-avoidance goal, and same-name instance re-registration can no longer silently keep the old implementation. Phase 008 is ready to be called done.

## Implementation Closeout 1

Session A closeout pass after Implementation Review 1.

Final cleanup to bring intent/docs/comments into alignment with the implementation now that `register_provider(...)` has been removed. No behavior changes in this pass; only stale-text fixes.

- `spec-diff.md` invariants block updated: the public Python provider API list no longer includes `Knocker.register_provider(...)`, and the "may remain temporarily for compatibility/introspection" sentence is replaced with a one-line statement that `register_provider(...)` is removed (Implementation Review 1 finding N2) and a pointer to the instance-path model.
- `ROADMAP.md` current-status text updated: the public provider plugin surface bullet now lists the actual surface (`Provider`, `ProviderRequest`, `ProviderResult`, `Knocker.provider_versions`) and notes that the instance path on `add_endpoint(...)` is the only path for non-curated providers — no global string-lookup registry. The "Still intentionally not implemented" bullet for the `providers/<name>/` catalog is rephrased as "the source-only catalog landed in Phase 008" so future readers do not assume that catalog is still pending.
- `packages/knocker/python/knocker/verifiers.py::_validate_instance_provider` docstring updated: removed the dangling `register_provider(...)` reference and stated the instance-path validation rules directly (non-empty lowercase name, SemVer version, no built-in collision).
- Historical mentions of `register_provider(...)` inside Review 1, Pre-Decisions, Response 3, and earlier reviews/responses are intentionally left in place. Those blocks are append-only history describing the state at the time they were written; rewriting them would obscure the trail of how the steady-state model was reached.
- Phase 007 documents are not touched. They correctly record the API as it shipped in 007.

### Verification

- `uv run --group dev pytest tests/` — 111 passed.
- `npm --prefix site run build` — 12 pages built.

### Status

Phase 008 is closed.

# Reviews And Decisions

This file is append-only.

## Spec-Diff + Plan Review 1

Session B (Claude Opus 4.7), reviewing `spec-diff.md` and `plan.md` together since they were authored as a pair and there's no separate spec-diff/plan round.

### Positive conformance review

- **P1 — The "plugin at API level, not runtime" call is the right one.** Native dynamic loading, WASM, and entry-point auto-discovery are explicitly out. That avoids the "random Nebraska package props up every Stripe user" supply-chain failure mode and keeps `knocker-core` provider-neutral. The phase notes section names this concern by name; matching trap is in the plan.
- **P2 — `ProviderResult` combines verify + extract.** Single pass over headers/body, one return value carrying both verification outcome and metadata. This is what makes the orphan-delivery-with-metadata invariant possible: an invalid signature can still produce a Delivery row with `provider_delivery_id` populated, which is operationally useful.
- **P3 — Three-tier doc taxonomy.** built-in / app-local / community is clean, doesn't fragment the code (one Provider interface, three sources), and the support promise is testable: "tested with valid, invalid, missing-header, and rotation/timestamp cases where applicable" pins what "built-in" actually costs.
- **P4 — Stripe migration is intentionally a no-op.** Step 4 explicitly says "no intentional behavior change" and existing test coverage carries forward. That's the right risk posture for the migration.
- **P5 — GitHub upgrade fills a real product gap.** GitHub was metadata-only before; now it gets `X-Hub-Signature-256` verification with the same shape as Stripe. Provider count goes from 1 to 2, which is enough to prove the interface without inflating the catalog.
- **P6 — Compatibility paths preserved.** `verification={"kind": ...}`, `delivery_key`/`event_key` callables, explicit `receive(...)` metadata override — all named in plan step 6 with regression tests in step 7. No silent breaking changes.
- **P7 — `knocker-core` and `knocker-extension` stay provider-neutral.** Plan's "Areas not to touch" includes this explicitly; trap forbids verification UDFs in the extension. Good — the SQLite-contract layering survives.

### Negative conformance review

- **N1 — "Duplicate provider registration fails" doesn't say what happens to a built-in vs a built-in.** Built-ins are auto-registered in `__init__`. If a user calls `register_provider(community_stripe)` to override the built-in, that fails. Pin this explicitly: "built-in providers cannot be overridden via `register_provider`. Adding an override knob (e.g., `register_provider(provider, override=True)`) is intentionally deferred." Otherwise a user who reads the spec might expect to be able to swap the built-in.
- **N2 — Forward-compat trap: a community provider name that's later promoted to a built-in becomes a breaking change for users.** Today: user registers `acme` as a community provider. Tomorrow: Knocker ships built-in `acme`. The user's `Knocker(...)` now construct-time fails because the built-in is already registered when their `register_provider` runs. The spec should commit to one of: (a) "built-in additions are breaking changes; document new built-ins in CHANGELOG and reserve names ahead of time," or (b) the override knob from N1. Pick before plan lands.
- **N3 — `provider_options` shape isn't pinned.** Plan adds `add_endpoint(..., provider_options=None)` with `{"tolerance_s": 300}` as the Stripe example. But: free-form dict with no schema means typo'd keys silently no-op. Pin: "providers must reject unknown option keys with `ValueError`" so misconfiguration fails loudly. This is a real fail-fast issue, not cosmetic.
- **N4 — Provider invocation order during `add_endpoint`.** What happens if a user does `add_endpoint(provider="acme", ...)` before `register_provider(acme_provider)`? Plan doesn't say. Should fail with `KeyError("unknown provider: acme")` — not silently no-op or buffer. Pin in the invariants.
- **N5 — `provider_versions()` return shape unstated.** "reports the registered provider names and semantic versions" — `dict[str, str]`? `list[tuple[str, str]]`? Typed `ProviderVersion` dataclass? Pick. Convention from 003/004/006 is typed dataclasses for stable API surfaces. Either commit to `dict[str, str]` (simple, immutable-by-copy) or add a `ProviderVersion(name: str, version: str)` to the public model layer.
- **N6 — `provider_versions()` semantics undocumented.** "Provider versions are semantic-version strings surfaced for inspection." Is this the *implementation* version (Knocker-bundled provider code version) or the *target API* version (the provider's API version, e.g., Stripe API 2024-04-15)? Pin: implementation version, not target API version. Provider versions are NOT part of the SemVer compatibility contract.
- **N7 — Spec-diff doesn't mention `provider_options`.** Plan introduces it; spec-diff is silent. Either add a "What changes" bullet about `provider_options`, or move the decision into the spec-diff invariants. Right now an executor reading the spec alone wouldn't know about this kwarg.

### Adversarial review

- **A1 — Provider that raises an unexpected exception.** A buggy community provider's `verify(...)` raises `RuntimeError` mid-call. Today's verifier code returns `_VerificationResult(False, str(exc))` from `_StripeVerifier.verify` only for known parse errors; an unhandled exception propagates. Under the new registry, a community provider's bug becomes Knocker's runtime error. The spec/plan should commit: "if `Provider.verify` raises, Knocker treats it as a verification failure with `signature_error="provider error: <repr>"`, stores an orphan delivery, and does not crash the worker." Otherwise a community provider can take down a worker. This is the v1 of the supply-chain concern P1 closed at the loading layer; verify-time runtime errors are the next slice of it.
- **A2 — `secrets=None` semantics under the registry.** With `verification=None` today, `add_endpoint` uses no verifier and every delivery is treated as `signature_valid=True`. With `provider="stripe", secrets=None`, what happens? Three options: (a) skip verification entirely (compat with verification=None); (b) call the provider with empty secrets — Stripe's verify will reject all signatures because no secret matches; (c) fail at config time with "secrets required for provider stripe." Plan/spec should pick. My lean: (c) — fail loudly at config time. Most users passing a provider name *intend* to verify, and "no secrets" is almost certainly a mistake.
- **A3 — `add_endpoint(provider="github")` with no secrets.** Same shape as A2. GitHub provider reads `X-Hub-Signature-256` and rejects mismatches. If secrets=None, every GitHub webhook gets `signature_error="missing secret"` and lands as an orphan delivery. That's correct fail-safe behavior but maybe surprising. Whatever you pick for A2, apply it consistently.
- **A4 — `ProviderRequest.json()` body shape.** Plan says "with cached body parsing." What if the body isn't JSON? GitHub sometimes sends `application/x-www-form-urlencoded` for `ping` events, JSON for everything else. A provider author writing `request.json()` against form-encoded body — does it raise? Return `None`? Return `{}`? Pin: "raises `ValueError` on non-JSON body; provider authors should check Content-Type or call `json()` inside try/except." Otherwise providers that assume JSON will silently misbehave on form-encoded payloads.
- **A5 — GitHub dedupe via `X-GitHub-Delivery`.** Spec/plan correctly defer body-level dedupe. But GitHub's own UI lets operators "redeliver" a webhook from the dashboard — the redelivery carries the *same* `X-GitHub-Delivery`. So a redelivery from GitHub's UI is correctly deduped (treated as a duplicate). But the assumption is that `X-GitHub-Delivery` is per-receipt, and GitHub's docs say it's per-receipt. Worth one sentence in spec invariants: "GitHub provider's dedupe identity is `X-GitHub-Delivery`, which is stable across operator-initiated redeliveries from the GitHub dashboard." Otherwise an operator might wonder why a redeliver attempt didn't reprocess.
- **A6 — Generic HMAC stays in the dict path only.** Plan's reasoning is implicit; spec doesn't explain. A reader might wonder "why isn't `provider="hmac-sha256"` a thing?" The honest answer: generic HMAC has too many per-provider knobs (header name, prefix, secret format) to belong in the curated catalog without becoming "any provider, configure it yourself," which dilutes the curated promise. Spec-diff "Notes" should explain this once. One sentence prevents future agents from "let's just add it."
- **A7 — `providers/<name>/` repo-level catalog is forward-looking, not implemented.** Spec mentions it; plan doesn't build it (the providers in 007 live inside the Python package). That's fine but the spec should label it explicitly: "this phase does not introduce the `providers/<name>/` repo-level catalog; it is a forward-looking placeholder for accepted upstream providers in a later phase." Otherwise an executor might try to build the catalog now.
- **A8 — Test gap: orphan delivery with extracted provider metadata.** Spec invariant: "Provider implementations return both verification outcome and extracted metadata: ... why verification failed, when it failed, provider delivery id, provider event id, event type." Plan's test list has "failed verification storing an orphan delivery" but doesn't assert that the orphan delivery has `provider_delivery_id`/`provider_event_id` populated from the failed-verification provider call. That's the actual operational benefit of the combined ProviderResult — without a test pinning it, the implementation might short-circuit metadata extraction on verify failure. Add: "failed verification still surfaces extracted metadata on the orphan delivery row where the provider was able to extract it before the signature check failed."

### Module organization (small)

- **M1 — `packages/knocker/python/knocker/providers.py` collides namespace-wise with the existing `verifiers.py`.** Plan step 1 says "create or repurpose `providers.py`." Today's `verifiers.py` carries `_GenericHmacVerifier`, `_StripeVerifier`, `_RequestVerifier`, `_VerificationResult`, `_IngressRequest`, `_EndpointConfig`, `_ProviderPreset`, plus their helpers. Some of those become public Provider machinery; others stay internal to the binding. Pick: rename `verifiers.py` to `providers.py` and split internals back out, OR keep both and document the split (`providers.py` = public API; `verifiers.py` = internal HMAC/Stripe parsing helpers). Mixing public and private in one module without clear rule has been a source of friction before (`coercion.py` carries payload helpers — same shape).

### Review verdict

The spec-diff and plan are both solid. The architectural calls (no native loading, ProviderResult combines verify+extract, knocker-core stays neutral, three-tier docs) are right. The shape of the public API (`ProviderRequest`, `ProviderResult`, `Provider`, `register_provider`, `provider_versions`) is conventional and consistent with prior phases.

Items to resolve in the spec-diff before the plan lands:

- **N1** — built-in override semantics
- **N2** — forward-compat policy for built-in additions
- **N3** — `provider_options` rejection of unknown keys (fail-fast)
- **N4** — order-of-registration semantics (`add_endpoint` before `register_provider` should fail)
- **A1** — provider `verify(...)` exception handling contract
- **A2 / A3** — `secrets=None` with a provider name (pick: skip verify vs config-time error)

Items to resolve in the plan:

- **N5 / N6** — `provider_versions()` return shape and "implementation version, not API version" semantics
- **N7** — pull `provider_options` into the spec-diff
- **A4** — `ProviderRequest.json()` non-JSON body behavior
- **A5** — pin GitHub's `X-GitHub-Delivery`-based dedupe in invariants
- **A6** — explain generic HMAC's exclusion from the curated catalog
- **A7** — label the `providers/<name>/` catalog as forward-looking
- **A8** — add the orphan-delivery-with-metadata test
- **M1** — pick the `providers.py` vs `verifiers.py` split before code lands

Once those land, the implementation can proceed without leaving load-bearing decisions to the executor.

## Spec-Diff + Plan Response 1

Session A response.

- **D1 / N1** — Built-in providers cannot be overridden by `register_provider(...)`. An explicit override knob is deferred.
- **D2 / N2** — Adding a new built-in provider name is compatibility-affecting because it may collide with app-local/community providers. New built-ins must be called out in the changelog.
- **D3 / N3 / N7** — `provider_options` is now part of the spec. Providers schema-check options and reject unknown keys with `ValueError`.
- **D4 / N4** — `add_endpoint(provider="name", ...)` fails immediately for unknown providers; registration must happen first.
- **D5 / N5 / N6** — `provider_versions()` returns a plain `dict[str, str]` copy, and versions are provider implementation SemVer strings, not upstream provider API versions.
- **D6 / A1** — Unexpected provider `verify(...)` exceptions become verification failures with useful `signature_error`; Knocker stores an orphan `Delivery` instead of crashing the caller.
- **D7 / A2 / A3** — Built-in providers that require secrets reject missing/`None`/empty secrets at endpoint registration time.
- **D8 / A4** — `ProviderRequest.json()` raises `ValueError` on non-JSON bodies.
- **D9 / A5** — GitHub dedupe identity is pinned to `X-GitHub-Delivery`, including dashboard redelivery behavior.
- **D10 / A6** — Generic HMAC remains explicit `verification={...}` configuration because its knobs are too provider-specific for the curated provider catalog.
- **D11 / A7** — The repo-level `providers/<name>/` catalog is explicitly forward-looking and not built in `007`.
- **D12 / A8** — Tests must assert invalid/orphan deliveries still retain extracted provider metadata when extraction succeeded before signature failure.
- **D13 / M1** — `providers.py` is the public provider API and built-in provider registry home. `verifiers.py` remains internal only if useful for legacy verification adapters.

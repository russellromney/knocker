"""Phase 008 surface tests: instance-path provider registration and public
import-path stability for the provider symbols.

These tests are split out of ``test_providers.py`` so each test file stays
under the project line-count standard.
"""

import knocker
import pytest

from tests.helpers import require_event_id as _require_event_id


# Provider-instance path (Phase 008) --------------------------------------


async def test_add_endpoint_accepts_provider_instance(db_path):
    """App-local providers can be passed as Provider instances directly,
    with no register_provider(...) call."""

    app = knocker.open(db_path)

    class _AcmeProvider(knocker.Provider):
        name = "acme"
        version = "1.0.0"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            token = request.header("x-acme-token")
            for secret in secrets:
                if token == secret.decode("utf-8"):
                    return knocker.ProviderResult.accept(
                        provider_delivery_id=request.header("x-acme-delivery"),
                    )
            return knocker.ProviderResult.reject("acme token mismatch")

    app.add_endpoint(
        name="acme",
        path="/webhooks/acme",
        provider=_AcmeProvider(),
        secrets=["acme-secret"],
    )

    result = app.receive(
        endpoint="acme",
        body=b"{}",
        headers={"X-Acme-Token": "acme-secret", "X-Acme-Delivery": "d-1"},
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_delivery_id == "d-1"


async def test_add_endpoint_provider_instance_rejects_builtin_name_collision(db_path):
    """Curated string names are reserved; an instance whose .name shadows a
    built-in is rejected at add_endpoint time."""

    app = knocker.open(db_path)

    class _ShadowStripe(knocker.Provider):
        name = "stripe"
        version = "9.9.9"

        def verify(self, request, *, secrets, options):
            return knocker.ProviderResult.accept()

    with pytest.raises(ValueError, match="collides with a built-in"):
        app.add_endpoint(
            name="hook",
            path="/webhooks/hook",
            provider=_ShadowStripe(),
            secrets=["s"],
        )


async def test_add_endpoint_provider_instance_validates_name_and_version(db_path):
    app = knocker.open(db_path)

    class _UpperName(knocker.Provider):
        name = "Acme"
        version = "1.0.0"

        def verify(self, request, *, secrets, options):
            return knocker.ProviderResult.accept()

    with pytest.raises(ValueError, match="lowercase"):
        app.add_endpoint(
            name="hook",
            path="/webhooks/hook",
            provider=_UpperName(),
            secrets=["s"],
        )

    class _BadVersion(knocker.Provider):
        name = "acme"
        version = "v1"

        def verify(self, request, *, secrets, options):
            return knocker.ProviderResult.accept()

    with pytest.raises(ValueError, match="semantic version"):
        app.add_endpoint(
            name="hook",
            path="/webhooks/hook",
            provider=_BadVersion(),
            secrets=["s"],
        )


async def test_add_endpoint_provider_instance_rejects_non_provider_value(db_path):
    app = knocker.open(db_path)

    with pytest.raises(TypeError, match="must be a curated provider name"):
        app.add_endpoint(
            name="hook",
            path="/webhooks/hook",
            provider=object(),
            secrets=["s"],
        )


async def test_add_endpoint_provider_instance_requires_secrets_for_required(db_path):
    app = knocker.open(db_path)

    class _AcmeProvider(knocker.Provider):
        name = "acme"
        version = "1.0.0"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            return knocker.ProviderResult.accept()

    with pytest.raises(ValueError, match="requires non-empty secrets"):
        app.add_endpoint(
            name="hook",
            path="/webhooks/hook",
            provider=_AcmeProvider(),
        )


async def test_add_endpoint_provider_instance_validates_options(db_path):
    app = knocker.open(db_path)

    class _AcmeProvider(knocker.Provider):
        name = "acme"
        version = "1.0.0"
        option_keys = frozenset({"window_s"})
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            return knocker.ProviderResult.accept()

    with pytest.raises(ValueError, match="unknown provider options"):
        app.add_endpoke = None  # noqa: silence pyflakes if any
        app.add_endpoint(
            name="hook",
            path="/webhooks/hook",
            provider=_AcmeProvider(),
            secrets=["s"],
            provider_options={"windowww_s": 60},
        )


async def test_add_endpoint_instance_provider_does_not_mutate_registry(db_path):
    """Instance-path providers are per-endpoint and do not show up in
    provider_versions(); the registry stays clean."""

    app = knocker.open(db_path)

    class _AcmeProvider(knocker.Provider):
        name = "acme"
        version = "1.2.3"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            return knocker.ProviderResult.accept()

    app.add_endpoint(
        name="acme",
        path="/webhooks/acme",
        provider=_AcmeProvider(),
        secrets=["s"],
    )

    versions = app.provider_versions()
    assert "acme" not in versions
    # Built-ins are still there.
    assert "stripe" in versions and "github" in versions


# Public surface stability (Phase 008) ------------------------------------


def test_public_provider_symbols_reachable_from_top_level():
    """Phase 008 cleanup must not regress import paths for the public surface.

    String-based ``register_provider`` was retired in Phase 008 Implementation
    Review 1 (finding N2); ``Knocker`` no longer carries that method.
    """

    import knocker as top_level

    assert top_level.Provider is not None
    assert top_level.ProviderRequest is not None
    assert top_level.ProviderResult is not None
    assert top_level.Knocker is not None
    assert hasattr(top_level.Knocker, "provider_versions")
    assert hasattr(top_level.Knocker, "queue_name")
    assert not hasattr(top_level.Knocker, "register_provider")
    # The provider symbols are the same objects via knocker.providers too.
    from knocker import providers as providers_mod

    assert providers_mod.Provider is top_level.Provider
    assert providers_mod.ProviderRequest is top_level.ProviderRequest
    assert providers_mod.ProviderResult is top_level.ProviderResult


def test_app_queue_name_returns_configured_queue():
    """app.queue_name is the inspection surface that replaced raw app.queue."""

    app = knocker.open(":memory:")
    assert app.queue_name == "knocker.events"

    custom = knocker.open(":memory:", queue_name="custom.queue")
    assert custom.queue_name == "custom.queue"


def test_raw_queue_attribute_is_no_longer_public():
    """Phase 008: raw queue object is internal; only queue_name is public."""

    app = knocker.open(":memory:")
    assert not hasattr(app, "queue")


# Regression: instance-path re-registration must not silently reuse the
# previous verifier just because two providers share a ``.name`` (Phase 008
# Implementation Review 1, finding N1).


async def test_re_add_endpoint_with_new_instance_rebuilds_verifier_with_new_secrets(db_path):
    """Two providers with the same .name but different impls must not
    collapse to the old verifier on re-add_endpoint."""

    app = knocker.open(db_path)

    class _P1(knocker.Provider):
        name = "acme"
        version = "1.0.0"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            if request.header("x-acme-impl") == "p1":
                return knocker.ProviderResult.accept(provider_delivery_id="p1")
            return knocker.ProviderResult.reject("p1 only accepts X-Acme-Impl=p1")

    class _P2(knocker.Provider):
        name = "acme"
        version = "2.0.0"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            if request.header("x-acme-impl") == "p2":
                return knocker.ProviderResult.accept(provider_delivery_id="p2")
            return knocker.ProviderResult.reject("p2 only accepts X-Acme-Impl=p2")

    app.add_endpoint(
        name="acme",
        path="/webhooks/acme",
        provider=_P1(),
        secrets=["s"],
    )
    # Re-add with the same name but a new instance + new secrets must
    # route subsequent requests through P2, not P1.
    app.add_endpoint(
        name="acme",
        path="/webhooks/acme",
        provider=_P2(),
        secrets=["s"],
    )

    p2_result = app.receive(
        endpoint="acme",
        body=b"{}",
        headers={"X-Acme-Impl": "p2"},
    )
    assert p2_result.event_id is not None
    delivery = app.get_delivery(p2_result.delivery_id)
    assert delivery.provider_delivery_id == "p2"

    # And requests targeting P1 should now be rejected, since the verifier
    # comes from P2.
    p1_result = app.receive(
        endpoint="acme",
        body=b"{}",
        headers={"X-Acme-Impl": "p1"},
    )
    assert p1_result.event_id is None
    p1_delivery = app.get_delivery(p1_result.delivery_id)
    assert "p2 only accepts" in (p1_delivery.signature_error or "")


async def test_re_add_endpoint_with_new_instance_no_secrets_is_rejected(db_path):
    """Re-adding the same endpoint with a fresh instance but no new secrets
    must fail loudly rather than silently reusing the previous verifier."""

    app = knocker.open(db_path)

    class _P1(knocker.Provider):
        name = "acme"
        version = "1.0.0"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            return knocker.ProviderResult.accept()

    class _P2(knocker.Provider):
        name = "acme"
        version = "2.0.0"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            return knocker.ProviderResult.accept()

    app.add_endpoint(
        name="acme",
        path="/webhooks/acme",
        provider=_P1(),
        secrets=["s"],
    )
    with pytest.raises(ValueError, match="requires non-empty secrets"):
        app.add_endpoint(
            name="acme",
            path="/webhooks/acme",
            provider=_P2(),
        )

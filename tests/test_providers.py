import json
import hashlib
import hmac
from pathlib import Path

import knocker
import pytest

from tests.helpers import (
    require_event_id as _require_event_id,
    stripe_signature as _stripe_signature,
)


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROVIDERS_DIR = _REPO_ROOT / "providers"
_CURATED_PROVIDER_NAMES = {
    "stripe",
    "github",
    "shopify",
    "slack",
    "postmark",
    "resend",
    "paddle",
    "lemon-squeezy",
    "standard-webhooks",
    "clerk",
    "twilio",
    "sendgrid",
    "linear",
    "meta",
    "discord",
    "zendesk",
    "intercom",
    "hubspot",
    "token-header",
    "bearer-token",
    "basic-auth",
}


def _github_signature(secret: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _load_provider_fixture(provider_name: str, fixture_id: str) -> dict:
    path = _PROVIDERS_DIR / provider_name / "fixtures" / f"{fixture_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _receive_provider_fixture(app, provider_name: str, fixture_id: str):
    fixture = _load_provider_fixture(provider_name, fixture_id)
    request = fixture["request"]
    provider_options = dict(request.get("provider_options", {}))
    if "now_s" in request:
        provider_options["tolerance_s"] = max(
            int(provider_options.get("tolerance_s", 0)),
            10_000_000_000,
        )
    app.add_endpoint(
        name=provider_name,
        path=f"/webhooks/{provider_name}",
        provider=provider_name,
        secrets=request["secrets"],
        provider_options=provider_options or None,
    )
    result = app.receive(
        endpoint=provider_name,
        body=request["body"].encode("utf-8"),
        headers=dict(request.get("headers", {})),
        query=dict(request.get("query", {})),
    )
    return fixture, result


# Provider registry surface ------------------------------------------------


async def test_builtin_provider_versions_includes_curated_provider_pack(db_path):
    app = knocker.open(db_path)

    versions = app.provider_versions()

    assert _CURATED_PROVIDER_NAMES.issubset(versions)
    for name in _CURATED_PROVIDER_NAMES:
        assert isinstance(versions[name], str) and versions[name]


async def test_provider_versions_returns_fresh_copy(db_path):
    app = knocker.open(db_path)

    first = app.provider_versions()
    first["mutated"] = "1.0.0"
    second = app.provider_versions()

    assert "mutated" not in second


async def test_register_provider_method_is_no_longer_present(db_path):
    """Phase 008: string-based custom-provider lookup is retired. Curated
    string names are reserved for built-ins; app-local and community
    providers use the instance path on ``add_endpoint(...)`` instead."""

    app = knocker.open(db_path)
    assert not hasattr(app, "register_provider")


async def test_provider_versions_only_lists_curated_builtins(db_path):
    """``provider_versions()`` reflects only curated built-ins. There is no
    additive registry; instance-path providers are per-endpoint and do not
    appear here."""

    app = knocker.open(db_path)
    versions = app.provider_versions()
    assert set(versions.keys()) == _CURATED_PROVIDER_NAMES


async def test_add_endpoint_rejects_unknown_provider_name(db_path):
    app = knocker.open(db_path)

    with pytest.raises(ValueError, match="unknown provider"):
        app.add_endpoint(
            name="acme",
            path="/webhooks/acme",
            provider="acme",
            secrets=["secret"],
        )


async def test_add_endpoint_switching_to_different_provider_requires_new_secrets(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(
        name="hook",
        path="/webhooks/hook",
        provider="stripe",
        secrets=["whsec_test"],
    )

    with pytest.raises(ValueError, match="requires non-empty secrets"):
        app.add_endpoint(
            name="hook",
            path="/webhooks/hook",
            provider="github",
        )


async def test_add_endpoint_same_provider_can_reuse_existing_secrets(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_reuse","type":"invoice.paid"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        secrets=["whsec_test"],
    )

    # Updating the endpoint without new secrets should keep the same verifier
    # only when the provider name is unchanged.
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe-updated",
        provider="stripe",
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body)},
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_event_id == "evt_reuse"


async def test_add_endpoint_rejects_missing_secrets_for_builtin(db_path):
    app = knocker.open(db_path)

    with pytest.raises(ValueError, match="requires non-empty secrets"):
        app.add_endpoint(
            name="stripe",
            path="/webhooks/stripe",
            provider="stripe",
        )

    with pytest.raises(ValueError, match="requires non-empty secrets"):
        app.add_endpoint(
            name="stripe2",
            path="/webhooks/stripe2",
            provider="stripe",
            secrets=None,
        )

    with pytest.raises(ValueError, match="at least one secret"):
        app.add_endpoint(
            name="stripe3",
            path="/webhooks/stripe3",
            provider="stripe",
            secrets=[],
        )


async def test_add_endpoint_rejects_unknown_provider_options(db_path):
    app = knocker.open(db_path)

    with pytest.raises(ValueError, match="unknown provider options"):
        app.add_endpoint(
            name="stripe",
            path="/webhooks/stripe",
            provider="stripe",
            secrets=["whsec_test"],
            provider_options={"toleranace_s": 300},  # typo
        )


async def test_add_endpoint_validates_stripe_tolerance(db_path):
    app = knocker.open(db_path)

    with pytest.raises(ValueError, match="non-negative"):
        app.add_endpoint(
            name="stripe",
            path="/webhooks/stripe",
            provider="stripe",
            secrets=["whsec_test"],
            provider_options={"tolerance_s": -1},
        )

    with pytest.raises(TypeError, match="must be an integer"):
        app.add_endpoint(
            name="stripe2",
            path="/webhooks/stripe2",
            provider="stripe",
            secrets=["whsec_test"],
            provider_options={"tolerance_s": "300"},
        )


@pytest.mark.parametrize("provider_name", ["slack", "resend", "paddle"])
async def test_add_endpoint_validates_tolerance_for_curated_provider(db_path, provider_name):
    app = knocker.open(db_path)

    with pytest.raises(ValueError, match="non-negative"):
        app.add_endpoint(
            name=provider_name,
            path=f"/webhooks/{provider_name}",
            provider=provider_name,
            secrets=["test-secret"],
            provider_options={"tolerance_s": -1},
        )

    with pytest.raises(TypeError, match="must be an integer"):
        app.add_endpoint(
            name=f"{provider_name}-2",
            path=f"/webhooks/{provider_name}-2",
            provider=provider_name,
            secrets=["test-secret"],
            provider_options={"tolerance_s": "300"},
        )


async def test_add_endpoint_rejects_provider_options_without_registry_path(db_path):
    app = knocker.open(db_path)

    with pytest.raises(ValueError, match="provider_options"):
        app.add_endpoint(
            name="stripe",
            path="/webhooks/stripe",
            provider="stripe",
            verification={"kind": "stripe", "secret": "whsec_test"},
            provider_options={"tolerance_s": 300},
        )


async def test_add_endpoint_rejects_provider_options_dict_shape(db_path):
    app = knocker.open(db_path)

    with pytest.raises(TypeError, match="provider_options must be a dict"):
        app.add_endpoint(
            name="stripe",
            path="/webhooks/stripe",
            provider="stripe",
            secrets=["whsec_test"],
            provider_options=["tolerance_s", 300],
        )


# Provider error handling -------------------------------------------------


async def test_provider_unexpected_exception_creates_orphan_delivery(db_path):
    app = knocker.open(db_path)

    class _BrokenProvider(knocker.Provider):
        name = "broken"
        version = "0.1.0"
        option_keys = frozenset()
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            raise RuntimeError("boom in provider")

    app.add_endpoint(
        name="broken",
        path="/webhooks/broken",
        provider=_BrokenProvider(),
        secrets=["s"],
    )

    result = app.receive(endpoint="broken", body=b"{}", headers={})

    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is False
    assert "provider error" in (delivery.signature_error or "")
    assert "RuntimeError" in (delivery.signature_error or "")
    assert "boom in provider" in (delivery.signature_error or "")


async def test_provider_returning_non_provider_result_creates_orphan_delivery(db_path):
    app = knocker.open(db_path)

    class _BadShapeProvider(knocker.Provider):
        name = "badshape"
        version = "0.1.0"

        def verify(self, request, *, secrets, options):
            return {"valid": True}

    app.add_endpoint(
        name="badshape",
        path="/webhooks/badshape",
        provider=_BadShapeProvider(),
        secrets=["s"],
    )

    result = app.receive(endpoint="badshape", body=b"{}", headers={})

    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is False
    assert "non-ProviderResult" in (delivery.signature_error or "")


# App-local provider end-to-end -------------------------------------------


async def test_app_local_provider_accepts_and_extracts_metadata(db_path):
    app = knocker.open(db_path)

    class _AcmeProvider(knocker.Provider):
        name = "acme"
        version = "1.0.0"
        option_keys = frozenset()
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            token = request.header("x-acme-token")
            delivery_id = request.header("x-acme-delivery")
            event_type = request.header("x-acme-event")
            for secret in secrets:
                if hmac.compare_digest(token or "", secret.decode("utf-8")):
                    return knocker.ProviderResult.accept(
                        provider_delivery_id=delivery_id,
                        event_type=event_type,
                    )
            return knocker.ProviderResult.reject(
                "acme token mismatch",
                provider_delivery_id=delivery_id,
                event_type=event_type,
            )

    app.add_endpoint(
        name="acme",
        path="/webhooks/acme",
        provider=_AcmeProvider(),
        secrets=["acme-secret"],
    )

    result = app.receive(
        endpoint="acme",
        body=b'{"hello":"world"}',
        headers={
            "X-Acme-Token": "acme-secret",
            "X-Acme-Delivery": "delivery-1",
            "X-Acme-Event": "ping",
        },
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is True
    assert event.provider_delivery_id == "delivery-1"
    assert event.event_type == "ping"


async def test_app_local_provider_invalid_orphan_keeps_extracted_metadata(db_path):
    app = knocker.open(db_path)

    class _AcmeProvider(knocker.Provider):
        name = "acme"
        version = "1.0.0"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            delivery_id = request.header("x-acme-delivery")
            event_type = request.header("x-acme-event")
            return knocker.ProviderResult.reject(
                "always-fail",
                provider_delivery_id=delivery_id,
                event_type=event_type,
            )

    app.add_endpoint(
        name="acme",
        path="/webhooks/acme",
        provider=_AcmeProvider(),
        secrets=["s"],
    )

    result = app.receive(
        endpoint="acme",
        body=b"{}",
        headers={"X-Acme-Delivery": "delivery-7", "X-Acme-Event": "demo"},
    )

    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is False
    assert delivery.provider_delivery_id == "delivery-7"
    assert delivery.event_type == "demo"
    assert delivery.signature_error == "always-fail"


async def test_provider_cannot_mutate_stored_raw_receipt(db_path):
    app = knocker.open(db_path)

    class _AcmeProvider(knocker.Provider):
        name = "acme"
        version = "1.0.0"
        requires_secrets = True

        def verify(self, request, *, secrets, options):
            with pytest.raises(TypeError):
                request.headers["X-Injected"] = "yes"
            with pytest.raises(TypeError):
                request.query["page"] = "2"
            return knocker.ProviderResult.accept()

    app.add_endpoint(
        name="acme",
        path="/webhooks/acme",
        provider=_AcmeProvider(),
        secrets=["s"],
    )

    result = app.receive(
        endpoint="acme",
        body=b"{}",
        headers={"X-Original": "present"},
        query={"page": "1"},
    )

    delivery = app.get_delivery(result.delivery_id)
    assert delivery.headers == {"X-Original": "present"}
    assert delivery.query == {"page": "1"}


# GitHub provider end-to-end ----------------------------------------------


async def test_github_valid_signature_creates_linked_event(db_path):
    app = knocker.open(db_path)
    secret = "github-secret"
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
        secrets=[secret],
    )

    body = b'{"zen":"keep it logically awesome"}'
    result = app.receive(
        endpoint="github",
        body=body,
        headers={
            "X-Hub-Signature-256": _github_signature(secret, body),
            "X-GitHub-Delivery": "delivery-abc",
            "X-GitHub-Event": "push",
        },
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is True
    assert delivery.signature_error is None
    assert event.provider_delivery_id == "delivery-abc"
    assert event.event_type == "push"


async def test_github_invalid_signature_creates_orphan_with_metadata(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
        secrets=["github-secret"],
    )

    body = b'{"zen":"non-blocking I/O"}'
    result = app.receive(
        endpoint="github",
        body=body,
        headers={
            "X-Hub-Signature-256": _github_signature("wrong-secret", body),
            "X-GitHub-Delivery": "delivery-xyz",
            "X-GitHub-Event": "push",
        },
    )

    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is False
    assert "github signature mismatch" in (delivery.signature_error or "")
    # invalid orphan still surfaces extracted metadata for debugging
    assert delivery.provider_delivery_id == "delivery-xyz"
    assert delivery.event_type == "push"


async def test_github_missing_signature_header_rejected(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
        secrets=["github-secret"],
    )

    result = app.receive(
        endpoint="github",
        body=b"{}",
        headers={
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "ping",
        },
    )

    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert "missing signature header: x-hub-signature-256" in (
        delivery.signature_error or ""
    )
    assert delivery.provider_delivery_id == "delivery-1"
    assert delivery.event_type == "ping"


async def test_github_missing_delivery_header_rejected(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
        secrets=["github-secret"],
    )

    body = b"{}"
    result = app.receive(
        endpoint="github",
        body=body,
        headers={
            "X-Hub-Signature-256": _github_signature("github-secret", body),
            "X-GitHub-Event": "push",
        },
    )

    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert "missing delivery header: x-github-delivery" in (
        delivery.signature_error or ""
    )


async def test_github_signature_must_use_sha256_prefix(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
        secrets=["github-secret"],
    )

    body = b"{}"
    bare_digest = hmac.new(b"github-secret", body, hashlib.sha256).hexdigest()
    result = app.receive(
        endpoint="github",
        body=body,
        headers={
            "X-Hub-Signature-256": bare_digest,
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "push",
        },
    )

    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert "sha256=" in (delivery.signature_error or "")
    assert delivery.provider_delivery_id == "delivery-1"


async def test_github_dedupes_on_delivery_id(db_path):
    app = knocker.open(db_path)
    secret = "github-secret"
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
        secrets=[secret],
    )

    body = b'{"zen":"keep it logically awesome"}'
    headers = {
        "X-Hub-Signature-256": _github_signature(secret, body),
        "X-GitHub-Delivery": "delivery-redeliver",
        "X-GitHub-Event": "push",
    }
    first = app.receive(endpoint="github", body=body, headers=headers)
    # GitHub dashboard redelivery preserves X-GitHub-Delivery; treat as duplicate.
    second = app.receive(endpoint="github", body=body, headers=headers)

    event_id = _require_event_id(first)
    assert second.event_id == event_id
    assert second.duplicate is True


# Stripe regression through registry --------------------------------------


async def test_stripe_provider_via_registry_with_provider_options_tolerance(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_tol","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        secrets=["whsec_test"],
        provider_options={"tolerance_s": 600},
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body)},
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_event_id == "evt_tol"
    assert event.event_type == "checkout.session.completed"


async def test_stripe_invalid_signature_orphan_preserves_extracted_metadata(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_orphan","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        secrets=["whsec_test"],
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_wrong", body)},
    )

    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is False
    assert delivery.provider_event_id == "evt_orphan"
    assert delivery.event_type == "checkout.session.completed"


@pytest.mark.parametrize("provider_name", sorted(_CURATED_PROVIDER_NAMES))
async def test_curated_provider_valid_fixture_flows_through_receive(db_path, provider_name):
    app = knocker.open(db_path)

    fixture, result = _receive_provider_fixture(app, provider_name, "valid")

    expected = fixture["expected"]
    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is True
    assert delivery.signature_error is None
    assert delivery.body == fixture["request"]["body"].encode("utf-8")
    if "provider_delivery_id" in expected:
        assert event.provider_delivery_id == expected["provider_delivery_id"]
        assert delivery.provider_delivery_id == expected["provider_delivery_id"]
    if "provider_event_id" in expected:
        assert event.provider_event_id == expected["provider_event_id"]
        assert delivery.provider_event_id == expected["provider_event_id"]
    if "event_type" in expected:
        assert event.event_type == expected["event_type"]
        assert delivery.event_type == expected["event_type"]


@pytest.mark.parametrize(
    "provider_name,fixture_id",
    [(name, "invalid_signature") for name in sorted(_CURATED_PROVIDER_NAMES)],
)
async def test_curated_provider_invalid_fixture_becomes_orphan_delivery(
    db_path, provider_name, fixture_id
):
    app = knocker.open(db_path)

    fixture, result = _receive_provider_fixture(app, provider_name, fixture_id)

    expected = fixture["expected"]
    assert result.event_id is None
    delivery = app.get_delivery(result.delivery_id)
    assert delivery.signature_valid is False
    assert delivery.body == fixture["request"]["body"].encode("utf-8")
    assert expected["signature_error_contains"] in (delivery.signature_error or "")
    if "provider_delivery_id" in expected:
        assert delivery.provider_delivery_id == expected["provider_delivery_id"]
    if "provider_event_id" in expected:
        assert delivery.provider_event_id == expected["provider_event_id"]
    if "event_type" in expected:
        assert delivery.event_type == expected["event_type"]


# Compatibility paths -----------------------------------------------------


async def test_explicit_receive_metadata_overrides_provider_extraction(db_path):
    app = knocker.open(db_path)
    secret = "github-secret"
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
        secrets=[secret],
    )

    body = b"{}"
    result = app.receive(
        endpoint="github",
        body=body,
        headers={
            "X-Hub-Signature-256": _github_signature(secret, body),
            "X-GitHub-Delivery": "delivery-from-header",
            "X-GitHub-Event": "push",
        },
        provider_delivery_id="explicit-delivery",
        event_type="explicit-event",
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_delivery_id == "explicit-delivery"
    assert event.event_type == "explicit-event"


async def test_user_delivery_key_callable_overrides_provider_extraction(db_path):
    app = knocker.open(db_path)
    secret = "github-secret"
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
        secrets=[secret],
        delivery_key=lambda req: req.headers.get("X-Custom-Id"),
    )

    body = b"{}"
    result = app.receive(
        endpoint="github",
        body=body,
        headers={
            "X-Hub-Signature-256": _github_signature(secret, body),
            "X-GitHub-Delivery": "delivery-from-header",
            "X-GitHub-Event": "push",
            "X-Custom-Id": "delivery-from-callable",
        },
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_delivery_id == "delivery-from-callable"


async def test_legacy_verification_dict_stripe_routes_through_registry(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_legacy","type":"invoice.paid"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": "whsec_test"},
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body)},
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_event_id == "evt_legacy"
    assert event.event_type == "invoice.paid"


async def test_legacy_hmac_verification_dict_with_github_label_extracts_via_preset(db_path):
    app = knocker.open(db_path)
    body = b"{}"
    secret = "topsecret"
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    app.add_endpoint(
        name="github-mix",
        path="/webhooks/github-mix",
        provider="github",  # legacy preset extracts delivery id and event type
        verification={
            "kind": "hmac-sha256",
            "secret": secret,
            "header": "x-acme-signature",
            "prefix": "sha256=",
        },
    )

    result = app.receive(
        endpoint="github-mix",
        body=body,
        headers={
            "X-Acme-Signature": f"sha256={digest}",
            "X-GitHub-Delivery": "delivery-mix",
            "X-GitHub-Event": "push",
        },
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_delivery_id == "delivery-mix"
    assert event.event_type == "push"


# ProviderRequest helpers --------------------------------------------------


def test_provider_request_header_lookup_is_case_insensitive():
    request = knocker.ProviderRequest(
        method="POST",
        headers={"X-Custom-Header": "abc", "Content-Type": "application/json"},
        query={},
        body=b"{}",
    )

    assert request.header("x-custom-header") == "abc"
    assert request.header("X-CUSTOM-HEADER") == "abc"
    assert request.header("missing", default="fallback") == "fallback"


def test_provider_request_json_caches_and_raises_on_non_json():
    request = knocker.ProviderRequest(
        method="POST",
        headers={},
        query={},
        body=b'{"ok":true}',
    )

    assert request.json() == {"ok": True}
    # cached on second call (no exception, same result)
    assert request.json() == {"ok": True}

    bad = knocker.ProviderRequest(
        method="POST", headers={}, query={}, body=b"not json"
    )
    with pytest.raises(ValueError, match="not JSON"):
        bad.json()


def test_provider_request_headers_and_query_are_read_only():
    request = knocker.ProviderRequest(
        method="POST",
        headers={"X-Test": "abc"},
        query={"page": "1"},
        body=b"{}",
    )

    with pytest.raises(TypeError):
        request.headers["X-Test"] = "changed"
    with pytest.raises(TypeError):
        request.query["page"] = "2"


# Public docstring smoke ---------------------------------------------------


def test_public_provider_classes_have_docstrings():
    for obj in (
        knocker.Provider,
        knocker.ProviderRequest,
        knocker.ProviderResult,
        knocker.ProviderResult.accept,
        knocker.ProviderResult.reject,
        knocker.Provider.verify,
        knocker.ProviderRequest.header,
        knocker.ProviderRequest.json,
        knocker.Knocker.provider_versions,
    ):
        doc = (obj.__doc__ or "").strip()
        assert doc, f"missing docstring on {obj!r}"

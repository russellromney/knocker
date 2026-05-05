"""Repo-owned curated-provider conformance tests.

This file loads each fixture under ``providers/<name>/fixtures/`` and asserts
the bundled Python provider implementation produces the expected verification
outcome and extracted metadata. Fixtures are binding-neutral JSON; the loader
here is test-only scaffolding (see Phase 008 plan decision PD8 / D10).

For Stripe, ``request.now_s`` injects a fixed clock into ``_StripeProvider``
so timestamp-tolerance fixtures stay valid forever (decision PD1 / N3).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import knocker
from knocker._builtin_github import _GitHubProvider
from knocker._builtin_lemonsqueezy import _LemonSqueezyProvider
from knocker._builtin_paddle import _PaddleProvider
from knocker._builtin_postmark import _PostmarkProvider
from knocker._builtin_resend import _ResendProvider
from knocker._builtin_shopify import _ShopifyProvider
from knocker._builtin_slack import _SlackProvider
from knocker._builtin_stripe import _StripeProvider
from knocker.providers import _coerce_provider_options


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PROVIDERS_DIR = _REPO_ROOT / "providers"


def _load_fixtures(provider_name: str) -> list[tuple[str, dict[str, Any]]]:
    """Return ``[(fixture_id, fixture_dict), ...]`` sorted by file name."""

    directory = _PROVIDERS_DIR / provider_name / "fixtures"
    if not directory.is_dir():
        raise FileNotFoundError(f"missing fixtures directory: {directory}")
    out: list[tuple[str, dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        with path.open("rb") as handle:
            out.append((path.stem, json.load(handle)))
    return out


def _build_request(fixture: dict[str, Any]) -> knocker.ProviderRequest:
    request = fixture["request"]
    return knocker.ProviderRequest(
        method=request.get("method", "POST"),
        headers=dict(request.get("headers", {})),
        query=dict(request.get("query", {})),
        body=request["body"].encode("utf-8"),
    )


def _coerce_secrets(fixture: dict[str, Any]) -> tuple[bytes, ...]:
    raw = fixture["request"].get("secrets", [])
    if not isinstance(raw, list):
        raise TypeError("fixture request.secrets must be a list of UTF-8 strings")
    return tuple(value.encode("utf-8") for value in raw)


def _provider_for(name: str, fixture: dict[str, Any]) -> knocker.Provider:
    if name == "stripe":
        now_s = fixture["request"].get("now_s")
        if now_s is None:
            return _StripeProvider()
        frozen = int(now_s)
        return _StripeProvider(clock=lambda: frozen)
    if name == "github":
        return _GitHubProvider()
    if name == "shopify":
        return _ShopifyProvider()
    if name == "slack":
        now_s = fixture["request"].get("now_s")
        if now_s is None:
            return _SlackProvider()
        frozen = int(now_s)
        return _SlackProvider(clock=lambda: frozen)
    if name == "postmark":
        return _PostmarkProvider()
    if name == "resend":
        now_s = fixture["request"].get("now_s")
        if now_s is None:
            return _ResendProvider()
        frozen = int(now_s)
        return _ResendProvider(clock=lambda: frozen)
    if name == "paddle":
        now_s = fixture["request"].get("now_s")
        if now_s is None:
            return _PaddleProvider()
        frozen = int(now_s)
        return _PaddleProvider(clock=lambda: frozen)
    if name == "lemon-squeezy":
        return _LemonSqueezyProvider()
    raise ValueError(f"no curated python implementation for provider {name!r}")


def _assert_expected(
    fixture_id: str, expected: dict[str, Any], result: knocker.ProviderResult
) -> None:
    assert result.valid is bool(expected["signature_valid"]), (
        f"{fixture_id}: signature_valid mismatch (got {result.valid}, "
        f"expected {expected['signature_valid']}, error={result.signature_error!r})"
    )
    if "signature_error_contains" in expected:
        substring = expected["signature_error_contains"]
        actual_error = result.signature_error or ""
        assert substring in actual_error, (
            f"{fixture_id}: signature_error did not contain expected substring "
            f"(got {actual_error!r}, expected substring {substring!r})"
        )
    for field in ("provider_delivery_id", "provider_event_id", "event_type"):
        if field in expected:
            actual = getattr(result, field)
            assert actual == expected[field], (
                f"{fixture_id}: {field} mismatch (got {actual!r}, "
                f"expected {expected[field]!r})"
            )


def _run_fixture(provider_name: str, fixture_id: str, fixture: dict[str, Any]) -> None:
    provider = _provider_for(provider_name, fixture)
    options = _coerce_provider_options(
        fixture["request"].get("provider_options"), provider
    )
    request = _build_request(fixture)
    secrets = _coerce_secrets(fixture)
    result = provider.verify(request, secrets=secrets, options=options)
    _assert_expected(fixture_id, fixture["expected"], result)


@pytest.mark.parametrize(
    "provider_name",
    sorted(directory.name for directory in _PROVIDERS_DIR.iterdir() if directory.is_dir()),
)
def test_curated_fixture_conformance(provider_name):
    for fixture_id, fixture in _load_fixtures(provider_name):
        _run_fixture(provider_name, fixture_id, fixture)


@pytest.mark.parametrize(
    "provider_name,provider_cls",
    [
        ("stripe", _StripeProvider),
        ("github", _GitHubProvider),
        ("shopify", _ShopifyProvider),
        ("slack", _SlackProvider),
        ("postmark", _PostmarkProvider),
        ("resend", _ResendProvider),
        ("paddle", _PaddleProvider),
        ("lemon-squeezy", _LemonSqueezyProvider),
    ],
)
def test_metadata_matches_python_implementation(provider_name, provider_cls):
    metadata = json.loads(
        (_PROVIDERS_DIR / provider_name / "metadata.json").read_text(encoding="utf-8")
    )
    impl = provider_cls()
    assert metadata["name"] == impl.name == provider_name
    assert metadata["version"] == impl.version
    assert metadata["support_tier"] == "curated"


# Catalog shape -----------------------------------------------------------


def test_catalog_metadata_files_share_required_fields():
    """Every curated provider directory must carry the small pinned schema."""

    required = {"name", "version", "support_tier", "description"}
    for directory in sorted(_PROVIDERS_DIR.iterdir()):
        if not directory.is_dir():
            continue
        metadata_path = directory / "metadata.json"
        assert metadata_path.is_file(), f"missing metadata.json in {directory}"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        missing = required - set(metadata)
        assert not missing, (
            f"{directory.name}/metadata.json missing required fields: {sorted(missing)}"
        )
        assert metadata["support_tier"] == "curated"
        fixtures_dir = directory / "fixtures"
        assert fixtures_dir.is_dir() and any(fixtures_dir.glob("*.json")), (
            f"{directory.name}/fixtures must contain at least one JSON fixture"
        )


def test_fixture_files_share_required_schema_fields():
    """Each fixture must carry the pinned request/expected schema."""

    request_required = {"method", "headers", "body", "secrets"}
    for directory in sorted(_PROVIDERS_DIR.iterdir()):
        if not directory.is_dir():
            continue
        for path in sorted((directory / "fixtures").glob("*.json")):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            assert "request" in fixture and "expected" in fixture, (
                f"{path}: must have 'request' and 'expected' top-level keys"
            )
            request = fixture["request"]
            missing = request_required - set(request)
            assert not missing, f"{path}: request missing fields {sorted(missing)}"
            assert isinstance(request["secrets"], list), (
                f"{path}: request.secrets must be a list of UTF-8 strings"
            )
            assert all(isinstance(value, str) for value in request["secrets"]), (
                f"{path}: request.secrets entries must be strings"
            )
            assert "signature_valid" in fixture["expected"], (
                f"{path}: expected.signature_valid is required"
            )

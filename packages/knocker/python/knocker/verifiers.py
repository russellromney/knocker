from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from knocker.providers import (
    Provider,
    ProviderRequest,
    ProviderResult,
    _coerce_provider_options,
    _coerce_secrets,
    _json_string_field,
)


_UNSET = object()
_SEMVER_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


KeyExtractor = Callable[[ProviderRequest], Optional[str]]


@dataclass(frozen=True, slots=True)
class _ProviderPreset:
    delivery_key: Optional[KeyExtractor] = None
    event_key: Optional[KeyExtractor] = None
    event_type: Optional[KeyExtractor] = None


@dataclass(frozen=True, slots=True)
class _EndpointConfig:
    verifier: Optional["_Verifier"] = None
    provider_name: Optional[str] = None
    legacy_delivery_key: Optional[KeyExtractor] = None
    legacy_event_key: Optional[KeyExtractor] = None
    legacy_event_type: Optional[KeyExtractor] = None
    delivery_key: Optional[KeyExtractor] = None
    event_key: Optional[KeyExtractor] = None


class _Verifier:
    """Internal unified verifier shape: ``(request) -> ProviderResult``."""

    def verify(self, request: ProviderRequest) -> ProviderResult:
        raise NotImplementedError


class _ProviderVerifier(_Verifier):
    """Adapter wrapping a registered ``Provider`` with secrets and options.

    Translates unexpected provider exceptions into a rejected ``ProviderResult``
    so a buggy app-local provider never crashes the caller; the orphan delivery
    carries a useful ``signature_error``.
    """

    __slots__ = ("provider", "secrets", "options")

    def __init__(
        self,
        provider: Provider,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> None:
        self.provider = provider
        self.secrets = secrets
        self.options = options

    def verify(self, request: ProviderRequest) -> ProviderResult:
        try:
            result = self.provider.verify(
                request, secrets=self.secrets, options=self.options
            )
        except Exception as exc:  # noqa: BLE001 — adversarial provider safety
            return ProviderResult.reject(
                f"provider error: {type(exc).__name__}: {exc}"
            )
        if not isinstance(result, ProviderResult):
            return ProviderResult.reject(
                f"provider {self.provider.name!r} returned non-ProviderResult: {type(result).__name__}"
            )
        return result


class _LegacyHmacVerifier(_Verifier):
    """Generic HMAC verifier for the legacy ``verification={...}`` config path.

    Verifies a single body-only HMAC-SHA256 signature in a configurable header
    with optional prefix. Does not extract metadata; metadata extraction
    happens via the legacy provider preset, when present, or via user
    ``delivery_key`` / ``event_key`` callables.
    """

    __slots__ = ("header", "secrets", "prefix")

    def __init__(
        self,
        header: str,
        secrets: tuple[bytes, ...],
        prefix: Optional[str],
    ) -> None:
        self.header = header
        self.secrets = secrets
        self.prefix = prefix

    def verify(self, request: ProviderRequest) -> ProviderResult:
        header_value = request.header(self.header)
        if header_value is None:
            return ProviderResult.reject(f"missing signature header: {self.header}")
        for secret in self.secrets:
            digest = hmac.new(secret, request.body, hashlib.sha256).hexdigest()
            expected = (
                f"{self.prefix}{digest}" if self.prefix is not None else digest
            )
            if hmac.compare_digest(header_value, expected):
                return ProviderResult.accept()
        return ProviderResult.reject("signature mismatch")


def _build_legacy_verifier(
    verification: Any, provider_registry: dict[str, Provider]
) -> _Verifier:
    """Build a verifier for ``verification={...}`` legacy config.

    ``kind="stripe"`` adapts to the registered Stripe provider so the
    implementation stays unified. ``kind="hmac-sha256"`` (also accepts
    ``generic-hmac-sha256`` and ``hmac``) keeps the standalone
    ``_LegacyHmacVerifier``.
    """

    if not isinstance(verification, dict):
        raise TypeError("verification must be a dict or None")
    kind = str(verification.get("kind", "")).lower()
    secrets = _coerce_secrets_from_verification(verification)
    if kind in {"hmac-sha256", "generic-hmac-sha256", "hmac"}:
        header = verification.get("header")
        if not isinstance(header, str) or not header:
            raise ValueError("generic hmac verification requires a non-empty 'header'")
        prefix_value = verification.get("prefix", "sha256=")
        if prefix_value is not None and not isinstance(prefix_value, str):
            raise TypeError("verification 'prefix' must be a string or None")
        return _LegacyHmacVerifier(header=header, secrets=secrets, prefix=prefix_value)
    if kind == "stripe":
        tolerance_value = verification.get("tolerance_s", 300)
        if isinstance(tolerance_value, bool) or not isinstance(tolerance_value, int):
            raise TypeError("stripe verification 'tolerance_s' must be an integer")
        if tolerance_value < 0:
            raise ValueError("stripe verification 'tolerance_s' must be non-negative")
        provider = provider_registry["stripe"]
        return _ProviderVerifier(
            provider=provider,
            secrets=secrets,
            options={"tolerance_s": tolerance_value},
        )
    raise ValueError(f"unsupported verification kind: {kind!r}")


def _legacy_preset(provider: Optional[str]) -> _ProviderPreset:
    """Return legacy preset extractors keyed by provider name.

    These run as a fallback when the configured verifier did not extract
    the metadata field itself, so a legacy
    ``provider="stripe", verification={"kind": "hmac-sha256", ...}``
    configuration still gets Stripe-style JSON extraction.
    """

    provider_name = (provider or "").lower()
    if provider_name == "stripe":
        return _ProviderPreset(
            event_key=lambda request: _json_string_field(request.body, "id"),
            event_type=lambda request: _json_string_field(request.body, "type"),
        )
    if provider_name == "github":
        return _ProviderPreset(
            delivery_key=lambda request: request.header("x-github-delivery"),
            event_type=lambda request: request.header("x-github-event"),
        )
    return _ProviderPreset()


def _build_endpoint_config(
    *,
    providers: dict[str, Provider],
    builtin_names: frozenset[str],
    previous_config: Optional[_EndpointConfig],
    provider: Any,
    verification: Any,
    secrets: Any,
    provider_options: Any,
    delivery_key: Any,
    event_key: Any,
    unset: Any,
) -> tuple[_EndpointConfig, Optional[str]]:
    """Assemble a validated ``_EndpointConfig`` from raw add_endpoint args.

    Returns ``(config, stored_provider_tag)``. ``stored_provider_tag`` is the
    string written to the endpoint row's provider column: the lowercased
    string name for the curated/string path, ``instance.name`` for the
    instance path, or ``None`` when no provider was passed.
    """

    if verification is not unset and secrets is not unset:
        raise ValueError("pass either verification=... or secrets=..., not both")
    if verification is not unset and provider_options is not unset:
        raise ValueError(
            "provider_options requires the provider registry path; "
            "pass it alongside secrets=..., not verification=..."
        )

    instance: Optional[Provider] = None
    provider_name: Optional[str] = None
    if isinstance(provider, Provider):
        _validate_instance_provider(provider, builtin_names)
        instance = provider
        provider_name = provider.name
    elif isinstance(provider, str) and provider:
        provider_name = provider.lower()
        if (
            provider_name not in providers
            and verification is unset
        ):
            raise ValueError(f"unknown provider: {provider!r}")
    elif provider is not None:
        raise TypeError(
            "provider must be a curated provider name string, a knocker.Provider "
            "instance, or None"
        )

    preset = _legacy_preset(provider_name)
    delivery_key_override = (
        None if delivery_key is unset else _coerce_extractor(delivery_key, "delivery_key")
    )
    event_key_override = (
        None if event_key is unset else _coerce_extractor(event_key, "event_key")
    )
    verifier = _resolve_endpoint_verifier(
        providers=providers,
        instance=instance,
        previous_config=previous_config,
        provider_arg=provider,
        provider_name=provider_name,
        verification=verification,
        secrets=secrets,
        provider_options=provider_options,
        unset=unset,
    )
    config = _EndpointConfig(
        verifier=verifier,
        provider_name=provider_name,
        legacy_delivery_key=preset.delivery_key,
        legacy_event_key=preset.event_key,
        legacy_event_type=preset.event_type,
        delivery_key=delivery_key_override,
        event_key=event_key_override,
    )
    return config, provider_name


def _validate_instance_provider(
    instance: Provider, builtin_names: frozenset[str]
) -> None:
    """Validate a ``Provider`` instance passed via the instance path.

    The instance path is the only path for app-local and community
    providers. Curated string names are reserved for built-ins, so an
    instance whose ``name`` collides with a built-in is rejected. The
    instance's ``name`` must be a non-empty lowercase identifier and its
    ``version`` must be a SemVer string.
    """

    name = instance.name
    if not isinstance(name, str) or not name or name != name.lower():
        raise ValueError(
            "provider instance name must be a non-empty lowercase string"
        )
    version = instance.version
    if not isinstance(version, str) or not _SEMVER_RE.fullmatch(version):
        raise ValueError(
            "provider instance version must be a semantic version string like '1.2.3'"
        )
    if name in builtin_names:
        raise ValueError(
            f"provider instance name {name!r} collides with a built-in curated "
            "provider; pick a distinct name for app-local or community providers"
        )


def _resolve_endpoint_verifier(
    *,
    providers: dict[str, Provider],
    instance: Optional[Provider],
    previous_config: Optional[_EndpointConfig],
    provider_arg: Any,
    provider_name: Optional[str],
    verification: Any,
    secrets: Any,
    provider_options: Any,
    unset: Any,
) -> Optional[_Verifier]:
    """Resolve an ``_EndpointConfig.verifier`` from ``add_endpoint`` arguments.

    Precedence: legacy ``verification={...}`` wins when present; otherwise an
    instance-path ``Provider`` or a registered string name builds a verifier;
    missing secrets for a provider that requires them is a config error.
    """

    if verification is not unset:
        return _build_legacy_verifier(verification, providers)

    def _provider_for(name: Optional[str]) -> Provider:
        if instance is not None:
            return instance
        if name is None:
            raise ValueError("secrets=... requires a provider")
        return providers[name]

    if secrets is not unset:
        provider_obj = _provider_for(provider_name)
        options = _coerce_provider_options(
            None if provider_options is unset else provider_options, provider_obj
        )
        if provider_obj.requires_secrets:
            if secrets is None:
                raise ValueError(
                    f"provider {_describe_provider(provider_arg)} requires non-empty secrets=..."
                )
            secrets_tuple = _coerce_secrets(secrets)
        else:
            secrets_tuple = () if secrets is None else _coerce_secrets(secrets)
        return _ProviderVerifier(provider_obj, secrets_tuple, options)
    if provider_name is not None:
        provider_obj = _provider_for(provider_name)
        if provider_obj.requires_secrets:
            if (
                # Only the curated string-name path may reuse the previous
                # verifier as a no-secrets re-registration. A fresh provider
                # instance is authoritative and must rebuild the verifier
                # even when its ``.name`` matches the previous one — same
                # name is not the same implementation.
                instance is None
                and previous_config is not None
                and previous_config.verifier is not None
                and previous_config.provider_name == provider_name
            ):
                if provider_options is not unset:
                    raise ValueError(
                        "provider_options=... requires secrets=... on the same call"
                    )
                return previous_config.verifier
            raise ValueError(
                f"provider {_describe_provider(provider_arg)} requires non-empty secrets=..."
            )
        options = _coerce_provider_options(
            None if provider_options is unset else provider_options, provider_obj
        )
        return _ProviderVerifier(provider_obj, (), options)
    if provider_options is not unset:
        raise ValueError(
            "provider_options=... requires a provider and secrets=..."
        )
    return previous_config.verifier if previous_config is not None else None


def _describe_provider(provider_arg: Any) -> str:
    """Render a provider arg for error messages without leaking object reprs."""

    if isinstance(provider_arg, Provider):
        return repr(provider_arg.name)
    return repr(provider_arg)


def _verify_and_extract(
    config: _EndpointConfig, request: ProviderRequest
) -> ProviderResult:
    """Run the configured verifier and merge legacy preset / user override extraction.

    Returns a ``ProviderResult`` whose metadata fields are the final extracted
    values before explicit ``receive(...)`` arguments win. The legacy preset
    only fires for fields the verifier did not extract; user
    ``delivery_key`` / ``event_key`` callables override both.
    """

    if config.verifier is None:
        result = ProviderResult.accept()
    else:
        result = config.verifier.verify(request)
    event_id = result.provider_event_id
    if event_id is None:
        event_id = _extract_optional(config.legacy_event_key, request)
    delivery_id = result.provider_delivery_id
    if delivery_id is None:
        delivery_id = _extract_optional(config.legacy_delivery_key, request)
    event_type = result.event_type
    if event_type is None:
        event_type = _extract_optional(config.legacy_event_type, request)
    if config.event_key is not None:
        event_id = _extract_optional(config.event_key, request)
    if config.delivery_key is not None:
        delivery_id = _extract_optional(config.delivery_key, request)
    return ProviderResult(
        valid=result.valid,
        signature_error=result.signature_error,
        provider_delivery_id=delivery_id,
        provider_event_id=event_id,
        event_type=event_type,
    )


def _coerce_extractor(value: Any, name: str) -> Optional[KeyExtractor]:
    if value is None:
        return None
    if not callable(value):
        raise TypeError(f"{name} must be callable or None")
    return value


def _extract_optional(
    extractor: Optional[KeyExtractor], request: ProviderRequest
) -> Optional[str]:
    if extractor is None:
        return None
    value = extractor(request)
    if value is None:
        return None
    return str(value)


def _coerce_secrets_from_verification(verification: dict[str, Any]) -> tuple[bytes, ...]:
    if "secrets" in verification:
        raw = verification["secrets"]
        if not isinstance(raw, (list, tuple)):
            raise TypeError("verification 'secrets' must be a list or tuple")
        values: Any = raw
    elif "secret" in verification:
        values = [verification["secret"]]
    else:
        raise ValueError("verification requires 'secret' or 'secrets'")
    return _coerce_secrets(values)

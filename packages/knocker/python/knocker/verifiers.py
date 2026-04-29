from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


KeyExtractor = Callable[["_IngressRequest"], Optional[str]]


@dataclass(frozen=True, slots=True)
class _VerificationResult:
    valid: bool
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class _IngressRequest:
    method: str
    headers: dict[str, Any]
    query: dict[str, Any]
    body: bytes


@dataclass(frozen=True, slots=True)
class _EndpointConfig:
    verifier: Optional["_RequestVerifier"] = None
    delivery_key: Optional[KeyExtractor] = None
    event_key: Optional[KeyExtractor] = None
    event_type: Optional[KeyExtractor] = None


@dataclass(frozen=True, slots=True)
class _ProviderPreset:
    delivery_key: Optional[KeyExtractor] = None
    event_key: Optional[KeyExtractor] = None
    event_type: Optional[KeyExtractor] = None


class _RequestVerifier:
    def verify(self, body: bytes, headers: dict[str, Any]) -> _VerificationResult:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class _GenericHmacVerifier(_RequestVerifier):
    header: str
    secrets: tuple[bytes, ...]
    prefix: Optional[str]

    def verify(self, body: bytes, headers: dict[str, Any]) -> _VerificationResult:
        header_value = _get_header(headers, self.header)
        if header_value is None:
            return _VerificationResult(False, f"missing signature header: {self.header}")
        for secret in self.secrets:
            digest = hmac.new(secret, body, hashlib.sha256).hexdigest()
            expected = f"{self.prefix}{digest}" if self.prefix is not None else digest
            if hmac.compare_digest(header_value, expected):
                return _VerificationResult(True)
        return _VerificationResult(False, "signature mismatch")


@dataclass(frozen=True, slots=True)
class _StripeVerifier(_RequestVerifier):
    secrets: tuple[bytes, ...]
    tolerance_s: int

    def verify(self, body: bytes, headers: dict[str, Any]) -> _VerificationResult:
        header_value = _get_header(headers, "stripe-signature")
        if header_value is None:
            return _VerificationResult(False, "missing signature header: stripe-signature")
        try:
            timestamp, signatures = _parse_stripe_signature(header_value)
        except ValueError as exc:
            return _VerificationResult(False, str(exc))
        if abs(int(time.time()) - timestamp) > self.tolerance_s:
            return _VerificationResult(False, "stripe signature timestamp outside tolerance")
        signed_payload = f"{timestamp}.".encode("utf-8") + body
        for secret in self.secrets:
            expected = hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()
            for candidate in signatures:
                if hmac.compare_digest(candidate, expected):
                    return _VerificationResult(True)
        return _VerificationResult(False, "stripe signature mismatch")


def _build_request_verifier(verification: Any) -> Optional[_RequestVerifier]:
    if verification is None:
        return None
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
        return _GenericHmacVerifier(header=header, secrets=secrets, prefix=prefix_value)
    if kind == "stripe":
        tolerance_value = verification.get("tolerance_s", 300)
        if isinstance(tolerance_value, bool) or not isinstance(tolerance_value, int):
            raise TypeError("stripe verification 'tolerance_s' must be an integer")
        if tolerance_value < 0:
            raise ValueError("stripe verification 'tolerance_s' must be non-negative")
        return _StripeVerifier(secrets=secrets, tolerance_s=tolerance_value)
    raise ValueError(f"unsupported verification kind: {kind!r}")


def _build_provider_verifier(provider: Optional[str], secrets: Any) -> _RequestVerifier:
    secrets_tuple = _coerce_secret_values(secrets)
    provider_name = (provider or "").lower()
    if provider_name == "stripe":
        return _StripeVerifier(secrets=secrets_tuple, tolerance_s=300)
    raise ValueError(
        f"provider preset verification is not supported for {provider!r}; use verification=..."
    )


def _provider_preset(provider: Optional[str]) -> _ProviderPreset:
    provider_name = (provider or "").lower()
    if provider_name == "stripe":
        return _ProviderPreset(
            event_key=lambda request: _json_string_field(request.body, "id"),
            event_type=lambda request: _json_string_field(request.body, "type"),
        )
    if provider_name == "github":
        return _ProviderPreset(
            delivery_key=lambda request: _get_header(request.headers, "x-github-delivery"),
            event_type=lambda request: _get_header(request.headers, "x-github-event"),
        )
    return _ProviderPreset()


def _coerce_extractor(value: Any, name: str) -> Optional[KeyExtractor]:
    if value is None:
        return None
    if not callable(value):
        raise TypeError(f"{name} must be callable or None")
    return value


def _extract_optional(extractor: Optional[KeyExtractor], request: _IngressRequest) -> Optional[str]:
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
        values = raw
    elif "secret" in verification:
        values = [verification["secret"]]
    else:
        raise ValueError("verification requires 'secret' or 'secrets'")
    return _coerce_secret_values(values)


def _coerce_secret_values(values: Any) -> tuple[bytes, ...]:
    if isinstance(values, (bytes, str)):
        iterable = [values]
    elif isinstance(values, (list, tuple)):
        iterable = list(values)
    else:
        raise TypeError("secrets must be a str, bytes, list, or tuple")
    secrets = tuple(_coerce_secret(value) for value in iterable)
    if not secrets:
        raise ValueError("verification requires at least one secret")
    return secrets


def _coerce_secret(value: Any) -> bytes:
    if isinstance(value, bytes):
        if not value:
            raise ValueError("verification secrets must be non-empty")
        return value
    if isinstance(value, str):
        if not value:
            raise ValueError("verification secrets must be non-empty")
        return value.encode("utf-8")
    raise TypeError("verification secrets must be str or bytes")


def _get_header(headers: dict[str, Any], name: str) -> Optional[str]:
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target:
            return str(value)
    return None


def _parse_stripe_signature(header_value: str) -> tuple[int, list[str]]:
    timestamp: Optional[int] = None
    signatures: list[str] = []
    for part in header_value.split(","):
        key, sep, value = part.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if key == "t":
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None:
        raise ValueError("stripe signature missing timestamp")
    if not signatures:
        raise ValueError("stripe signature missing v1 digest")
    return timestamp, signatures


def _json_string_field(body: bytes, field: str) -> Optional[str]:
    try:
        value = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    result = value.get(field)
    if result is None:
        return None
    return str(result)

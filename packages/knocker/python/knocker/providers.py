from __future__ import annotations

import json
from types import MappingProxyType
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class ProviderResult:
    """Combined verification outcome and extracted provider metadata.

    Provider implementations parse headers and body once and return a single
    result that carries both the signature verification outcome and any
    metadata they could extract. Extracted metadata is preserved on rejected
    results when the provider was able to read it before signature failure,
    so orphan deliveries remain useful for debugging.
    """

    valid: bool
    signature_error: Optional[str] = None
    provider_delivery_id: Optional[str] = None
    provider_event_id: Optional[str] = None
    event_type: Optional[str] = None

    @classmethod
    def accept(
        cls,
        *,
        provider_delivery_id: Optional[str] = None,
        provider_event_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> "ProviderResult":
        """Return a successful verification with optional extracted metadata."""

        return cls(
            valid=True,
            signature_error=None,
            provider_delivery_id=provider_delivery_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
        )

    @classmethod
    def reject(
        cls,
        error: str,
        *,
        provider_delivery_id: Optional[str] = None,
        provider_event_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> "ProviderResult":
        """Return a failed verification with a useful error and optional metadata."""

        if not isinstance(error, str) or not error:
            raise ValueError("ProviderResult.reject requires a non-empty error string")
        return cls(
            valid=False,
            signature_error=error,
            provider_delivery_id=provider_delivery_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class ProviderRequest:
    """Inbound request view passed to ``Provider.verify(...)``.

    Exposes ``method``, ``headers``, ``query``, and ``body`` as attributes plus
    case-insensitive ``header(name, default=None)`` and ``json()`` helpers.
    ``json()`` raises ``ValueError`` on non-JSON bodies and caches its parsed
    result. ``headers`` and ``query`` are read-only views so provider code
    cannot mutate the stored raw receipt by accident. Provider authors should
    treat ``json()`` as opt-in and guard with try/except for endpoints that may
    receive non-JSON payloads.
    """

    __slots__ = ("method", "headers", "query", "body", "_json_value", "_json_done")

    def __init__(
        self,
        *,
        method: str,
        headers: dict[str, Any],
        query: dict[str, Any],
        body: bytes,
    ) -> None:
        self.method = method
        self.headers = MappingProxyType(dict(headers))
        self.query = MappingProxyType(dict(query))
        self.body = body
        self._json_value: Any = None
        self._json_done: bool = False

    def header(self, name: str, default: Optional[str] = None) -> Optional[str]:
        """Return the first matching header value with case-insensitive lookup."""

        target = name.lower()
        for key, value in self.headers.items():
            if str(key).lower() == target:
                return str(value)
        return default

    def json(self) -> Any:
        """Return the parsed JSON body or raise ``ValueError`` on non-JSON input.

        The result is cached on first successful call.
        """

        if not self._json_done:
            try:
                self._json_value = json.loads(self.body)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError(f"request body is not JSON: {exc}") from exc
            self._json_done = True
        return self._json_value


class Provider:
    """Base class for Knocker providers.

    Subclasses must set ``name`` (lowercase identifier) and ``version``
    (implementation SemVer) and override ``verify(...)``. Set
    ``option_keys`` to declare accepted ``provider_options`` keys; unknown
    keys are rejected at endpoint registration time. Set
    ``requires_secrets = False`` for providers that do not need configured
    secrets (extraction-only or public-key providers).
    """

    name: str = ""
    version: str = ""
    option_keys: frozenset[str] = frozenset()
    requires_secrets: bool = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        """Verify ``request`` and return a ``ProviderResult``.

        Implementations should extract delivery id, event id, and event type
        before failing verification, so invalid receipts still produce useful
        orphan delivery rows.
        """

        raise NotImplementedError

    def validate_options(self, options: dict[str, Any]) -> None:
        """Reject unknown option keys against ``option_keys``.

        Override to also validate option values. Called at endpoint
        registration time before any request is processed.
        """

        if not isinstance(options, dict):
            raise TypeError("provider_options must be a dict")
        unknown = set(options) - set(self.option_keys)
        if unknown:
            raise ValueError(
                f"unknown provider options for {self.name!r}: {sorted(unknown)}"
            )


def _builtin_providers() -> tuple[Provider, ...]:
    """Return fresh built-in provider instances per ``Knocker`` instance.

    Imports happen here to keep the public providers module free of
    built-in implementation detail. Built-in modules (``_builtin_stripe``,
    ``_builtin_github``) live as siblings of this file and may be imported
    directly by repo-internal conformance tooling.
    """

    from knocker._builtin_stripe import _StripeProvider
    from knocker._builtin_github import _GitHubProvider
    from knocker._builtin_shopify import _ShopifyProvider
    from knocker._builtin_slack import _SlackProvider
    from knocker._builtin_postmark import _PostmarkProvider
    from knocker._builtin_resend import _ResendProvider
    from knocker._builtin_paddle import _PaddleProvider
    from knocker._builtin_lemonsqueezy import _LemonSqueezyProvider

    return (
        _StripeProvider(),
        _GitHubProvider(),
        _ShopifyProvider(),
        _SlackProvider(),
        _PostmarkProvider(),
        _ResendProvider(),
        _PaddleProvider(),
        _LemonSqueezyProvider(),
    )


def _builtin_provider_names() -> frozenset[str]:
    """Reserved built-in provider names that cannot be overridden."""

    return frozenset(
        {
            "stripe",
            "github",
            "shopify",
            "slack",
            "postmark",
            "resend",
            "paddle",
            "lemon-squeezy",
        }
    )


def _coerce_secrets(values: Any) -> tuple[bytes, ...]:
    """Coerce a secrets argument into a non-empty tuple of bytes.

    Accepts ``str``, ``bytes``, list, or tuple. Raises ``ValueError`` on
    missing/empty inputs and ``TypeError`` on the wrong shape.
    """

    if values is None:
        raise ValueError("provider requires at least one secret")
    if isinstance(values, (bytes, str)):
        iterable: list[Any] = [values]
    elif isinstance(values, (list, tuple)):
        iterable = list(values)
    else:
        raise TypeError("secrets must be a str, bytes, list, or tuple")
    if not iterable:
        raise ValueError("provider requires at least one secret")
    return tuple(_coerce_secret(value) for value in iterable)


def _coerce_secret(value: Any) -> bytes:
    if isinstance(value, bytes):
        if not value:
            raise ValueError("provider secrets must be non-empty")
        return value
    if isinstance(value, str):
        if not value:
            raise ValueError("provider secrets must be non-empty")
        return value.encode("utf-8")
    raise TypeError("provider secrets must be str or bytes")


def _coerce_provider_options(value: Any, provider: Provider) -> dict[str, Any]:
    """Build a validated provider options dict for ``provider``."""

    if value is None:
        options: dict[str, Any] = {}
    elif isinstance(value, dict):
        options = dict(value)
    else:
        raise TypeError("provider_options must be a dict")
    provider.validate_options(options)
    return options


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
    value = _json_object(body)
    if not isinstance(value, dict):
        return None
    result = value.get(field)
    if result is None:
        return None
    return str(result)


def _json_object(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (TypeError, json.JSONDecodeError):
        return None


def _json_string_path(body: bytes, *path: str) -> Optional[str]:
    value = _json_object(body)
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    if value is None:
        return None
    return str(value)


def _first_non_empty(*values: Optional[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None

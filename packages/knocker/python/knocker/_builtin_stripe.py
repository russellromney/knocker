from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Callable, Optional

from knocker.providers import (
    Provider,
    ProviderRequest,
    ProviderResult,
    _json_string_field,
    _parse_stripe_signature,
)


class _StripeProvider(Provider):
    """Built-in Stripe webhook provider.

    Verifies the ``Stripe-Signature`` header with optional secret rotation
    and a configurable timestamp tolerance. Extracts the upstream event id
    and event type from the JSON body when present.

    The optional ``clock`` constructor argument is an internal seam used by
    the conformance fixture loader to evaluate timestamp tolerance against
    a fixed point in time. Production usage relies on the default
    wall-clock implementation; the seam is not part of the public Provider
    contract.
    """

    name = "stripe"
    version = "1.0.0"
    option_keys = frozenset({"tolerance_s"})
    requires_secrets = True

    def __init__(self, *, clock: Optional[Callable[[], int]] = None) -> None:
        self._clock = clock if clock is not None else (lambda: int(time.time()))

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        if "tolerance_s" in options:
            value = options["tolerance_s"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    "stripe provider option 'tolerance_s' must be an integer"
                )
            if value < 0:
                raise ValueError(
                    "stripe provider option 'tolerance_s' must be non-negative"
                )

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        tolerance_s = int(options.get("tolerance_s", 300))
        provider_event_id = _json_string_field(request.body, "id")
        event_type = _json_string_field(request.body, "type")
        header_value = request.header("stripe-signature")
        if header_value is None:
            return ProviderResult.reject(
                "missing signature header: stripe-signature",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        try:
            timestamp, signatures = _parse_stripe_signature(header_value)
        except ValueError as exc:
            return ProviderResult.reject(
                str(exc),
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if abs(self._clock() - timestamp) > tolerance_s:
            return ProviderResult.reject(
                "stripe signature timestamp outside tolerance",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed_payload = f"{timestamp}.".encode("utf-8") + request.body
        for secret in secrets:
            expected = hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()
            for candidate in signatures:
                if hmac.compare_digest(candidate, expected):
                    return ProviderResult.accept(
                        provider_event_id=provider_event_id,
                        event_type=event_type,
                    )
        return ProviderResult.reject(
            "stripe signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )

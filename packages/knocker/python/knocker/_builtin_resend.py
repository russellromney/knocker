from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any, Callable, Optional

from knocker.providers import (
    Provider,
    ProviderRequest,
    ProviderResult,
    _json_string_path,
)


class _ResendProvider(Provider):
    """Resend webhook verification via the Svix signature scheme."""

    name = "resend"
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
                    "resend provider option 'tolerance_s' must be an integer"
                )
            if value < 0:
                raise ValueError(
                    "resend provider option 'tolerance_s' must be non-negative"
                )

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        tolerance_s = int(options.get("tolerance_s", 300))
        delivery_id = request.header("svix-id")
        provider_event_id = _json_string_path(request.body, "data", "email_id")
        event_type = _json_string_path(request.body, "type")
        timestamp = request.header("svix-timestamp")
        signature = request.header("svix-signature")
        if delivery_id is None:
            return ProviderResult.reject(
                "missing signature id header: svix-id",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if timestamp is None:
            return ProviderResult.reject(
                "missing signature timestamp header: svix-timestamp",
                provider_delivery_id=delivery_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: svix-signature",
                provider_delivery_id=delivery_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        try:
            ts = int(timestamp)
        except ValueError:
            return ProviderResult.reject(
                "resend signature timestamp must be an integer",
                provider_delivery_id=delivery_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if abs(self._clock() - ts) > tolerance_s:
            return ProviderResult.reject(
                "resend signature timestamp outside tolerance",
                provider_delivery_id=delivery_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed_payload = f"{delivery_id}.{timestamp}.".encode("utf-8") + request.body
        candidates = [part.strip() for part in signature.split() if part.strip()]
        for secret in secrets:
            secret_bytes = _svix_secret_bytes(secret)
            if secret_bytes is None:
                continue
            expected = base64.b64encode(
                hmac.new(secret_bytes, signed_payload, hashlib.sha256).digest()
            ).decode("utf-8")
            for candidate in candidates:
                version, _, digest = candidate.partition(",")
                if version == "v1" and digest and hmac.compare_digest(digest, expected):
                    return ProviderResult.accept(
                        provider_delivery_id=delivery_id,
                        provider_event_id=provider_event_id,
                        event_type=event_type,
                    )
        return ProviderResult.reject(
            "resend signature mismatch",
            provider_delivery_id=delivery_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


def _svix_secret_bytes(secret: bytes) -> Optional[bytes]:
    prefix = b"whsec_"
    if not secret.startswith(prefix):
        return secret
    encoded = secret[len(prefix):]
    for candidate in (
        encoded,
        encoded + b"=" * (-len(encoded) % 4),
    ):
        try:
            return base64.b64decode(candidate, validate=True)
        except ValueError:
            continue
    return None

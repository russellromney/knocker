from __future__ import annotations

import hashlib
import hmac
from typing import Any

from knocker.providers import (
    Provider,
    ProviderRequest,
    ProviderResult,
    _first_non_empty,
    _json_string_path,
)


class _LemonSqueezyProvider(Provider):
    """Lemon Squeezy signed-request verification."""

    name = "lemon-squeezy"
    version = "1.0.0"
    option_keys = frozenset()
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        event_type = _first_non_empty(
            request.header("x-event-name"),
            _json_string_path(request.body, "meta", "event_name"),
        )
        provider_event_id = _first_non_empty(
            _json_string_path(request.body, "data", "attributes", "identifier"),
            _json_string_path(request.body, "data", "id"),
        )
        signature = request.header("x-signature")
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-signature",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        for secret in secrets:
            expected = hmac.new(secret, request.body, hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected):
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "lemon-squeezy signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )

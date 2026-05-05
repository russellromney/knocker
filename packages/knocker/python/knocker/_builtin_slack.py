from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any, Callable, Optional

from knocker.providers import (
    Provider,
    ProviderRequest,
    ProviderResult,
    _first_non_empty,
    _json_string_path,
)


class _SlackProvider(Provider):
    """Slack signed-secret verification.

    Verifies `X-Slack-Signature` against the `v0:{timestamp}:{body}` base
    string using HMAC-SHA256. Extracts `event_id` when present and falls back
    to the top-level/nested event type from the JSON body.
    """

    name = "slack"
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
                    "slack provider option 'tolerance_s' must be an integer"
                )
            if value < 0:
                raise ValueError(
                    "slack provider option 'tolerance_s' must be non-negative"
                )

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        tolerance_s = int(options.get("tolerance_s", 300))
        provider_event_id = _json_string_path(request.body, "event_id")
        event_type = _first_non_empty(
            _json_string_path(request.body, "event", "type"),
            _json_string_path(request.body, "type"),
        )
        timestamp = request.header("x-slack-request-timestamp")
        if timestamp is None:
            return ProviderResult.reject(
                "missing signature timestamp header: x-slack-request-timestamp",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signature = request.header("x-slack-signature")
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-slack-signature",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        try:
            ts = int(timestamp)
        except ValueError:
            return ProviderResult.reject(
                "slack signature timestamp must be an integer",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if abs(self._clock() - ts) > tolerance_s:
            return ProviderResult.reject(
                "slack signature timestamp outside tolerance",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed_payload = f"v0:{ts}:".encode("utf-8") + request.body
        for secret in secrets:
            expected = "v0=" + hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()
            if hmac.compare_digest(signature, expected):
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "slack signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )

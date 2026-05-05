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


class _PaddleProvider(Provider):
    """Paddle webhook signature verification."""

    name = "paddle"
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
                    "paddle provider option 'tolerance_s' must be an integer"
                )
            if value < 0:
                raise ValueError(
                    "paddle provider option 'tolerance_s' must be non-negative"
                )

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        tolerance_s = int(options.get("tolerance_s", 5))
        provider_event_id = _json_string_path(request.body, "event_id")
        event_type = _first_non_empty(
            _json_string_path(request.body, "event_type"),
            _json_string_path(request.body, "event", "type"),
        )
        header = request.header("paddle-signature")
        if header is None:
            return ProviderResult.reject(
                "missing signature header: paddle-signature",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        timestamp = None
        signatures: list[str] = []
        for part in header.split(";"):
            key, sep, value = part.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if key == "ts":
                try:
                    timestamp = int(value)
                except ValueError:
                    return ProviderResult.reject(
                        "paddle signature timestamp must be an integer",
                        provider_event_id=provider_event_id,
                        event_type=event_type,
                    )
            elif key == "h1" and value:
                signatures.append(value)
        if timestamp is None:
            return ProviderResult.reject(
                "paddle signature missing timestamp",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if not signatures:
            return ProviderResult.reject(
                "paddle signature missing h1 digest",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if abs(self._clock() - timestamp) > tolerance_s:
            return ProviderResult.reject(
                "paddle signature timestamp outside tolerance",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed_payload = f"{timestamp}:".encode("utf-8") + request.body
        for secret in secrets:
            expected = hmac.new(secret, signed_payload, hashlib.sha256).hexdigest()
            for candidate in signatures:
                if hmac.compare_digest(candidate, expected):
                    return ProviderResult.accept(
                        provider_event_id=provider_event_id,
                        event_type=event_type,
                    )
        return ProviderResult.reject(
            "paddle signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )

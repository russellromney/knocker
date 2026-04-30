from __future__ import annotations

import hashlib
import hmac
from typing import Any

from knocker.providers import Provider, ProviderRequest, ProviderResult


class _GitHubProvider(Provider):
    """Built-in GitHub webhook provider.

    Verifies the ``X-Hub-Signature-256`` header as ``sha256=<hex hmac>`` over
    the raw body. Extracts ``X-GitHub-Delivery`` and ``X-GitHub-Event`` and
    uses the delivery id as Knocker's dedupe identity, which is stable across
    operator-initiated redeliveries from the GitHub dashboard.
    """

    name = "github"
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
        delivery_id = request.header("x-github-delivery")
        event_type = request.header("x-github-event")
        if not delivery_id:
            return ProviderResult.reject(
                "missing delivery header: x-github-delivery",
                event_type=event_type,
            )
        signature = request.header("x-hub-signature-256")
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-hub-signature-256",
                provider_delivery_id=delivery_id,
                event_type=event_type,
            )
        if not signature.startswith("sha256="):
            return ProviderResult.reject(
                "github signature must use 'sha256=<hex>' format",
                provider_delivery_id=delivery_id,
                event_type=event_type,
            )
        for secret in secrets:
            digest = hmac.new(secret, request.body, hashlib.sha256).hexdigest()
            expected = f"sha256={digest}"
            if hmac.compare_digest(signature, expected):
                return ProviderResult.accept(
                    provider_delivery_id=delivery_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "github signature mismatch",
            provider_delivery_id=delivery_id,
            event_type=event_type,
        )

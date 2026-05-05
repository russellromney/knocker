from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from knocker.providers import Provider, ProviderRequest, ProviderResult


class _ShopifyProvider(Provider):
    """Shopify HTTPS webhook verification.

    Verifies `X-Shopify-Hmac-Sha256` as a base64-encoded HMAC-SHA256 over the
    raw body. Extracts the delivery identity from `X-Shopify-Webhook-Id`, the
    upstream event identity from `X-Shopify-Event-Id`, and the topic from
    `X-Shopify-Topic`.
    """

    name = "shopify"
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
        delivery_id = request.header("x-shopify-webhook-id")
        provider_event_id = request.header("x-shopify-event-id")
        event_type = request.header("x-shopify-topic")
        signature = request.header("x-shopify-hmac-sha256")
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-shopify-hmac-sha256",
                provider_delivery_id=delivery_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        for secret in secrets:
            expected = base64.b64encode(
                hmac.new(secret, request.body, hashlib.sha256).digest()
            ).decode("utf-8")
            if hmac.compare_digest(signature, expected):
                return ProviderResult.accept(
                    provider_delivery_id=delivery_id,
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "shopify signature mismatch",
            provider_delivery_id=delivery_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
        )

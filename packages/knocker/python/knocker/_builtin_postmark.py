from __future__ import annotations

import base64
import hmac
from typing import Any

from knocker.providers import (
    Provider,
    ProviderRequest,
    ProviderResult,
    _json_string_field,
)


class _PostmarkProvider(Provider):
    """Postmark webhook verification via HTTP Basic Auth.

    Postmark does not provide a built-in signature scheme for webhook payloads;
    their documented protection options are Basic Auth and custom headers. This
    curated provider pins the Basic Auth path. Secrets are configured as raw
    `username:password` credential strings and matched against the inbound
    `Authorization: Basic ...` header.
    """

    name = "postmark"
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
        provider_event_id = _json_string_field(request.body, "MessageID")
        event_type = _json_string_field(request.body, "RecordType")
        auth = request.header("authorization")
        if auth is None:
            return ProviderResult.reject(
                "missing authorization header",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        for secret in secrets:
            expected = "Basic " + base64.b64encode(secret).decode("utf-8")
            if hmac.compare_digest(auth, expected):
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "postmark basic auth mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )

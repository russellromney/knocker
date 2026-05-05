from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any, Callable, Optional
from urllib.parse import parse_qsl

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from knocker.providers import (
    Provider,
    ProviderRequest,
    ProviderResult,
    _first_non_empty,
    _json_string_field,
    _json_string_path,
)


class _ToleranceProvider(Provider):
    option_keys = frozenset({"tolerance_s"})

    def __init__(self, *, clock: Optional[Callable[[], int]] = None) -> None:
        self._clock = clock if clock is not None else (lambda: int(time.time()))

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        if "tolerance_s" in options:
            value = options["tolerance_s"]
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(
                    f"{self.name} provider option 'tolerance_s' must be an integer"
                )
            if value < 0:
                raise ValueError(
                    f"{self.name} provider option 'tolerance_s' must be non-negative"
                )

    def _outside_tolerance(self, timestamp: int, options: dict[str, Any], default: int) -> bool:
        return abs(self._clock() - timestamp) > int(options.get("tolerance_s", default))


class _StandardWebhooksProvider(_ToleranceProvider):
    name = "standard-webhooks"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        provider_event_id = _first_non_empty(
            _json_string_field(request.body, "id"),
            _json_string_path(request.body, "data", "id"),
        )
        event_type = _json_string_field(request.body, "type")
        delivery_id = _first_non_empty(
            request.header("webhook-id"),
            request.header("webhooks-id"),
            request.header("svix-id"),
        )
        timestamp = _first_non_empty(
            request.header("webhook-timestamp"),
            request.header("webhooks-timestamp"),
            request.header("svix-timestamp"),
        )
        signature = _first_non_empty(
            request.header("webhook-signature"),
            request.header("webhooks-signature"),
            request.header("svix-signature"),
        )
        if delivery_id is None:
            return ProviderResult.reject(
                f"{self.name} missing webhook id",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if timestamp is None:
            return ProviderResult.reject(
                f"{self.name} missing webhook timestamp",
                provider_delivery_id=delivery_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        try:
            ts = int(timestamp)
        except ValueError:
            ts = -1
        if ts < 0 or self._outside_tolerance(ts, options, 300):
            return ProviderResult.reject(
                f"{self.name} timestamp outside tolerance",
                provider_delivery_id=delivery_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if signature is None:
            return ProviderResult.reject(
                f"{self.name} missing webhook signature",
                provider_delivery_id=delivery_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed_payload = f"{delivery_id}.{timestamp}.".encode("utf-8") + request.body
        candidates = [
            part[3:].strip()
            for part in signature.split()
            if part.startswith(("v1,", "v1=")) and part[3:].strip()
        ]
        for secret in secrets:
            secret_bytes = _svix_secret_bytes(secret)
            if secret_bytes is None:
                continue
            expected = base64.b64encode(
                hmac.new(secret_bytes, signed_payload, hashlib.sha256).digest()
            ).decode("utf-8")
            if any(hmac.compare_digest(candidate, expected) for candidate in candidates):
                return ProviderResult.accept(
                    provider_delivery_id=delivery_id,
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            f"invalid {self.name} signature",
            provider_delivery_id=delivery_id,
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _ClerkProvider(_StandardWebhooksProvider):
    name = "clerk"


class _TwilioProvider(Provider):
    name = "twilio"
    version = "1.0.0"
    option_keys = frozenset({"url"})
    requires_secrets = True

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        if not isinstance(options.get("url"), str) or not options["url"]:
            raise ValueError("twilio provider option 'url' is required")

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        form = dict(parse_qsl(request.body.decode("utf-8", errors="replace")))
        provider_event_id = _first_non_empty(
            form.get("CallSid"),
            form.get("MessageSid"),
            form.get("SmsSid"),
        )
        event_type = _first_non_empty(
            _json_string_field(request.body, "EventType"),
            form.get("CallStatus"),
            form.get("SmsStatus"),
        )
        signature = request.header("x-twilio-signature")
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-twilio-signature",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed = str(options["url"]) + "".join(
            key + value for key, value in sorted(form.items())
        )
        for secret in secrets:
            expected = base64.b64encode(
                hmac.new(secret, signed.encode("utf-8"), hashlib.sha1).digest()
            ).decode("utf-8")
            if hmac.compare_digest(signature, expected):
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "twilio signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _SendGridProvider(_ToleranceProvider):
    name = "sendgrid"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        first = request.json()[0] if isinstance(request.json(), list) and request.json() else {}
        provider_event_id = _first_non_empty(
            str(first.get("sg_event_id")) if first.get("sg_event_id") is not None else None,
            str(first.get("sg_message_id")) if first.get("sg_message_id") is not None else None,
        )
        event_type = str(first.get("event")) if first.get("event") is not None else None
        timestamp = request.header("x-twilio-email-event-webhook-timestamp")
        signature = request.header("x-twilio-email-event-webhook-signature")
        if timestamp is None:
            return ProviderResult.reject(
                "missing signature timestamp header: x-twilio-email-event-webhook-timestamp",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        try:
            ts = int(timestamp)
        except ValueError:
            ts = -1
        if ts < 0 or self._outside_tolerance(ts, options, 300):
            return ProviderResult.reject(
                "sendgrid signature timestamp outside tolerance",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-twilio-email-event-webhook-signature",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed = timestamp.encode("utf-8") + request.body
        try:
            signature_bytes = base64.b64decode(signature)
        except ValueError:
            signature_bytes = b""
        for secret in secrets:
            try:
                key = serialization.load_pem_public_key(secret)
                if not isinstance(key, ec.EllipticCurvePublicKey):
                    continue
                key.verify(signature_bytes, signed, ec.ECDSA(hashes.SHA256()))
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
            except (ValueError, TypeError, InvalidSignature):
                continue
        return ProviderResult.reject(
            "sendgrid signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _LinearProvider(_ToleranceProvider):
    name = "linear"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        provider_event_id = _json_string_path(request.body, "data", "id")
        event_type = _first_non_empty(
            _json_string_field(request.body, "action"),
            _json_string_field(request.body, "type"),
        )
        timestamp_ms = request.json().get("webhookTimestamp") if isinstance(request.json(), dict) else None
        if isinstance(timestamp_ms, int) and self._outside_tolerance(timestamp_ms // 1000, options, 60):
            return ProviderResult.reject(
                "linear signature timestamp outside tolerance",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signature = request.header("linear-signature")
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: linear-signature",
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
            "linear signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _MetaProvider(Provider):
    name = "meta"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        del options
        event_type = _json_string_field(request.body, "object")
        signature = request.header("x-hub-signature-256")
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-hub-signature-256",
                event_type=event_type,
            )
        _, sep, digest = signature.partition("=")
        if sep != "=" or _ != "sha256":
            return ProviderResult.reject(
                "meta signature must use sha256=<hex>",
                event_type=event_type,
            )
        for secret in secrets:
            expected = hmac.new(secret, request.body, hashlib.sha256).hexdigest()
            if hmac.compare_digest(digest, expected):
                return ProviderResult.accept(event_type=event_type)
        return ProviderResult.reject("meta signature mismatch", event_type=event_type)


class _DiscordProvider(Provider):
    name = "discord"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        del options
        provider_event_id = _json_string_field(request.body, "id")
        event_type = _json_string_field(request.body, "type")
        timestamp = request.header("x-signature-timestamp")
        signature = request.header("x-signature-ed25519")
        if timestamp is None:
            return ProviderResult.reject(
                "missing signature timestamp header: x-signature-timestamp",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-signature-ed25519",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed = timestamp.encode("utf-8") + request.body
        for secret in secrets:
            try:
                key = ed25519.Ed25519PublicKey.from_public_bytes(bytes.fromhex(secret.decode()))
                key.verify(bytes.fromhex(signature), signed)
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
            except (ValueError, InvalidSignature):
                continue
        return ProviderResult.reject(
            "discord signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _ZendeskProvider(_ToleranceProvider):
    name = "zendesk"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        provider_event_id = _first_non_empty(
            _json_string_field(request.body, "ticket_id"),
            _json_string_field(request.body, "id"),
        )
        event_type = _first_non_empty(
            _json_string_field(request.body, "type"),
            request.header("x-zendesk-webhook-invocation-event"),
        )
        timestamp = request.header("x-zendesk-webhook-signature-timestamp")
        signature = request.header("x-zendesk-webhook-signature")
        if timestamp is None:
            return ProviderResult.reject(
                "missing signature timestamp header: x-zendesk-webhook-signature-timestamp",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        try:
            ts = int(timestamp)
        except ValueError:
            ts = self._clock()
        if self._outside_tolerance(ts, options, 300):
            return ProviderResult.reject(
                "zendesk signature timestamp outside tolerance",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-zendesk-webhook-signature",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        signed = timestamp.encode("utf-8") + request.body
        for secret in secrets:
            expected = base64.b64encode(
                hmac.new(secret, signed, hashlib.sha256).digest()
            ).decode("utf-8")
            if hmac.compare_digest(signature, expected):
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "zendesk signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _IntercomProvider(Provider):
    name = "intercom"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        del options
        provider_event_id = _json_string_field(request.body, "id")
        event_type = _first_non_empty(
            _json_string_field(request.body, "topic"),
            _json_string_field(request.body, "type"),
        )
        signature = request.header("x-hub-signature")
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-hub-signature",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        prefix = "sha1="
        if signature.startswith(prefix):
            digest = signature[len(prefix):]
        else:
            digest = signature
        if not digest:
            return ProviderResult.reject(
                "intercom signature must use sha1=<hex>",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        for secret in secrets:
            expected = hmac.new(secret, request.body, hashlib.sha1).hexdigest()
            if hmac.compare_digest(digest, expected):
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "intercom signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _HubSpotProvider(_ToleranceProvider):
    name = "hubspot"
    version = "1.0.0"
    option_keys = frozenset({"url", "method", "tolerance_s"})
    requires_secrets = True

    def validate_options(self, options: dict[str, Any]) -> None:
        super().validate_options(options)
        if not isinstance(options.get("url"), str) or not options["url"]:
            raise ValueError("hubspot provider option 'url' is required")
        if "method" in options and not isinstance(options["method"], str):
            raise TypeError("hubspot provider option 'method' must be a string")

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        body = request.json()
        first = body[0] if isinstance(body, list) and body else {}
        provider_event_id = _first_non_empty(
            str(first.get("eventId")) if first.get("eventId") is not None else None,
            str(first.get("objectId")) if first.get("objectId") is not None else None,
        )
        event_type = (
            str(first.get("subscriptionType"))
            if first.get("subscriptionType") is not None
            else None
        )
        timestamp = request.header("x-hubspot-request-timestamp")
        signature = request.header("x-hubspot-signature-v3")
        if timestamp is None:
            return ProviderResult.reject(
                "missing signature timestamp header: x-hubspot-request-timestamp",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        try:
            timestamp_ms = int(timestamp)
        except ValueError:
            timestamp_ms = -1
        if timestamp_ms < 0 or self._outside_tolerance(timestamp_ms // 1000, options, 300):
            return ProviderResult.reject(
                "hubspot signature timestamp outside tolerance",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if signature is None:
            return ProviderResult.reject(
                "missing signature header: x-hubspot-signature-v3",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        method = str(options.get("method", request.method or "POST")).upper()
        signed = (
            method.encode("utf-8")
            + str(options["url"]).encode("utf-8")
            + request.body
            + timestamp.encode("utf-8")
        )
        for secret in secrets:
            expected = base64.b64encode(
                hmac.new(secret, signed, hashlib.sha256).digest()
            ).decode("utf-8")
            if hmac.compare_digest(signature, expected):
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "hubspot signature mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _TokenHeaderProvider(Provider):
    name = "token-header"
    version = "1.0.0"
    option_keys = frozenset({"header"})
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        provider_event_id = _json_string_field(request.body, "id")
        event_type = _json_string_field(request.body, "type")
        header_name = str(options.get("header", "x-knocker-token"))
        value = request.header(header_name)
        if value is None:
            return ProviderResult.reject(
                f"missing token header: {header_name}",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if any(hmac.compare_digest(secret.decode(), value) for secret in secrets):
            return ProviderResult.accept(
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        return ProviderResult.reject(
            "token header mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _BearerTokenProvider(Provider):
    name = "bearer-token"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        del options
        provider_event_id = _json_string_field(request.body, "id")
        event_type = _json_string_field(request.body, "type")
        value = request.header("authorization")
        if value is None:
            return ProviderResult.reject(
                "missing authorization header",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        prefix = "Bearer "
        if not value.startswith(prefix):
            return ProviderResult.reject(
                "authorization header must use bearer scheme",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        token = value[len(prefix):]
        if any(hmac.compare_digest(secret.decode(), token) for secret in secrets):
            return ProviderResult.accept(
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        return ProviderResult.reject(
            "bearer token mismatch",
            provider_event_id=provider_event_id,
            event_type=event_type,
        )


class _BasicAuthProvider(Provider):
    name = "basic-auth"
    version = "1.0.0"
    requires_secrets = True

    def verify(
        self,
        request: ProviderRequest,
        *,
        secrets: tuple[bytes, ...],
        options: dict[str, Any],
    ) -> ProviderResult:
        del options
        provider_event_id = _json_string_field(request.body, "id")
        event_type = _json_string_field(request.body, "type")
        value = request.header("authorization")
        if value is None:
            return ProviderResult.reject(
                "missing authorization header",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        if not value.startswith("Basic "):
            return ProviderResult.reject(
                "authorization header must use basic scheme",
                provider_event_id=provider_event_id,
                event_type=event_type,
            )
        for secret in secrets:
            expected = "Basic " + base64.b64encode(secret).decode("utf-8")
            if hmac.compare_digest(expected, value):
                return ProviderResult.accept(
                    provider_event_id=provider_event_id,
                    event_type=event_type,
                )
        return ProviderResult.reject(
            "basic auth mismatch",
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

import asyncio
import json
import sqlite3
import time

import knocker
import pytest

from tests.helpers import (
    generic_hmac_signature as _generic_hmac_signature,
    require_event_id as _require_event_id,
    stripe_signature as _stripe_signature,
    wait_for_status as _wait_for_status,
)


async def test_receive_valid_generic_hmac_is_stored_and_enqueued(db_path):
    app = knocker.open(db_path)
    body = b'{"ok":true}'
    app.add_endpoint(
        name="generic",
        path="/webhooks/generic",
        provider="generic",
        verification={
            "kind": "hmac-sha256",
            "secret": "topsecret",
            "header": "x-signature",
        },
    )

    result = app.receive(
        endpoint="generic",
        body=body,
        headers={"X-Signature": _generic_hmac_signature("topsecret", body)},
        provider_delivery_id="delivery-1",
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    delivery = app.get_delivery(result.delivery_id)
    assert result.status_code == 204
    assert delivery.signature_valid is True
    assert delivery.signature_error is None
    assert delivery.event_id == event_id
    assert event.status == "received"
    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert rows[0]["c"] == 1

async def test_receive_invalid_generic_hmac_creates_orphan_delivery(db_path):
    app = knocker.open(db_path)
    body = b'{"ok":false}'
    app.add_endpoint(
        name="generic",
        path="/webhooks/generic",
        provider="generic",
        verification={
            "kind": "hmac-sha256",
            "secret": "topsecret",
            "header": "x-signature",
        },
    )

    result = app.receive(
        endpoint="generic",
        body=body,
        headers={"X-Signature": _generic_hmac_signature("wrongsecret", body)},
        provider_delivery_id="delivery-2",
    )

    delivery = app.get_delivery(result.delivery_id)
    assert result.status_code == 401
    assert result.event_id is None
    assert delivery.event_id is None
    assert delivery.signature_valid is False
    assert "signature mismatch" in (delivery.signature_error or "")
    assert app.list_events() == []
    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert rows[0]["c"] == 0

async def test_receive_valid_stripe_signature_is_stored_and_enqueued(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_1","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": "whsec_test"},
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body)},
        provider_event_id="evt_1",
        provider_delivery_id="delivery-stripe-1",
        event_type="checkout.session.completed",
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    delivery = app.get_delivery(result.delivery_id)
    assert result.status_code == 204
    assert delivery.signature_valid is True
    assert event.status == "received"
    rows = app.db.query("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?", [app.queue.name])
    assert rows[0]["c"] == 1

async def test_receive_invalid_stripe_signature_creates_orphan_delivery(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_2","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": "whsec_test"},
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_wrong", body)},
        provider_event_id="evt_2",
        provider_delivery_id="delivery-stripe-2",
        event_type="checkout.session.completed",
    )

    delivery = app.get_delivery(result.delivery_id)
    assert result.status_code == 401
    assert result.event_id is None
    assert delivery.event_id is None
    assert delivery.signature_valid is False
    assert "stripe signature mismatch" in (delivery.signature_error or "")
    assert app.list_events() == []

async def test_secret_rotation_accepts_overlapping_stripe_secrets(db_path):
    app = knocker.open(db_path)
    old_secret = "whsec_old"
    new_secret = "whsec_new"
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secrets": [old_secret, new_secret]},
    )

    old_body = b'{"id":"evt_old","type":"checkout.session.completed"}'
    old_result = app.receive(
        endpoint="stripe",
        body=old_body,
        headers={"Stripe-Signature": _stripe_signature(old_secret, old_body)},
        provider_event_id="evt_old",
        provider_delivery_id="delivery-old",
    )
    new_body = b'{"id":"evt_new","type":"checkout.session.completed"}'
    new_result = app.receive(
        endpoint="stripe",
        body=new_body,
        headers={"Stripe-Signature": _stripe_signature(new_secret, new_body)},
        provider_event_id="evt_new",
        provider_delivery_id="delivery-new",
    )

    old_event = app.get_event(_require_event_id(old_result))
    new_event = app.get_event(_require_event_id(new_result))
    assert app.get_delivery(old_result.delivery_id).signature_valid is True
    assert app.get_delivery(new_result.delivery_id).signature_valid is True
    assert old_event.status == "received"
    assert new_event.status == "received"

async def test_stripe_verifier_rejects_expired_timestamps(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt-expired","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": "whsec_test", "tolerance_s": 300},
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body, timestamp=int(time.time()) - 601)},
        provider_event_id="evt-expired",
    )

    delivery = app.get_delivery(result.delivery_id)
    assert result.event_id is None
    assert result.status_code == 401
    assert delivery.signature_valid is False
    assert "outside tolerance" in (delivery.signature_error or "")

async def test_stripe_verifier_rejects_negative_tolerance_config(db_path):
    app = knocker.open(db_path)
    with pytest.raises(ValueError, match="must be non-negative"):
        app.add_endpoint(
            name="stripe",
            path="/webhooks/stripe",
            provider="stripe",
            verification={"kind": "stripe", "secret": "whsec_test", "tolerance_s": -1},
        )

async def test_secret_rotation_can_remove_old_secret(db_path):
    app = knocker.open(db_path)
    old_secret = "whsec_old"
    new_secret = "whsec_new"
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": new_secret},
    )

    old_body = b'{"id":"evt_old_removed","type":"checkout.session.completed"}'
    old_result = app.receive(
        endpoint="stripe",
        body=old_body,
        headers={"Stripe-Signature": _stripe_signature(old_secret, old_body)},
        provider_event_id="evt_old_removed",
        provider_delivery_id="delivery-old-removed",
    )
    new_body = b'{"id":"evt_new_only","type":"checkout.session.completed"}'
    new_result = app.receive(
        endpoint="stripe",
        body=new_body,
        headers={"Stripe-Signature": _stripe_signature(new_secret, new_body)},
        provider_event_id="evt_new_only",
        provider_delivery_id="delivery-new-only",
    )

    old_delivery = app.get_delivery(old_result.delivery_id)
    new_delivery = app.get_delivery(new_result.delivery_id)
    new_event = app.get_event(_require_event_id(new_result))
    assert old_result.event_id is None
    assert old_delivery.signature_valid is False
    assert new_delivery.signature_valid is True
    assert new_event.status == "received"

async def test_invalid_first_valid_later_creates_two_deliveries_and_one_event(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_rotate","type":"checkout.session.completed"}'
    old_secret = "whsec_old"
    new_secret = "whsec_new"
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        verification={"kind": "stripe", "secret": new_secret},
    )

    first = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature(old_secret, body)},
        provider_event_id="evt_rotate",
        provider_delivery_id="delivery-rotate-1",
    )
    second = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature(new_secret, body)},
        provider_event_id="evt_rotate",
        provider_delivery_id="delivery-rotate-2",
    )

    event_id = _require_event_id(second)
    deliveries = app.list_deliveries()
    linked = app.list_deliveries(event_id=event_id)
    event = app.get_event(event_id)
    assert first.event_id is None
    assert second.duplicate is False
    assert len(deliveries) == 2
    assert [delivery.event_id for delivery in deliveries] == [event_id, None]
    assert [delivery.signature_valid for delivery in deliveries] == [True, False]
    assert len(linked) == 1
    assert event.provider_delivery_id == "delivery-rotate-2"

async def test_stripe_provider_preset_can_fill_verifier_and_extract_metadata(db_path):
    app = knocker.open(db_path)
    body = b'{"id":"evt_preset","type":"checkout.session.completed"}'
    app.add_endpoint(
        name="stripe",
        path="/webhooks/stripe",
        provider="stripe",
        secrets=["whsec_test"],
    )

    result = app.receive(
        endpoint="stripe",
        body=body,
        headers={"Stripe-Signature": _stripe_signature("whsec_test", body)},
    )

    event_id = _require_event_id(result)
    event = app.get_event(event_id)
    assert event.provider_event_id == "evt_preset"
    assert event.event_type == "checkout.session.completed"

async def test_github_provider_preset_extracts_delivery_key_for_dedupe(db_path):
    app = knocker.open(db_path)
    app.add_endpoint(
        name="github",
        path="/webhooks/github",
        provider="github",
    )

    first = app.receive(
        endpoint="github",
        body=b'{"zen":"keep it logically awesome"}',
        headers={
            "X-GitHub-Delivery": "delivery-123",
            "X-GitHub-Event": "push",
        },
    )
    second = app.receive(
        endpoint="github",
        body=b'{"zen":"keep it logically awesome"}',
        headers={
            "X-GitHub-Delivery": "delivery-123",
            "X-GitHub-Event": "push",
        },
    )

    event_id = _require_event_id(first)
    assert second.event_id == event_id
    assert second.duplicate is True
    event = app.get_event(event_id)
    deliveries = app.list_deliveries(event_id=event_id)
    assert event.event_type == "push"
    assert len(deliveries) == 2

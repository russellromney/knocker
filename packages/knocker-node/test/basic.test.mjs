import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { KnockerSqlite } from "../index.mjs";

function tempDbPath() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "knocker-node-"));
  return path.join(dir, "app.db");
}

test("node can bootstrap, register endpoint, and ingest", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({
    name: "stripe",
    path: "/webhooks/stripe",
    provider: "stripe"
  });

  const result = db.ingest({
    endpoint: "stripe",
    body: Buffer.from('{"id":"evt_1"}'),
    providerEventId: "evt_1",
    providerDeliveryId: "delivery_1",
    eventType: "checkout.session.completed"
  });

  assert.equal(result.duplicate, 0);
  assert.equal(result.status_code, 204);

  const event = db.getEvent(result.event_id);
  assert.equal(event.endpoint, "stripe");
  assert.equal(event.status, "received");
});

test("node sees dedupe through the shared contract", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({
    name: "github",
    path: "/webhooks/github",
    provider: "github"
  });

  const first = db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "delivery_123"
  });
  const second = db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "delivery_123"
  });

  assert.equal(first.duplicate, 0);
  assert.equal(second.duplicate, 1);
  assert.equal(second.event_id, first.event_id);
});

test("node can read delivery audit rows through the shared contract", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({
    name: "stripe",
    path: "/webhooks/stripe",
    provider: "stripe"
  });

  const result = db.ingest({
    endpoint: "stripe",
    body: Buffer.from('{"id":"evt_delivery","n":1}'),
    providerEventId: "evt_delivery",
    providerDeliveryId: "delivery_1",
    eventType: "checkout.session.completed"
  });

  const delivery = db.getDelivery(result.delivery_id);
  const deliveries = db.listDeliveriesForEvent(result.event_id);

  assert.equal(delivery.endpoint, "stripe");
  assert.equal(delivery.provider_delivery_id, "delivery_1");
  assert.equal(Buffer.from(delivery.body_blob).toString("utf8"), '{"id":"evt_delivery","n":1}');
  assert.equal(deliveries.length, 1);
  assert.equal(deliveries[0].provider_delivery_id, "delivery_1");
});

test("node can run a handler and replay a handled event through the shared contract", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({
    name: "github",
    path: "/webhooks/github",
    provider: "github"
  });

  const first = db.ingest({
    endpoint: "github",
    body: Buffer.from('{"ok":true}'),
    providerDeliveryId: "delivery-replay"
  });

  const handled = [];
  db.registerHandler("github", (event) => {
    handled.push(Number(event.id));
    assert.equal(Buffer.from(event.body_blob).toString("utf8"), '{"ok":true}');
  });
  assert.equal(db.runWorkerOnce("node-replay-worker"), first.event_id);
  assert.deepEqual(handled, [first.event_id]);
  assert.equal(db.getEvent(first.event_id).status, "handled");
  assert.equal(db.liveCount(), 0);

  db.replay(first.event_id);

  const replayed = db.getEvent(first.event_id);
  assert.equal(replayed.status, "received");
  assert.equal(replayed.attempt_count, 0);
  assert.equal(db.liveCount(), 1);
});

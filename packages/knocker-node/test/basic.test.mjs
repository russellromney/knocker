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

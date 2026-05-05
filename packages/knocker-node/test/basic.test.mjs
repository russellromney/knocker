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
  db.db.exec("CREATE TABLE handled_events (event_id INTEGER PRIMARY KEY, body TEXT)");
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
  db.registerHandler("github", (event, tx) => {
    handled.push(Number(event.id));
    assert.equal(Buffer.from(event.body_blob).toString("utf8"), '{"ok":true}');
    tx.prepare("INSERT INTO handled_events (event_id, body) VALUES (?, ?)").run(
      BigInt(event.id),
      Buffer.from(event.body_blob).toString("utf8"),
    );
  });
  assert.equal(db.runWorkerOnce("node-replay-worker"), first.event_id);
  assert.deepEqual(handled, [first.event_id]);
  assert.equal(db.getEvent(first.event_id).status, "handled");
  assert.equal(db.db.prepare("SELECT body FROM handled_events WHERE event_id=?").get(BigInt(first.event_id)).body, '{"ok":true}');
  assert.equal(db.liveCount(), 0);

  db.replay(first.event_id);

  const replayed = db.getEvent(first.event_id);
  assert.equal(replayed.status, "received");
  assert.equal(replayed.attempt_count, 0);
  assert.equal(db.liveCount(), 1);
});

test("node binding exposes operator parity helpers", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({ name: "github", path: "/webhooks/github", provider: "github" });

  const result = db.ingest({
    endpoint: "github",
    body: Buffer.from('{"version":1}'),
    providerDeliveryId: "node-delivery-parity",
    eventType: "v1",
  });
  const duplicate = db.ingest({
    endpoint: "github",
    body: Buffer.from('{"version":2}'),
    providerDeliveryId: "node-delivery-parity",
    eventType: "v2",
  });
  assert.equal(duplicate.duplicate, 1);

  const deliveries = db.listDeliveries({ eventId: result.event_id });
  assert.equal(deliveries.length, 2);
  assert.equal(db.getDelivery(duplicate.delivery_id).provider_delivery_id, "node-delivery-parity");

  db.registerHandler("github", (event) => {
    assert.equal(Buffer.from(event.body_blob).toString("utf8"), '{"version":1}');
  });
  db.runWorkerOnce("node-parity-worker");
  assert.equal(db.getEvent(result.event_id).status, "handled");

  const replayBodies = [];
  db.registerHandler("github", () => {
    throw new Error("replay_delivery used endpoint fallback instead of delivery event type");
  });
  db.registerHandler("github", (event) => {
    replayBodies.push(`${event.event_type}:${Buffer.from(event.body_blob).toString("utf8")}`);
  }, "v2");
  db.replayDelivery(duplicate.delivery_id);
  assert.equal(db.getEvent(result.event_id).status, "received");
  db.runWorkerOnce("node-delivery-worker");
  assert.deepEqual(replayBodies, ['v2:{"version":2}']);

  db.replay(result.event_id);
  assert.equal(db.getEvent(result.event_id).status, "received");
  db.ignore(result.event_id);
  assert.equal(db.getEvent(result.event_id).status, "ignored");
  db.requeue(result.event_id);
  assert.equal(db.getEvent(result.event_id).status, "received");
  db.ignore(result.event_id);
  assert.equal(db.getEvent(result.event_id).status, "ignored");

  const prune = db.pruneEvents({ statuses: ["ignored"], olderThan: Math.floor(Date.now() / 1000) + 1, limit: 10 });
  assert.equal(prune.events_pruned, 1);
  const audits = db.listPruneAudits({ limit: 10 });
  assert.equal(audits[0].kind, "prune_events");

  const orphan = db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "invalid-node",
    signatureValid: false,
    signatureError: "bad signature",
  });
  assert.equal(orphan.event_id, null);
  const orphanPrune = db.pruneOrphanDeliveries({ olderThan: Math.floor(Date.now() / 1000) + 1, limit: 10 });
  assert.equal(orphanPrune.deliveries_pruned, 1);
});

test("node rolls back handler writes before recording retry and dead-letter failure", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.db.exec("CREATE TABLE side_effects (event_id INTEGER)");
  db.addEndpoint({ name: "github", path: "/webhooks/github", provider: "github" });

  const result = db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "node-failure-rollback",
    maxAttempts: 2,
  });

  db.registerHandler("github", (_event, tx) => {
    tx.prepare("INSERT INTO side_effects (event_id) VALUES (?)").run(BigInt(result.event_id));
    throw new Error("boom");
  });

  assert.throws(() => db.runWorkerOnce("node-failing-worker"), /boom/);
  assert.equal(db.db.prepare("SELECT COUNT(*) AS c FROM side_effects").get().c, 0);
  assert.equal(db.getEvent(result.event_id).status, "failed");
  assert.equal(db.liveCount(), 1);

  assert.throws(() => db.runWorkerOnce("node-failing-worker"), /boom/);
  assert.equal(db.db.prepare("SELECT COUNT(*) AS c FROM side_effects").get().c, 0);
  assert.equal(db.getEvent(result.event_id).status, "dead");
  assert.equal(db.liveCount(), 0);
});

test("node dispatches event-type handlers and can run a bounded worker loop", async () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({ name: "github", path: "/webhooks/github", provider: "github" });
  db.ingest({ endpoint: "github", body: Buffer.from("{}"), providerDeliveryId: "node-type-a", eventType: "push" });
  db.ingest({ endpoint: "github", body: Buffer.from("{}"), providerDeliveryId: "node-type-b", eventType: "pull_request" });

  const seen = [];
  db.registerHandler("github", (event) => seen.push(`fallback:${event.event_type}`));
  db.registerHandler("github", (event) => seen.push(`typed:${event.event_type}`), "pull_request");

  assert.equal(await db.runWorker({ workerId: "node-loop-worker", maxJobs: 2, idlePollMs: 1 }), 2);
  assert.deepEqual(seen.sort(), ["fallback:push", "typed:pull_request"]);
});

test("node long-running worker exits on stop and reports loop errors", async () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({ name: "github", path: "/webhooks/github", provider: "github" });

  let polls = 0;
  const emptyProcessed = await db.runWorker({
    workerId: "node-empty-loop",
    maxJobs: 1,
    idlePollMs: 1,
    shouldStop: () => ++polls > 1,
  });
  assert.equal(emptyProcessed, 0);
  assert.equal(db.liveCount(), 0);

  db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "node-loop-error",
    maxAttempts: 1,
  });
  db.registerHandler("github", () => {
    throw new Error("loop boom");
  });
  const errors = [];
  let stop = false;
  const failingProcessed = await db.runWorker({
    workerId: "node-error-loop",
    maxJobs: 1,
    idlePollMs: 1,
    shouldStop: () => stop,
    onError: (error) => {
      errors.push(error.message);
      stop = true;
    },
  });
  assert.equal(failingProcessed, 0);
  assert.deepEqual(errors, ["loop boom"]);
  assert.equal(db.liveCount(), 0);
});

test("node receive verifies GitHub requests through the SQLite extension", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({
    name: "github",
    path: "/webhooks/github",
    provider: "github",
    secrets: ["github-secret"],
  });

  const body = Buffer.from('{"zen":"keep it logically awesome"}');
  const result = db.receive({
    endpoint: "github",
    body,
    headers: {
      "X-Hub-Signature-256": "sha256=2467a1987473c6ee89a22fe24f010dca30cb93d575240b9689ab697bed4b6eab",
      "X-GitHub-Delivery": "delivery-abc",
      "X-GitHub-Event": "push",
    },
  });

  assert.equal(result.status_code, 204);
  assert.equal(db.getEvent(result.event_id).provider_delivery_id, "delivery-abc");
  assert.equal(db.getEvent(result.event_id).event_type, "push");

  const rejected = db.receive({
    endpoint: "github",
    body,
    headers: {
      "X-Hub-Signature-256": "sha256=bad",
      "X-GitHub-Delivery": "delivery-bad",
      "X-GitHub-Event": "push",
    },
  });
  assert.equal(rejected.status_code, 401);
  assert.equal(rejected.event_id, null);
  assert.equal(db.getDelivery(rejected.delivery_id).signature_valid, 0);
});

test("node retention loop prunes handled events through shared retention SQL", async () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({ name: "github", path: "/webhooks/github", provider: "github" });
  const result = db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "node-retention",
  });
  db.registerHandler("github", () => {});
  db.runWorkerOnce("node-retention-worker");

  assert.equal(await db.runRetention({ maxRuns: 1, eventOlderThanS: 0, now: Math.floor(Date.now() / 1000) + 10 }), 1);
  assert.equal(db.listPruneAudits({ limit: 1 })[0].events_pruned, 1);
  assert.equal(db.getEvent(result.event_id), undefined);
  assert.equal(db.listPruneAudits({ limit: 1 })[0].kind, "prune_events");
});

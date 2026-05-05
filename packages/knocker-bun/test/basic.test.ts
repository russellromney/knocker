import { expect, test } from "bun:test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { KnockerSqlite } from "../index";

function tempDbPath() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "knocker-bun-"));
  return path.join(dir, "app.db");
}

test("bun binding runs the minimal shared-contract flow", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.exec("CREATE TABLE handled_events (event_id INTEGER PRIMARY KEY, body TEXT)");
  db.addEndpoint({
    name: "github",
    path: "/webhooks/github",
    provider: "github",
  });

  const result = db.ingest({
    endpoint: "github",
    body: Buffer.from('{"hello":"world"}'),
    providerDeliveryId: "bun-delivery-1",
    signatureValid: true,
  });

  expect(result.event_id).toBeNumber();
  expect(db.getEvent(result.event_id).status).toBe("received");
  expect(db.listEvents(10)).toHaveLength(1);

  const handled: number[] = [];
  db.registerHandler("github", (event, tx) => {
    handled.push(Number(event.id));
    expect(Buffer.from(event.body_blob as Buffer).toString("utf8")).toBe('{"hello":"world"}');
    tx.exec(`INSERT INTO handled_events (event_id, body) VALUES (${Number(event.id)}, '{"hello":"world"}')`);
  });

  expect(db.runWorkerOnce("bun-worker")).toBe(result.event_id);
  expect(handled).toEqual([result.event_id]);
  expect(db.getEvent(result.event_id).status).toBe("handled");
  expect(String(db.scalar(`SELECT body FROM handled_events WHERE event_id=${result.event_id}`))).toBe('{"hello":"world"}');

  db.replay(result.event_id);
  expect(db.getEvent(result.event_id).status).toBe("received");
  db.close();
});

test("bun binding exposes operator parity helpers", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({
    name: "github",
    path: "/webhooks/github",
    provider: "github",
  });

  const result = db.ingest({
    endpoint: "github",
    body: Buffer.from('{"version":1}'),
    providerDeliveryId: "bun-delivery-parity",
    eventType: "v1",
    signatureValid: true,
  });
  const duplicate = db.ingest({
    endpoint: "github",
    body: Buffer.from('{"version":2}'),
    providerDeliveryId: "bun-delivery-parity",
    eventType: "v2",
    signatureValid: true,
  });

  expect(duplicate.duplicate).toBe(1);
  expect(db.listDeliveries({ eventId: result.event_id })).toHaveLength(2);
  expect(db.getDelivery(duplicate.delivery_id).provider_delivery_id).toBe("bun-delivery-parity");

  db.registerHandler("github", () => {});
  db.runWorkerOnce("bun-parity-worker");
  expect(db.getEvent(result.event_id).status).toBe("handled");

  const replayBodies: string[] = [];
  db.registerHandler("github", () => {
    throw new Error("replay_delivery used endpoint fallback instead of delivery event type");
  });
  db.registerHandler("github", (event) => {
    replayBodies.push(`${event.event_type}:${String(event.body_blob)}`);
  }, "v2");
  db.replayDelivery(duplicate.delivery_id);
  db.runWorkerOnce("bun-delivery-worker");
  expect(replayBodies).toEqual(['v2:{"version":2}']);

  db.replay(result.event_id);
  db.ignore(result.event_id);
  expect(db.getEvent(result.event_id).status).toBe("ignored");
  db.requeue(result.event_id);
  expect(db.getEvent(result.event_id).status).toBe("received");
  db.ignore(result.event_id);

  const prune = db.pruneEvents({ statuses: ["ignored"], olderThan: Math.floor(Date.now() / 1000) + 1, limit: 10 });
  expect(prune.events_pruned).toBe(1);
  expect(db.listPruneAudits({ limit: 10 })[0].kind).toBe("prune_events");

  const orphan = db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "invalid-bun",
    signatureValid: false,
    signatureError: "bad signature",
  });
  expect(orphan.event_id).toBeNull();
  expect(db.pruneOrphanDeliveries({ olderThan: Math.floor(Date.now() / 1000) + 1, limit: 10 }).deliveries_pruned).toBe(1);
  db.close();
});

test("bun rolls back handler writes and records retry/dead-letter state", () => {
  const db = new KnockerSqlite(tempDbPath());
  db.exec("CREATE TABLE side_effects (event_id INTEGER)");
  db.addEndpoint({ name: "github", path: "/webhooks/github", provider: "github" });
  const result = db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "bun-failure-rollback",
    maxAttempts: 2,
  });
  db.registerHandler("github", (_event, tx) => {
    tx.exec(`INSERT INTO side_effects (event_id) VALUES (${result.event_id})`);
    throw new Error("boom");
  });

  expect(() => db.runWorkerOnce("bun-failing-worker")).toThrow("boom");
  expect(Number(db.scalar("SELECT COUNT(*) FROM side_effects"))).toBe(0);
  expect(db.getEvent(result.event_id).status).toBe("failed");
  expect(Number(db.scalar("SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'"))).toBe(1);

  expect(() => db.runWorkerOnce("bun-failing-worker")).toThrow("boom");
  expect(Number(db.scalar("SELECT COUNT(*) FROM side_effects"))).toBe(0);
  expect(db.getEvent(result.event_id).status).toBe("dead");
  expect(Number(db.scalar("SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'"))).toBe(0);
  db.close();
});

test("bun dispatches by event type and runs a bounded worker loop", async () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({ name: "github", path: "/webhooks/github", provider: "github" });
  db.ingest({ endpoint: "github", body: Buffer.from("{}"), providerDeliveryId: "bun-type-a", eventType: "push" });
  db.ingest({ endpoint: "github", body: Buffer.from("{}"), providerDeliveryId: "bun-type-b", eventType: "pull_request" });

  const seen: string[] = [];
  db.registerHandler("github", (event) => seen.push(`fallback:${event.event_type}`));
  db.registerHandler("github", (event) => seen.push(`typed:${event.event_type}`), "pull_request");

  expect(await db.runWorker({ workerId: "bun-loop-worker", maxJobs: 2, idlePollMs: 1 })).toBe(2);
  expect(seen.sort()).toEqual(["fallback:push", "typed:pull_request"]);
  db.close();
});

test("bun long-running worker exits on stop and reports loop errors", async () => {
  const db = new KnockerSqlite(tempDbPath());
  db.addEndpoint({ name: "github", path: "/webhooks/github", provider: "github" });

  let polls = 0;
  expect(await db.runWorker({
    workerId: "bun-empty-loop",
    maxJobs: 1,
    idlePollMs: 1,
    shouldStop: () => ++polls > 1,
  })).toBe(0);

  db.ingest({
    endpoint: "github",
    body: Buffer.from("{}"),
    providerDeliveryId: "bun-loop-error",
    maxAttempts: 1,
  });
  db.registerHandler("github", () => {
    throw new Error("loop boom");
  });
  const errors: string[] = [];
  let stop = false;
  expect(await db.runWorker({
    workerId: "bun-error-loop",
    maxJobs: 1,
    idlePollMs: 1,
    shouldStop: () => stop,
    onError: (error) => {
      errors.push(error instanceof Error ? error.message : String(error));
      stop = true;
    },
  })).toBe(0);
  expect(errors).toEqual(["loop boom"]);
  expect(Number(db.scalar("SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'"))).toBe(0);
  db.close();
});

test("bun receive verifies GitHub requests and retention prunes through shared SQL", async () => {
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
  expect(result.status_code).toBe(204);
  expect(db.getEvent(result.event_id).event_type).toBe("push");

  const rejected = db.receive({
    endpoint: "github",
    body,
    headers: {
      "X-Hub-Signature-256": "sha256=bad",
      "X-GitHub-Delivery": "delivery-bad",
      "X-GitHub-Event": "push",
    },
  });
  expect(rejected.status_code).toBe(401);
  expect(rejected.event_id).toBeNull();

  db.registerHandler("github", () => {});
  db.runWorkerOnce("bun-retention-worker");
  expect(await db.runRetention({ maxRuns: 1, eventOlderThanS: 0, now: Math.floor(Date.now() / 1000) + 10 })).toBe(1);
  const audit = db.listPruneAudits({ limit: 1 })[0];
  expect(audit.kind).toBe("prune_events");
  expect(audit.events_pruned).toBe(1);
  expect(Number(db.scalar(`SELECT COUNT(*) FROM knocker_events WHERE id=${result.event_id}`))).toBe(0);
  db.close();
});

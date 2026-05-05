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
  db.registerHandler("github", (event) => {
    handled.push(Number(event.id));
    expect(Buffer.from(event.body_blob as Buffer).toString("utf8")).toBe('{"hello":"world"}');
  });

  expect(db.runWorkerOnce("bun-worker")).toBe(result.event_id);
  expect(handled).toEqual([result.event_id]);
  expect(db.getEvent(result.event_id).status).toBe("handled");

  db.replay(result.event_id);
  expect(db.getEvent(result.event_id).status).toBe("received");
  db.close();
});

import path from "node:path";
import { DatabaseSync } from "node:sqlite";

function extensionPath() {
  const ext = process.platform === "darwin" ? "dylib" : process.platform === "win32" ? "dll" : "so";
  return path.resolve(import.meta.dirname, "../../target/release", `libknocker_ext.${ext}`);
}

export class KnockerSqlite {
  constructor(dbPath) {
    this.db = new DatabaseSync(dbPath, { allowExtension: true });
    this.db.loadExtension(extensionPath());
    this.db.enableLoadExtension(false);
    this.db.prepare("SELECT knocker_bootstrap()").get();
  }

  addEndpoint({ name, path: endpointPath, provider = null, enabled = true }) {
    return this.db
      .prepare("SELECT knocker_endpoint_upsert(?, ?, ?, ?) AS id")
      .get(name, endpointPath, provider, enabled ? 1n : 0n).id;
  }

  ingest({
    endpoint,
    body,
    headers = {},
    query = {},
    method = "POST",
    eventType = null,
    providerEventId = null,
    providerDeliveryId = null,
    dedupeKey = null,
    signatureValid = true,
    signatureError = null,
    queueName = "knocker.events",
    maxAttempts = 3
  }) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const resultJson = this.db
        .prepare(`
          SELECT knocker_ingest(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) AS result_json
        `)
        .get(
          endpoint,
          method,
          JSON.stringify(headers),
          body,
          JSON.stringify(query),
          signatureValid === null ? null : signatureValid ? 1n : 0n,
          signatureError,
          providerEventId,
          providerDeliveryId,
          eventType,
          dedupeKey,
          queueName,
          BigInt(maxAttempts)
        ).result_json;
      this.db.exec("COMMIT");
      return JSON.parse(resultJson);
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  getEvent(eventId) {
    return this.db.prepare(`
      SELECT
        e.id,
        ep.name AS endpoint,
        e.event_type,
        e.provider_event_id,
        e.provider_delivery_id,
        e.dedupe_key,
        e.status,
        e.attempt_count
      FROM knocker_events e
      JOIN knocker_endpoints ep ON ep.id = e.endpoint_id
      WHERE e.id=?
    `).get(BigInt(eventId));
  }
}

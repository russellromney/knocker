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
    this.handlers = new Map();
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
        e.attempt_count,
        e.body_blob
      FROM knocker_events e
      JOIN knocker_endpoints ep ON ep.id = e.endpoint_id
      WHERE e.id=?
    `).get(BigInt(eventId));
  }

  getDelivery(deliveryId) {
    return this.db.prepare(`
      SELECT
        d.id,
        d.event_id,
        ep.name AS endpoint,
        d.event_type,
        d.provider_event_id,
        d.provider_delivery_id,
        d.dedupe_key,
        d.signature_valid,
        d.signature_error,
        d.body_blob
      FROM knocker_deliveries d
      JOIN knocker_endpoints ep ON ep.id = d.endpoint_id
      WHERE d.id=?
    `).get(BigInt(deliveryId));
  }

  listDeliveriesForEvent(eventId) {
    return this.db.prepare(`
      SELECT
        d.id,
        d.event_id,
        d.provider_delivery_id,
        d.provider_event_id,
        d.signature_valid,
        d.signature_error
      FROM knocker_deliveries d
      WHERE d.event_id=?
      ORDER BY d.id DESC
    `).all(BigInt(eventId));
  }

  markHandled(eventId, durationMs = 0) {
    this.db
      .prepare("SELECT knocker_mark_handled(?, ?)")
      .get(BigInt(eventId), BigInt(durationMs));
  }

  replay(eventId, queueName = "knocker.events", maxAttempts = 3) {
    this.db
      .prepare("SELECT knocker_replay(?, ?, ?)")
      .get(BigInt(eventId), queueName, BigInt(maxAttempts));
  }

  registerHandler(endpoint, handler) {
    this.handlers.set(endpoint, handler);
  }

  runWorkerOnce(workerId = "node-worker", queueName = "knocker.events", visibilityTimeoutS = 60) {
    const claim = this.claimOne(workerId, queueName, visibilityTimeoutS);
    if (!claim) return null;
    const payload = JSON.parse(claim.payload);
    const eventId = Number(payload.event_id);
    const event = this.getEvent(eventId);
    const handler = this.handlers.get(event.endpoint);
    if (!handler) {
      const error = new Error(`no handler registered for endpoint ${event.endpoint}`);
      this.#failClaim(claim, workerId, eventId, error.message);
      throw error;
    }

    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db
        .prepare("SELECT knocker_mark_processing(?, ?)")
        .get(BigInt(eventId), BigInt(claim.attempts));
      handler(event);
      this.markHandled(eventId, 0);
      assertTransition(
        this.ack(claim.id, workerId),
        `failed to ack claimed job ${claim.id} for event ${eventId}`,
      );
      this.db.exec("COMMIT");
      return eventId;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      try {
        const terminal = Number(claim.attempts) >= Number(claim.max_attempts);
        this.db
          .prepare("SELECT knocker_mark_failed(?, ?, ?, ?, ?)")
          .get(BigInt(eventId), BigInt(claim.attempts), message, terminal ? 1n : 0n, 0n);
        if (terminal) {
          this.db
            .prepare("SELECT honker_fail(?, ?, ?)")
            .get(BigInt(claim.id), workerId, message);
        } else {
          this.db
            .prepare("SELECT honker_retry(?, ?, ?, ?)")
            .get(BigInt(claim.id), workerId, 0n, message);
        }
        this.db.exec("COMMIT");
      } catch {
        this.db.exec("ROLLBACK");
      }
      throw error;
    }
  }

  claimOne(workerId = "node-test", queueName = "knocker.events", visibilityTimeoutS = 60) {
    const rowsJson = this.db
      .prepare("SELECT honker_claim_batch(?, ?, ?, ?) AS rows_json")
      .get(queueName, workerId, 1n, BigInt(visibilityTimeoutS)).rows_json;
    const rows = JSON.parse(rowsJson);
    return rows.length === 0 ? null : rows[0];
  }

  ack(jobId, workerId) {
    return Boolean(
      this.db
        .prepare("SELECT honker_ack(?, ?) AS r")
        .get(BigInt(jobId), workerId).r
    );
  }

  liveCount(queueName = "knocker.events") {
    return Number(
      this.db
        .prepare("SELECT COUNT(*) AS c FROM _honker_live WHERE queue=?")
        .get(queueName).c
    );
  }

  #failClaim(claim, workerId, eventId, message) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db
        .prepare("SELECT knocker_mark_failed(?, ?, ?, ?, ?)")
        .get(BigInt(eventId), BigInt(claim.attempts), message, 1n, 0n);
      this.db
        .prepare("SELECT honker_fail(?, ?, ?)")
        .get(BigInt(claim.id), workerId, message);
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }
}

function assertTransition(result, message) {
  if (!result) {
    throw new Error(message);
  }
}

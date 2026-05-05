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
    this.endpointConfigs = new Map();
  }

  addEndpoint({ name, path: endpointPath, provider = null, enabled = true, secrets = [], providerOptions = {} }) {
    const id = this.db
      .prepare("SELECT knocker_endpoint_upsert(?, ?, ?, ?) AS id")
      .get(name, endpointPath, provider, enabled ? 1n : 0n).id;
    this.endpointConfigs.set(name, { provider, secrets, providerOptions });
    return id;
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

  receive({
    endpoint,
    body,
    headers = {},
    query = {},
    method = "POST",
    provider = null,
    secrets = null,
    providerOptions = null,
    eventType = null,
    providerEventId = null,
    providerDeliveryId = null,
    dedupeKey = null,
    queueName = "knocker.events",
    maxAttempts = 3,
  }) {
    const config = this.endpointConfigs.get(endpoint) ?? {};
    const providerName = provider ?? config.provider;
    if (!providerName) throw new Error(`endpoint ${endpoint} has no provider configured`);
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const resultJson = this.db
        .prepare(`
          SELECT knocker_receive(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) AS result_json
        `)
        .get(
          endpoint,
          providerName,
          JSON.stringify(secrets ?? config.secrets ?? []),
          JSON.stringify(providerOptions ?? config.providerOptions ?? {}),
          method,
          JSON.stringify(headers),
          body,
          JSON.stringify(query),
          providerEventId,
          providerDeliveryId,
          eventType,
          dedupeKey,
          queueName,
          BigInt(maxAttempts),
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
    return this.listDeliveries({ eventId });
  }

  listDeliveries({ eventId = null, endpoint = null, signatureValid = null, orphaned = null, since = null, limit = 100 } = {}) {
    const clauses = [];
    const params = [];
    if (eventId !== null) {
      clauses.push("d.event_id=?");
      params.push(BigInt(eventId));
    }
    if (endpoint !== null) {
      clauses.push("ep.name=?");
      params.push(endpoint);
    }
    if (signatureValid !== null) {
      clauses.push(signatureValid ? "d.signature_valid=1" : "(d.signature_valid=0 OR d.signature_valid IS NULL)");
    }
    if (orphaned !== null) {
      clauses.push(orphaned ? "d.event_id IS NULL" : "d.event_id IS NOT NULL");
    }
    if (since !== null) {
      clauses.push("d.received_at>=?");
      params.push(BigInt(since));
    }
    params.push(BigInt(limit));
    return this.db.prepare(`
      SELECT
        d.id,
        d.event_id,
        ep.name AS endpoint,
        d.event_type,
        d.provider_event_id,
        d.provider_delivery_id,
        d.dedupe_key,
        d.method,
        d.headers_json,
        d.query_json,
        d.body_blob,
        d.received_at,
        d.signature_valid,
        d.signature_error
      FROM knocker_deliveries d
      JOIN knocker_endpoints ep ON ep.id = d.endpoint_id
      ${clauses.length === 0 ? "" : `WHERE ${clauses.join(" AND ")}`}
      ORDER BY d.received_at DESC, d.id DESC
      LIMIT ?
    `).all(...params);
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

  requeue(eventId, queueName = "knocker.events", maxAttempts = 3) {
    this.db
      .prepare("SELECT knocker_requeue(?, ?, ?)")
      .get(BigInt(eventId), queueName, BigInt(maxAttempts));
  }

  ignore(eventId) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const event = this.getEvent(eventId);
      if (!event) throw new Error(`unknown event id: ${eventId}`);
      if (event.status !== "ignored") {
        if (!["received", "failed", "dead"].includes(event.status)) {
          throw new Error(`event ${eventId} with status ${event.status} cannot be ignored`);
        }
        this.db.prepare("SELECT knocker_mark_ignored(?, ?)").get(BigInt(eventId), 0n);
      }
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  replayDelivery(deliveryId, queueName = "knocker.events", maxAttempts = 3) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const delivery = this.getDelivery(deliveryId);
      if (!delivery) throw new Error(`unknown delivery id: ${deliveryId}`);
      if (delivery.event_id === null || delivery.event_id === undefined) {
        throw new Error(`delivery ${deliveryId} is not linked to an event`);
      }
      const event = this.getEvent(delivery.event_id);
      if (!["handled", "failed", "dead", "ignored"].includes(event.status)) {
        throw new Error(`event ${delivery.event_id} with status ${event.status} cannot replay a delivery`);
      }
      this.#deleteLiveJobsForEvent(Number(delivery.event_id), queueName);
      this.db.prepare("SELECT knocker_reset_event(?)").get(BigInt(delivery.event_id));
      this.db.prepare("SELECT honker_enqueue(?, ?, ?, ?, ?, ?, ?)").get(
        queueName,
        JSON.stringify({ event_id: Number(delivery.event_id), delivery_id: Number(delivery.id) }),
        null,
        null,
        0n,
        BigInt(maxAttempts),
        null,
      );
      this.db.exec("COMMIT");
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  pruneEvents({ statuses, olderThan, limit, queueName = "knocker.events" }) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const resultJson = this.db
        .prepare("SELECT knocker_prune_events(?, ?, ?, ?) AS result_json")
        .get(JSON.stringify(statuses), BigInt(olderThan), BigInt(limit), queueName).result_json;
      this.db.exec("COMMIT");
      return JSON.parse(resultJson);
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  pruneOrphanDeliveries({ olderThan, limit, queueName = "knocker.events" }) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const resultJson = this.db
        .prepare("SELECT knocker_prune_orphan_deliveries(?, ?, ?) AS result_json")
        .get(BigInt(olderThan), BigInt(limit), queueName).result_json;
      this.db.exec("COMMIT");
      return JSON.parse(resultJson);
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  listPruneAudits({ kind = null, since = null, limit = 50 } = {}) {
    const clauses = [];
    const params = [];
    if (kind !== null) {
      clauses.push("kind=?");
      params.push(kind);
    }
    if (since !== null) {
      clauses.push("executed_at>=?");
      params.push(BigInt(since));
    }
    params.push(BigInt(limit));
    return this.db.prepare(`
      SELECT
        id,
        kind,
        queue_name,
        executed_at,
        events_pruned,
        deliveries_pruned,
        attempts_pruned,
        live_jobs_pruned,
        summary_json
      FROM knocker_prune_audits
      ${clauses.length === 0 ? "" : `WHERE ${clauses.join(" AND ")}`}
      ORDER BY executed_at DESC, id DESC
      LIMIT ?
    `).all(...params);
  }

  runRetentionOnce({
    statuses = ["handled", "ignored"],
    eventOlderThanS = null,
    eventLimit = 1000,
    orphanDeliveriesOlderThanS = null,
    orphanDeliveriesLimit = 1000,
    queueName = "knocker.events",
    now = Math.floor(Date.now() / 1000),
  } = {}) {
    const eventCutoff = eventOlderThanS === null ? null : BigInt(now - eventOlderThanS);
    const orphanCutoff = orphanDeliveriesOlderThanS === null ? null : BigInt(now - orphanDeliveriesOlderThanS);
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const resultJson = this.db
        .prepare("SELECT knocker_run_retention_pass(?, ?, ?, ?, ?, ?) AS result_json")
        .get(
          JSON.stringify(statuses),
          eventCutoff,
          BigInt(eventLimit),
          orphanCutoff,
          BigInt(orphanDeliveriesLimit),
          queueName,
        ).result_json;
      this.db.exec("COMMIT");
      return JSON.parse(resultJson);
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  async runRetention({ intervalMs = 60_000, maxRuns = null, shouldStop = null, ...policy } = {}) {
    let runs = 0;
    while (maxRuns === null || runs < maxRuns) {
      if (shouldStop?.()) break;
      this.runRetentionOnce(policy);
      runs += 1;
      if (maxRuns !== null && runs >= maxRuns) break;
      await new Promise((resolve) => setTimeout(resolve, intervalMs));
    }
    return runs;
  }

  registerHandler(endpoint, handler, eventType = null) {
    if (typeof eventType === "function") {
      const actualHandler = eventType;
      eventType = handler;
      handler = actualHandler;
    }
    this.handlers.set(KnockerSqlite.#handlerKey(endpoint, eventType), handler);
  }

  runWorkerOnce(workerId = "node-worker", queueName = "knocker.events", visibilityTimeoutS = 60) {
    const claim = this.claimOne(workerId, queueName, visibilityTimeoutS);
    if (!claim) return null;
    const payload = JSON.parse(claim.payload);
    const eventId = Number(payload.event_id);
    let event = this.getEvent(eventId);
    if (payload.delivery_id !== undefined && payload.delivery_id !== null) {
      const delivery = this.getDelivery(Number(payload.delivery_id));
      event = {
        ...event,
        event_type: delivery.event_type,
        provider_event_id: delivery.provider_event_id,
        provider_delivery_id: delivery.provider_delivery_id,
        dedupe_key: delivery.dedupe_key,
        body_blob: delivery.body_blob,
      };
    }
    const handler =
      this.handlers.get(KnockerSqlite.#handlerKey(event.endpoint, event.event_type)) ??
      this.handlers.get(KnockerSqlite.#handlerKey(event.endpoint, null));
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
      handler(event, new KnockerTransaction(this.db));
      this.markHandled(eventId, 0);
      assertTransition(
        this.ack(claim.id, workerId),
        `failed to ack claimed job ${claim.id} for event ${eventId}`,
      );
      this.db.exec("COMMIT");
      return eventId;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.db.exec("ROLLBACK");
      this.#recordFailedClaim(claim, workerId, eventId, message);
      throw error;
    }
  }

  async runWorker({
    workerId = "node-worker",
    queueName = "knocker.events",
    visibilityTimeoutS = 60,
    idlePollMs = 100,
    maxJobs = null,
    shouldStop = null,
    onError = null,
  } = {}) {
    let processed = 0;
    while (maxJobs === null || processed < maxJobs) {
      if (shouldStop?.()) break;
      try {
        const eventId = this.runWorkerOnce(workerId, queueName, visibilityTimeoutS);
        if (eventId === null) {
          await new Promise((resolve) => setTimeout(resolve, idlePollMs));
          continue;
        }
        processed += 1;
      } catch (error) {
        if (onError) {
          onError(error);
        } else {
          throw error;
        }
      }
    }
    return processed;
  }

  #recordFailedClaim(claim, workerId, eventId, message) {
    const attemptCount = Number(claim.attempts);
    const terminal = attemptCount >= Number(claim.max_attempts);
    this.db.exec("BEGIN IMMEDIATE");
    try {
      this.db
        .prepare("SELECT knocker_mark_failed(?, ?, ?, ?, ?)")
        .get(BigInt(eventId), BigInt(attemptCount), message, terminal ? 1n : 0n, 0n);
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
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  claimOne(workerId = "node-test", queueName = "knocker.events", visibilityTimeoutS = 60) {
    const rowsJson = this.db
      .prepare("SELECT honker_claim_batch(?, ?, ?, ?) AS rows_json")
      .get(queueName, workerId, 1n, BigInt(visibilityTimeoutS)).rows_json;
    const rows = JSON.parse(rowsJson);
    if (rows.length === 0) return null;
    if (rows[0].max_attempts === undefined) {
      rows[0].max_attempts = Number(
        this.db.prepare("SELECT max_attempts FROM _honker_live WHERE id=?").get(BigInt(rows[0].id)).max_attempts,
      );
    }
    return rows[0];
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

  static #handlerKey(endpoint, eventType) {
    return `${endpoint}\0${eventType ?? ""}`;
  }

  #deleteLiveJobsForEvent(eventId, queueName) {
    const rows = this.db
      .prepare("SELECT id, payload FROM _honker_live WHERE queue=?")
      .all(queueName);
    for (const row of rows) {
      let payload;
      try {
        payload = JSON.parse(row.payload);
      } catch {
        continue;
      }
      if (Number(payload.event_id) === Number(eventId)) {
        this.db.prepare("DELETE FROM _honker_live WHERE id=?").get(BigInt(row.id));
      }
    }
  }
}

function assertTransition(result, message) {
  if (!result) {
    throw new Error(message);
  }
}

export class KnockerTransaction {
  constructor(db) {
    this.db = db;
  }

  exec(sql) {
    this.db.exec(sql);
  }

  prepare(sql) {
    return this.db.prepare(sql);
  }

  query(sql, ...params) {
    return this.db.prepare(sql).all(...params);
  }

  scalar(sql, ...params) {
    const row = this.db.prepare(sql).get(...params);
    if (!row) return null;
    return row[Object.keys(row)[0]];
  }
}

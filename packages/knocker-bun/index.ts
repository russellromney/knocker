import { dlopen, FFIType, ptr } from "bun:ffi";
import fs from "node:fs";
import path from "node:path";

const SQLITE_ROW = 100;
const SQLITE_DONE = 101;

function sqliteLibPath(): string {
  const candidates = [
    process.env.KNOCKER_SQLITE3_LIB,
    process.env.LIBSQLITE3_PATH,
    "/opt/homebrew/opt/sqlite/lib/libsqlite3.dylib",
    "/usr/local/opt/sqlite/lib/libsqlite3.dylib",
    "/usr/lib/libsqlite3.dylib",
    "/usr/lib/x86_64-linux-gnu/libsqlite3.so",
    "/usr/lib/aarch64-linux-gnu/libsqlite3.so",
    "/usr/lib64/libsqlite3.so",
    "/usr/lib/libsqlite3.so",
  ].filter((value): value is string => Boolean(value));
  const found = candidates.find((candidate) => fs.existsSync(candidate));
  if (!found) {
    throw new Error("unable to locate a loadable libsqlite3; set KNOCKER_SQLITE3_LIB");
  }
  return found;
}

const sqlite = dlopen(sqliteLibPath(), {
  sqlite3_open: { args: [FFIType.cstring, FFIType.ptr], returns: FFIType.i32 },
  sqlite3_close: { args: [FFIType.ptr], returns: FFIType.i32 },
  sqlite3_enable_load_extension: { args: [FFIType.ptr, FFIType.i32], returns: FFIType.i32 },
  sqlite3_load_extension: { args: [FFIType.ptr, FFIType.cstring, FFIType.cstring, FFIType.ptr], returns: FFIType.i32 },
  sqlite3_errmsg: { args: [FFIType.ptr], returns: FFIType.cstring },
  sqlite3_exec: { args: [FFIType.ptr, FFIType.cstring, FFIType.ptr, FFIType.ptr, FFIType.ptr], returns: FFIType.i32 },
  sqlite3_prepare_v2: { args: [FFIType.ptr, FFIType.cstring, FFIType.i32, FFIType.ptr, FFIType.ptr], returns: FFIType.i32 },
  sqlite3_step: { args: [FFIType.ptr], returns: FFIType.i32 },
  sqlite3_column_text: { args: [FFIType.ptr, FFIType.i32], returns: FFIType.cstring },
  sqlite3_finalize: { args: [FFIType.ptr], returns: FFIType.i32 },
});

function cstr(value: string): Buffer {
  return Buffer.from(`${value}\0`);
}

function quote(value: unknown): string {
  if (value === null || value === undefined) return "NULL";
  return `'${String(value).replaceAll("'", "''")}'`;
}

function blobLiteral(value: string | Buffer): string {
  return `X'${Buffer.from(value).toString("hex")}'`;
}

function extensionPath(): string {
  const ext = process.platform === "darwin" ? "dylib" : process.platform === "win32" ? "dll" : "so";
  return path.resolve(import.meta.dirname, "../../target/release", `libknocker_ext.${ext}`);
}

export class KnockerSqlite {
  db: number;
  handlers: Map<string, (event: Record<string, unknown>, tx: KnockerTransaction) => void>;
  endpointConfigs: Map<string, { provider: string | null; secrets: string[]; providerOptions: Record<string, unknown> }>;

  constructor(dbPath: string) {
    const dbPtr = new BigUint64Array(1);
    this.#check(sqlite.symbols.sqlite3_open(cstr(dbPath), ptr(dbPtr)));
    this.db = Number(dbPtr[0]);
    this.#check(sqlite.symbols.sqlite3_enable_load_extension(this.db, 1));
    const errPtr = new BigUint64Array(1);
    this.#check(
      sqlite.symbols.sqlite3_load_extension(
        this.db,
        cstr(extensionPath()),
        cstr("sqlite3_knockerext_init"),
        ptr(errPtr),
      ),
    );
    this.#check(sqlite.symbols.sqlite3_enable_load_extension(this.db, 0));
    this.exec("SELECT knocker_bootstrap()");
    this.handlers = new Map();
    this.endpointConfigs = new Map();
  }

  close() {
    this.#check(sqlite.symbols.sqlite3_close(this.db));
  }

  addEndpoint({ name, path: endpointPath, provider = null, enabled = true, secrets = [], providerOptions = {} }: {
    name: string;
    path: string;
    provider?: string | null;
    enabled?: boolean;
    secrets?: string[];
    providerOptions?: Record<string, unknown>;
  }) {
    const id = Number(
      this.scalar(
        `SELECT knocker_endpoint_upsert(${quote(name)}, ${quote(endpointPath)}, ${quote(provider)}, ${enabled ? 1 : 0})`,
      ),
    );
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
    maxAttempts = 3,
  }: {
    endpoint: string;
    body: string | Buffer;
    headers?: Record<string, unknown>;
    query?: Record<string, unknown>;
    method?: string;
    eventType?: string | null;
    providerEventId?: string | null;
    providerDeliveryId?: string | null;
    dedupeKey?: string | null;
    signatureValid?: boolean | null;
    signatureError?: string | null;
    queueName?: string;
    maxAttempts?: number;
  }) {
    const signatureSql = signatureValid === null ? "NULL" : signatureValid ? "1" : "0";
    this.exec("BEGIN IMMEDIATE");
    try {
      const resultJson = this.scalar(
        `SELECT knocker_ingest(${quote(endpoint)}, ${quote(method)}, ${quote(JSON.stringify(headers))}, ${blobLiteral(body)}, ${quote(JSON.stringify(query))}, ${signatureSql}, ${quote(signatureError)}, ${quote(providerEventId)}, ${quote(providerDeliveryId)}, ${quote(eventType)}, ${quote(dedupeKey)}, ${quote(queueName)}, ${maxAttempts})`,
      );
      this.exec("COMMIT");
      return JSON.parse(resultJson);
    } catch (error) {
      this.exec("ROLLBACK");
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
  }: {
    endpoint: string;
    body: string | Buffer;
    headers?: Record<string, unknown>;
    query?: Record<string, unknown>;
    method?: string;
    provider?: string | null;
    secrets?: string[] | null;
    providerOptions?: Record<string, unknown> | null;
    eventType?: string | null;
    providerEventId?: string | null;
    providerDeliveryId?: string | null;
    dedupeKey?: string | null;
    queueName?: string;
    maxAttempts?: number;
  }) {
    const config = this.endpointConfigs.get(endpoint) ?? { provider: null, secrets: [], providerOptions: {} };
    const providerName = provider ?? config.provider;
    if (!providerName) throw new Error(`endpoint ${endpoint} has no provider configured`);
    this.exec("BEGIN IMMEDIATE");
    try {
      const resultJson = this.scalar(
        `SELECT knocker_receive(${quote(endpoint)}, ${quote(providerName)}, ${quote(JSON.stringify(secrets ?? config.secrets))}, ${quote(JSON.stringify(providerOptions ?? config.providerOptions))}, ${quote(method)}, ${quote(JSON.stringify(headers))}, ${blobLiteral(body)}, ${quote(JSON.stringify(query))}, ${quote(providerEventId)}, ${quote(providerDeliveryId)}, ${quote(eventType)}, ${quote(dedupeKey)}, ${quote(queueName)}, ${maxAttempts})`,
      );
      this.exec("COMMIT");
      return JSON.parse(resultJson);
    } catch (error) {
      this.exec("ROLLBACK");
      throw error;
    }
  }

  getEvent(eventId: number) {
    return JSON.parse(
      this.scalar(
        `SELECT json_object('id', e.id, 'endpoint', ep.name, 'event_type', e.event_type, 'provider_event_id', e.provider_event_id, 'provider_delivery_id', e.provider_delivery_id, 'status', e.status, 'attempt_count', e.attempt_count, 'body_blob', CAST(e.body_blob AS TEXT)) FROM knocker_events e JOIN knocker_endpoints ep ON ep.id = e.endpoint_id WHERE e.id=${eventId}`,
      ),
    );
  }

  listEvents(limit = 50) {
    return JSON.parse(
      this.scalar(
        `SELECT COALESCE(json_group_array(json_object('id', id, 'endpoint', endpoint, 'event_type', event_type, 'provider_event_id', provider_event_id, 'provider_delivery_id', provider_delivery_id, 'status', status, 'attempt_count', attempt_count)), '[]') FROM (SELECT e.id AS id, ep.name AS endpoint, e.event_type AS event_type, e.provider_event_id AS provider_event_id, e.provider_delivery_id AS provider_delivery_id, e.status AS status, e.attempt_count AS attempt_count FROM knocker_events e JOIN knocker_endpoints ep ON ep.id = e.endpoint_id ORDER BY e.id LIMIT ${limit})`,
      ),
    );
  }

  getDelivery(deliveryId: number) {
    return JSON.parse(
      this.scalar(
        `SELECT json_object('id', d.id, 'event_id', d.event_id, 'endpoint', ep.name, 'event_type', d.event_type, 'provider_event_id', d.provider_event_id, 'provider_delivery_id', d.provider_delivery_id, 'dedupe_key', d.dedupe_key, 'method', d.method, 'headers_json', d.headers_json, 'query_json', d.query_json, 'body_blob', CAST(d.body_blob AS TEXT), 'received_at', d.received_at, 'signature_valid', d.signature_valid, 'signature_error', d.signature_error) FROM knocker_deliveries d JOIN knocker_endpoints ep ON ep.id = d.endpoint_id WHERE d.id=${deliveryId}`,
      ),
    );
  }

  listDeliveries({
    eventId = null,
    endpoint = null,
    signatureValid = null,
    orphaned = null,
    since = null,
    limit = 100,
  }: {
    eventId?: number | null;
    endpoint?: string | null;
    signatureValid?: boolean | null;
    orphaned?: boolean | null;
    since?: number | null;
    limit?: number;
  } = {}) {
    const clauses: string[] = [];
    if (eventId !== null) clauses.push(`d.event_id=${eventId}`);
    if (endpoint !== null) clauses.push(`ep.name=${quote(endpoint)}`);
    if (signatureValid !== null) {
      clauses.push(signatureValid ? "d.signature_valid=1" : "(d.signature_valid=0 OR d.signature_valid IS NULL)");
    }
    if (orphaned !== null) clauses.push(orphaned ? "d.event_id IS NULL" : "d.event_id IS NOT NULL");
    if (since !== null) clauses.push(`d.received_at>=${since}`);
    return JSON.parse(
      this.scalar(
        `SELECT COALESCE(json_group_array(json_object('id', id, 'event_id', event_id, 'endpoint', endpoint, 'event_type', event_type, 'provider_event_id', provider_event_id, 'provider_delivery_id', provider_delivery_id, 'dedupe_key', dedupe_key, 'method', method, 'headers_json', headers_json, 'query_json', query_json, 'body_blob', body_blob, 'received_at', received_at, 'signature_valid', signature_valid, 'signature_error', signature_error)), '[]') FROM (SELECT d.id AS id, d.event_id AS event_id, ep.name AS endpoint, d.event_type AS event_type, d.provider_event_id AS provider_event_id, d.provider_delivery_id AS provider_delivery_id, d.dedupe_key AS dedupe_key, d.method AS method, d.headers_json AS headers_json, d.query_json AS query_json, CAST(d.body_blob AS TEXT) AS body_blob, d.received_at AS received_at, d.signature_valid AS signature_valid, d.signature_error AS signature_error FROM knocker_deliveries d JOIN knocker_endpoints ep ON ep.id = d.endpoint_id ${clauses.length === 0 ? "" : `WHERE ${clauses.join(" AND ")}`} ORDER BY d.received_at DESC, d.id DESC LIMIT ${limit})`,
      ),
    );
  }

  replay(eventId: number) {
    this.scalar(`SELECT knocker_replay(${eventId}, 'knocker.events', 3)`);
  }

  requeue(eventId: number) {
    this.scalar(`SELECT knocker_requeue(${eventId}, 'knocker.events', 3)`);
  }

  ignore(eventId: number) {
    this.exec("BEGIN IMMEDIATE");
    try {
      const event = this.getEvent(eventId);
      if (event.status !== "ignored") {
        if (!["received", "failed", "dead"].includes(String(event.status))) {
          throw new Error(`event ${eventId} with status ${String(event.status)} cannot be ignored`);
        }
        this.scalar(`SELECT knocker_mark_ignored(${eventId}, 0)`);
      }
      this.exec("COMMIT");
    } catch (error) {
      this.exec("ROLLBACK");
      throw error;
    }
  }

  replayDelivery(deliveryId: number) {
    this.exec("BEGIN IMMEDIATE");
    try {
      const delivery = this.getDelivery(deliveryId);
      if (delivery.event_id === null || delivery.event_id === undefined) {
        throw new Error(`delivery ${deliveryId} is not linked to an event`);
      }
      const event = this.getEvent(Number(delivery.event_id));
      if (!["handled", "failed", "dead", "ignored"].includes(String(event.status))) {
        throw new Error(`event ${Number(delivery.event_id)} with status ${String(event.status)} cannot replay a delivery`);
      }
      this.#deleteLiveJobsForEvent(Number(delivery.event_id), "knocker.events");
      this.scalar(`SELECT knocker_reset_event(${Number(delivery.event_id)})`);
      this.scalar(
        `SELECT honker_enqueue('knocker.events', ${quote(JSON.stringify({ event_id: Number(delivery.event_id), delivery_id: Number(delivery.id) }))}, NULL, NULL, 0, 3, NULL)`,
      );
      this.exec("COMMIT");
    } catch (error) {
      this.exec("ROLLBACK");
      throw error;
    }
  }

  pruneEvents({ statuses, olderThan, limit }: { statuses: string[]; olderThan: number; limit: number }) {
    this.exec("BEGIN IMMEDIATE");
    try {
      const result = JSON.parse(
        this.scalar(`SELECT knocker_prune_events(${quote(JSON.stringify(statuses))}, ${olderThan}, ${limit}, 'knocker.events')`),
      );
      this.exec("COMMIT");
      return result;
    } catch (error) {
      this.exec("ROLLBACK");
      throw error;
    }
  }

  pruneOrphanDeliveries({ olderThan, limit }: { olderThan: number; limit: number }) {
    this.exec("BEGIN IMMEDIATE");
    try {
      const result = JSON.parse(
        this.scalar(`SELECT knocker_prune_orphan_deliveries(${olderThan}, ${limit}, 'knocker.events')`),
      );
      this.exec("COMMIT");
      return result;
    } catch (error) {
      this.exec("ROLLBACK");
      throw error;
    }
  }

  listPruneAudits({ kind = null, since = null, limit = 50 }: { kind?: string | null; since?: number | null; limit?: number } = {}) {
    const clauses: string[] = [];
    if (kind !== null) clauses.push(`kind=${quote(kind)}`);
    if (since !== null) clauses.push(`executed_at>=${since}`);
    return JSON.parse(
      this.scalar(
        `SELECT COALESCE(json_group_array(json_object('id', id, 'kind', kind, 'queue_name', queue_name, 'executed_at', executed_at, 'events_pruned', events_pruned, 'deliveries_pruned', deliveries_pruned, 'attempts_pruned', attempts_pruned, 'live_jobs_pruned', live_jobs_pruned, 'summary_json', summary_json)), '[]') FROM (SELECT * FROM knocker_prune_audits ${clauses.length === 0 ? "" : `WHERE ${clauses.join(" AND ")}`} ORDER BY executed_at DESC, id DESC LIMIT ${limit})`,
      ),
    );
  }

  runRetentionOnce({
    statuses = ["handled", "ignored"],
    eventOlderThanS = null,
    eventLimit = 1000,
    orphanDeliveriesOlderThanS = null,
    orphanDeliveriesLimit = 1000,
    queueName = "knocker.events",
    now = Math.floor(Date.now() / 1000),
  }: {
    statuses?: string[];
    eventOlderThanS?: number | null;
    eventLimit?: number;
    orphanDeliveriesOlderThanS?: number | null;
    orphanDeliveriesLimit?: number;
    queueName?: string;
    now?: number;
  } = {}) {
    const eventCutoff = eventOlderThanS === null ? "NULL" : String(now - eventOlderThanS);
    const orphanCutoff = orphanDeliveriesOlderThanS === null ? "NULL" : String(now - orphanDeliveriesOlderThanS);
    this.exec("BEGIN IMMEDIATE");
    try {
      const result = JSON.parse(
        this.scalar(`SELECT knocker_run_retention_pass(${quote(JSON.stringify(statuses))}, ${eventCutoff}, ${eventLimit}, ${orphanCutoff}, ${orphanDeliveriesLimit}, ${quote(queueName)})`),
      );
      this.exec("COMMIT");
      return result;
    } catch (error) {
      this.exec("ROLLBACK");
      throw error;
    }
  }

  async runRetention({ intervalMs = 60_000, maxRuns = null, shouldStop = null, ...policy }: {
    intervalMs?: number;
    maxRuns?: number | null;
    shouldStop?: (() => boolean) | null;
    [key: string]: unknown;
  } = {}) {
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

  registerHandler(endpoint: string, handler: (event: Record<string, unknown>, tx: KnockerTransaction) => void, eventType: string | null = null) {
    this.handlers.set(KnockerSqlite.handlerKey(endpoint, eventType), handler);
  }

  runWorkerOnce(workerId = "bun-worker") {
    const rows = JSON.parse(this.scalar(`SELECT honker_claim_batch('knocker.events', ${quote(workerId)}, 1, 60)`));
    if (rows.length === 0) return null;
    if (rows[0].max_attempts === undefined) {
      rows[0].max_attempts = Number(this.scalar(`SELECT max_attempts FROM _honker_live WHERE id=${Number(rows[0].id)}`));
    }
    const payload = JSON.parse(rows[0].payload);
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
      this.handlers.get(KnockerSqlite.handlerKey(String(event.endpoint), event.event_type === null ? null : String(event.event_type))) ??
      this.handlers.get(KnockerSqlite.handlerKey(String(event.endpoint), null));
    if (!handler) {
      const error = new Error(`no handler registered for endpoint ${String(event.endpoint)}`);
      this.#failClaim(rows[0], workerId, eventId, error, true);
      throw error;
    }
    this.exec("BEGIN IMMEDIATE");
    try {
      this.scalar(`SELECT knocker_mark_processing(${eventId}, ${Number(rows[0].attempts)})`);
      handler(event, new KnockerTransaction(this));
      this.scalar(`SELECT knocker_mark_handled(${eventId}, 0)`);
      this.scalar(`SELECT honker_ack(${Number(rows[0].id)}, ${quote(workerId)})`);
      this.exec("COMMIT");
      return eventId;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.exec("ROLLBACK");
      this.#recordFailedClaim(rows[0], workerId, eventId, message);
      throw error;
    }
  }

  async runWorker({
    workerId = "bun-worker",
    idlePollMs = 100,
    maxJobs = null,
    shouldStop = null,
    onError = null,
  }: {
    workerId?: string;
    idlePollMs?: number;
    maxJobs?: number | null;
    shouldStop?: (() => boolean) | null;
    onError?: ((error: unknown) => void) | null;
  } = {}) {
    let processed = 0;
    while (maxJobs === null || processed < maxJobs) {
      if (shouldStop?.()) break;
      try {
        const eventId = this.runWorkerOnce(workerId);
        if (eventId === null) {
          await new Promise((resolve) => setTimeout(resolve, idlePollMs));
          continue;
        }
        processed += 1;
      } catch (error) {
        if (onError) onError(error);
        else throw error;
      }
    }
    return processed;
  }

  exec(sql: string) {
    const errPtr = new BigUint64Array(1);
    const rc = sqlite.symbols.sqlite3_exec(this.db, cstr(sql), 0, 0, ptr(errPtr));
    this.#check(rc);
  }

  scalar(sql: string): string {
    const stmtPtr = new BigUint64Array(1);
    this.#check(sqlite.symbols.sqlite3_prepare_v2(this.db, cstr(sql), -1, ptr(stmtPtr), 0));
    const stmt = Number(stmtPtr[0]);
    try {
      const rc = sqlite.symbols.sqlite3_step(stmt);
      if (rc === SQLITE_DONE) return "";
      if (rc !== SQLITE_ROW) this.#check(rc);
      return sqlite.symbols.sqlite3_column_text(stmt, 0) || "";
    } finally {
      sqlite.symbols.sqlite3_finalize(stmt);
    }
  }

  #check(rc: number) {
    if (rc === 0) return;
    throw new Error(sqlite.symbols.sqlite3_errmsg(this.db) || `sqlite rc=${rc}`);
  }

  #failClaim(job: Record<string, unknown>, workerId: string, eventId: number, error: Error, terminal: boolean) {
    this.exec("BEGIN IMMEDIATE");
    try {
      this.scalar(
        `SELECT knocker_mark_failed(${eventId}, ${Number(job.attempts)}, ${quote(error.message)}, ${terminal ? 1 : 0}, 0)`,
      );
      this.scalar(`SELECT honker_fail(${Number(job.id)}, ${quote(workerId)}, ${quote(error.message)})`);
      this.exec("COMMIT");
    } catch (innerError) {
      this.exec("ROLLBACK");
      throw innerError;
    }
  }

  #recordFailedClaim(job: Record<string, unknown>, workerId: string, eventId: number, message: string) {
    const attemptCount = Number(job.attempts);
    const terminal = attemptCount >= Number(job.max_attempts);
    this.exec("BEGIN IMMEDIATE");
    try {
      this.scalar(
        `SELECT knocker_mark_failed(${eventId}, ${attemptCount}, ${quote(message)}, ${terminal ? 1 : 0}, 0)`,
      );
      if (terminal) {
        this.scalar(`SELECT honker_fail(${Number(job.id)}, ${quote(workerId)}, ${quote(message)})`);
      } else {
        this.scalar(`SELECT honker_retry(${Number(job.id)}, ${quote(workerId)}, 0, ${quote(message)})`);
      }
      this.exec("COMMIT");
    } catch (error) {
      this.exec("ROLLBACK");
      throw error;
    }
  }

  static handlerKey(endpoint: string, eventType: string | null) {
    return `${endpoint}\0${eventType ?? ""}`;
  }

  #deleteLiveJobsForEvent(eventId: number, queueName: string) {
    const rows = JSON.parse(
      this.scalar(
        `SELECT COALESCE(json_group_array(json_object('id', id, 'payload', payload)), '[]') FROM _honker_live WHERE queue=${quote(queueName)}`,
      ),
    );
    for (const row of rows) {
      try {
        const payload = JSON.parse(String(row.payload));
        if (Number(payload.event_id) === eventId) {
          this.exec(`DELETE FROM _honker_live WHERE id=${Number(row.id)}`);
        }
      } catch {
        continue;
      }
    }
  }
}

export class KnockerTransaction {
  app: KnockerSqlite;

  constructor(app: KnockerSqlite) {
    this.app = app;
  }

  exec(sql: string) {
    return this.app.exec(sql);
  }

  scalar(sql: string) {
    return this.app.scalar(sql);
  }
}

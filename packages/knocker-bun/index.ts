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
  handlers: Map<string, (event: Record<string, unknown>) => void>;

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
  }

  close() {
    this.#check(sqlite.symbols.sqlite3_close(this.db));
  }

  addEndpoint({ name, path: endpointPath, provider = null, enabled = true }: {
    name: string;
    path: string;
    provider?: string | null;
    enabled?: boolean;
  }) {
    return Number(
      this.scalar(
        `SELECT knocker_endpoint_upsert(${quote(name)}, ${quote(endpointPath)}, ${quote(provider)}, ${enabled ? 1 : 0})`,
      ),
    );
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

  replay(eventId: number) {
    this.scalar(`SELECT knocker_replay(${eventId}, 'knocker.events', 3)`);
  }

  registerHandler(endpoint: string, handler: (event: Record<string, unknown>) => void) {
    this.handlers.set(endpoint, handler);
  }

  runWorkerOnce(workerId = "bun-worker") {
    const rows = JSON.parse(this.scalar(`SELECT honker_claim_batch('knocker.events', ${quote(workerId)}, 1, 60)`));
    if (rows.length === 0) return null;
    const payload = JSON.parse(rows[0].payload);
    const eventId = Number(payload.event_id);
    const event = this.getEvent(eventId);
    const handler = this.handlers.get(String(event.endpoint));
    if (!handler) {
      const error = new Error(`no handler registered for endpoint ${String(event.endpoint)}`);
      this.#failClaim(rows[0], workerId, eventId, error, true);
      throw error;
    }
    this.exec("BEGIN IMMEDIATE");
    try {
      this.scalar(`SELECT knocker_mark_processing(${eventId}, ${Number(rows[0].attempts)})`);
      handler(event);
      this.scalar(`SELECT knocker_mark_handled(${eventId}, 0)`);
      this.scalar(`SELECT honker_ack(${Number(rows[0].id)}, ${quote(workerId)})`);
      this.exec("COMMIT");
      return eventId;
    } catch (error) {
      try {
        const message = error instanceof Error ? error.message : String(error);
        const terminal = Number(rows[0].attempts) >= Number(rows[0].max_attempts);
        this.scalar(
          `SELECT knocker_mark_failed(${eventId}, ${Number(rows[0].attempts)}, ${quote(message)}, ${terminal ? 1 : 0}, 0)`,
        );
        if (terminal) {
          this.scalar(`SELECT honker_fail(${Number(rows[0].id)}, ${quote(workerId)}, ${quote(message)})`);
        } else {
          this.scalar(`SELECT honker_retry(${Number(rows[0].id)}, ${quote(workerId)}, 0, ${quote(message)})`);
        }
        this.exec("COMMIT");
      } catch {
        this.exec("ROLLBACK");
      }
      throw error;
    }
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
}

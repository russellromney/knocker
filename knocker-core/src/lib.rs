mod knocker_ops;

pub use knocker_ops::attach_knocker_functions;

use rusqlite::{Connection, OptionalExtension, params};

#[derive(thiserror::Error, Debug)]
pub enum Error {
    #[error("Honker error: {0}")]
    Honker(#[from] honker_core::Error),
    #[error("Database error: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

pub const KNOCKER_SCHEMA_VERSION: &str = "2";

const KNOCKER_META_SQL: &str = "
    CREATE TABLE IF NOT EXISTS knocker_meta (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
";

const BOOTSTRAP_KNOCKER_SQL_V2: &str = "
    CREATE TABLE IF NOT EXISTS knocker_endpoints (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL UNIQUE,
      path TEXT NOT NULL UNIQUE,
      provider TEXT,
      enabled INTEGER NOT NULL DEFAULT 1,
      created_at INTEGER NOT NULL DEFAULT (unixepoch())
    );

    CREATE TABLE IF NOT EXISTS knocker_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      endpoint_id INTEGER NOT NULL REFERENCES knocker_endpoints(id),
      received_at INTEGER NOT NULL DEFAULT (unixepoch()),
      provider_event_id TEXT,
      provider_delivery_id TEXT,
      event_type TEXT,
      method TEXT NOT NULL,
      headers_json TEXT NOT NULL,
      body_blob BLOB NOT NULL,
      query_json TEXT NOT NULL,
      dedupe_key TEXT,
      status TEXT NOT NULL CHECK (
        status IN ('received', 'processing', 'handled', 'failed', 'dead', 'ignored')
      ),
      attempt_count INTEGER NOT NULL DEFAULT 0,
      handled_at INTEGER,
      last_error TEXT
    );

    CREATE UNIQUE INDEX IF NOT EXISTS knocker_events_dedupe
      ON knocker_events(endpoint_id, dedupe_key)
      WHERE dedupe_key IS NOT NULL;

    CREATE INDEX IF NOT EXISTS knocker_events_status
      ON knocker_events(status, endpoint_id, event_type, id);

    CREATE TABLE IF NOT EXISTS knocker_deliveries (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      endpoint_id INTEGER NOT NULL REFERENCES knocker_endpoints(id),
      event_id INTEGER REFERENCES knocker_events(id),
      received_at INTEGER NOT NULL DEFAULT (unixepoch()),
      provider_event_id TEXT,
      provider_delivery_id TEXT,
      event_type TEXT,
      method TEXT NOT NULL,
      headers_json TEXT NOT NULL,
      body_blob BLOB NOT NULL,
      query_json TEXT NOT NULL,
      signature_valid INTEGER,
      signature_error TEXT,
      dedupe_key TEXT
    );

    CREATE INDEX IF NOT EXISTS knocker_deliveries_event
      ON knocker_deliveries(event_id, id);

    CREATE INDEX IF NOT EXISTS knocker_deliveries_endpoint_received
      ON knocker_deliveries(endpoint_id, received_at, id);

    CREATE TABLE IF NOT EXISTS knocker_attempts (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      event_id INTEGER NOT NULL REFERENCES knocker_events(id) ON DELETE CASCADE,
      attempted_at INTEGER NOT NULL DEFAULT (unixepoch()),
      outcome TEXT NOT NULL,
      error TEXT,
      duration_ms INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS knocker_attempts_event
      ON knocker_attempts(event_id, id);
";

pub fn bootstrap_knocker_schema(conn: &Connection) -> Result<(), Error> {
    honker_core::bootstrap_honker_schema(conn)?;
    conn.execute_batch(KNOCKER_META_SQL)?;

    let schema_version: Option<String> = conn
        .query_row(
            "SELECT value FROM knocker_meta WHERE key='schema_version'",
            [],
            |row| row.get(0),
        )
        .optional()?;

    match schema_version.as_deref() {
        None => {
            conn.execute_batch(BOOTSTRAP_KNOCKER_SQL_V2)?;
            set_schema_version(conn, KNOCKER_SCHEMA_VERSION)?;
        }
        Some("1") => migrate_v1_to_v2(conn)?,
        Some(KNOCKER_SCHEMA_VERSION) => {
            conn.execute_batch(BOOTSTRAP_KNOCKER_SQL_V2)?;
            set_schema_version(conn, KNOCKER_SCHEMA_VERSION)?;
        }
        Some(other) => {
            return Err(rusqlite::Error::InvalidParameterName(format!(
                "unsupported knocker schema version: {other}"
            ))
            .into());
        }
    }

    Ok(())
}

fn migrate_v1_to_v2(conn: &Connection) -> rusqlite::Result<()> {
    conn.execute_batch(BOOTSTRAP_KNOCKER_SQL_V2)?;
    conn.execute(
        "
        INSERT INTO knocker_deliveries (
            endpoint_id,
            event_id,
            received_at,
            provider_event_id,
            provider_delivery_id,
            event_type,
            method,
            headers_json,
            body_blob,
            query_json,
            signature_valid,
            signature_error,
            dedupe_key
        )
        SELECT
            e.endpoint_id,
            e.id,
            e.received_at,
            e.provider_event_id,
            e.provider_delivery_id,
            e.event_type,
            e.method,
            e.headers_json,
            e.body_blob,
            e.query_json,
            e.signature_valid,
            e.signature_error,
            e.dedupe_key
        FROM knocker_events e
        WHERE NOT EXISTS (
            SELECT 1
            FROM knocker_deliveries d
            WHERE d.event_id = e.id
        )
        ",
        [],
    )?;

    if column_exists(conn, "knocker_events", "signature_valid")? {
        conn.execute_batch("ALTER TABLE knocker_events DROP COLUMN signature_valid;")?;
    }
    if column_exists(conn, "knocker_events", "signature_error")? {
        conn.execute_batch("ALTER TABLE knocker_events DROP COLUMN signature_error;")?;
    }

    set_schema_version(conn, KNOCKER_SCHEMA_VERSION)?;
    Ok(())
}

fn set_schema_version(conn: &Connection, version: &str) -> rusqlite::Result<()> {
    conn.execute(
        "
        INSERT INTO knocker_meta (key, value)
        VALUES ('schema_version', ?1)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        ",
        params![version],
    )?;
    Ok(())
}

fn column_exists(conn: &Connection, table_name: &str, column_name: &str) -> rusqlite::Result<bool> {
    let pragma_sql = format!("PRAGMA table_info({table_name})");
    let mut stmt = conn.prepare(&pragma_sql)?;
    let mut rows = stmt.query([])?;
    while let Some(row) = rows.next()? {
        let name: String = row.get(1)?;
        if name == column_name {
            return Ok(true);
        }
    }
    Ok(false)
}

#[cfg(test)]
mod tests {
    use super::*;
    use honker_core::{attach_honker_functions, open_conn};
    use tempfile::NamedTempFile;

    fn open_test_conn() -> Connection {
        let file = NamedTempFile::new().unwrap();
        let path = file.path().to_string_lossy().to_string();
        std::mem::forget(file);
        let conn = open_conn(&path, true).unwrap();
        attach_honker_functions(&conn).unwrap();
        attach_knocker_functions(&conn).unwrap();
        conn
    }

    fn insert_endpoint(conn: &Connection, name: &str, path: &str, provider: &str) {
        conn.query_row(
            "SELECT knocker_endpoint_upsert(?1, ?2, ?3, ?4)",
            params![name, path, provider, 1],
            |_| Ok(()),
        )
        .unwrap();
    }

    #[test]
    fn bootstrap_is_idempotent() {
        let conn = open_test_conn();

        bootstrap_knocker_schema(&conn).unwrap();
        bootstrap_knocker_schema(&conn).unwrap();

        let version: String = conn
            .query_row(
                "SELECT value FROM knocker_meta WHERE key='schema_version'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(version, KNOCKER_SCHEMA_VERSION);
    }

    #[test]
    fn ingest_stores_delivery_event_and_enqueues_job() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "stripe", "/webhooks/stripe", "stripe");

        let result_json: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "stripe",
                    "POST",
                    "{}",
                    b"{\"id\":\"evt_1\"}".to_vec(),
                    "{}",
                    1,
                    Option::<String>::None,
                    Some("evt_1"),
                    Some("delivery_1"),
                    Some("checkout.session.completed"),
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();

        let result: serde_json::Value = serde_json::from_str(&result_json).unwrap();
        assert_eq!(result["duplicate"], 0);
        assert_eq!(result["status_code"], 204);
        assert!(result["delivery_id"].as_i64().is_some());
        assert!(result["event_id"].as_i64().is_some());

        let event_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM knocker_events", [], |row| row.get(0))
            .unwrap();
        assert_eq!(event_count, 1);

        let delivery_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM knocker_deliveries", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(delivery_count, 1);

        let queue_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(queue_count, 1);
    }

    #[test]
    fn ingest_duplicate_valid_creates_second_delivery_and_does_not_enqueue_twice() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "github", "/webhooks/github", "github");

        let first: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "github",
                    "POST",
                    "{}",
                    b"{}".to_vec(),
                    "{}",
                    1,
                    Option::<String>::None,
                    Option::<String>::None,
                    Some("delivery_1"),
                    Option::<String>::None,
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();
        let second: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "github",
                    "POST",
                    "{}",
                    b"{}".to_vec(),
                    "{}",
                    1,
                    Option::<String>::None,
                    Option::<String>::None,
                    Some("delivery_1"),
                    Option::<String>::None,
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();

        let first_id = serde_json::from_str::<serde_json::Value>(&first).unwrap()["event_id"]
            .as_i64()
            .unwrap();
        let second_val = serde_json::from_str::<serde_json::Value>(&second).unwrap();
        assert_eq!(second_val["duplicate"], 1);
        assert_eq!(second_val["event_id"], first_id);

        let event_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM knocker_events", [], |row| row.get(0))
            .unwrap();
        assert_eq!(event_count, 1);

        let delivery_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM knocker_deliveries", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(delivery_count, 2);

        let queue_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(queue_count, 1);
    }

    #[test]
    fn dead_duplicate_ingest_stores_delivery_without_mutating_event_or_queue() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "stripe", "/webhooks/stripe", "stripe");

        let first: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "stripe",
                    "POST",
                    "{}",
                    b"{\"id\":\"evt_dead\"}".to_vec(),
                    "{}",
                    1,
                    Option::<String>::None,
                    Some("evt_dead"),
                    Some("delivery_1"),
                    Option::<String>::None,
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();
        let event_id = serde_json::from_str::<serde_json::Value>(&first).unwrap()["event_id"]
            .as_i64()
            .unwrap();
        conn.query_row(
            "SELECT knocker_mark_failed(?1, ?2, ?3, ?4, ?5)",
            params![event_id, 1i64, "boom", 1i64, 7i64],
            |_| Ok(()),
        )
        .unwrap();
        conn.execute("DELETE FROM _honker_live", []).unwrap();

        let second: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "stripe",
                    "POST",
                    "{}",
                    b"{\"id\":\"evt_dead\",\"n\":2}".to_vec(),
                    "{}",
                    1,
                    Option::<String>::None,
                    Some("evt_dead"),
                    Some("delivery_2"),
                    Option::<String>::None,
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();

        let second_val = serde_json::from_str::<serde_json::Value>(&second).unwrap();
        assert_eq!(second_val["duplicate"], 1);
        assert_eq!(second_val["event_id"], event_id);

        let status: String = conn
            .query_row(
                "SELECT status FROM knocker_events WHERE id=?1",
                params![event_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "dead");

        let delivery_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM knocker_deliveries WHERE event_id=?1",
                params![event_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(delivery_count, 2);

        let queue_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM _honker_live", [], |row| row.get(0))
            .unwrap();
        assert_eq!(queue_count, 0);
    }

    #[test]
    fn invalid_signature_creates_delivery_only_and_is_not_enqueued() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "slack", "/webhooks/slack", "slack");

        let result_json: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "slack",
                    "POST",
                    "{}",
                    b"{}".to_vec(),
                    "{}",
                    0,
                    Some("bad signature"),
                    Option::<String>::None,
                    Option::<String>::None,
                    Option::<String>::None,
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();

        let result: serde_json::Value = serde_json::from_str(&result_json).unwrap();
        assert_eq!(result["status_code"], 401);
        assert!(result["event_id"].is_null());
        assert!(result["delivery_id"].as_i64().is_some());

        let event_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM knocker_events", [], |row| row.get(0))
            .unwrap();
        assert_eq!(event_count, 0);

        let delivery: (Option<i64>, Option<String>) = conn
            .query_row(
                "SELECT signature_valid, signature_error FROM knocker_deliveries",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(delivery.0, Some(0));
        assert_eq!(delivery.1.as_deref(), Some("bad signature"));

        let queue_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM _honker_live", [], |row| row.get(0))
            .unwrap();
        assert_eq!(queue_count, 0);
    }

    #[test]
    fn invalid_first_valid_later_creates_two_deliveries_and_one_event() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "stripe", "/webhooks/stripe", "stripe");

        let first: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "stripe",
                    "POST",
                    "{\"stripe-signature\":\"bad\"}",
                    b"{\"id\":\"evt_1\"}".to_vec(),
                    "{}",
                    0,
                    Some("bad signature"),
                    Some("evt_1"),
                    Some("delivery_1"),
                    Some("checkout.session.completed"),
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();
        let second: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "stripe",
                    "POST",
                    "{\"stripe-signature\":\"good\"}",
                    b"{\"id\":\"evt_1\"}".to_vec(),
                    "{}",
                    1,
                    Option::<String>::None,
                    Some("evt_1"),
                    Some("delivery_1"),
                    Some("checkout.session.completed"),
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();

        let first_val = serde_json::from_str::<serde_json::Value>(&first).unwrap();
        let second_val = serde_json::from_str::<serde_json::Value>(&second).unwrap();
        assert_eq!(first_val["status_code"], 401);
        assert_eq!(second_val["status_code"], 204);
        assert_eq!(second_val["duplicate"], 0);
        assert!(first_val["event_id"].is_null());

        let event: (String, Option<String>, Option<String>) = conn
            .query_row(
                "
                SELECT status, provider_event_id, provider_delivery_id
                FROM knocker_events
                WHERE id=1
                ",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(event.0, "received");
        assert_eq!(event.1.as_deref(), Some("evt_1"));
        assert_eq!(event.2.as_deref(), Some("delivery_1"));

        let delivery_rows: Vec<(Option<i64>, Option<i64>, Option<String>)> = conn
            .prepare(
                "
                SELECT event_id, signature_valid, signature_error
                FROM knocker_deliveries
                ORDER BY id
                ",
            )
            .unwrap()
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        assert_eq!(
            delivery_rows,
            vec![
                (None, Some(0), Some("bad signature".to_string())),
                (Some(1), Some(1), None),
            ]
        );

        let queue_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM _honker_live", [], |row| row.get(0))
            .unwrap();
        assert_eq!(queue_count, 1);
    }

    #[test]
    fn migration_from_v1_backfills_deliveries_and_drops_event_verification_fields() {
        let conn = open_test_conn();
        honker_core::bootstrap_honker_schema(&conn).unwrap();
        conn.execute_batch(
            "
            CREATE TABLE IF NOT EXISTS knocker_meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            INSERT INTO knocker_meta(key, value)
              VALUES ('schema_version', '1')
              ON CONFLICT(key) DO UPDATE SET value=excluded.value;

            CREATE TABLE IF NOT EXISTS knocker_endpoints (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              path TEXT NOT NULL UNIQUE,
              provider TEXT,
              enabled INTEGER NOT NULL DEFAULT 1,
              created_at INTEGER NOT NULL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS knocker_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              endpoint_id INTEGER NOT NULL REFERENCES knocker_endpoints(id),
              received_at INTEGER NOT NULL DEFAULT (unixepoch()),
              provider_event_id TEXT,
              provider_delivery_id TEXT,
              event_type TEXT,
              method TEXT NOT NULL,
              headers_json TEXT NOT NULL,
              body_blob BLOB NOT NULL,
              query_json TEXT NOT NULL,
              signature_valid INTEGER,
              signature_error TEXT,
              dedupe_key TEXT,
              status TEXT NOT NULL CHECK (
                status IN ('received', 'processing', 'handled', 'failed', 'dead', 'ignored')
              ),
              attempt_count INTEGER NOT NULL DEFAULT 0,
              handled_at INTEGER,
              last_error TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS knocker_events_dedupe
              ON knocker_events(endpoint_id, dedupe_key)
              WHERE dedupe_key IS NOT NULL;

            CREATE INDEX IF NOT EXISTS knocker_events_status
              ON knocker_events(status, endpoint_id, event_type, id);

            CREATE TABLE IF NOT EXISTS knocker_attempts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id INTEGER NOT NULL REFERENCES knocker_events(id) ON DELETE CASCADE,
              attempted_at INTEGER NOT NULL DEFAULT (unixepoch()),
              outcome TEXT NOT NULL,
              error TEXT,
              duration_ms INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS knocker_attempts_event
              ON knocker_attempts(event_id, id);

            INSERT INTO knocker_endpoints (id, name, path, provider, enabled)
              VALUES (1, 'stripe', '/webhooks/stripe', 'stripe', 1);

            INSERT INTO knocker_events (
              id,
              endpoint_id,
              provider_event_id,
              provider_delivery_id,
              event_type,
              method,
              headers_json,
              body_blob,
              query_json,
              signature_valid,
              signature_error,
              dedupe_key,
              status
            )
            VALUES (
              1,
              1,
              'evt_legacy',
              'delivery_legacy',
              'checkout.session.completed',
              'POST',
              '{}',
              X'7B7D',
              '{}',
              1,
              NULL,
              'evt_legacy',
              'received'
            );
            ",
        )
        .unwrap();

        bootstrap_knocker_schema(&conn).unwrap();

        let version: String = conn
            .query_row(
                "SELECT value FROM knocker_meta WHERE key='schema_version'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(version, KNOCKER_SCHEMA_VERSION);

        let delivery_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM knocker_deliveries WHERE event_id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(delivery_count, 1);

        assert!(!column_exists(&conn, "knocker_events", "signature_valid").unwrap());
        assert!(!column_exists(&conn, "knocker_events", "signature_error").unwrap());
    }

    #[test]
    fn mark_handled_records_attempt_history() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "stripe", "/webhooks/stripe", "stripe");
        conn.query_row(
            "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
            params![
                "stripe",
                "POST",
                "{}",
                b"{}".to_vec(),
                "{}",
                1,
                Option::<String>::None,
                Option::<String>::None,
                Some("delivery_1"),
                Option::<String>::None,
                Option::<String>::None,
                "knocker.events",
                3,
            ],
            |_| Ok(()),
        )
        .unwrap();

        conn.query_row(
            "SELECT knocker_mark_processing(?1, ?2)",
            params![1i64, 1i64],
            |_| Ok(()),
        )
        .unwrap();
        conn.query_row(
            "SELECT knocker_mark_handled(?1, ?2)",
            params![1i64, 12i64],
            |_| Ok(()),
        )
        .unwrap();

        let status: String = conn
            .query_row("SELECT status FROM knocker_events WHERE id=1", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(status, "handled");

        let outcomes: Vec<String> = conn
            .prepare("SELECT outcome FROM knocker_attempts WHERE event_id=1 ORDER BY id")
            .unwrap()
            .query_map([], |row| row.get(0))
            .unwrap()
            .map(Result::unwrap)
            .collect();
        assert_eq!(outcomes, vec!["handled".to_string()]);
    }

    #[test]
    fn lifecycle_udfs_fail_on_unknown_event_ids() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();

        let calls = [
            "SELECT knocker_mark_processing(?1, ?2)",
            "SELECT knocker_mark_handled(?1, ?2)",
            "SELECT knocker_mark_failed(?1, ?2, ?3, ?4, ?5)",
            "SELECT knocker_mark_ignored(?1, ?2)",
        ];

        let processing = conn.query_row(calls[0], params![404i64, 1i64], |_| Ok(()));
        let handled = conn.query_row(calls[1], params![404i64, 10i64], |_| Ok(()));
        let failed = conn.query_row(
            calls[2],
            params![404i64, 1i64, "boom", 1i64, 10i64],
            |_| Ok(()),
        );
        let ignored = conn.query_row(calls[3], params![404i64, 10i64], |_| Ok(()));

        assert!(processing.is_err());
        assert!(handled.is_err());
        assert!(failed.is_err());
        assert!(ignored.is_err());

        let attempt_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM knocker_attempts", [], |row| row.get(0))
            .unwrap();
        assert_eq!(attempt_count, 0);
    }

    #[test]
    fn deleting_event_cascades_attempts_when_foreign_keys_are_enabled() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "stripe", "/webhooks/stripe", "stripe");
        let result_json: String = conn
            .query_row(
                "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
                params![
                    "stripe",
                    "POST",
                    "{}",
                    b"{\"id\":\"evt_fk\"}".to_vec(),
                    "{}",
                    1,
                    Option::<String>::None,
                    Some("evt_fk"),
                    Option::<String>::None,
                    Option::<String>::None,
                    Option::<String>::None,
                    "knocker.events",
                    3,
                ],
                |row| row.get(0),
            )
            .unwrap();
        let event_id = serde_json::from_str::<serde_json::Value>(&result_json).unwrap()["event_id"]
            .as_i64()
            .unwrap();

        conn.query_row(
            "SELECT knocker_mark_handled(?1, ?2)",
            params![event_id, 11i64],
            |_| Ok(()),
        )
        .unwrap();
        conn.execute(
            "DELETE FROM knocker_deliveries WHERE event_id=?1",
            params![event_id],
        )
        .unwrap();
        conn.execute("DELETE FROM knocker_events WHERE id=?1", params![event_id])
            .unwrap();

        let attempt_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM knocker_attempts WHERE event_id=?1",
                params![event_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(attempt_count, 0);
    }

    #[test]
    fn requeue_requires_failed_like_state_and_replay_resets_attempt_count() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "stripe", "/webhooks/stripe", "stripe");
        conn.query_row(
            "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
            params![
                "stripe",
                "POST",
                "{}",
                b"{}".to_vec(),
                "{}",
                1,
                Option::<String>::None,
                Option::<String>::None,
                Some("delivery_2"),
                Option::<String>::None,
                Option::<String>::None,
                "knocker.events",
                3,
            ],
            |_| Ok(()),
        )
        .unwrap();
        conn.execute("DELETE FROM _honker_live", []).unwrap();

        let err = conn.query_row(
            "SELECT knocker_requeue(?1, ?2, ?3)",
            params![1i64, "knocker.events", 3i64],
            |_| Ok(()),
        );
        assert!(err.is_err());

        conn.query_row(
            "SELECT knocker_mark_failed(?1, ?2, ?3, ?4, ?5)",
            params![1i64, 2i64, "boom", 1i64, 7i64],
            |_| Ok(()),
        )
        .unwrap();
        conn.query_row(
            "SELECT knocker_requeue(?1, ?2, ?3)",
            params![1i64, "knocker.events", 3i64],
            |_| Ok(()),
        )
        .unwrap();

        let state: (String, i64) = conn
            .query_row(
                "SELECT status, attempt_count FROM knocker_events WHERE id=1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(state.0, "received");
        assert_eq!(state.1, 0);

        conn.execute("DELETE FROM _honker_live", []).unwrap();
        let err = conn.query_row(
            "SELECT knocker_replay(?1, ?2, ?3)",
            params![1i64, "knocker.events", 3i64],
            |_| Ok(()),
        );
        assert!(err.is_err());

        conn.query_row(
            "SELECT knocker_mark_handled(?1, ?2)",
            params![1i64, 0i64],
            |_| Ok(()),
        )
        .unwrap();
        conn.query_row(
            "SELECT knocker_replay(?1, ?2, ?3)",
            params![1i64, "knocker.events", 3i64],
            |_| Ok(()),
        )
        .unwrap();

        let queue_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(queue_count, 1);
    }

    #[test]
    fn requeue_clears_live_jobs_with_delivery_replay_payloads() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "stripe", "/webhooks/stripe", "stripe");
        conn.query_row(
            "SELECT knocker_ingest(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13)",
            params![
                "stripe",
                "POST",
                "{}",
                b"{}".to_vec(),
                "{}",
                1,
                Option::<String>::None,
                Option::<String>::None,
                Some("delivery_replay"),
                Option::<String>::None,
                Option::<String>::None,
                "knocker.events",
                3,
            ],
            |_| Ok(()),
        )
        .unwrap();
        conn.query_row(
            "SELECT knocker_mark_failed(?1, ?2, ?3, ?4, ?5)",
            params![1i64, 1i64, "boom", 1i64, 10i64],
            |_| Ok(()),
        )
        .unwrap();
        conn.query_row(
            "SELECT honker_enqueue(?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                "knocker.events",
                serde_json::json!({"event_id": 1, "delivery_id": 1}).to_string(),
                Option::<i64>::None,
                Option::<i64>::None,
                0i64,
                3i64,
                Option::<i64>::None,
            ],
            |_| Ok(()),
        )
        .unwrap();

        conn.query_row(
            "SELECT knocker_requeue(?1, ?2, ?3)",
            params![1i64, "knocker.events", 3i64],
            |_| Ok(()),
        )
        .unwrap();

        let queue_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM _honker_live WHERE queue='knocker.events'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(queue_count, 1);
    }
}

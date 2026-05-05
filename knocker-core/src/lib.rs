mod knocker_ops;

pub use knocker_ops::attach_knocker_functions;

use rusqlite::{Connection, params};

#[derive(thiserror::Error, Debug)]
pub enum Error {
    #[error("Honker error: {0}")]
    Honker(#[from] honker_core::Error),
    #[error("Database error: {0}")]
    Sqlite(#[from] rusqlite::Error),
}

const BOOTSTRAP_KNOCKER_SQL: &str = "
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

    CREATE TABLE IF NOT EXISTS knocker_prune_audits (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      kind TEXT NOT NULL,
      queue_name TEXT NOT NULL,
      executed_at INTEGER NOT NULL DEFAULT (unixepoch()),
      events_pruned INTEGER,
      deliveries_pruned INTEGER,
      attempts_pruned INTEGER,
      live_jobs_pruned INTEGER,
      summary_json TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS knocker_prune_audits_kind_executed
      ON knocker_prune_audits(kind, executed_at DESC, id DESC);
";

pub fn bootstrap_knocker_schema(conn: &Connection) -> Result<(), Error> {
    honker_core::bootstrap_honker_schema(conn)?;
    conn.execute_batch(BOOTSTRAP_KNOCKER_SQL)?;
    ensure_current_schema(conn)?;
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

fn table_exists(conn: &Connection, table_name: &str) -> rusqlite::Result<bool> {
    let count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?1",
        params![table_name],
        |row| row.get(0),
    )?;
    Ok(count == 1)
}

fn ensure_current_schema(conn: &Connection) -> rusqlite::Result<()> {
    let required_tables = [
        "knocker_endpoints",
        "knocker_events",
        "knocker_deliveries",
        "knocker_attempts",
        "knocker_prune_audits",
    ];
    for table_name in required_tables {
        if !table_exists(conn, table_name)? {
            return Err(rusqlite::Error::InvalidParameterName(format!(
                "unsupported knocker schema: missing required table {table_name}; rebuild the database with the current schema"
            )));
        }
    }
    if column_exists(conn, "knocker_events", "signature_valid")?
        || column_exists(conn, "knocker_events", "signature_error")?
    {
        return Err(rusqlite::Error::InvalidParameterName(
            "unsupported legacy knocker schema: rebuild the database with the current schema"
                .to_string(),
        ));
    }
    Ok(())
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

        let table_exists: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='knocker_prune_audits'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(table_exists, 1);
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
    fn bootstrap_rejects_legacy_event_schema() {
        let conn = open_test_conn();
        honker_core::bootstrap_honker_schema(&conn).unwrap();
        conn.execute_batch(
            "
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

        let err = bootstrap_knocker_schema(&conn).unwrap_err().to_string();
        assert!(err.contains("unsupported legacy knocker schema"));
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
        let failed = conn.query_row(calls[2], params![404i64, 1i64, "boom", 1i64, 10i64], |_| {
            Ok(())
        });
        let ignored = conn.query_row(calls[3], params![404i64, 10i64], |_| Ok(()));

        assert!(processing.is_err());
        assert!(handled.is_err());
        assert!(failed.is_err());
        assert!(ignored.is_err());

        let attempt_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM knocker_attempts", [], |row| {
                row.get(0)
            })
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

    #[test]
    fn knocker_reset_event_resets_status_attempt_count_and_clears_errors() {
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
            "SELECT knocker_mark_handled(?1, ?2)",
            params![1i64, 0i64],
            |_| Ok(()),
        )
        .unwrap();

        conn.query_row("SELECT knocker_reset_event(?1)", params![1i64], |_| Ok(()))
            .unwrap();

        let state: (String, i64, Option<String>, Option<i64>) = conn
            .query_row(
                "SELECT status, attempt_count, last_error, handled_at FROM knocker_events WHERE id=1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(state.0, "received");
        assert_eq!(state.1, 0);
        assert_eq!(state.2, None::<String>);
        assert_eq!(state.3, None::<i64>);
    }

    #[test]
    fn knocker_reset_event_does_not_record_attempt_history() {
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
            "SELECT knocker_mark_handled(?1, ?2)",
            params![1i64, 0i64],
            |_| Ok(()),
        )
        .unwrap();

        let before: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM knocker_attempts WHERE event_id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(before, 1);

        conn.query_row("SELECT knocker_reset_event(?1)", params![1i64], |_| Ok(()))
            .unwrap();

        let after: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM knocker_attempts WHERE event_id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(after, 1);
    }

    #[test]
    fn receive_verifies_github_and_records_invalid_signature_as_orphan_delivery() {
        let conn = open_test_conn();
        bootstrap_knocker_schema(&conn).unwrap();
        insert_endpoint(&conn, "github", "/webhooks/github", "github");

        let body = br#"{"zen":"keep it logically awesome"}"#.to_vec();
        let result_json: String = conn
            .query_row(
                "SELECT knocker_receive(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
                params![
                    "github",
                    "github",
                    "[\"github-secret\"]",
                    "{}",
                    "POST",
                    "{\"X-Hub-Signature-256\":\"sha256=2467a1987473c6ee89a22fe24f010dca30cb93d575240b9689ab697bed4b6eab\",\"X-GitHub-Delivery\":\"delivery-abc\",\"X-GitHub-Event\":\"push\"}",
                    body.clone(),
                    "{}",
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
        assert_eq!(result["status_code"], 204);

        let event: (String, String) = conn
            .query_row(
                "SELECT provider_delivery_id, event_type FROM knocker_events WHERE id=?1",
                params![result["event_id"].as_i64().unwrap()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(event.0, "delivery-abc");
        assert_eq!(event.1, "push");

        let rejected_json: String = conn
            .query_row(
                "SELECT knocker_receive(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
                params![
                    "github",
                    "github",
                    "[\"github-secret\"]",
                    "{}",
                    "POST",
                    "{\"X-Hub-Signature-256\":\"sha256=bad\",\"X-GitHub-Delivery\":\"delivery-bad\",\"X-GitHub-Event\":\"push\"}",
                    body,
                    "{}",
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
        let rejected: serde_json::Value = serde_json::from_str(&rejected_json).unwrap();
        assert_eq!(rejected["status_code"], 401);
        assert!(rejected["event_id"].is_null());

        let orphan: (Option<i64>, i64) = conn
            .query_row(
                "SELECT event_id, signature_valid FROM knocker_deliveries WHERE id=?1",
                params![rejected["delivery_id"].as_i64().unwrap()],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(orphan.0, None);
        assert_eq!(orphan.1, 0);
    }

    #[test]
    fn receive_valid_fixtures_cover_every_curated_provider() {
        let providers = [
            "github",
            "stripe",
            "shopify",
            "slack",
            "postmark",
            "resend",
            "paddle",
            "lemon-squeezy",
        ];

        for provider in providers {
            let conn = open_test_conn();
            bootstrap_knocker_schema(&conn).unwrap();
            insert_endpoint(&conn, provider, &format!("/webhooks/{provider}"), provider);

            let fixture_path = std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .join("providers")
                .join(provider)
                .join("fixtures")
                .join("valid.json");
            let fixture_text = std::fs::read_to_string(&fixture_path)
                .unwrap_or_else(|err| panic!("read {fixture_path:?}: {err}"));
            let fixture: serde_json::Value = serde_json::from_str(&fixture_text).unwrap();
            let request = &fixture["request"];
            let expected = &fixture["expected"];
            let mut options = request
                .get("provider_options")
                .cloned()
                .unwrap_or_else(|| serde_json::json!({}));
            if request.get("now_s").is_some() {
                options["tolerance_s"] = serde_json::json!(10_000_000_000i64);
            }

            let result_json: String = conn
                .query_row(
                    "SELECT knocker_receive(?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
                    params![
                        provider,
                        provider,
                        serde_json::to_string(&request["secrets"]).unwrap(),
                        options.to_string(),
                        request["method"].as_str().unwrap_or("POST"),
                        serde_json::to_string(&request["headers"]).unwrap(),
                        request["body"].as_str().unwrap().as_bytes().to_vec(),
                        serde_json::to_string(&request["query"]).unwrap(),
                        Option::<String>::None,
                        Option::<String>::None,
                        Option::<String>::None,
                        Option::<String>::None,
                        "knocker.events",
                        3,
                    ],
                    |row| row.get(0),
                )
                .unwrap_or_else(|err| panic!("{provider} receive fixture failed: {err}"));
            let result: serde_json::Value = serde_json::from_str(&result_json).unwrap();
            assert_eq!(result["status_code"], 204, "{provider}");
            assert_eq!(result["duplicate"], 0, "{provider}");

            let row: (
                Option<String>,
                Option<String>,
                Option<String>,
                String,
                Vec<u8>,
            ) = conn
                .query_row(
                    "SELECT provider_event_id, provider_delivery_id, event_type, status, body_blob FROM knocker_events WHERE id=?1",
                    params![result["event_id"].as_i64().unwrap()],
                    |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
                )
                .unwrap();

            assert_eq!(
                row.0,
                expected_string(expected, "provider_event_id"),
                "{provider}"
            );
            assert_eq!(
                row.1,
                expected_string(expected, "provider_delivery_id"),
                "{provider}"
            );
            assert_eq!(row.2, expected_string(expected, "event_type"), "{provider}");
            assert_eq!(row.3, "received", "{provider}");
            assert_eq!(
                row.4,
                request["body"].as_str().unwrap().as_bytes(),
                "{provider}"
            );
        }
    }

    fn expected_string(value: &serde_json::Value, key: &str) -> Option<String> {
        value
            .get(key)
            .and_then(|inner| inner.as_str())
            .map(str::to_owned)
    }
}

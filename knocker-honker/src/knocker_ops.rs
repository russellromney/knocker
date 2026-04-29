use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, OptionalExtension, params};

fn to_sql_err<E: std::fmt::Display>(e: E) -> rusqlite::Error {
    rusqlite::Error::UserFunctionError(Box::new(std::io::Error::other(e.to_string())))
}

pub fn attach_knocker_functions(conn: &Connection) -> rusqlite::Result<()> {
    conn.create_scalar_function("knocker_bootstrap", 0, FunctionFlags::SQLITE_UTF8, |ctx| {
        let db = unsafe { ctx.get_connection() }?;
        super::bootstrap_knocker_schema(&db).map_err(to_sql_err)?;
        Ok(1i64)
    })?;

    conn.create_scalar_function(
        "knocker_endpoint_upsert",
        4,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let name: String = ctx.get(0)?;
            let path: String = ctx.get(1)?;
            let provider: Option<String> = ctx.get(2)?;
            let enabled: i64 = ctx.get(3)?;
            let db = unsafe { ctx.get_connection() }?;
            endpoint_upsert(&db, &name, &path, provider.as_deref(), enabled != 0)
                .map_err(to_sql_err)
        },
    )?;

    conn.create_scalar_function("knocker_ingest", 13, FunctionFlags::SQLITE_UTF8, |ctx| {
        let endpoint_name: String = ctx.get(0)?;
        let method: String = ctx.get(1)?;
        let headers_json: String = ctx.get(2)?;
        let body_blob: Vec<u8> = ctx.get(3)?;
        let query_json: String = ctx.get(4)?;
        let signature_valid: Option<i64> = ctx.get(5)?;
        let signature_error: Option<String> = ctx.get(6)?;
        let provider_event_id: Option<String> = ctx.get(7)?;
        let provider_delivery_id: Option<String> = ctx.get(8)?;
        let event_type: Option<String> = ctx.get(9)?;
        let dedupe_key: Option<String> = ctx.get(10)?;
        let queue_name: String = ctx.get(11)?;
        let max_attempts: i64 = ctx.get(12)?;
        let db = unsafe { ctx.get_connection() }?;
        ingest(
            &db,
            &endpoint_name,
            &method,
            &headers_json,
            &body_blob,
            &query_json,
            signature_valid,
            signature_error.as_deref(),
            provider_event_id.as_deref(),
            provider_delivery_id.as_deref(),
            event_type.as_deref(),
            dedupe_key.as_deref(),
            &queue_name,
            max_attempts,
        )
        .map_err(to_sql_err)
    })?;

    conn.create_scalar_function(
        "knocker_mark_processing",
        2,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let event_id: i64 = ctx.get(0)?;
            let attempt_count: i64 = ctx.get(1)?;
            let db = unsafe { ctx.get_connection() }?;
            mark_processing(&db, event_id, attempt_count).map_err(to_sql_err)
        },
    )?;

    conn.create_scalar_function(
        "knocker_mark_handled",
        2,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let event_id: i64 = ctx.get(0)?;
            let duration_ms: i64 = ctx.get(1)?;
            let db = unsafe { ctx.get_connection() }?;
            mark_handled(&db, event_id, duration_ms).map_err(to_sql_err)
        },
    )?;

    conn.create_scalar_function(
        "knocker_mark_failed",
        5,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let event_id: i64 = ctx.get(0)?;
            let attempt_count: i64 = ctx.get(1)?;
            let error: String = ctx.get(2)?;
            let terminal: i64 = ctx.get(3)?;
            let duration_ms: i64 = ctx.get(4)?;
            let db = unsafe { ctx.get_connection() }?;
            mark_failed(
                &db,
                event_id,
                attempt_count,
                &error,
                terminal != 0,
                duration_ms,
            )
            .map_err(to_sql_err)
        },
    )?;

    conn.create_scalar_function(
        "knocker_mark_ignored",
        2,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let event_id: i64 = ctx.get(0)?;
            let duration_ms: i64 = ctx.get(1)?;
            let db = unsafe { ctx.get_connection() }?;
            mark_ignored(&db, event_id, duration_ms).map_err(to_sql_err)
        },
    )?;

    conn.create_scalar_function("knocker_replay", 3, FunctionFlags::SQLITE_UTF8, |ctx| {
        let event_id: i64 = ctx.get(0)?;
        let queue_name: String = ctx.get(1)?;
        let max_attempts: i64 = ctx.get(2)?;
        let db = unsafe { ctx.get_connection() }?;
        replay(&db, event_id, &queue_name, max_attempts).map_err(to_sql_err)
    })?;

    conn.create_scalar_function("knocker_requeue", 3, FunctionFlags::SQLITE_UTF8, |ctx| {
        let event_id: i64 = ctx.get(0)?;
        let queue_name: String = ctx.get(1)?;
        let max_attempts: i64 = ctx.get(2)?;
        let db = unsafe { ctx.get_connection() }?;
        requeue(&db, event_id, &queue_name, max_attempts).map_err(to_sql_err)
    })?;

    Ok(())
}

pub fn endpoint_upsert(
    conn: &Connection,
    name: &str,
    path: &str,
    provider: Option<&str>,
    enabled: bool,
) -> rusqlite::Result<i64> {
    conn.execute(
        "
        INSERT INTO knocker_endpoints (name, path, provider, enabled)
        VALUES (?1, ?2, ?3, ?4)
        ON CONFLICT(name) DO UPDATE SET
            path = excluded.path,
            provider = excluded.provider,
            enabled = excluded.enabled
        ",
        params![name, path, provider, if enabled { 1 } else { 0 }],
    )?;
    conn.query_row(
        "SELECT id FROM knocker_endpoints WHERE name=?1",
        params![name],
        |row| row.get(0),
    )
}

#[allow(clippy::too_many_arguments)]
/// Caller must run this inside an outer transaction. This function issues
/// multiple writes and does not begin or commit a transaction itself.
pub fn ingest(
    conn: &Connection,
    endpoint_name: &str,
    method: &str,
    headers_json: &str,
    body_blob: &[u8],
    query_json: &str,
    signature_valid: Option<i64>,
    signature_error: Option<&str>,
    provider_event_id: Option<&str>,
    provider_delivery_id: Option<&str>,
    event_type: Option<&str>,
    dedupe_key: Option<&str>,
    queue_name: &str,
    max_attempts: i64,
) -> rusqlite::Result<String> {
    let endpoint_id: i64 = conn.query_row(
        "SELECT id FROM knocker_endpoints WHERE name=?1 AND enabled=1",
        params![endpoint_name],
        |row| row.get(0),
    )?;

    let chosen_dedupe = dedupe_key
        .map(str::to_owned)
        .or_else(|| provider_event_id.map(str::to_owned))
        .or_else(|| provider_delivery_id.map(str::to_owned));

    if signature_valid == Some(0) {
        let delivery_id = insert_delivery(
            conn,
            endpoint_id,
            None,
            provider_event_id,
            provider_delivery_id,
            event_type,
            method,
            headers_json,
            body_blob,
            query_json,
            signature_valid,
            signature_error,
            chosen_dedupe.as_deref(),
        )?;

        return Ok(serde_json::json!({
            "delivery_id": delivery_id,
            "event_id": serde_json::Value::Null,
            "duplicate": 0,
            "status_code": 401,
        })
        .to_string());
    }

    let existing_event_id = find_existing_event_id(conn, endpoint_id, chosen_dedupe.as_deref())?;
    let (event_id, duplicate) = if let Some(event_id) = existing_event_id {
        (event_id, true)
    } else {
        (
            insert_event(
                conn,
                endpoint_id,
                provider_event_id,
                provider_delivery_id,
                event_type,
                method,
                headers_json,
                body_blob,
                query_json,
                chosen_dedupe.as_deref(),
            )?,
            false,
        )
    };

    let delivery_id = insert_delivery(
        conn,
        endpoint_id,
        Some(event_id),
        provider_event_id,
        provider_delivery_id,
        event_type,
        method,
        headers_json,
        body_blob,
        query_json,
        signature_valid,
        signature_error,
        chosen_dedupe.as_deref(),
    )?;

    if !duplicate {
        enqueue_event(conn, event_id, queue_name, max_attempts)?;
    }

    Ok(serde_json::json!({
        "delivery_id": delivery_id,
        "event_id": event_id,
        "duplicate": if duplicate { 1 } else { 0 },
        "status_code": 204,
    })
    .to_string())
}

fn find_existing_event_id(
    conn: &Connection,
    endpoint_id: i64,
    dedupe_key: Option<&str>,
) -> rusqlite::Result<Option<i64>> {
    let Some(dedupe_key) = dedupe_key else {
        return Ok(None);
    };

    conn.query_row(
        "
        SELECT id
        FROM knocker_events
        WHERE endpoint_id=?1
          AND dedupe_key=?2
        ",
        params![endpoint_id, dedupe_key],
        |row| row.get(0),
    )
    .optional()
}

#[allow(clippy::too_many_arguments)]
fn insert_event(
    conn: &Connection,
    endpoint_id: i64,
    provider_event_id: Option<&str>,
    provider_delivery_id: Option<&str>,
    event_type: Option<&str>,
    method: &str,
    headers_json: &str,
    body_blob: &[u8],
    query_json: &str,
    dedupe_key: Option<&str>,
) -> rusqlite::Result<i64> {
    conn.query_row(
        "
        INSERT INTO knocker_events (
            endpoint_id,
            provider_event_id,
            provider_delivery_id,
            event_type,
            method,
            headers_json,
            body_blob,
            query_json,
            dedupe_key,
            status
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, 'received')
        RETURNING id
        ",
        params![
            endpoint_id,
            provider_event_id,
            provider_delivery_id,
            event_type,
            method,
            headers_json,
            body_blob,
            query_json,
            dedupe_key,
        ],
        |row| row.get(0),
    )
}

#[allow(clippy::too_many_arguments)]
fn insert_delivery(
    conn: &Connection,
    endpoint_id: i64,
    event_id: Option<i64>,
    provider_event_id: Option<&str>,
    provider_delivery_id: Option<&str>,
    event_type: Option<&str>,
    method: &str,
    headers_json: &str,
    body_blob: &[u8],
    query_json: &str,
    signature_valid: Option<i64>,
    signature_error: Option<&str>,
    dedupe_key: Option<&str>,
) -> rusqlite::Result<i64> {
    conn.query_row(
        "
        INSERT INTO knocker_deliveries (
            endpoint_id,
            event_id,
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
        ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)
        RETURNING id
        ",
        params![
            endpoint_id,
            event_id,
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
        ],
        |row| row.get(0),
    )
}

pub fn mark_processing(
    conn: &Connection,
    event_id: i64,
    attempt_count: i64,
) -> rusqlite::Result<i64> {
    conn.execute(
        "
        UPDATE knocker_events
        SET status='processing',
            attempt_count=?2,
            last_error=NULL
        WHERE id=?1
        ",
        params![event_id, attempt_count],
    )?;
    Ok(1)
}

pub fn mark_handled(conn: &Connection, event_id: i64, duration_ms: i64) -> rusqlite::Result<i64> {
    conn.execute(
        "
        UPDATE knocker_events
        SET status='handled',
            handled_at=unixepoch(),
            last_error=NULL
        WHERE id=?1
        ",
        params![event_id],
    )?;
    conn.execute(
        "
        INSERT INTO knocker_attempts (event_id, outcome, error, duration_ms)
        VALUES (?1, 'handled', NULL, ?2)
        ",
        params![event_id, duration_ms],
    )?;
    Ok(1)
}

pub fn mark_failed(
    conn: &Connection,
    event_id: i64,
    attempt_count: i64,
    error: &str,
    terminal: bool,
    duration_ms: i64,
) -> rusqlite::Result<i64> {
    let outcome = if terminal { "dead" } else { "failed" };
    conn.execute(
        "
        UPDATE knocker_events
        SET status=?2,
            attempt_count=?3,
            last_error=?4
        WHERE id=?1
        ",
        params![event_id, outcome, attempt_count, error],
    )?;
    conn.execute(
        "
        INSERT INTO knocker_attempts (event_id, outcome, error, duration_ms)
        VALUES (?1, ?2, ?3, ?4)
        ",
        params![event_id, outcome, error, duration_ms],
    )?;
    Ok(1)
}

pub fn mark_ignored(conn: &Connection, event_id: i64, duration_ms: i64) -> rusqlite::Result<i64> {
    conn.execute(
        "
        UPDATE knocker_events
        SET status='ignored',
            last_error=NULL
        WHERE id=?1
        ",
        params![event_id],
    )?;
    conn.execute(
        "
        INSERT INTO knocker_attempts (event_id, outcome, error, duration_ms)
        VALUES (?1, 'ignored', NULL, ?2)
        ",
        params![event_id, duration_ms],
    )?;
    Ok(1)
}

pub fn replay(
    conn: &Connection,
    event_id: i64,
    queue_name: &str,
    max_attempts: i64,
) -> rusqlite::Result<i64> {
    let status = event_status(conn, event_id)?;
    if !matches!(status.as_str(), "handled" | "failed" | "dead" | "ignored") {
        return Err(rusqlite::Error::InvalidParameterName(format!(
            "event {} with status {} cannot be replayed",
            event_id, status
        )));
    }
    delete_live_jobs_for_event(conn, event_id, queue_name)?;
    reset_event(conn, event_id)?;
    enqueue_event(conn, event_id, queue_name, max_attempts)?;
    Ok(1)
}

pub fn requeue(
    conn: &Connection,
    event_id: i64,
    queue_name: &str,
    max_attempts: i64,
) -> rusqlite::Result<i64> {
    let status = event_status(conn, event_id)?;
    if !matches!(status.as_str(), "failed" | "dead" | "ignored") {
        return Err(rusqlite::Error::InvalidParameterName(format!(
            "event {} with status {} cannot be requeued",
            event_id, status
        )));
    }
    delete_live_jobs_for_event(conn, event_id, queue_name)?;
    reset_event(conn, event_id)?;
    enqueue_event(conn, event_id, queue_name, max_attempts)?;
    Ok(1)
}

fn event_status(conn: &Connection, event_id: i64) -> rusqlite::Result<String> {
    conn.query_row(
        "SELECT status FROM knocker_events WHERE id=?1",
        params![event_id],
        |row| row.get(0),
    )
}

fn reset_event(conn: &Connection, event_id: i64) -> rusqlite::Result<()> {
    conn.execute(
        "
        UPDATE knocker_events
        SET status='received',
            attempt_count=0,
            handled_at=NULL,
            last_error=NULL
        WHERE id=?1
        ",
        params![event_id],
    )?;
    Ok(())
}

fn enqueue_event(
    conn: &Connection,
    event_id: i64,
    queue_name: &str,
    max_attempts: i64,
) -> rusqlite::Result<()> {
    let payload = serde_json::json!({ "event_id": event_id }).to_string();
    let _: i64 = conn.query_row(
        "SELECT honker_enqueue(?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            queue_name,
            payload,
            Option::<i64>::None,
            Option::<i64>::None,
            0,
            max_attempts,
            Option::<i64>::None,
        ],
        |row| row.get(0),
    )?;
    Ok(())
}

fn delete_live_jobs_for_event(
    conn: &Connection,
    event_id: i64,
    queue_name: &str,
) -> rusqlite::Result<()> {
    let payload = serde_json::json!({ "event_id": event_id }).to_string();
    conn.execute(
        "DELETE FROM _honker_live WHERE queue=?1 AND payload=?2",
        params![queue_name, payload],
    )?;
    Ok(())
}

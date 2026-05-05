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

    conn.create_scalar_function(
        "knocker_prune_events",
        4,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let statuses_json: String = ctx.get(0)?;
            let older_than: i64 = ctx.get(1)?;
            let limit: i64 = ctx.get(2)?;
            let queue_name: String = ctx.get(3)?;
            let db = unsafe { ctx.get_connection() }?;
            prune_events(&db, &statuses_json, older_than, limit, &queue_name).map_err(to_sql_err)
        },
    )?;

    conn.create_scalar_function(
        "knocker_prune_orphan_deliveries",
        3,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let older_than: i64 = ctx.get(0)?;
            let limit: i64 = ctx.get(1)?;
            let queue_name: String = ctx.get(2)?;
            let db = unsafe { ctx.get_connection() }?;
            prune_orphan_deliveries(&db, older_than, limit, &queue_name).map_err(to_sql_err)
        },
    )?;

    conn.create_scalar_function(
        "knocker_run_retention_pass",
        6,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let statuses_json: String = ctx.get(0)?;
            let event_older_than: Option<i64> = ctx.get(1)?;
            let event_limit: i64 = ctx.get(2)?;
            let orphan_older_than: Option<i64> = ctx.get(3)?;
            let orphan_limit: i64 = ctx.get(4)?;
            let queue_name: String = ctx.get(5)?;
            let db = unsafe { ctx.get_connection() }?;
            run_retention_pass(
                &db,
                &statuses_json,
                event_older_than,
                event_limit,
                orphan_older_than,
                orphan_limit,
                &queue_name,
            )
            .map_err(to_sql_err)
        },
    )?;

    conn.create_scalar_function(
        "knocker_reset_event",
        1,
        FunctionFlags::SQLITE_UTF8,
        |ctx| {
            let event_id: i64 = ctx.get(0)?;
            let db = unsafe { ctx.get_connection() }?;
            reset_event(&db, event_id).map_err(to_sql_err)?;
            Ok(1i64)
        },
    )?;

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
    let changed = conn.execute(
        "
        UPDATE knocker_events
        SET status='processing',
            attempt_count=?2,
            last_error=NULL
        WHERE id=?1
        ",
        params![event_id, attempt_count],
    )?;
    ensure_event_updated(changed, event_id, "processing")?;
    Ok(1)
}

pub fn mark_handled(conn: &Connection, event_id: i64, duration_ms: i64) -> rusqlite::Result<i64> {
    let changed = conn.execute(
        "
        UPDATE knocker_events
        SET status='handled',
            handled_at=unixepoch(),
            last_error=NULL
        WHERE id=?1
        ",
        params![event_id],
    )?;
    ensure_event_updated(changed, event_id, "handled")?;
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
    let changed = conn.execute(
        "
        UPDATE knocker_events
        SET status=?2,
            attempt_count=?3,
            last_error=?4
        WHERE id=?1
        ",
        params![event_id, outcome, attempt_count, error],
    )?;
    ensure_event_updated(changed, event_id, outcome)?;
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
    let changed = conn.execute(
        "
        UPDATE knocker_events
        SET status='ignored',
            last_error=NULL
        WHERE id=?1
        ",
        params![event_id],
    )?;
    ensure_event_updated(changed, event_id, "ignored")?;
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

/// Caller must run this inside an outer transaction. This function issues
/// multiple writes and does not begin or commit a transaction itself.
pub fn prune_events(
    conn: &Connection,
    statuses_json: &str,
    older_than: i64,
    limit: i64,
    queue_name: &str,
) -> rusqlite::Result<String> {
    let statuses = parse_prune_statuses(statuses_json)?;
    let older_than = validate_non_negative("older_than", older_than)?;
    let limit = validate_limit(limit)?;

    let event_ids = prune_event_candidate_ids(conn, &statuses, older_than, limit)?;
    let attempts_pruned = if event_ids.is_empty() {
        0
    } else {
        count_event_attempts(conn, &event_ids)?
    };
    let deliveries_pruned = if event_ids.is_empty() {
        0
    } else {
        count_event_deliveries(conn, &event_ids)?
    };
    let live_job_ids = stale_live_job_ids(conn, &event_ids, queue_name)?;
    delete_live_jobs_by_id(conn, &live_job_ids)?;
    delete_deliveries_for_event_ids(conn, &event_ids)?;
    delete_events_by_id(conn, &event_ids)?;
    write_prune_audit(
        conn,
        "prune_events",
        queue_name,
        Some(event_ids.len() as i64),
        deliveries_pruned,
        Some(attempts_pruned),
        Some(live_job_ids.len() as i64),
        serde_json::json!({
            "statuses": statuses,
            "older_than": older_than,
            "limit": limit,
        })
        .to_string(),
    )?;

    Ok(serde_json::json!({
        "events_pruned": event_ids.len(),
        "attempts_pruned": attempts_pruned,
        "deliveries_pruned": deliveries_pruned,
        "live_jobs_pruned": live_job_ids.len(),
    })
    .to_string())
}

/// Caller must run this inside an outer transaction. This function issues
/// multiple writes and does not begin or commit a transaction itself.
pub fn prune_orphan_deliveries(
    conn: &Connection,
    older_than: i64,
    limit: i64,
    queue_name: &str,
) -> rusqlite::Result<String> {
    let older_than = validate_non_negative("older_than", older_than)?;
    let limit = validate_limit(limit)?;
    let delivery_ids = prune_orphan_delivery_candidate_ids(conn, older_than, limit)?;
    delete_deliveries_by_id(conn, &delivery_ids)?;
    write_prune_audit(
        conn,
        "prune_orphan_deliveries",
        queue_name,
        None,
        delivery_ids.len() as i64,
        None,
        None,
        serde_json::json!({
            "older_than": older_than,
            "limit": limit,
        })
        .to_string(),
    )?;

    Ok(serde_json::json!({
        "deliveries_pruned": delivery_ids.len(),
    })
    .to_string())
}

/// Caller must run this inside an outer transaction. This function issues
/// multiple writes and does not begin or commit a transaction itself.
pub fn run_retention_pass(
    conn: &Connection,
    statuses_json: &str,
    event_older_than: Option<i64>,
    event_limit: i64,
    orphan_older_than: Option<i64>,
    orphan_limit: i64,
    queue_name: &str,
) -> rusqlite::Result<String> {
    if event_older_than.is_none() && orphan_older_than.is_none() {
        return Err(rusqlite::Error::InvalidParameterName(
            "retention pass must enable at least one prune path".to_string(),
        ));
    }

    let event_result = match event_older_than {
        Some(cutoff) => Some(serde_json::from_str::<serde_json::Value>(&prune_events(
            conn,
            statuses_json,
            cutoff,
            event_limit,
            queue_name,
        )?)
        .map_err(to_sql_err)?),
        None => None,
    };
    let orphan_result = match orphan_older_than {
        Some(cutoff) => Some(serde_json::from_str::<serde_json::Value>(
            &prune_orphan_deliveries(conn, cutoff, orphan_limit, queue_name)?,
        )
        .map_err(to_sql_err)?),
        None => None,
    };

    Ok(serde_json::json!({
        "event_result": event_result,
        "orphan_result": orphan_result,
    })
    .to_string())
}

fn event_status(conn: &Connection, event_id: i64) -> rusqlite::Result<String> {
    conn.query_row(
        "SELECT status FROM knocker_events WHERE id=?1",
        params![event_id],
        |row| row.get(0),
    )
}

pub fn reset_event(conn: &Connection, event_id: i64) -> rusqlite::Result<()> {
    let changed = conn.execute(
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
    ensure_event_updated(changed, event_id, "reset")?;
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
    let mut stmt = conn.prepare("SELECT id, payload FROM _honker_live WHERE queue=?1")?;
    let rows = stmt.query_map(params![queue_name], |row| {
        Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut job_ids = Vec::new();
    for row in rows {
        let (job_id, payload) = row?;
        if payload_event_id(&payload) == Some(event_id) {
            job_ids.push(job_id);
        }
    }
    for job_id in job_ids {
        conn.execute("DELETE FROM _honker_live WHERE id=?1", params![job_id])?;
    }
    Ok(())
}

fn ensure_event_updated(changed: usize, event_id: i64, action: &str) -> rusqlite::Result<()> {
    if changed == 0 {
        return Err(rusqlite::Error::InvalidParameterName(format!(
            "event {} cannot be marked {}; event does not exist",
            event_id, action
        )));
    }
    Ok(())
}

fn payload_event_id(payload: &str) -> Option<i64> {
    let value = serde_json::from_str::<serde_json::Value>(payload).ok()?;
    if value.get("event_id")?.is_boolean() {
        return None;
    }
    value.get("event_id")?.as_i64()
}

fn parse_prune_statuses(statuses_json: &str) -> rusqlite::Result<Vec<String>> {
    let statuses = serde_json::from_str::<Vec<String>>(statuses_json).map_err(to_sql_err)?;
    if statuses.is_empty() {
        return Err(rusqlite::Error::InvalidParameterName(
            "statuses must not be empty".to_string(),
        ));
    }
    for status in &statuses {
        if !matches!(status.as_str(), "handled" | "ignored") {
            return Err(rusqlite::Error::InvalidParameterName(format!(
                "unsupported prune status: {status}"
            )));
        }
    }
    Ok(statuses)
}

fn validate_non_negative(name: &str, value: i64) -> rusqlite::Result<i64> {
    if value < 0 {
        return Err(rusqlite::Error::InvalidParameterName(format!(
            "{name} must be non-negative"
        )));
    }
    Ok(value)
}

fn validate_limit(limit: i64) -> rusqlite::Result<i64> {
    if !(1..=1000).contains(&limit) {
        return Err(rusqlite::Error::InvalidParameterName(
            "limit must be between 1 and 1000".to_string(),
        ));
    }
    Ok(limit)
}

fn prune_event_candidate_ids(
    conn: &Connection,
    statuses: &[String],
    older_than: i64,
    limit: i64,
) -> rusqlite::Result<Vec<i64>> {
    let placeholders = std::iter::repeat("?")
        .take(statuses.len())
        .collect::<Vec<_>>()
        .join(", ");
    let sql = format!(
        "
        SELECT id
        FROM knocker_events
        WHERE status IN ({placeholders})
          AND received_at < ?
        ORDER BY received_at ASC, id ASC
        LIMIT ?
        "
    );
    let mut values: Vec<rusqlite::types::Value> = statuses
        .iter()
        .cloned()
        .map(rusqlite::types::Value::Text)
        .collect();
    values.push(rusqlite::types::Value::Integer(older_than));
    values.push(rusqlite::types::Value::Integer(limit));
    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(values), |row| row.get::<_, i64>(0))?;
    rows.collect()
}

fn count_event_attempts(conn: &Connection, event_ids: &[i64]) -> rusqlite::Result<i64> {
    let mut total = 0;
    for event_id in event_ids {
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM knocker_attempts WHERE event_id=?1",
            params![event_id],
            |row| row.get(0),
        )?;
        total += count;
    }
    Ok(total)
}

fn count_event_deliveries(conn: &Connection, event_ids: &[i64]) -> rusqlite::Result<i64> {
    let mut total = 0;
    for event_id in event_ids {
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM knocker_deliveries WHERE event_id=?1",
            params![event_id],
            |row| row.get(0),
        )?;
        total += count;
    }
    Ok(total)
}

fn stale_live_job_ids(
    conn: &Connection,
    event_ids: &[i64],
    queue_name: &str,
) -> rusqlite::Result<Vec<i64>> {
    let candidate_event_ids: std::collections::HashSet<i64> = event_ids.iter().copied().collect();
    let mut stmt = conn.prepare("SELECT id, payload FROM _honker_live WHERE queue=?1")?;
    let rows = stmt.query_map(params![queue_name], |row| {
        Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut job_ids = Vec::new();
    for row in rows {
        let (job_id, payload) = row?;
        if let Some(event_id) = payload_event_id(&payload) {
            if candidate_event_ids.contains(&event_id) {
                job_ids.push(job_id);
            }
        }
    }
    Ok(job_ids)
}

fn delete_live_jobs_by_id(conn: &Connection, job_ids: &[i64]) -> rusqlite::Result<()> {
    for job_id in job_ids {
        conn.execute("DELETE FROM _honker_live WHERE id=?1", params![job_id])?;
    }
    Ok(())
}

fn delete_deliveries_for_event_ids(conn: &Connection, event_ids: &[i64]) -> rusqlite::Result<()> {
    for event_id in event_ids {
        conn.execute("DELETE FROM knocker_deliveries WHERE event_id=?1", params![event_id])?;
    }
    Ok(())
}

fn delete_events_by_id(conn: &Connection, event_ids: &[i64]) -> rusqlite::Result<()> {
    for event_id in event_ids {
        conn.execute("DELETE FROM knocker_events WHERE id=?1", params![event_id])?;
    }
    Ok(())
}

fn prune_orphan_delivery_candidate_ids(
    conn: &Connection,
    older_than: i64,
    limit: i64,
) -> rusqlite::Result<Vec<i64>> {
    let mut stmt = conn.prepare(
        "
        SELECT id
        FROM knocker_deliveries
        WHERE event_id IS NULL
          AND received_at < ?1
        ORDER BY received_at ASC, id ASC
        LIMIT ?2
        ",
    )?;
    let rows = stmt.query_map(params![older_than, limit], |row| row.get::<_, i64>(0))?;
    rows.collect()
}

fn delete_deliveries_by_id(conn: &Connection, delivery_ids: &[i64]) -> rusqlite::Result<()> {
    for delivery_id in delivery_ids {
        conn.execute("DELETE FROM knocker_deliveries WHERE id=?1", params![delivery_id])?;
    }
    Ok(())
}

fn write_prune_audit(
    conn: &Connection,
    kind: &str,
    queue_name: &str,
    events_pruned: Option<i64>,
    deliveries_pruned: i64,
    attempts_pruned: Option<i64>,
    live_jobs_pruned: Option<i64>,
    summary_json: String,
) -> rusqlite::Result<()> {
    conn.execute(
        "
        INSERT INTO knocker_prune_audits
            (kind, queue_name, events_pruned, deliveries_pruned,
             attempts_pruned, live_jobs_pruned, summary_json)
        VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
        ",
        params![
            kind,
            queue_name,
            events_pruned,
            deliveries_pruned,
            attempts_pruned,
            live_jobs_pruned,
            summary_json,
        ],
    )?;
    Ok(())
}

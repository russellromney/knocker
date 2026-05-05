use base64::{Engine as _, engine::general_purpose};
use ed25519_dalek::{Signature as Ed25519Signature, VerifyingKey};
use hmac::{Hmac, Mac};
use p256::ecdsa::signature::Verifier as EcdsaVerifier;
use p256::ecdsa::{Signature as P256Signature, VerifyingKey as P256VerifyingKey};
use p256::pkcs8::DecodePublicKey;
use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, OptionalExtension, params};
use serde_json::{Map, Value};
use sha1::Sha1;
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;
type HmacSha1 = Hmac<Sha1>;

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

    conn.create_scalar_function("knocker_receive", 14, FunctionFlags::SQLITE_UTF8, |ctx| {
        let endpoint_name: String = ctx.get(0)?;
        let provider: String = ctx.get(1)?;
        let secrets_json: String = ctx.get(2)?;
        let options_json: String = ctx.get(3)?;
        let method: String = ctx.get(4)?;
        let headers_json: String = ctx.get(5)?;
        let body_blob: Vec<u8> = ctx.get(6)?;
        let query_json: String = ctx.get(7)?;
        let provider_event_id: Option<String> = ctx.get(8)?;
        let provider_delivery_id: Option<String> = ctx.get(9)?;
        let event_type: Option<String> = ctx.get(10)?;
        let dedupe_key: Option<String> = ctx.get(11)?;
        let queue_name: String = ctx.get(12)?;
        let max_attempts: i64 = ctx.get(13)?;
        let db = unsafe { ctx.get_connection() }?;
        receive(
            &db,
            &endpoint_name,
            &provider,
            &secrets_json,
            &options_json,
            &method,
            &headers_json,
            &body_blob,
            &query_json,
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

#[allow(clippy::too_many_arguments)]
/// Verify a curated provider request and ingest the delivery/event through the
/// same shared primitive every language binding uses.
pub fn receive(
    conn: &Connection,
    endpoint_name: &str,
    provider: &str,
    secrets_json: &str,
    options_json: &str,
    method: &str,
    headers_json: &str,
    body_blob: &[u8],
    query_json: &str,
    provider_event_id: Option<&str>,
    provider_delivery_id: Option<&str>,
    event_type: Option<&str>,
    dedupe_key: Option<&str>,
    queue_name: &str,
    max_attempts: i64,
) -> rusqlite::Result<String> {
    let secrets = parse_string_array("secrets_json", secrets_json)?;
    let options = parse_json_value("options_json", options_json)?;
    let headers = parse_object("headers_json", headers_json)?;
    let outcome = verify_provider(provider, &headers, body_blob, &secrets, &options)?;

    ingest(
        conn,
        endpoint_name,
        method,
        headers_json,
        body_blob,
        query_json,
        Some(if outcome.valid { 1 } else { 0 }),
        outcome.signature_error.as_deref(),
        provider_event_id.or(outcome.provider_event_id.as_deref()),
        provider_delivery_id.or(outcome.provider_delivery_id.as_deref()),
        event_type.or(outcome.event_type.as_deref()),
        dedupe_key,
        queue_name,
        max_attempts,
    )
}

struct ProviderOutcome {
    valid: bool,
    signature_error: Option<String>,
    provider_delivery_id: Option<String>,
    provider_event_id: Option<String>,
    event_type: Option<String>,
}

impl ProviderOutcome {
    fn accept(
        provider_delivery_id: Option<String>,
        provider_event_id: Option<String>,
        event_type: Option<String>,
    ) -> Self {
        Self {
            valid: true,
            signature_error: None,
            provider_delivery_id,
            provider_event_id,
            event_type,
        }
    }

    fn reject(
        signature_error: impl Into<String>,
        provider_delivery_id: Option<String>,
        provider_event_id: Option<String>,
        event_type: Option<String>,
    ) -> Self {
        Self {
            valid: false,
            signature_error: Some(signature_error.into()),
            provider_delivery_id,
            provider_event_id,
            event_type,
        }
    }
}

fn verify_provider(
    provider: &str,
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    match provider.to_ascii_lowercase().as_str() {
        "github" => verify_github(headers, body_blob, secrets),
        "stripe" => verify_stripe(headers, body_blob, secrets, options),
        "shopify" => verify_shopify(headers, body_blob, secrets),
        "slack" => verify_slack(headers, body_blob, secrets, options),
        "postmark" => verify_postmark(headers, body_blob, secrets),
        "resend" => verify_resend(headers, body_blob, secrets, options),
        "paddle" => verify_paddle(headers, body_blob, secrets, options),
        "lemon-squeezy" | "lemonsqueezy" => verify_lemonsqueezy(headers, body_blob, secrets),
        "standard-webhooks" | "standard_webhooks" | "svix" => {
            verify_standard_webhooks(headers, body_blob, secrets, options, "standard-webhooks")
        }
        "clerk" => verify_standard_webhooks(headers, body_blob, secrets, options, "clerk"),
        "twilio" => verify_twilio(headers, body_blob, secrets, options),
        "sendgrid" => verify_sendgrid(headers, body_blob, secrets, options),
        "linear" => verify_linear(headers, body_blob, secrets, options),
        "meta" | "facebook" => verify_meta(headers, body_blob, secrets),
        "discord" => verify_discord(headers, body_blob, secrets),
        "zendesk" => verify_zendesk(headers, body_blob, secrets, options),
        "intercom" => verify_intercom(headers, body_blob, secrets),
        "hubspot" => verify_hubspot(headers, body_blob, secrets, options),
        "token-header" | "header-token" => {
            verify_token_header(headers, body_blob, secrets, options)
        }
        "bearer-token" | "bearer" => verify_bearer_token(headers, body_blob, secrets),
        "basic-auth" | "basic" => verify_basic_auth(headers, body_blob, secrets),
        other => Err(rusqlite::Error::InvalidParameterName(format!(
            "unknown provider: {other}"
        ))),
    }
}

fn verify_github(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let delivery_id = header(headers, "x-github-delivery");
    let event_type = header(headers, "x-github-event");
    let Some(signature) = header(headers, "x-hub-signature-256") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Hub-Signature-256",
            delivery_id,
            None,
            event_type,
        ));
    };
    let Some(hex) = signature.strip_prefix("sha256=") else {
        return Ok(ProviderOutcome::reject(
            "malformed X-Hub-Signature-256",
            delivery_id,
            None,
            event_type,
        ));
    };
    if secrets
        .iter()
        .any(|secret| hmac_hex(secret.as_bytes(), body_blob) == hex)
    {
        Ok(ProviderOutcome::accept(delivery_id, None, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid GitHub signature",
            delivery_id,
            None,
            event_type,
        ))
    }
}

fn verify_stripe(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "id");
    let event_type = json_string_field(body.as_ref(), "type");
    let Some(signature) = header(headers, "stripe-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing Stripe-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let parts = semicolon_parts(&signature, ',');
    let Some(timestamp) = parts.get("t").and_then(|value| value.parse::<i64>().ok()) else {
        return Ok(ProviderOutcome::reject(
            "missing Stripe timestamp",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if outside_tolerance(timestamp, tolerance_s(options, 300)?) {
        return Ok(ProviderOutcome::reject(
            "Stripe timestamp outside tolerance",
            None,
            provider_event_id,
            event_type,
        ));
    }
    let signed = [timestamp.to_string().as_bytes(), b".", body_blob].concat();
    let signatures = repeated_parts(&signature, ',', "v1");
    if secrets.iter().any(|secret| {
        signatures
            .iter()
            .any(|sig| hmac_hex(secret.as_bytes(), &signed) == *sig)
    }) {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Stripe signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_shopify(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let delivery_id = header(headers, "x-shopify-webhook-id");
    let provider_event_id = header(headers, "x-shopify-event-id");
    let event_type = header(headers, "x-shopify-topic");
    let Some(signature) = header(headers, "x-shopify-hmac-sha256") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Shopify-Hmac-Sha256",
            delivery_id,
            provider_event_id,
            event_type,
        ));
    };
    if secrets
        .iter()
        .any(|secret| hmac_base64(secret.as_bytes(), body_blob) == signature)
    {
        Ok(ProviderOutcome::accept(
            delivery_id,
            provider_event_id,
            event_type,
        ))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Shopify signature",
            delivery_id,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_slack(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "event_id");
    let event_type = json_string_path(body.as_ref(), &["event", "type"])
        .or_else(|| json_string_field(body.as_ref(), "type"));
    let Some(timestamp) =
        header(headers, "x-slack-request-timestamp").and_then(|value| value.parse::<i64>().ok())
    else {
        return Ok(ProviderOutcome::reject(
            "missing X-Slack-Request-Timestamp",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if outside_tolerance(timestamp, tolerance_s(options, 300)?) {
        return Ok(ProviderOutcome::reject(
            "Slack timestamp outside tolerance",
            None,
            provider_event_id,
            event_type,
        ));
    }
    let Some(signature) = header(headers, "x-slack-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Slack-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let signed = [b"v0:", timestamp.to_string().as_bytes(), b":", body_blob].concat();
    if secrets
        .iter()
        .any(|secret| format!("v0={}", hmac_hex(secret.as_bytes(), &signed)) == signature)
    {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Slack signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_postmark(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "MessageID");
    let event_type = json_string_field(body.as_ref(), "RecordType");
    let Some(authorization) = header(headers, "authorization") else {
        return Ok(ProviderOutcome::reject(
            "missing Authorization",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if !authorization.starts_with("Basic ") {
        return Ok(ProviderOutcome::reject(
            "invalid Authorization scheme",
            None,
            provider_event_id,
            event_type,
        ));
    }
    if secrets.iter().any(|secret| {
        let expected = format!(
            "Basic {}",
            general_purpose::STANDARD.encode(secret.as_bytes())
        );
        authorization == expected
    }) {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Postmark Authorization",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_resend(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_path(body.as_ref(), &["data", "email_id"]);
    let event_type = json_string_field(body.as_ref(), "type");
    let delivery_id = header(headers, "svix-id");
    let Some(timestamp) = header(headers, "svix-timestamp") else {
        return Ok(ProviderOutcome::reject(
            "missing svix-timestamp",
            delivery_id,
            provider_event_id,
            event_type,
        ));
    };
    let timestamp_int = timestamp.parse::<i64>().unwrap_or(-1);
    if timestamp_int < 0 || outside_tolerance(timestamp_int, tolerance_s(options, 300)?) {
        return Ok(ProviderOutcome::reject(
            "Svix timestamp outside tolerance",
            delivery_id,
            provider_event_id,
            event_type,
        ));
    }
    let Some(signature) = header(headers, "svix-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing svix-signature",
            delivery_id,
            provider_event_id,
            event_type,
        ));
    };
    let Some(delivery) = delivery_id.as_deref() else {
        return Ok(ProviderOutcome::reject(
            "missing svix-id",
            delivery_id,
            provider_event_id,
            event_type,
        ));
    };
    let signed = [
        delivery.as_bytes(),
        b".",
        timestamp.as_bytes(),
        b".",
        body_blob,
    ]
    .concat();
    let signatures = signature
        .split_whitespace()
        .filter_map(|part| part.strip_prefix("v1,").map(str::to_owned))
        .collect::<Vec<_>>();
    if secrets.iter().any(|secret| {
        svix_secret_bytes(secret)
            .map(|raw_secret| {
                let expected = hmac_base64(&raw_secret, &signed);
                signatures
                    .iter()
                    .any(|sig| constant_time_eq(sig, &expected))
            })
            .unwrap_or(false)
    }) {
        Ok(ProviderOutcome::accept(
            delivery_id,
            provider_event_id,
            event_type,
        ))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Svix signature",
            delivery_id,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_paddle(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "event_id");
    let event_type = json_string_field(body.as_ref(), "event_type")
        .or_else(|| json_string_path(body.as_ref(), &["event", "type"]));
    let Some(signature) = header(headers, "paddle-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing Paddle-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let parts = semicolon_parts(&signature, ';');
    let Some(timestamp) = parts.get("ts").and_then(|value| value.parse::<i64>().ok()) else {
        return Ok(ProviderOutcome::reject(
            "missing Paddle timestamp",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if outside_tolerance(timestamp, tolerance_s(options, 5)?) {
        return Ok(ProviderOutcome::reject(
            "Paddle timestamp outside tolerance",
            None,
            provider_event_id,
            event_type,
        ));
    }
    let signed = [timestamp.to_string().as_bytes(), b":", body_blob].concat();
    let Some(h1) = parts.get("h1") else {
        return Ok(ProviderOutcome::reject(
            "missing Paddle h1 signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if secrets
        .iter()
        .any(|secret| hmac_hex(secret.as_bytes(), &signed) == *h1)
    {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Paddle signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_lemonsqueezy(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_path(body.as_ref(), &["data", "attributes", "identifier"])
        .or_else(|| json_string_path(body.as_ref(), &["data", "id"]));
    let event_type = header(headers, "x-event-name")
        .or_else(|| json_string_path(body.as_ref(), &["meta", "event_name"]));
    let Some(signature) = header(headers, "x-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if secrets
        .iter()
        .any(|secret| hmac_hex(secret.as_bytes(), body_blob) == signature)
    {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Lemon Squeezy signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_standard_webhooks(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
    provider_name: &str,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "id")
        .or_else(|| json_string_path(body.as_ref(), &["data", "id"]));
    let event_type = json_string_field(body.as_ref(), "type");
    let delivery_id = header(headers, "webhook-id")
        .or_else(|| header(headers, "webhooks-id"))
        .or_else(|| header(headers, "svix-id"));
    let timestamp = header(headers, "webhook-timestamp")
        .or_else(|| header(headers, "webhooks-timestamp"))
        .or_else(|| header(headers, "svix-timestamp"));
    let signature = header(headers, "webhook-signature")
        .or_else(|| header(headers, "webhooks-signature"))
        .or_else(|| header(headers, "svix-signature"));
    let Some(delivery) = delivery_id.as_deref() else {
        return Ok(ProviderOutcome::reject(
            format!("{provider_name} missing webhook id"),
            delivery_id,
            provider_event_id,
            event_type,
        ));
    };
    let Some(timestamp) = timestamp else {
        return Ok(ProviderOutcome::reject(
            format!("{provider_name} missing webhook timestamp"),
            delivery_id,
            provider_event_id,
            event_type,
        ));
    };
    let timestamp_int = timestamp.parse::<i64>().unwrap_or(-1);
    if timestamp_int < 0 || outside_tolerance(timestamp_int, tolerance_s(options, 300)?) {
        return Ok(ProviderOutcome::reject(
            format!("{provider_name} timestamp outside tolerance"),
            delivery_id,
            provider_event_id,
            event_type,
        ));
    }
    let Some(signature) = signature else {
        return Ok(ProviderOutcome::reject(
            format!("{provider_name} missing webhook signature"),
            delivery_id,
            provider_event_id,
            event_type,
        ));
    };
    let signed = [
        delivery.as_bytes(),
        b".",
        timestamp.as_bytes(),
        b".",
        body_blob,
    ]
    .concat();
    let signatures = signature
        .split_whitespace()
        .filter_map(|part| {
            part.strip_prefix("v1,")
                .or_else(|| part.strip_prefix("v1="))
                .map(str::to_owned)
        })
        .collect::<Vec<_>>();
    if secrets.iter().any(|secret| {
        svix_secret_bytes(secret)
            .map(|raw_secret| {
                let expected = hmac_base64(&raw_secret, &signed);
                signatures
                    .iter()
                    .any(|sig| constant_time_eq(sig, &expected))
            })
            .unwrap_or(false)
    }) {
        Ok(ProviderOutcome::accept(
            delivery_id,
            provider_event_id,
            event_type,
        ))
    } else {
        Ok(ProviderOutcome::reject(
            format!("invalid {provider_name} signature"),
            delivery_id,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_twilio(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let event_type = json_string_field(parse_body_json(body_blob).as_ref(), "EventType")
        .or_else(|| form_value(body_blob, "CallStatus"))
        .or_else(|| form_value(body_blob, "SmsStatus"));
    let provider_event_id = form_value(body_blob, "CallSid")
        .or_else(|| form_value(body_blob, "MessageSid"))
        .or_else(|| form_value(body_blob, "SmsSid"));
    let Some(url) = option_string(options, "url") else {
        return Err(rusqlite::Error::InvalidParameterName(
            "twilio provider option 'url' is required".to_string(),
        ));
    };
    let Some(signature) = header(headers, "x-twilio-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Twilio-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let signed = twilio_signed_payload(&url, body_blob);
    if secrets.iter().any(|secret| {
        constant_time_eq(
            &hmac_base64_sha1(secret.as_bytes(), signed.as_bytes()),
            &signature,
        )
    }) {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Twilio signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_sendgrid(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let first = body
        .as_ref()
        .and_then(Value::as_array)
        .and_then(|items| items.first());
    let provider_event_id = first
        .and_then(|value| value.get("sg_event_id"))
        .and_then(Value::as_str)
        .map(str::to_owned)
        .or_else(|| {
            first
                .and_then(|value| value.get("sg_message_id"))
                .and_then(Value::as_str)
                .map(str::to_owned)
        });
    let event_type = first
        .and_then(|value| value.get("event"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    let Some(timestamp) = header(headers, "x-twilio-email-event-webhook-timestamp") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Twilio-Email-Event-Webhook-Timestamp",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let timestamp_int = timestamp.parse::<i64>().unwrap_or(-1);
    if timestamp_int < 0 || outside_tolerance(timestamp_int, tolerance_s(options, 300)?) {
        return Ok(ProviderOutcome::reject(
            "SendGrid timestamp outside tolerance",
            None,
            provider_event_id,
            event_type,
        ));
    }
    let Some(signature) = header(headers, "x-twilio-email-event-webhook-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Twilio-Email-Event-Webhook-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let signed = [timestamp.as_bytes(), body_blob].concat();
    let Ok(sig_bytes) = general_purpose::STANDARD.decode(signature.as_bytes()) else {
        return Ok(ProviderOutcome::reject(
            "invalid SendGrid signature encoding",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let Ok(signature) = P256Signature::from_der(&sig_bytes) else {
        return Ok(ProviderOutcome::reject(
            "invalid SendGrid signature format",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if secrets.iter().any(|secret| {
        P256VerifyingKey::from_public_key_pem(secret)
            .map(|key| EcdsaVerifier::verify(&key, &signed, &signature).is_ok())
            .unwrap_or(false)
    }) {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid SendGrid signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_linear(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_path(body.as_ref(), &["data", "id"]);
    let event_type = json_string_field(body.as_ref(), "action")
        .or_else(|| json_string_field(body.as_ref(), "type"));
    if let Some(timestamp_ms) = body
        .as_ref()
        .and_then(Value::as_object)
        .and_then(|object| object.get("webhookTimestamp"))
        .and_then(Value::as_i64)
    {
        if outside_tolerance(timestamp_ms / 1000, tolerance_s(options, 60)?) {
            return Ok(ProviderOutcome::reject(
                "Linear timestamp outside tolerance",
                None,
                provider_event_id,
                event_type,
            ));
        }
    }
    let Some(signature) = header(headers, "linear-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing Linear-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if secrets
        .iter()
        .any(|secret| constant_time_eq(&hmac_hex(secret.as_bytes(), body_blob), &signature))
    {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Linear signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_meta(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let event_type = json_string_field(body.as_ref(), "object");
    let Some(signature) = header(headers, "x-hub-signature-256") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Hub-Signature-256",
            None,
            None,
            event_type,
        ));
    };
    let Some(hex) = signature.strip_prefix("sha256=") else {
        return Ok(ProviderOutcome::reject(
            "malformed X-Hub-Signature-256",
            None,
            None,
            event_type,
        ));
    };
    if secrets
        .iter()
        .any(|secret| constant_time_eq(&hmac_hex(secret.as_bytes(), body_blob), hex))
    {
        Ok(ProviderOutcome::accept(None, None, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Meta signature",
            None,
            None,
            event_type,
        ))
    }
}

fn verify_discord(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "id");
    let event_type = json_string_field(body.as_ref(), "type");
    let Some(timestamp) = header(headers, "x-signature-timestamp") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Signature-Timestamp",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let Some(signature_hex) = header(headers, "x-signature-ed25519") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Signature-Ed25519",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let Some(signature_bytes) = hex_decode(&signature_hex) else {
        return Ok(ProviderOutcome::reject(
            "malformed Discord signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let Ok(signature_array) = <[u8; 64]>::try_from(signature_bytes.as_slice()) else {
        return Ok(ProviderOutcome::reject(
            "malformed Discord signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let signature = Ed25519Signature::from_bytes(&signature_array);
    let signed = [timestamp.as_bytes(), body_blob].concat();
    if secrets.iter().any(|secret| {
        let Some(public_key) = hex_decode(secret) else {
            return false;
        };
        let Ok(public_key) = <[u8; 32]>::try_from(public_key.as_slice()) else {
            return false;
        };
        VerifyingKey::from_bytes(&public_key)
            .map(|key| key.verify(&signed, &signature).is_ok())
            .unwrap_or(false)
    }) {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Discord signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_zendesk(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "ticket_id")
        .or_else(|| json_string_field(body.as_ref(), "id"));
    let event_type = json_string_field(body.as_ref(), "type")
        .or_else(|| header(headers, "x-zendesk-webhook-invocation-event"));
    let Some(timestamp) = header(headers, "x-zendesk-webhook-signature-timestamp") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Zendesk-Webhook-Signature-Timestamp",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if let Ok(timestamp_int) = timestamp.parse::<i64>() {
        if outside_tolerance(timestamp_int, tolerance_s(options, 300)?) {
            return Ok(ProviderOutcome::reject(
                "Zendesk timestamp outside tolerance",
                None,
                provider_event_id,
                event_type,
            ));
        }
    }
    let Some(signature) = header(headers, "x-zendesk-webhook-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Zendesk-Webhook-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let signed = [timestamp.as_bytes(), body_blob].concat();
    if secrets
        .iter()
        .any(|secret| constant_time_eq(&hmac_base64(secret.as_bytes(), &signed), &signature))
    {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Zendesk signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_intercom(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "id");
    let event_type = json_string_field(body.as_ref(), "topic")
        .or_else(|| json_string_field(body.as_ref(), "type"));
    let Some(signature) = header(headers, "x-hub-signature") else {
        return Ok(ProviderOutcome::reject(
            "missing X-Hub-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let Some(hex) = signature.strip_prefix("sha1=") else {
        return Ok(ProviderOutcome::reject(
            "malformed X-Hub-Signature",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if secrets
        .iter()
        .any(|secret| constant_time_eq(&hmac_hex_sha1(secret.as_bytes(), body_blob), hex))
    {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid Intercom signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_hubspot(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let first = body
        .as_ref()
        .and_then(Value::as_array)
        .and_then(|items| items.first());
    let provider_event_id = first
        .and_then(|value| value.get("eventId"))
        .and_then(json_scalar_to_string)
        .or_else(|| {
            first
                .and_then(|value| value.get("objectId"))
                .and_then(json_scalar_to_string)
        });
    let event_type = first
        .and_then(|value| value.get("subscriptionType"))
        .and_then(json_scalar_to_string);
    let Some(url) = option_string(options, "url") else {
        return Err(rusqlite::Error::InvalidParameterName(
            "hubspot provider option 'url' is required".to_string(),
        ));
    };
    let Some(timestamp) = header(headers, "x-hubspot-request-timestamp") else {
        return Ok(ProviderOutcome::reject(
            "missing X-HubSpot-Request-Timestamp",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let timestamp_ms = timestamp.parse::<i64>().unwrap_or(-1);
    if timestamp_ms < 0 || outside_tolerance(timestamp_ms / 1000, tolerance_s(options, 300)?) {
        return Ok(ProviderOutcome::reject(
            "HubSpot timestamp outside tolerance",
            None,
            provider_event_id,
            event_type,
        ));
    }
    let Some(signature) = header(headers, "x-hubspot-signature-v3") else {
        return Ok(ProviderOutcome::reject(
            "missing X-HubSpot-Signature-v3",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let method = option_string(options, "method")
        .unwrap_or_else(|| "POST".to_string())
        .to_ascii_uppercase();
    let mut signed = Vec::new();
    signed.extend_from_slice(method.as_bytes());
    signed.extend_from_slice(url.as_bytes());
    signed.extend_from_slice(body_blob);
    signed.extend_from_slice(timestamp.as_bytes());
    if secrets
        .iter()
        .any(|secret| constant_time_eq(&hmac_base64(secret.as_bytes(), &signed), &signature))
    {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid HubSpot signature",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_token_header(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
    options: &Value,
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "id");
    let event_type = json_string_field(body.as_ref(), "type");
    let header_name =
        option_string(options, "header").unwrap_or_else(|| "x-knocker-token".to_string());
    let Some(value) = header(headers, &header_name) else {
        return Ok(ProviderOutcome::reject(
            format!("missing token header: {header_name}"),
            None,
            provider_event_id,
            event_type,
        ));
    };
    if secrets
        .iter()
        .any(|secret| constant_time_eq(secret, &value))
    {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid token header",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_bearer_token(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "id");
    let event_type = json_string_field(body.as_ref(), "type");
    let Some(value) = header(headers, "authorization") else {
        return Ok(ProviderOutcome::reject(
            "missing Authorization",
            None,
            provider_event_id,
            event_type,
        ));
    };
    let Some(token) = value.strip_prefix("Bearer ") else {
        return Ok(ProviderOutcome::reject(
            "invalid Authorization scheme",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if secrets.iter().any(|secret| constant_time_eq(secret, token)) {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid bearer token",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn verify_basic_auth(
    headers: &Map<String, Value>,
    body_blob: &[u8],
    secrets: &[String],
) -> rusqlite::Result<ProviderOutcome> {
    let body = parse_body_json(body_blob);
    let provider_event_id = json_string_field(body.as_ref(), "id");
    let event_type = json_string_field(body.as_ref(), "type");
    let Some(value) = header(headers, "authorization") else {
        return Ok(ProviderOutcome::reject(
            "missing Authorization",
            None,
            provider_event_id,
            event_type,
        ));
    };
    if !value.starts_with("Basic ") {
        return Ok(ProviderOutcome::reject(
            "invalid Authorization scheme",
            None,
            provider_event_id,
            event_type,
        ));
    }
    if secrets.iter().any(|secret| {
        let expected = format!(
            "Basic {}",
            general_purpose::STANDARD.encode(secret.as_bytes())
        );
        constant_time_eq(&expected, &value)
    }) {
        Ok(ProviderOutcome::accept(None, provider_event_id, event_type))
    } else {
        Ok(ProviderOutcome::reject(
            "invalid basic auth",
            None,
            provider_event_id,
            event_type,
        ))
    }
}

fn parse_string_array(name: &str, json_text: &str) -> rusqlite::Result<Vec<String>> {
    serde_json::from_str::<Vec<String>>(json_text).map_err(|_| {
        rusqlite::Error::InvalidParameterName(format!("{name} must be a JSON string array"))
    })
}

fn parse_json_value(name: &str, json_text: &str) -> rusqlite::Result<Value> {
    serde_json::from_str::<Value>(json_text)
        .map_err(|_| rusqlite::Error::InvalidParameterName(format!("{name} must be valid JSON")))
}

fn parse_object(name: &str, json_text: &str) -> rusqlite::Result<Map<String, Value>> {
    match parse_json_value(name, json_text)? {
        Value::Object(map) => Ok(map),
        _ => Err(rusqlite::Error::InvalidParameterName(format!(
            "{name} must be a JSON object"
        ))),
    }
}

fn header(headers: &Map<String, Value>, name: &str) -> Option<String> {
    headers.iter().find_map(|(key, value)| {
        if key.eq_ignore_ascii_case(name) {
            value.as_str().map(str::to_owned)
        } else {
            None
        }
    })
}

fn parse_body_json(body_blob: &[u8]) -> Option<Value> {
    serde_json::from_slice::<Value>(body_blob).ok()
}

fn json_string_field(value: Option<&Value>, key: &str) -> Option<String> {
    value
        .and_then(Value::as_object)
        .and_then(|object| object.get(key))
        .and_then(json_scalar_to_string)
}

fn json_string_path(value: Option<&Value>, path: &[&str]) -> Option<String> {
    let mut current = value?;
    for segment in path {
        current = current.as_object()?.get(*segment)?;
    }
    json_scalar_to_string(current)
}

fn json_scalar_to_string(value: &Value) -> Option<String> {
    match value {
        Value::String(text) => Some(text.to_owned()),
        Value::Number(number) => Some(number.to_string()),
        Value::Bool(value) => Some(value.to_string()),
        _ => None,
    }
}

fn hmac_hex(secret: &[u8], payload: &[u8]) -> String {
    let mut mac = HmacSha256::new_from_slice(secret).expect("HMAC accepts any key length");
    mac.update(payload);
    mac.finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn hmac_base64(secret: &[u8], payload: &[u8]) -> String {
    let mut mac = HmacSha256::new_from_slice(secret).expect("HMAC accepts any key length");
    mac.update(payload);
    general_purpose::STANDARD.encode(mac.finalize().into_bytes())
}

fn hmac_base64_sha1(secret: &[u8], payload: &[u8]) -> String {
    let mut mac = HmacSha1::new_from_slice(secret).expect("HMAC accepts any key length");
    mac.update(payload);
    general_purpose::STANDARD.encode(mac.finalize().into_bytes())
}

fn svix_secret_bytes(secret: &str) -> Option<Vec<u8>> {
    let Some(encoded) = secret.strip_prefix("whsec_") else {
        return Some(secret.as_bytes().to_vec());
    };
    general_purpose::STANDARD
        .decode(encoded.as_bytes())
        .ok()
        .or_else(|| {
            general_purpose::STANDARD_NO_PAD
                .decode(encoded.as_bytes())
                .ok()
        })
}

fn hmac_hex_sha1(secret: &[u8], payload: &[u8]) -> String {
    let mut mac = HmacSha1::new_from_slice(secret).expect("HMAC accepts any key length");
    mac.update(payload);
    mac.finalize()
        .into_bytes()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn option_string(options: &Value, key: &str) -> Option<String> {
    options
        .as_object()
        .and_then(|object| object.get(key))
        .and_then(Value::as_str)
        .map(str::to_owned)
}

fn form_value(body_blob: &[u8], key: &str) -> Option<String> {
    form_urlencoded::parse(body_blob).find_map(|(name, value)| {
        if name == key {
            Some(value.into_owned())
        } else {
            None
        }
    })
}

fn twilio_signed_payload(url: &str, body_blob: &[u8]) -> String {
    let mut params = form_urlencoded::parse(body_blob)
        .map(|(key, value)| (key.into_owned(), value.into_owned()))
        .collect::<Vec<_>>();
    params.sort_by(|left, right| left.0.cmp(&right.0));
    let mut signed = url.to_string();
    for (key, value) in params {
        signed.push_str(&key);
        signed.push_str(&value);
    }
    signed
}

fn hex_decode(text: &str) -> Option<Vec<u8>> {
    if text.len() % 2 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(text.len() / 2);
    for index in (0..text.len()).step_by(2) {
        let byte = u8::from_str_radix(&text[index..index + 2], 16).ok()?;
        out.push(byte);
    }
    Some(out)
}

fn constant_time_eq(left: &str, right: &str) -> bool {
    let left = left.as_bytes();
    let right = right.as_bytes();
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0u8;
    for (a, b) in left.iter().zip(right.iter()) {
        diff |= a ^ b;
    }
    diff == 0
}

fn semicolon_parts(text: &str, delimiter: char) -> std::collections::HashMap<String, String> {
    text.split(delimiter)
        .filter_map(|part| {
            let (key, value) = part.split_once('=')?;
            Some((key.trim().to_string(), value.trim().to_string()))
        })
        .collect()
}

fn repeated_parts(text: &str, delimiter: char, wanted_key: &str) -> Vec<String> {
    text.split(delimiter)
        .filter_map(|part| {
            let (key, value) = part.split_once('=')?;
            if key.trim() == wanted_key {
                Some(value.trim().to_string())
            } else {
                None
            }
        })
        .collect()
}

fn tolerance_s(options: &Value, default: i64) -> rusqlite::Result<i64> {
    let Some(object) = options.as_object() else {
        return Err(rusqlite::Error::InvalidParameterName(
            "options_json must be a JSON object".to_string(),
        ));
    };
    match object.get("tolerance_s") {
        None => Ok(default),
        Some(value) => value.as_i64().filter(|value| *value >= 0).ok_or_else(|| {
            rusqlite::Error::InvalidParameterName(
                "provider option 'tolerance_s' must be a non-negative integer".to_string(),
            )
        }),
    }
}

fn outside_tolerance(timestamp: i64, tolerance: i64) -> bool {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs() as i64)
        .unwrap_or(0);
    (now - timestamp).abs() > tolerance
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
        Some(cutoff) => Some(
            serde_json::from_str::<serde_json::Value>(&prune_events(
                conn,
                statuses_json,
                cutoff,
                event_limit,
                queue_name,
            )?)
            .map_err(to_sql_err)?,
        ),
        None => None,
    };
    let orphan_result = match orphan_older_than {
        Some(cutoff) => Some(
            serde_json::from_str::<serde_json::Value>(&prune_orphan_deliveries(
                conn,
                cutoff,
                orphan_limit,
                queue_name,
            )?)
            .map_err(to_sql_err)?,
        ),
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
    let rows = stmt.query_map(rusqlite::params_from_iter(values), |row| {
        row.get::<_, i64>(0)
    })?;
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
        conn.execute(
            "DELETE FROM knocker_deliveries WHERE event_id=?1",
            params![event_id],
        )?;
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
        conn.execute(
            "DELETE FROM knocker_deliveries WHERE id=?1",
            params![delivery_id],
        )?;
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

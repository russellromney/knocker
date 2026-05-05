package knockersqlite

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	sqlite3 "github.com/mattn/go-sqlite3"
)

type KnockerSqlite struct {
	db              *sql.DB
	conn            *sql.Conn
	handlers        map[string]func(Event, *Tx) error
	endpointConfigs map[string]endpointConfig
}

type Tx struct {
	conn *sql.Conn
}

type AddEndpointParams struct {
	Name            string
	Path            string
	Provider        *string
	Enabled         bool
	Secrets         []string
	ProviderOptions map[string]any
}

type endpointConfig struct {
	Provider        *string
	Secrets         []string
	ProviderOptions map[string]any
}

type IngestParams struct {
	Endpoint           string
	Body               []byte
	Headers            map[string]any
	Query              map[string]any
	Method             string
	EventType          *string
	ProviderEventID    *string
	ProviderDeliveryID *string
	DedupeKey          *string
	SignatureValid     *bool
	SignatureError     *string
	QueueName          string
	MaxAttempts        int64
}

type IngestResult struct {
	DeliveryID int64  `json:"delivery_id"`
	EventID    *int64 `json:"event_id"`
	Duplicate  int    `json:"duplicate"`
	StatusCode int    `json:"status_code"`
}

type ReceiveParams struct {
	Endpoint           string
	Body               []byte
	Headers            map[string]any
	Query              map[string]any
	Method             string
	Provider           *string
	Secrets            []string
	ProviderOptions    map[string]any
	EventType          *string
	ProviderEventID    *string
	ProviderDeliveryID *string
	DedupeKey          *string
	QueueName          string
	MaxAttempts        int64
}

type RetentionPolicy struct {
	Statuses                   []string
	EventOlderThanS            *int64
	EventLimit                 int64
	OrphanDeliveriesOlderThanS *int64
	OrphanDeliveriesLimit      int64
	QueueName                  string
	Now                        int64
}

type Event struct {
	ID                 int64
	Endpoint           string
	EventType          sql.NullString
	ProviderEventID    sql.NullString
	ProviderDeliveryID sql.NullString
	Status             string
	AttemptCount       int64
	Body               []byte
}

type Delivery struct {
	ID                 int64
	EventID            sql.NullInt64
	Endpoint           string
	EventType          sql.NullString
	ProviderEventID    sql.NullString
	ProviderDeliveryID sql.NullString
	DedupeKey          sql.NullString
	Method             string
	HeadersJSON        string
	QueryJSON          string
	Body               []byte
	ReceivedAt         int64
	SignatureValid     sql.NullInt64
	SignatureError     sql.NullString
}

type ListDeliveriesParams struct {
	EventID        *int64
	Endpoint       *string
	SignatureValid *bool
	Orphaned       *bool
	Since          *int64
	Limit          int64
}

type PruneAudit struct {
	ID               int64
	Kind             string
	QueueName        string
	ExecutedAt       int64
	EventsPruned     sql.NullInt64
	DeliveriesPruned int64
	AttemptsPruned   sql.NullInt64
	LiveJobsPruned   sql.NullInt64
	SummaryJSON      string
}

type claimedJob struct {
	ID          int64  `json:"id"`
	Payload     string `json:"payload"`
	Attempts    int64  `json:"attempts"`
	MaxAttempts int64  `json:"max_attempts"`
}

func extensionPath() string {
	_, thisFile, _, ok := runtime.Caller(0)
	baseDir := "."
	if ok {
		baseDir = filepath.Dir(thisFile)
	}
	ext := "so"
	if runtime.GOOS == "darwin" {
		ext = "dylib"
	} else if runtime.GOOS == "windows" {
		ext = "dll"
	}
	return filepath.Clean(filepath.Join(baseDir, "..", "..", "target", "release", "libknocker_ext."+ext))
}

func Open(path string) (*KnockerSqlite, error) {
	db, err := sql.Open("sqlite3", path)
	if err != nil {
		return nil, err
	}
	conn, err := db.Conn(context.Background())
	if err != nil {
		_ = db.Close()
		return nil, err
	}
	if err := conn.Raw(func(driverConn any) error {
		c := driverConn.(*sqlite3.SQLiteConn)
		return c.LoadExtension(extensionPath(), "sqlite3_knockerext_init")
	}); err != nil {
		_ = conn.Close()
		_ = db.Close()
		return nil, err
	}
	if _, err := conn.ExecContext(context.Background(), "SELECT knocker_bootstrap()"); err != nil {
		_ = conn.Close()
		_ = db.Close()
		return nil, err
	}
	return &KnockerSqlite{
		db:              db,
		conn:            conn,
		handlers:        map[string]func(Event, *Tx) error{},
		endpointConfigs: map[string]endpointConfig{},
	}, nil
}

func (k *KnockerSqlite) Close() error {
	if k.conn != nil {
		_ = k.conn.Close()
	}
	if k.db != nil {
		return k.db.Close()
	}
	return nil
}

func (k *KnockerSqlite) AddEndpoint(params AddEndpointParams) (int64, error) {
	enabled := int64(0)
	if params.Enabled {
		enabled = 1
	}
	row := k.conn.QueryRowContext(
		context.Background(),
		"SELECT knocker_endpoint_upsert(?, ?, ?, ?) AS id",
		params.Name,
		params.Path,
		params.Provider,
		enabled,
	)
	var id int64
	if err := row.Scan(&id); err != nil {
		return 0, err
	}
	k.endpointConfigs[params.Name] = endpointConfig{
		Provider:        params.Provider,
		Secrets:         params.Secrets,
		ProviderOptions: params.ProviderOptions,
	}
	return id, nil
}

func (k *KnockerSqlite) Ingest(params IngestParams) (*IngestResult, error) {
	method := params.Method
	if method == "" {
		method = "POST"
	}
	queueName := params.QueueName
	if queueName == "" {
		queueName = "knocker.events"
	}
	maxAttempts := params.MaxAttempts
	if maxAttempts == 0 {
		maxAttempts = 3
	}
	headersJSON, err := json.Marshal(params.Headers)
	if err != nil {
		return nil, err
	}
	queryJSON, err := json.Marshal(params.Query)
	if err != nil {
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		return nil, err
	}
	var resultJSON string
	row := k.conn.QueryRowContext(
		context.Background(),
		`SELECT knocker_ingest(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) AS result_json`,
		params.Endpoint,
		method,
		string(headersJSON),
		params.Body,
		string(queryJSON),
		nullableBoolInt(params.SignatureValid),
		params.SignatureError,
		params.ProviderEventID,
		params.ProviderDeliveryID,
		params.EventType,
		params.DedupeKey,
		queueName,
		maxAttempts,
	)
	if err := row.Scan(&resultJSON); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "COMMIT"); err != nil {
		return nil, err
	}
	var result IngestResult
	if err := json.Unmarshal([]byte(resultJSON), &result); err != nil {
		return nil, err
	}
	return &result, nil
}

func (k *KnockerSqlite) Receive(params ReceiveParams) (*IngestResult, error) {
	method := params.Method
	if method == "" {
		method = "POST"
	}
	queueName := params.QueueName
	if queueName == "" {
		queueName = "knocker.events"
	}
	maxAttempts := params.MaxAttempts
	if maxAttempts == 0 {
		maxAttempts = 3
	}
	config := k.endpointConfigs[params.Endpoint]
	provider := params.Provider
	if provider == nil {
		provider = config.Provider
	}
	if provider == nil || *provider == "" {
		return nil, fmt.Errorf("endpoint %s has no provider configured", params.Endpoint)
	}
	secrets := params.Secrets
	if secrets == nil {
		secrets = config.Secrets
	}
	providerOptions := params.ProviderOptions
	if providerOptions == nil {
		providerOptions = config.ProviderOptions
	}
	headersJSON, err := json.Marshal(params.Headers)
	if err != nil {
		return nil, err
	}
	queryJSON, err := json.Marshal(params.Query)
	if err != nil {
		return nil, err
	}
	secretsJSON, err := json.Marshal(secrets)
	if err != nil {
		return nil, err
	}
	optionsJSON, err := json.Marshal(providerOptions)
	if err != nil {
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		return nil, err
	}
	var resultJSON string
	row := k.conn.QueryRowContext(
		context.Background(),
		`SELECT knocker_receive(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) AS result_json`,
		params.Endpoint,
		*provider,
		string(secretsJSON),
		string(optionsJSON),
		method,
		string(headersJSON),
		params.Body,
		string(queryJSON),
		params.ProviderEventID,
		params.ProviderDeliveryID,
		params.EventType,
		params.DedupeKey,
		queueName,
		maxAttempts,
	)
	if err := row.Scan(&resultJSON); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "COMMIT"); err != nil {
		return nil, err
	}
	var result IngestResult
	if err := json.Unmarshal([]byte(resultJSON), &result); err != nil {
		return nil, err
	}
	return &result, nil
}

func (k *KnockerSqlite) GetEvent(eventID int64) (*Event, error) {
	row := k.conn.QueryRowContext(
		context.Background(),
		`
		SELECT
		  e.id,
		  ep.name AS endpoint,
		  e.event_type,
		  e.provider_event_id,
		  e.provider_delivery_id,
		  e.status,
		  e.attempt_count,
		  e.body_blob
		FROM knocker_events e
		JOIN knocker_endpoints ep ON ep.id = e.endpoint_id
		WHERE e.id=?
		`,
		eventID,
	)
	var event Event
	if err := row.Scan(
		&event.ID,
		&event.Endpoint,
		&event.EventType,
		&event.ProviderEventID,
		&event.ProviderDeliveryID,
		&event.Status,
		&event.AttemptCount,
		&event.Body,
	); err != nil {
		return nil, err
	}
	return &event, nil
}

func (k *KnockerSqlite) ListEvents(limit int64) ([]Event, error) {
	rows, err := k.conn.QueryContext(
		context.Background(),
		`
		SELECT
		  e.id,
		  ep.name AS endpoint,
		  e.event_type,
		  e.provider_event_id,
		  e.provider_delivery_id,
		  e.status,
		  e.attempt_count
		FROM knocker_events e
		JOIN knocker_endpoints ep ON ep.id = e.endpoint_id
		ORDER BY e.id
		LIMIT ?
		`,
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Event{}
	for rows.Next() {
		var event Event
		if err := rows.Scan(
			&event.ID,
			&event.Endpoint,
			&event.EventType,
			&event.ProviderEventID,
			&event.ProviderDeliveryID,
			&event.Status,
			&event.AttemptCount,
		); err != nil {
			return nil, err
		}
		out = append(out, event)
	}
	return out, rows.Err()
}

func (k *KnockerSqlite) RegisterHandler(endpoint string, handler func(Event, *Tx) error) {
	k.handlers[handlerKey(endpoint, "")] = handler
}

func (k *KnockerSqlite) RegisterEventHandler(endpoint string, eventType string, handler func(Event, *Tx) error) {
	k.handlers[handlerKey(endpoint, eventType)] = handler
}

func (k *KnockerSqlite) Replay(eventID int64) error {
	_, err := k.conn.ExecContext(
		context.Background(),
		"SELECT knocker_replay(?, ?, ?)",
		eventID,
		"knocker.events",
		3,
	)
	return err
}

func (k *KnockerSqlite) Requeue(eventID int64) error {
	_, err := k.conn.ExecContext(
		context.Background(),
		"SELECT knocker_requeue(?, ?, ?)",
		eventID,
		"knocker.events",
		3,
	)
	return err
}

func (k *KnockerSqlite) Ignore(eventID int64) error {
	if _, err := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		return err
	}
	event, err := k.GetEvent(eventID)
	if err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return err
	}
	if event.Status != "ignored" {
		if event.Status != "received" && event.Status != "failed" && event.Status != "dead" {
			_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
			return fmt.Errorf("event %d with status %s cannot be ignored", eventID, event.Status)
		}
		if _, err := k.conn.ExecContext(context.Background(), "SELECT knocker_mark_ignored(?, ?)", eventID, 0); err != nil {
			_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
			return err
		}
	}
	_, err = k.conn.ExecContext(context.Background(), "COMMIT")
	return err
}

func (k *KnockerSqlite) GetDelivery(deliveryID int64) (*Delivery, error) {
	row := k.conn.QueryRowContext(
		context.Background(),
		`
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
		WHERE d.id=?
		`,
		deliveryID,
	)
	var delivery Delivery
	if err := row.Scan(
		&delivery.ID,
		&delivery.EventID,
		&delivery.Endpoint,
		&delivery.EventType,
		&delivery.ProviderEventID,
		&delivery.ProviderDeliveryID,
		&delivery.DedupeKey,
		&delivery.Method,
		&delivery.HeadersJSON,
		&delivery.QueryJSON,
		&delivery.Body,
		&delivery.ReceivedAt,
		&delivery.SignatureValid,
		&delivery.SignatureError,
	); err != nil {
		return nil, err
	}
	return &delivery, nil
}

func (k *KnockerSqlite) ListDeliveries(params ListDeliveriesParams) ([]Delivery, error) {
	clauses := []string{}
	args := []any{}
	if params.EventID != nil {
		clauses = append(clauses, "d.event_id=?")
		args = append(args, *params.EventID)
	}
	if params.Endpoint != nil {
		clauses = append(clauses, "ep.name=?")
		args = append(args, *params.Endpoint)
	}
	if params.SignatureValid != nil {
		if *params.SignatureValid {
			clauses = append(clauses, "d.signature_valid=1")
		} else {
			clauses = append(clauses, "(d.signature_valid=0 OR d.signature_valid IS NULL)")
		}
	}
	if params.Orphaned != nil {
		if *params.Orphaned {
			clauses = append(clauses, "d.event_id IS NULL")
		} else {
			clauses = append(clauses, "d.event_id IS NOT NULL")
		}
	}
	if params.Since != nil {
		clauses = append(clauses, "d.received_at>=?")
		args = append(args, *params.Since)
	}
	limit := params.Limit
	if limit == 0 {
		limit = 100
	}
	args = append(args, limit)
	where := ""
	if len(clauses) > 0 {
		where = "WHERE " + strings.Join(clauses, " AND ")
	}
	rows, err := k.conn.QueryContext(
		context.Background(),
		`
		SELECT
		  d.id, d.event_id, ep.name, d.event_type, d.provider_event_id,
		  d.provider_delivery_id, d.dedupe_key, d.method, d.headers_json,
		  d.query_json, d.body_blob, d.received_at, d.signature_valid,
		  d.signature_error
		FROM knocker_deliveries d
		JOIN knocker_endpoints ep ON ep.id = d.endpoint_id
		`+where+`
		ORDER BY d.received_at DESC, d.id DESC
		LIMIT ?
		`,
		args...,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []Delivery{}
	for rows.Next() {
		var delivery Delivery
		if err := rows.Scan(
			&delivery.ID,
			&delivery.EventID,
			&delivery.Endpoint,
			&delivery.EventType,
			&delivery.ProviderEventID,
			&delivery.ProviderDeliveryID,
			&delivery.DedupeKey,
			&delivery.Method,
			&delivery.HeadersJSON,
			&delivery.QueryJSON,
			&delivery.Body,
			&delivery.ReceivedAt,
			&delivery.SignatureValid,
			&delivery.SignatureError,
		); err != nil {
			return nil, err
		}
		out = append(out, delivery)
	}
	return out, rows.Err()
}

func (k *KnockerSqlite) ReplayDelivery(deliveryID int64) error {
	if _, err := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		return err
	}
	delivery, err := k.GetDelivery(deliveryID)
	if err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return err
	}
	if !delivery.EventID.Valid {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return fmt.Errorf("delivery %d is not linked to an event", deliveryID)
	}
	event, err := k.GetEvent(delivery.EventID.Int64)
	if err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return err
	}
	if event.Status != "handled" && event.Status != "failed" && event.Status != "dead" && event.Status != "ignored" {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return fmt.Errorf("event %d with status %s cannot replay a delivery", delivery.EventID.Int64, event.Status)
	}
	if err := k.deleteLiveJobsForEvent(delivery.EventID.Int64); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return err
	}
	if _, err := k.conn.ExecContext(context.Background(), "SELECT knocker_reset_event(?)", delivery.EventID.Int64); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return err
	}
	payloadBytes, _ := json.Marshal(map[string]int64{"event_id": delivery.EventID.Int64, "delivery_id": delivery.ID})
	if _, err := k.conn.ExecContext(
		context.Background(),
		"SELECT honker_enqueue(?, ?, ?, ?, ?, ?, ?)",
		"knocker.events",
		string(payloadBytes),
		nil,
		nil,
		0,
		3,
		nil,
	); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return err
	}
	_, err = k.conn.ExecContext(context.Background(), "COMMIT")
	return err
}

func (k *KnockerSqlite) PruneEvents(statuses []string, olderThan int64, limit int64) (map[string]any, error) {
	statusesJSON, err := json.Marshal(statuses)
	if err != nil {
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		return nil, err
	}
	var resultJSON string
	err = k.conn.QueryRowContext(
		context.Background(),
		"SELECT knocker_prune_events(?, ?, ?, ?)",
		string(statusesJSON),
		olderThan,
		limit,
		"knocker.events",
	).Scan(&resultJSON)
	if err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "COMMIT"); err != nil {
		return nil, err
	}
	var out map[string]any
	return out, json.Unmarshal([]byte(resultJSON), &out)
}

func (k *KnockerSqlite) PruneOrphanDeliveries(olderThan int64, limit int64) (map[string]any, error) {
	if _, err := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		return nil, err
	}
	var resultJSON string
	err := k.conn.QueryRowContext(
		context.Background(),
		"SELECT knocker_prune_orphan_deliveries(?, ?, ?)",
		olderThan,
		limit,
		"knocker.events",
	).Scan(&resultJSON)
	if err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "COMMIT"); err != nil {
		return nil, err
	}
	var out map[string]any
	return out, json.Unmarshal([]byte(resultJSON), &out)
}

func (k *KnockerSqlite) ListPruneAudits(limit int64) ([]PruneAudit, error) {
	rows, err := k.conn.QueryContext(
		context.Background(),
		`
		SELECT id, kind, queue_name, executed_at, events_pruned, deliveries_pruned,
		       attempts_pruned, live_jobs_pruned, summary_json
		FROM knocker_prune_audits
		ORDER BY executed_at DESC, id DESC
		LIMIT ?
		`,
		limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []PruneAudit{}
	for rows.Next() {
		var audit PruneAudit
		if err := rows.Scan(
			&audit.ID,
			&audit.Kind,
			&audit.QueueName,
			&audit.ExecutedAt,
			&audit.EventsPruned,
			&audit.DeliveriesPruned,
			&audit.AttemptsPruned,
			&audit.LiveJobsPruned,
			&audit.SummaryJSON,
		); err != nil {
			return nil, err
		}
		out = append(out, audit)
	}
	return out, rows.Err()
}

func (k *KnockerSqlite) RunRetentionOnce(policy RetentionPolicy) (map[string]any, error) {
	statuses := policy.Statuses
	if len(statuses) == 0 {
		statuses = []string{"handled", "ignored"}
	}
	eventLimit := policy.EventLimit
	if eventLimit == 0 {
		eventLimit = 1000
	}
	orphanLimit := policy.OrphanDeliveriesLimit
	if orphanLimit == 0 {
		orphanLimit = 1000
	}
	queueName := policy.QueueName
	if queueName == "" {
		queueName = "knocker.events"
	}
	now := policy.Now
	if now == 0 {
		now = time.Now().Unix()
	}
	var eventCutoff any
	if policy.EventOlderThanS != nil {
		eventCutoff = now - *policy.EventOlderThanS
	}
	var orphanCutoff any
	if policy.OrphanDeliveriesOlderThanS != nil {
		orphanCutoff = now - *policy.OrphanDeliveriesOlderThanS
	}
	statusesJSON, err := json.Marshal(statuses)
	if err != nil {
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		return nil, err
	}
	var resultJSON string
	err = k.conn.QueryRowContext(
		context.Background(),
		"SELECT knocker_run_retention_pass(?, ?, ?, ?, ?, ?)",
		string(statusesJSON),
		eventCutoff,
		eventLimit,
		orphanCutoff,
		orphanLimit,
		queueName,
	).Scan(&resultJSON)
	if err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "COMMIT"); err != nil {
		return nil, err
	}
	var out map[string]any
	return out, json.Unmarshal([]byte(resultJSON), &out)
}

func (k *KnockerSqlite) RunRetention(ctx context.Context, policy RetentionPolicy, interval time.Duration, maxRuns int) (int, error) {
	if interval == 0 {
		interval = time.Minute
	}
	runs := 0
	for maxRuns == 0 || runs < maxRuns {
		select {
		case <-ctx.Done():
			return runs, ctx.Err()
		default:
		}
		if _, err := k.RunRetentionOnce(policy); err != nil {
			return runs, err
		}
		runs++
		if maxRuns != 0 && runs >= maxRuns {
			break
		}
		timer := time.NewTimer(interval)
		select {
		case <-ctx.Done():
			timer.Stop()
			return runs, ctx.Err()
		case <-timer.C:
		}
	}
	return runs, nil
}

func (k *KnockerSqlite) RunWorkerOnce(workerID string) (*int64, error) {
	var rowsJSON string
	row := k.conn.QueryRowContext(
		context.Background(),
		"SELECT honker_claim_batch(?, ?, ?, ?) AS rows_json",
		"knocker.events",
		workerID,
		1,
		60,
	)
	if err := row.Scan(&rowsJSON); err != nil {
		return nil, err
	}
	var jobs []claimedJob
	if err := json.Unmarshal([]byte(rowsJSON), &jobs); err != nil {
		return nil, err
	}
	if len(jobs) == 0 {
		return nil, nil
	}
	if jobs[0].MaxAttempts == 0 {
		if err := k.conn.QueryRowContext(
			context.Background(),
			"SELECT max_attempts FROM _honker_live WHERE id=?",
			jobs[0].ID,
		).Scan(&jobs[0].MaxAttempts); err != nil {
			return nil, err
		}
	}
	payload := map[string]any{}
	if err := json.Unmarshal([]byte(jobs[0].Payload), &payload); err != nil {
		return nil, err
	}
	eventIDValue, ok := payload["event_id"].(float64)
	if !ok {
		return nil, fmt.Errorf("job payload missing numeric event_id")
	}
	eventID := int64(eventIDValue)
	event, err := k.GetEvent(eventID)
	if err != nil {
		return nil, err
	}
	if deliveryIDValue, ok := payload["delivery_id"].(float64); ok {
		delivery, err := k.GetDelivery(int64(deliveryIDValue))
		if err != nil {
			return nil, err
		}
		event.EventType = delivery.EventType
		event.ProviderEventID = delivery.ProviderEventID
		event.ProviderDeliveryID = delivery.ProviderDeliveryID
		event.Body = delivery.Body
	}
	eventType := ""
	if event.EventType.Valid {
		eventType = event.EventType.String
	}
	handler := k.handlers[handlerKey(event.Endpoint, eventType)]
	if handler == nil {
		handler = k.handlers[handlerKey(event.Endpoint, "")]
	}
	if handler == nil {
		err := fmt.Errorf("no handler registered for endpoint %q", event.Endpoint)
		return nil, k.failClaim(workerID, jobs[0], eventID, err)
	}
	if _, err := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); err != nil {
		return nil, err
	}
	if _, err := k.conn.ExecContext(
		context.Background(),
		"SELECT knocker_mark_processing(?, ?)",
		eventID,
		jobs[0].Attempts,
	); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return nil, err
	}
	if err := handler(*event, &Tx{conn: k.conn}); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		if recordErr := k.recordFailedClaim(workerID, jobs[0], eventID, err.Error()); recordErr != nil {
			return nil, recordErr
		}
		return nil, err
	}
	if _, err := k.conn.ExecContext(
		context.Background(),
		"SELECT knocker_mark_handled(?, ?)",
		eventID,
		0,
	); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return nil, err
	}
	if _, err := k.conn.ExecContext(
		context.Background(),
		"SELECT honker_ack(?, ?)",
		jobs[0].ID,
		workerID,
	); err != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return nil, err
	}
	if _, err := k.conn.ExecContext(context.Background(), "COMMIT"); err != nil {
		return nil, err
	}
	return &eventID, nil
}

func (k *KnockerSqlite) RunWorker(ctx context.Context, workerID string, maxJobs int) (int, error) {
	processed := 0
	for maxJobs == 0 || processed < maxJobs {
		select {
		case <-ctx.Done():
			return processed, ctx.Err()
		default:
		}
		eventID, err := k.RunWorkerOnce(workerID)
		if err != nil {
			return processed, err
		}
		if eventID == nil {
			time.Sleep(100 * time.Millisecond)
			continue
		}
		processed++
	}
	return processed, nil
}

func (k *KnockerSqlite) recordFailedClaim(workerID string, job claimedJob, eventID int64, message string) error {
	if _, beginErr := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); beginErr != nil {
		return beginErr
	}
	attemptCount := job.Attempts
	terminal := attemptCount >= job.MaxAttempts
	if _, markErr := k.conn.ExecContext(
		context.Background(),
		"SELECT knocker_mark_failed(?, ?, ?, ?, ?)",
		eventID,
		attemptCount,
		message,
		boolToInt(terminal),
		0,
	); markErr != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return markErr
	}
	if terminal {
		if _, failErr := k.conn.ExecContext(
			context.Background(),
			"SELECT honker_fail(?, ?, ?)",
			job.ID,
			workerID,
			message,
		); failErr != nil {
			_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
			return failErr
		}
	} else {
		if _, retryErr := k.conn.ExecContext(
			context.Background(),
			"SELECT honker_retry(?, ?, ?, ?)",
			job.ID,
			workerID,
			0,
			message,
		); retryErr != nil {
			_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
			return retryErr
		}
	}
	_, err := k.conn.ExecContext(context.Background(), "COMMIT")
	return err
}

func (k *KnockerSqlite) failClaim(workerID string, job claimedJob, eventID int64, err error) error {
	if _, beginErr := k.conn.ExecContext(context.Background(), "BEGIN IMMEDIATE"); beginErr != nil {
		return beginErr
	}
	if _, markErr := k.conn.ExecContext(
		context.Background(),
		"SELECT knocker_mark_failed(?, ?, ?, ?, ?)",
		eventID,
		job.Attempts,
		err.Error(),
		1,
		0,
	); markErr != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return markErr
	}
	if _, failErr := k.conn.ExecContext(
		context.Background(),
		"SELECT honker_fail(?, ?, ?)",
		job.ID,
		workerID,
		err.Error(),
	); failErr != nil {
		_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
		return failErr
	}
	if _, commitErr := k.conn.ExecContext(context.Background(), "COMMIT"); commitErr != nil {
		return commitErr
	}
	return err
}

func (k *KnockerSqlite) deleteLiveJobsForEvent(eventID int64) error {
	rows, err := k.conn.QueryContext(
		context.Background(),
		"SELECT id, payload FROM _honker_live WHERE queue=?",
		"knocker.events",
	)
	if err != nil {
		return err
	}
	defer rows.Close()
	var jobIDs []int64
	for rows.Next() {
		var jobID int64
		var payloadJSON string
		if err := rows.Scan(&jobID, &payloadJSON); err != nil {
			return err
		}
		payload := map[string]any{}
		if err := json.Unmarshal([]byte(payloadJSON), &payload); err != nil {
			continue
		}
		if value, ok := payload["event_id"].(float64); ok && int64(value) == eventID {
			jobIDs = append(jobIDs, jobID)
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	for _, jobID := range jobIDs {
		if _, err := k.conn.ExecContext(context.Background(), "DELETE FROM _honker_live WHERE id=?", jobID); err != nil {
			return err
		}
	}
	return nil
}

func (tx *Tx) Exec(query string, args ...any) (sql.Result, error) {
	return tx.conn.ExecContext(context.Background(), query, args...)
}

func (tx *Tx) QueryRow(query string, args ...any) *sql.Row {
	return tx.conn.QueryRowContext(context.Background(), query, args...)
}

func (tx *Tx) Query(query string, args ...any) (*sql.Rows, error) {
	return tx.conn.QueryContext(context.Background(), query, args...)
}

func nullableBoolInt(value *bool) any {
	if value == nil {
		return nil
	}
	if *value {
		return 1
	}
	return 0
}

func boolToInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

func handlerKey(endpoint string, eventType string) string {
	return endpoint + "\x00" + eventType
}

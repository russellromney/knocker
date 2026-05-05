package knockersqlite

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"path/filepath"
	"runtime"

	sqlite3 "github.com/mattn/go-sqlite3"
)

type KnockerSqlite struct {
	db       *sql.DB
	conn     *sql.Conn
	handlers map[string]func(Event) error
}

type AddEndpointParams struct {
	Name     string
	Path     string
	Provider *string
	Enabled  bool
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
		db:       db,
		conn:     conn,
		handlers: map[string]func(Event) error{},
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
	return id, row.Scan(&id)
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

func (k *KnockerSqlite) RegisterHandler(endpoint string, handler func(Event) error) {
	k.handlers[endpoint] = handler
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
	handler := k.handlers[event.Endpoint]
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
	if err := handler(*event); err != nil {
		terminal := jobs[0].Attempts >= jobs[0].MaxAttempts
		if _, markErr := k.conn.ExecContext(
			context.Background(),
			"SELECT knocker_mark_failed(?, ?, ?, ?, ?)",
			eventID,
			jobs[0].Attempts,
			err.Error(),
			boolToInt(terminal),
			0,
		); markErr != nil {
			_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
			return nil, markErr
		}
		if terminal {
			if _, failErr := k.conn.ExecContext(
				context.Background(),
				"SELECT honker_fail(?, ?, ?)",
				jobs[0].ID,
				workerID,
				err.Error(),
			); failErr != nil {
				_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
				return nil, failErr
			}
		} else {
			if _, retryErr := k.conn.ExecContext(
				context.Background(),
				"SELECT honker_retry(?, ?, ?, ?)",
				jobs[0].ID,
				workerID,
				0,
				err.Error(),
			); retryErr != nil {
				_, _ = k.conn.ExecContext(context.Background(), "ROLLBACK")
				return nil, retryErr
			}
		}
		if _, commitErr := k.conn.ExecContext(context.Background(), "COMMIT"); commitErr != nil {
			return nil, commitErr
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

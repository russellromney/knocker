package knockersqlite

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func strptr(value string) *string {
	return &value
}

func TestGoBindingEndToEnd(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "app.db")

	app, err := Open(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer app.Close()

	if _, err := app.conn.ExecContext(nilContext(), "CREATE TABLE handled_events (event_id INTEGER PRIMARY KEY, body TEXT)"); err != nil {
		t.Fatalf("create handled table: %v", err)
	}

	if _, err := app.AddEndpoint(AddEndpointParams{
		Name:     "github",
		Path:     "/webhooks/github",
		Provider: strptr("github"),
		Enabled:  true,
	}); err != nil {
		t.Fatalf("add endpoint: %v", err)
	}

	providerDeliveryID := "go-delivery-1"
	result, err := app.Ingest(IngestParams{
		Endpoint:           "github",
		Body:               []byte(`{"hello":"world"}`),
		Headers:            map[string]any{},
		Query:              map[string]any{},
		Method:             "POST",
		ProviderDeliveryID: &providerDeliveryID,
		SignatureValid:     boolptr(true),
	})
	if err != nil {
		t.Fatalf("ingest: %v", err)
	}
	if result.EventID == nil {
		t.Fatalf("expected event id after ingest")
	}

	event, err := app.GetEvent(*result.EventID)
	if err != nil {
		t.Fatalf("get event: %v", err)
	}
	if event.Status != "received" {
		t.Fatalf("expected received status, got %s", event.Status)
	}

	events, err := app.ListEvents(10)
	if err != nil {
		t.Fatalf("list events: %v", err)
	}
	if len(events) != 1 {
		t.Fatalf("expected 1 event, got %d", len(events))
	}

	var handled []int64
	app.RegisterHandler("github", func(event Event, tx *Tx) error {
		handled = append(handled, event.ID)
		if string(event.Body) != `{"hello":"world"}` {
			t.Fatalf("expected handler body, got %q", string(event.Body))
		}
		if _, err := tx.Exec("INSERT INTO handled_events (event_id, body) VALUES (?, ?)", event.ID, string(event.Body)); err != nil {
			return err
		}
		return nil
	})

	processedEventID, err := app.RunWorkerOnce("go-test-worker")
	if err != nil {
		t.Fatalf("worker once: %v", err)
	}
	if processedEventID == nil || *processedEventID != *result.EventID {
		t.Fatalf("expected worker to process %d, got %#v", *result.EventID, processedEventID)
	}
	if len(handled) != 1 || handled[0] != *result.EventID {
		t.Fatalf("expected handler to process %d, got %#v", *result.EventID, handled)
	}

	event, err = app.GetEvent(*result.EventID)
	if err != nil {
		t.Fatalf("get handled event: %v", err)
	}
	if event.Status != "handled" {
		t.Fatalf("expected handled status, got %s", event.Status)
	}
	var handledBody string
	if err := app.conn.QueryRowContext(nilContext(), "SELECT body FROM handled_events WHERE event_id=?", *result.EventID).Scan(&handledBody); err != nil {
		t.Fatalf("read handled table: %v", err)
	}
	if handledBody != `{"hello":"world"}` {
		t.Fatalf("expected transactional handler write, got %q", handledBody)
	}

	if err := app.Replay(*result.EventID); err != nil {
		t.Fatalf("replay: %v", err)
	}
	event, err = app.GetEvent(*result.EventID)
	if err != nil {
		t.Fatalf("get replayed event: %v", err)
	}
	if event.Status != "received" {
		t.Fatalf("expected received after replay, got %s", event.Status)
	}

	if _, err := os.Stat(path); err != nil {
		t.Fatalf("expected db file to exist: %v", err)
	}
}

func TestGoBindingOperatorParity(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "app.db")

	app, err := Open(path)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer app.Close()

	if _, err := app.AddEndpoint(AddEndpointParams{
		Name:     "github",
		Path:     "/webhooks/github",
		Provider: strptr("github"),
		Enabled:  true,
	}); err != nil {
		t.Fatalf("add endpoint: %v", err)
	}

	deliveryID := "go-delivery-parity"
	eventTypeV1 := "v1"
	first, err := app.Ingest(IngestParams{
		Endpoint:           "github",
		Body:               []byte(`{"version":1}`),
		ProviderDeliveryID: &deliveryID,
		EventType:          &eventTypeV1,
		SignatureValid:     boolptr(true),
	})
	if err != nil {
		t.Fatalf("ingest first: %v", err)
	}
	eventTypeV2 := "v2"
	second, err := app.Ingest(IngestParams{
		Endpoint:           "github",
		Body:               []byte(`{"version":2}`),
		ProviderDeliveryID: &deliveryID,
		EventType:          &eventTypeV2,
		SignatureValid:     boolptr(true),
	})
	if err != nil {
		t.Fatalf("ingest duplicate: %v", err)
	}
	if second.Duplicate != 1 {
		t.Fatalf("expected duplicate")
	}

	deliveries, err := app.ListDeliveries(ListDeliveriesParams{EventID: first.EventID, Limit: 10})
	if err != nil {
		t.Fatalf("list deliveries: %v", err)
	}
	if len(deliveries) != 2 {
		t.Fatalf("expected 2 deliveries, got %d", len(deliveries))
	}
	delivery, err := app.GetDelivery(second.DeliveryID)
	if err != nil {
		t.Fatalf("get delivery: %v", err)
	}
	if delivery.ProviderDeliveryID.String != deliveryID {
		t.Fatalf("expected provider delivery id %q, got %q", deliveryID, delivery.ProviderDeliveryID.String)
	}

	app.RegisterHandler("github", func(event Event, tx *Tx) error { return nil })
	if _, err := app.RunWorkerOnce("go-parity-worker"); err != nil {
		t.Fatalf("worker once: %v", err)
	}
	event, err := app.GetEvent(*first.EventID)
	if err != nil {
		t.Fatalf("get handled event: %v", err)
	}
	if event.Status != "handled" {
		t.Fatalf("expected handled, got %s", event.Status)
	}

	var replayBodies []string
	app.RegisterHandler("github", func(event Event, tx *Tx) error {
		return fmt.Errorf("replay_delivery used endpoint fallback instead of delivery event type")
	})
	app.RegisterEventHandler("github", "v2", func(event Event, tx *Tx) error {
		replayBodies = append(replayBodies, event.EventType.String+":"+string(event.Body))
		return nil
	})
	if err := app.ReplayDelivery(second.DeliveryID); err != nil {
		t.Fatalf("replay delivery: %v", err)
	}
	if _, err := app.RunWorkerOnce("go-delivery-worker"); err != nil {
		t.Fatalf("worker replay delivery: %v", err)
	}
	if len(replayBodies) != 1 || replayBodies[0] != `v2:{"version":2}` {
		t.Fatalf("expected replay delivery body, got %#v", replayBodies)
	}

	if err := app.Replay(*first.EventID); err != nil {
		t.Fatalf("replay: %v", err)
	}
	if err := app.Ignore(*first.EventID); err != nil {
		t.Fatalf("ignore: %v", err)
	}
	if err := app.Requeue(*first.EventID); err != nil {
		t.Fatalf("requeue ignored event: %v", err)
	}
	event, err = app.GetEvent(*first.EventID)
	if err != nil {
		t.Fatalf("get requeued event: %v", err)
	}
	if event.Status != "received" {
		t.Fatalf("expected received after requeue, got %s", event.Status)
	}
	if err := app.Ignore(*first.EventID); err != nil {
		t.Fatalf("ignore before prune: %v", err)
	}

	prune, err := app.PruneEvents([]string{"ignored"}, 9999999999, 10)
	if err != nil {
		t.Fatalf("prune events: %v", err)
	}
	if int(prune["events_pruned"].(float64)) != 1 {
		t.Fatalf("expected 1 pruned event, got %#v", prune)
	}
	audits, err := app.ListPruneAudits(10)
	if err != nil {
		t.Fatalf("list prune audits: %v", err)
	}
	if len(audits) == 0 || audits[0].Kind != "prune_events" {
		t.Fatalf("expected prune_events audit, got %#v", audits)
	}

	invalidID := "invalid-go"
	orphan, err := app.Ingest(IngestParams{
		Endpoint:           "github",
		Body:               []byte(`{}`),
		ProviderDeliveryID: &invalidID,
		SignatureValid:     boolptr(false),
		SignatureError:     strptr("bad signature"),
	})
	if err != nil {
		t.Fatalf("ingest orphan: %v", err)
	}
	if orphan.EventID != nil {
		t.Fatalf("expected orphan delivery")
	}
	orphanPrune, err := app.PruneOrphanDeliveries(9999999999, 10)
	if err != nil {
		t.Fatalf("prune orphan deliveries: %v", err)
	}
	if int(orphanPrune["deliveries_pruned"].(float64)) != 1 {
		t.Fatalf("expected one orphan delivery pruned, got %#v", orphanPrune)
	}
}

func TestGoFailureRollbackEventTypeReceiveWorkerAndRetentionParity(t *testing.T) {
	app, err := Open(filepath.Join(t.TempDir(), "app.db"))
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer app.Close()
	if _, err := app.conn.ExecContext(nilContext(), "CREATE TABLE side_effects (event_id INTEGER)"); err != nil {
		t.Fatalf("create side effects: %v", err)
	}
	if _, err := app.AddEndpoint(AddEndpointParams{
		Name:     "github",
		Path:     "/webhooks/github",
		Provider: strptr("github"),
		Enabled:  true,
		Secrets:  []string{"github-secret"},
	}); err != nil {
		t.Fatalf("add endpoint: %v", err)
	}

	failingDeliveryID := "go-failure-rollback"
	failing, err := app.Ingest(IngestParams{
		Endpoint:           "github",
		Body:               []byte(`{}`),
		ProviderDeliveryID: &failingDeliveryID,
		MaxAttempts:        2,
	})
	if err != nil {
		t.Fatalf("ingest failing: %v", err)
	}
	app.RegisterHandler("github", func(event Event, tx *Tx) error {
		if _, err := tx.Exec("INSERT INTO side_effects (event_id) VALUES (?)", *failing.EventID); err != nil {
			return err
		}
		return fmt.Errorf("boom")
	})
	if _, err := app.RunWorkerOnce("go-failing-worker"); err == nil {
		t.Fatalf("expected first worker failure")
	}
	var sideEffects int
	if err := app.conn.QueryRowContext(nilContext(), "SELECT COUNT(*) FROM side_effects").Scan(&sideEffects); err != nil {
		t.Fatalf("count side effects: %v", err)
	}
	if sideEffects != 0 {
		t.Fatalf("expected side effect rollback, got %d", sideEffects)
	}
	event, err := app.GetEvent(*failing.EventID)
	if err != nil {
		t.Fatalf("get failed event: %v", err)
	}
	if event.Status != "failed" {
		t.Fatalf("expected failed after first error, got %s", event.Status)
	}
	if _, err := app.RunWorkerOnce("go-failing-worker"); err == nil {
		t.Fatalf("expected second worker failure")
	}
	event, err = app.GetEvent(*failing.EventID)
	if err != nil {
		t.Fatalf("get dead event: %v", err)
	}
	if event.Status != "dead" {
		t.Fatalf("expected dead after terminal error, got %s", event.Status)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if processed, err := app.RunWorker(ctx, "go-cancelled-worker", 1); err != context.Canceled || processed != 0 {
		t.Fatalf("expected cancelled empty loop with 0 processed, got processed=%d err=%v", processed, err)
	}
	loopErrorDeliveryID := "go-loop-error"
	if _, err := app.Ingest(IngestParams{
		Endpoint:           "github",
		Body:               []byte(`{}`),
		ProviderDeliveryID: &loopErrorDeliveryID,
		MaxAttempts:        1,
	}); err != nil {
		t.Fatalf("ingest loop error: %v", err)
	}
	app.RegisterHandler("github", func(event Event, tx *Tx) error {
		return fmt.Errorf("loop boom")
	})
	if processed, err := app.RunWorker(context.Background(), "go-error-loop", 1); err == nil || err.Error() != "loop boom" || processed != 0 {
		t.Fatalf("expected loop boom with 0 processed, got processed=%d err=%v", processed, err)
	}

	typeA := "push"
	typeB := "pull_request"
	deliveryA := "go-type-a"
	deliveryB := "go-type-b"
	if _, err := app.Ingest(IngestParams{Endpoint: "github", Body: []byte(`{}`), ProviderDeliveryID: &deliveryA, EventType: &typeA}); err != nil {
		t.Fatalf("ingest type a: %v", err)
	}
	if _, err := app.Ingest(IngestParams{Endpoint: "github", Body: []byte(`{}`), ProviderDeliveryID: &deliveryB, EventType: &typeB}); err != nil {
		t.Fatalf("ingest type b: %v", err)
	}
	seen := []string{}
	app.RegisterHandler("github", func(event Event, tx *Tx) error {
		seen = append(seen, "fallback:"+event.EventType.String)
		return nil
	})
	app.RegisterEventHandler("github", "pull_request", func(event Event, tx *Tx) error {
		seen = append(seen, "typed:"+event.EventType.String)
		return nil
	})
	processed, err := app.RunWorker(context.Background(), "go-loop-worker", 2)
	if err != nil {
		t.Fatalf("run worker: %v", err)
	}
	if processed != 2 {
		t.Fatalf("expected 2 processed jobs, got %d", processed)
	}
	if len(seen) != 2 || seen[0] != "fallback:push" || seen[1] != "typed:pull_request" {
		t.Fatalf("unexpected dispatch: %#v", seen)
	}

	body := []byte(`{"zen":"keep it logically awesome"}`)
	received, err := app.Receive(ReceiveParams{
		Endpoint: "github",
		Body:     body,
		Headers: map[string]any{
			"X-Hub-Signature-256": "sha256=2467a1987473c6ee89a22fe24f010dca30cb93d575240b9689ab697bed4b6eab",
			"X-GitHub-Delivery":   "delivery-abc",
			"X-GitHub-Event":      "push",
		},
	})
	if err != nil {
		t.Fatalf("receive: %v", err)
	}
	if received.StatusCode != 204 {
		t.Fatalf("expected 204 receive, got %#v", received)
	}
	receivedEvent, err := app.GetEvent(*received.EventID)
	if err != nil {
		t.Fatalf("get received event: %v", err)
	}
	if receivedEvent.EventType.String != "push" {
		t.Fatalf("expected event type push, got %#v", receivedEvent.EventType)
	}

	app.RegisterHandler("github", func(event Event, tx *Tx) error { return nil })
	if _, err := app.RunWorkerOnce("go-retention-worker"); err != nil {
		t.Fatalf("retention worker: %v", err)
	}
	runs, err := app.RunRetention(context.Background(), RetentionPolicy{
		EventOlderThanS: int64ptr(0),
		Now:             time.Now().Unix() + 10,
	}, time.Millisecond, 1)
	if err != nil {
		t.Fatalf("run retention: %v", err)
	}
	if runs != 1 {
		t.Fatalf("expected one retention run, got %d", runs)
	}
	audits, err := app.ListPruneAudits(1)
	if err != nil {
		t.Fatalf("audits: %v", err)
	}
	if len(audits) != 1 || audits[0].Kind != "prune_events" {
		t.Fatalf("expected retention audit, got %#v", audits)
	}
	if !audits[0].EventsPruned.Valid || audits[0].EventsPruned.Int64 < 1 {
		t.Fatalf("expected retention to prune one event, got %#v", audits[0])
	}
	var remaining int
	if err := app.conn.QueryRowContext(nilContext(), "SELECT COUNT(*) FROM knocker_events WHERE id=?", *received.EventID).Scan(&remaining); err != nil {
		t.Fatalf("count retained event: %v", err)
	}
	if remaining != 0 {
		t.Fatalf("expected retention-pruned event to be gone, got %d", remaining)
	}
}

func boolptr(value bool) *bool {
	return &value
}

func int64ptr(value int64) *int64 {
	return &value
}

func nilContext() context.Context {
	return context.Background()
}

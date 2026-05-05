package knockersqlite

import (
	"os"
	"path/filepath"
	"testing"
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
	app.RegisterHandler("github", func(event Event) error {
		handled = append(handled, event.ID)
		if string(event.Body) != `{"hello":"world"}` {
			t.Fatalf("expected handler body, got %q", string(event.Body))
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

func boolptr(value bool) *bool {
	return &value
}

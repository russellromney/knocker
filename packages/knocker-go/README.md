# knocker-go

Go binding for Knocker's loadable SQLite extension.

This module is currently repo-local. It loads `target/release/libknocker_ext.*`,
so build the extension first:

```bash
cargo build --release -p knocker-extension
```

The Go binding uses `github.com/mattn/go-sqlite3`, so CGO must be enabled.

## Use

```go
package main

import (
    "context"
    "fmt"

    knockersqlite "github.com/russellromney/knocker/packages/knocker-go"
)

func main() {
    webhooks, err := knockersqlite.Open("app.db")
    if err != nil {
        panic(err)
    }
    defer webhooks.Close()

    provider := "token-header"
    _, err = webhooks.AddEndpoint(knockersqlite.AddEndpointParams{
        Name:     "automation",
        Path:     "/webhooks/automation",
        Provider: &provider,
        Enabled:  true,
        Secrets:  []string{"dev-secret"},
    })
    if err != nil {
        panic(err)
    }

    webhooks.RegisterEventHandler("automation", "invoice.created", func(event knockersqlite.Event, tx *knockersqlite.Tx) error {
        fmt.Println("handled", event.ID)
        return nil
    })

    result, err := webhooks.Receive(knockersqlite.ReceiveParams{
        Endpoint: "automation",
        Body:     []byte(`{"id":"evt_1","type":"invoice.created"}`),
        Headers:  map[string]any{"X-Knocker-Token": "dev-secret"},
    })
    if err != nil {
        panic(err)
    }

    _, err = webhooks.RunWorker(context.Background(), "go-worker", 1)
    if err != nil {
        panic(err)
    }
    fmt.Println(result.StatusCode)
}
```

Use `Receive(...)` for provider-verified ingress. Use `Ingest(...)` only when
a trusted layer already verified the webhook.

## Test

```bash
make test-go
```

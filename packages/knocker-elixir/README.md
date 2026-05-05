# knocker-elixir

Elixir binding for Knocker's loadable SQLite extension.

This package is currently repo-local. It loads `target/release/libknocker_ext.*`,
so build the extension first:

```bash
cargo build --release -p knocker-extension
```

Install dependencies once:

```bash
make test-elixir-setup
```

## Use

```elixir
{:ok, opened} = KnockerSqlite.open("app.db")

webhooks =
  KnockerSqlite.register_handler(opened, "automation", "invoice.created", fn event, _tx ->
    IO.inspect(event["id"], label: "handled")
  end)

{:ok, _endpoint_id} =
  KnockerSqlite.add_endpoint(webhooks, %{
    name: "automation",
    path: "/webhooks/automation",
    provider: "token-header",
    enabled: true
  })

{:ok, result} =
  KnockerSqlite.receive(webhooks, %{
    endpoint: "automation",
    provider: "token-header",
    secrets: ["dev-secret"],
    body: ~s({"id":"evt_1","type":"invoice.created"}),
    headers: %{"X-Knocker-Token" => "dev-secret"}
  })

1 = KnockerSqlite.run_worker(webhooks, "elixir-worker", %{max_jobs: 1})
IO.inspect(result["status_code"])
```

Use `receive(...)` for provider-verified ingress. Use `ingest(...)` only when
a trusted layer already verified the webhook.

## Test

```bash
make test-elixir
```

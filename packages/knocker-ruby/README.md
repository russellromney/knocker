# knocker-ruby

Ruby binding for Knocker's loadable SQLite extension.

This package is currently repo-local. It loads `target/release/libknocker_ext.*`,
so build the extension first:

```bash
cargo build --release -p knocker-extension
```

The Ruby binding uses `Fiddle` to load `libsqlite3`. If auto-detection fails,
set:

```bash
export KNOCKER_SQLITE3_LIB=/path/to/libsqlite3.dylib
```

## Use

```ruby
require_relative "./packages/knocker-ruby/lib/knocker_sqlite"

webhooks = KnockerSqlite::Database.open("app.db")

webhooks.add_endpoint(
  name: "automation",
  path: "/webhooks/automation",
  provider: "token-header",
  secrets: ["dev-secret"],
)

webhooks.register_handler("automation", event_type: "invoice.created") do |event, tx|
  # Business writes through tx commit atomically with Knocker's handled
  # transition and queue ack.
  puts "handled #{event['id']}"
end

result = webhooks.receive(
  endpoint: "automation",
  body: "{\"id\":\"evt_1\",\"type\":\"invoice.created\"}",
  headers: { "X-Knocker-Token" => "dev-secret" },
)

webhooks.run_worker(worker_id: "ruby-worker", max_jobs: 1)
puts result["status_code"]
```

Use `receive(...)` for provider-verified ingress. Use `ingest(...)` only when
a trusted layer already verified the webhook.

## Test

```bash
make test-ruby
```

# knocker-bun

Bun binding for Knocker's loadable SQLite extension.

This package is currently repo-local. It loads `target/release/libknocker_ext.*`,
so build the extension first:

```bash
cargo build --release -p knocker-extension
```

The Bun binding uses FFI to load `libsqlite3`. If auto-detection fails, set:

```bash
export KNOCKER_SQLITE3_LIB=/path/to/libsqlite3.dylib
```

## Use

```ts
import { KnockerSqlite } from "./packages/knocker-bun/index";

const webhooks = new KnockerSqlite("app.db");

webhooks.addEndpoint({
  name: "automation",
  path: "/webhooks/automation",
  provider: "token-header",
  secrets: ["dev-secret"],
});

webhooks.registerHandler("automation", (event, tx) => {
  // Business writes through tx commit atomically with Knocker's handled
  // transition and queue ack.
  console.log("handled", event.id);
}, "invoice.created");

const result = webhooks.receive({
  endpoint: "automation",
  body: Buffer.from('{"id":"evt_1","type":"invoice.created"}'),
  headers: { "X-Knocker-Token": "dev-secret" },
});

await webhooks.runWorker({ workerId: "bun-worker", maxJobs: 1 });
console.log(result.status_code);
```

Use `receive(...)` for provider-verified ingress. Use `ingest(...)` only when
a trusted layer already verified the webhook.

## Test

```bash
make test-bun
```

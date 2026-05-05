# knocker-node

Node binding for Knocker's loadable SQLite extension.

This package is currently repo-local. It loads `target/release/libknocker_ext.*`,
so build the extension first:

```bash
cargo build --release -p knocker-extension
```

## Use

```js
import { KnockerSqlite } from "./packages/knocker-node/index.mjs";

const webhooks = new KnockerSqlite("app.db");

webhooks.addEndpoint({
  name: "automation",
  path: "/webhooks/automation",
  provider: "token-header",
  secrets: ["dev-secret"],
});

webhooks.registerHandler("automation", "invoice.created", (event, tx) => {
  // Business writes through tx commit atomically with Knocker's handled
  // transition and queue ack.
  console.log("handled", event.id);
});

const result = webhooks.receive({
  endpoint: "automation",
  body: Buffer.from('{"id":"evt_1","type":"invoice.created"}'),
  headers: { "X-Knocker-Token": "dev-secret" },
});

await webhooks.runWorker({ workerId: "node-worker", maxJobs: 1 });
console.log(result.status_code);
```

Use `receive(...)` for provider-verified ingress. Use `ingest(...)` only when
a trusted layer already verified the webhook.

## Test

```bash
npm --prefix packages/knocker-node test
```

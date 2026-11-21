# @sistemo/sdk (JavaScript / TypeScript)

Run AI agents and untrusted code in **real isolated Firecracker microVMs**

```bash
npm install @sistemo/sdk
```

Requires Node 18+ (uses the global `fetch`). Zero runtime dependencies.

## Quickstart (< 10 lines)

First, get an API key from the dashboard (**Dashboard → API Keys**) and export it:

```bash
export SISTEMO_API_KEY=sk_live_xxxxxxxx
```

Then save this as `hello.mjs` — it is a source file, not something to paste at a shell
prompt. The `.mjs` extension matters: the snippet uses `import` and top-level
`await`, so a plain `.js` file needs `"type": "module"` in your `package.json`.

```js
// hello.mjs
import { Sandbox } from "@sistemo/sdk";

const sb = await Sandbox.create();                  // reads SISTEMO_API_KEY
const res = await sb.run("node -e 'console.log(2 + 2)'");
console.log(res.stdout, res.exitCode);              // "4\n" 0
await sb.close();
```

Then run it:

```bash
node hello.mjs
```

## Configuration

| | |
|---|---|
| `SISTEMO_API_KEY` | your key (`sk_live_…`). Required. |
| `SISTEMO_BASE_URL` | override the API URL (default `https://api.sistemo.io`). |

Or pass them explicitly: `Sandbox.create({ apiKey: "sk_live_…", baseUrl: "https://…" })`.

## API

```ts
const sb = await Sandbox.create({ vcpus: 1, memoryMb: 1024, stack: "base" });
const res = await sb.run("echo hi && uname -a");     // -> { exitCode, stdout, stderr, truncated, requestId, lost, ok } (default 120s, max 24h)
await sb.run("node app.js", 120, { env: { FOO: "bar" }, cwd: "/tmp" });
await sb.close();                                    // destroy
```

A **read** API key can list resources but cannot create sandboxes or run code — use a **full** key with the SDK.

## Errors

```ts
import {
  APIError,
  AuthError,
  InsufficientBalanceError,
  QuotaExceededError,
  TooManyExecsError,
} from "@sistemo/sdk";

try {
  const sb = await Sandbox.create();
  await sb.run("...");
} catch (e) {
  // Check QuotaExceededError FIRST — it and AuthError both arrive as 403, but
  // they need opposite handling: an auth failure never succeeds on retry, a
  // quota refusal does once you free the resource it names.
  if (e instanceof QuotaExceededError) { console.log(e.detail); }
  else if (e instanceof InsufficientBalanceError) { /* 402 — top up; freeing machines does not help */ }
  else if (e instanceof TooManyExecsError) { /* 409 — this machine already has 16 commands running; wait */ }
  else if (e instanceof AuthError) { /* 401/403 — key invalid or read-only */ }
  else if (e instanceof APIError) { console.log(e.status, e.detail, e.code, e.requestId, e.retryable, e.docUrl); }
}
```

`QuotaExceededError` means an account limit was reached; `detail` names which
dimension bound and how much is in use. `client.getQuotas()` is
`GET /v1/quotas`: `tier`, `quotas` (used and limit per dimension), and
`per_machine`. `InsufficientBalanceError` is HTTP 402 (top up); it is
not a quota refusal and not an auth failure.

Every request sends `X-Request-Id`. Quote `e.requestId` when you contact support.
On `429`, the client honours `Retry-After` (capped at 60 s) from version 0.1.3.
`e.retryable` and `e.docUrl` come from the API body when it sent them.

Apache-2.0.

## List machines and volumes

Lists are keyset-paginated. `listVolumes()` / `listMachines()` return
`{ volumes|machines, nextCursor }` (`null` means the last page). Walk every
page with `iterVolumes()` / `iterMachines()`.

```ts
import { Client } from "@sistemo/sdk";

const c = new Client();
const page = await c.listVolumes();               // page.volumes[0].id, page.nextCursor
const next = await c.listVolumes({ cursor: page.nextCursor ?? undefined, limit: 50 });
for await (const vol of c.iterVolumes()) {
  console.log(vol.id);
}
```

## Power, wait, snapshots, images

`stop()` powers the machine off (`POST /v1/machines/{id}/stop`). `boot()`
powers it back on (`POST /v1/machines/{id}/start`). `boot()` is not
`start()`: `start(script)` runs a command inside the guest.

`connect()` is one GET and does not wait. `wait(timeoutMs)` polls
`GET /v1/machines/{id}` until `running`. `timeoutMs` is required.
A machine that is `stopped`, `stopping`, `error`, `destroyed`, or
`destroying` fails immediately — waiting will not start it; call `boot()`
for a stopped machine. Past the deadline the error is `MachineTimeout`.
Create already returns a running machine.

```ts
import { Client, Sandbox } from "@sistemo/sdk";

const sb = await Sandbox.connect(machineId);
await sb.stop();
await sb.boot();
await sb.wait(30_000);

const snap = await sb.createSnapshot("before");   // POST /v1/machines/{id}/snapshots
const page = await sb.listSnapshots();             // { snapshots, nextCursor }
await sb.restoreSnapshot(snap.id);                 // POST /v1/snapshots/{id}/restore
await sb.deleteSnapshot(snap.id);                  // DELETE /v1/snapshots/{id}

for (const image of await new Client().listImages()) {  // GET /v1/images
  console.log(image.name);                         // pass as stack when creating
}
```

`rootVolumeId` is kept on the instance after `create` and `connect`.
The create response does not include it; the value is the option you
passed until a later response names one.

`await using` calls `close()` on Node 24 and later. A close error is
swallowed, matching Python `with`, so it cannot mask the error from the
block:

```js
{
  await using sb = await Sandbox.create();
  await sb.run("uname -a");
}
```

On Node 18–22 the method is there (`sb[Symbol.asyncDispose]()`), but the
`await using` syntax does not parse. Call `close()` in `finally`.

## Long-running commands

`sb.run()` starts a guest job and waits for it on your machine (default **120
seconds**, maximum 24 hours). Each poll is a short request, so a proxy never
holds a connection for the whole command. Pass a longer timeout for installs
and builds:

```ts
const sb = await Sandbox.create();
try {
  const r = await sb.run("npm ci && npm run build", 3600);
  console.log(r.exitCode);
} finally {
  await sb.close();
}
```

`sb.start()` returns the handle without waiting, when you want to stream,
cancel, or reconnect:

```ts
const sb = await Sandbox.create();
try {
  const job = await sb.start("npm ci && npm run build", 3600);

  for await (const chunk of job.stream()) process.stdout.write(chunk);

  await job.wait();
  console.log(job.state, job.exitCode);
} finally {
  await sb.close();
}
```

The handle outlives the process that created it. Reconnect with
`Sandbox.connect(machineId)` (one GET; it does not provision a new microVM
and it does not wait for `running` — call `sb.wait(timeoutMs)` first if the
machine may still be starting), then
`sb.job(execId)` or `sb.jobs()` / `sb.iterJobs()`. `jobs()` is a real array
plus `nextCursor`; the guest keeps at most 64, so the default page holds them
all.

```ts
const sb = await Sandbox.connect(machineId);  // GET /v1/machines/{id}
const job = await sb.job(execId);
await job.wait();
```

### Three things worth knowing

**`exitCode` is `null` until it is known** — and `null` forever if the job is
`lost`. It is never `0` as a placeholder, because a zero would read as success.
Branch on `state`, or use `job.ok`.

**`lost` is not `failed`.** `failed` means the command ran and returned non-zero.
`lost` means the machine could not account for it — neither that it ran nor that
it did not. Do not retry blindly and do not assume completion.
If the machine stops while `run()` / `wait()` is polling, the result is
`lost: true` (not a thrown 409). `start()` on a machine that is already
stopped throws `MachineNotRunningError`.

**A failed `start()` may still have started something.** On
`ExecStartUnconfirmed` the command may be running; the error carries the handle:

```ts
let job;
try {
  job = await sb.start("./deploy.sh");
} catch (e) {
  if (!(e instanceof ExecStartUnconfirmed)) throw e;
  job = await sb.job(e.execId);   // find out what actually happened
}
await job.wait();
```

You mint `execId` (a UUID is generated if you omit it). Re-sending the same id
is a 409 and returns the existing job, not a second run. If start throws
`ExecStartUnconfirmed`, poll that id — do not start again.

The 17th concurrent command is `TooManyExecsError` (`too_many_execs`): wait for
one to finish; a new id does not help. `POST /execs` is never retried — not on
HTTP 5xx/429, and not on a dropped connection. A dropped start is recovered by
GET of the minted id (404 is retried for a few seconds — the API can 404 while
still accepting the POST). If that GET also fails, `ExecStartUnconfirmed` carries it.

## Locked network and secrets

By default a machine has the open internet. `network: "locked"` refuses every
outbound connection except HTTP and HTTPS (ports 80 and 443) to the hosts in
`allow`. An entry is an exact host (`pypi.org`) or one leading `*.`
(`*.pythonhosted.org`, which matches subdomains but not the name itself).
A locked machine with an empty `allow` has no public outbound at all.

```ts
const sb = await Sandbox.create({ network: "locked", allow: ["pypi.org", "*.pythonhosted.org"] });
await sb.run("pip install requests");               // allowed
await sb.run("curl -sS https://example.com");       // refused

await sb.setNetwork("locked", ["api.example.com"]); // replaces the WHOLE list
[sb.network, sb.allow];                             // -> ["locked", ["api.example.com"]]

const log = await sb.egress({ action: "blocked", limit: 50 }); // newest first
for (const e of log.activity) console.log(e.at, e.action, e.host);
log.complete; // false when rows may be missing; an empty list means
              // "no outbound calls" only when this is true
```

A refused plain-HTTP call gets `403` with `{"code": "egress_domain_not_allowed"}`;
a refused HTTPS call has its connection closed. A running machine changes
policy in place; a stopped one gets it at its next start.

**Secrets** are created and rotated in the dashboard; their values are
write-only and no API call ever returns one. The SDK binds them by name, and
each bound secret reaches the machine's commands as an environment variable of
the same name:

```ts
const sb = await Sandbox.create({ secrets: ["ORDERS_DB_URL"] });
await sb.run('psql "$ORDERS_DB_URL" -c "select 1"');

await sb.secrets.bind("STRIPE_KEY");      // -> ["ORDERS_DB_URL", "STRIPE_KEY"]
await sb.secrets.unbind("ORDERS_DB_URL"); // -> ["STRIPE_KEY"]
sb.secrets.names;                         // names only

await new Client().listSecrets();         // name, custody, hosts, bound_machines — never a value
```

⚠ A bound secret is readable by anything that runs on that machine — keep the
machine's code and its network policy as tight as the secret is sensitive.
Binding changes the NEXT command; one already running keeps its environment.
At most 16 per machine. Passing `env` with the same name as a bound secret is
a `400`, never a silent override. Every bind and unbind is in the Activity feed
(names only).

## Changelog

### 0.2.0

- Locked networks: `Sandbox.create({ network: "locked", allow: [...] })`, `sb.setNetwork(network, allow)` and `sb.egress({ action, limit })`. `sb.network` and `sb.allow` are read back from every machine response. A create without these options sends exactly what 0.1.x sent, so the open default is unchanged.
- Secrets by name: `Sandbox.create({ secrets: [...] })`, `sb.secrets.bind(...)` / `unbind(...)` / `names`, and `Client.listSecrets()`. Values are never returned.

### 0.1.8

- `Sandbox.stop()` powers a machine off (`POST /v1/machines/{id}/stop`). `Sandbox.boot()` powers it on (`POST /v1/machines/{id}/start`). `start()` still starts a command.
- `Sandbox.wait(timeoutMs)` polls until `running`. The deadline is required. `connect()` does not wait. `stopped`, `stopping`, `error`, `destroyed`, and `destroying` fail immediately. `MachineTimeout` is only the deadline.
- Snapshot create, list, restore, and delete. Lists use `nextCursor`.
- `Client.listImages()` returns the catalog from `GET /v1/images`.
- `Client.getQuotas()` returns the body of `GET /v1/quotas`.
- `rootVolumeId` is stored on the instance after create and connect.
- `[Symbol.asyncDispose]` calls `close()` and swallows a close error, so `await using` matches Python `with`. The syntax needs Node 24 or later.

### 0.1.7

- GET of a minted `execId` after a dropped start retries 404 for a few seconds. The API can 404 for 0.2–0.5 s while still accepting the POST; a single GET treated that as "nothing started" while the command ran to completion.

### 0.1.6

- A dropped `start()` / `run()` is recovered by GET of the minted `execId`, never by re-POSTing. If GET also fails, `ExecStartUnconfirmed` still carries the id.
- After a command has already succeeded, unread output is empty stdout — not `lost: true`. `run()` now reads `lost` after fetching output, matching Python.

### 0.1.5

- `TooManyExecsError` for the 17th concurrent `run()` / `start()`.
- `ProcessLimitError`, `DiskFullError`, `OutOfMemoryError`, `MachineNotRunningError`.
- `run()` / `wait()` return `lost: true` when the machine stops mid-wait.
- `POST /execs` is never retried, including dropped connections.

### 0.1.4

- Abort kept through the response body (no 300 s hang).
- GET retries after a dropped connection; invalid JSON is `SistemoError`.
- `retryable` / `docUrl` on `APIError`; `InsufficientBalanceError` for HTTP 402.
- Pagination helpers (`nextCursor`, `iter*`); `Sandbox.connect()`; `ExecResult.lost` / `ok`.

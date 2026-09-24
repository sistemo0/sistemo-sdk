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
const res = await sb.run("echo hi && uname -a");     // -> { exitCode, stdout, stderr, truncated } (default 120s, max 24h)
await sb.close();                                    // destroy
```

A **read** API key can list resources but cannot create sandboxes or run code — use a **full** key with the SDK.

## Errors

```ts
import { APIError, AuthError, QuotaExceededError } from "@sistemo/sdk";

try {
  const sb = await Sandbox.create();
  await sb.run("...");
} catch (e) {
  // Check QuotaExceededError FIRST — it and AuthError both arrive as 403, but
  // they need opposite handling: an auth failure never succeeds on retry, a
  // quota refusal does once you free the resource it names.
  if (e instanceof QuotaExceededError) { console.log(e.detail); }
  else if (e instanceof AuthError) { /* 401/403 — key invalid or read-only */ }
  else if (e instanceof APIError) { console.log(e.status, e.detail, e.code); }
}
```

`QuotaExceededError` means an account limit was reached; `detail` names which
dimension bound and how much is in use, and `GET /v1/quotas` reports every limit
alongside current usage.

Apache-2.0.

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

The handle outlives the process that created it, so a crashed client can
reconnect with `sb.job(execId)` or list what is running with `sb.jobs()`.

### Three things worth knowing

**`exitCode` is `null` until it is known** — and `null` forever if the job is
`lost`. It is never `0` as a placeholder, because a zero would read as success.
Branch on `state`, or use `job.ok`.

**`lost` is not `failed`.** `failed` means the command ran and returned non-zero.
`lost` means the machine could not account for it — neither that it ran nor that
it did not. Do not retry blindly and do not assume completion.

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

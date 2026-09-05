# @sistemo/sdk (JavaScript / TypeScript)

Run AI agents and untrusted code in **real isolated Firecracker microVMs** — self-host for free or use the cloud.

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
const res = await sb.run("echo hi && uname -a", 30); // -> { exitCode, stdout, stderr }
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

# Sistemo SDKs

Official client libraries for **cloud.sistemo.io** — run AI agents and untrusted code in real isolated Firecracker microVMs.

| Language | Package | Install | Path |
|---|---|---|---|
| Python | `sistemo` | `pip install sistemo` | [`python/`](./python) |
| JS / TS | `@sistemo/sdk` | `npm install @sistemo/sdk` | [`js/`](./js) |

## Get a key (30 seconds)

1. Sign in to the dashboard → **API Keys**.
2. **Create key** → choose **full** (read + write) → copy the `sk_live_…` secret (shown once).
3. `export SISTEMO_API_KEY=sk_live_xxxxxxxx`

> A **read** key can list resources but can't create sandboxes or run code. Use **full** with the SDKs.

## Run code in < 10 lines

**Python**
```python
from sistemo import Sandbox

with Sandbox() as sb:
    r = sb.run("python -c 'print(2 + 2)'")
    print(r.stdout, r.exit_code)
```

**TypeScript**
```ts
import { Sandbox } from "@sistemo/sdk";

const sb = await Sandbox.create();
const r = await sb.run("node -e 'console.log(2 + 2)'");
console.log(r.stdout, r.exitCode);
await sb.close();
```

Both SDKs are thin, **zero-dependency** wrappers over the REST API (`POST /v1/machines`, `POST /v1/machines/{id}/exec`, `DELETE /v1/machines/{id}`). Self-hosting? Point `SISTEMO_BASE_URL` at your own control plane.

See each package's README for configuration, the full API, and error handling.

---

*Want to run the tests, or change something? See [`CONTRIBUTING.md`](./CONTRIBUTING.md).*

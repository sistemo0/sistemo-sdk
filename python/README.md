# sistemo (Python)

Run AI agents and untrusted code in **real isolated Firecracker microVMs** — self-host for free or use the cloud.

```bash
pip install sistemo
```

## Quickstart (< 10 lines)

```python
from sistemo import Sandbox

with Sandbox() as sb:                       # reads SISTEMO_API_KEY
    result = sb.run("python -c 'print(2 + 2)'")
    print(result.stdout, result.exit_code)  # "4\n" 0
```

Get an API key from the dashboard (**Dashboard → API Keys**), then:

```bash
export SISTEMO_API_KEY=sk_live_xxxxxxxx
```

## Configuration

| | |
|---|---|
| `SISTEMO_API_KEY` | your key (`sk_live_…`). Required. |
| `SISTEMO_BASE_URL` | override the API URL (default `https://api.sistemo.io`; e.g. your self-hosted control plane). |

Or pass them explicitly: `Sandbox(api_key="sk_live_…", base_url="https://…")`.

## API

```python
sb = Sandbox(vcpus=1, memory_mb=1024, stack="base")  # provisions a microVM
res = sb.run("echo hi && uname -a", timeout=30)            # -> ExecResult
res.stdout, res.stderr, res.exit_code, res.ok
sb.close()                                                 # destroy (or use `with`)
```

A **read** API key can list resources but cannot create sandboxes or run code — use a **full** key with the SDK.

## Errors

```python
from sistemo import APIError, AuthError, QuotaExceededError

try:
    with Sandbox() as sb:
        sb.run("...")
except QuotaExceededError as e:
    # An account limit was reached. `detail` names which one and how much is in
    # use. Retrying SUCCEEDS once you free that resource — stop or destroy a
    # machine, delete a volume — or once the limit is raised.
    print(e.detail)
except AuthError:
    ...          # 401/403 — key missing/invalid/revoked, or read-only key
except APIError as e:
    print(e.status, e.detail, e.code)
```

Both `QuotaExceededError` and `AuthError` arrive as HTTP 403, so **catch
`QuotaExceededError` first** — it is a subclass of `APIError`, not of `AuthError`,
precisely because the two need opposite handling: an auth failure never succeeds
on retry, a quota refusal does once you free something. `GET /v1/quotas` reports
every limit alongside current usage.

Zero runtime dependencies (Python stdlib only). Apache-2.0.

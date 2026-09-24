# sistemo (Python)

Run AI agents and untrusted code in **real isolated Firecracker microVMs** — self-host for free or use the cloud.

```bash
pip install sistemo
```

On Debian, Ubuntu and Fedora the system Python is marked *externally managed*
(PEP 668), so that call is refused with `error: externally-managed-environment`.
Install into a virtual environment instead — this is about your OS, not this
package:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install sistemo
```

## Quickstart (< 10 lines)

First, get an API key from the dashboard (**Dashboard → API Keys**) and export it:

```bash
export SISTEMO_API_KEY=sk_live_xxxxxxxx
```

Then save this as `hello.py` — it is a Python file, not something to paste at a shell
prompt:

```python
# hello.py
from sistemo import Sandbox

with Sandbox() as sb:                       # reads SISTEMO_API_KEY
    result = sb.run("python3 -c 'print(2 + 2)'")
    print(result.stdout, result.exit_code)  # "4\n" 0
```

Then run it:

```bash
python3 hello.py
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
res = sb.run("echo hi && uname -a", timeout=30)            # -> ExecResult (max 24h)
res.stdout, res.stderr, res.exit_code, res.ok, res.truncated
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

## Long-running commands

`sb.run()` holds the HTTP connection open for the whole command and is capped at
**120 seconds**. Past that a command is not slow, it is impossible — no proxy or
load balancer between you and the machine will hold a request for a build.

`sb.start()` returns a handle instead:

```python
from sistemo import Sandbox

with Sandbox() as sb:
    job = sb.start("npm ci && npm run build", timeout=3600)

    for chunk in job.stream():        # output as it is produced
        print(chunk, end="")

    result = job.wait()
    print(result.state, result.exit_code)
```

The handle outlives the process that created it, so a crashed client can
reconnect with `sb.job(exec_id)` or list what is running with `sb.jobs()`.

### Three things worth knowing

**`exit_code` is `None` until it is known** — and `None` forever if the job is
`lost`. It is never `0` as a placeholder, because a zero would read as success.
Branch on `state`, or use `job.ok`.

**`lost` is not `failed`.** `failed` means the command ran and returned non-zero.
`lost` means the machine could not account for it — we can say neither that it
ran nor that it did not. Do not retry blindly and do not assume completion.

**A failed `start()` may still have started something.** If it raises
`ExecStartUnconfirmed`, the command may be running; the exception carries the
handle:

```python
from sistemo.errors import ExecStartUnconfirmed

try:
    job = sb.start("./deploy.sh")
except ExecStartUnconfirmed as e:
    job = sb.job(e.exec_id)     # find out what actually happened
    job.wait()
```

An `Idempotency-Key` is sent automatically, so an ordinary network retry returns
the *same* job rather than running your command twice.

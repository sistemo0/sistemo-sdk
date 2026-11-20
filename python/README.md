# sistemo (Python)

Run AI agents and untrusted code in **real isolated Firecracker microVMs**

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
| `SISTEMO_BASE_URL` | override the API URL (default `https://api.sistemo.io`). |

Or pass them explicitly: `Sandbox(api_key="sk_live_…", base_url="https://…")`.

## API

```python
sb = Sandbox(vcpus=1, memory_mb=1024, stack="base")  # provisions a microVM
res = sb.run("echo hi && uname -a")                        # -> ExecResult (default 120s, max 24h)
res.stdout, res.stderr, res.exit_code, res.ok, res.truncated, res.request_id, res.lost
sb.run("python3 app.py", env={"FOO": "bar"}, cwd="/tmp")   # env/cwd omitted unless set
sb.close()                                                 # destroy (or use `with`)
```

A **read** API key can list resources but cannot create sandboxes or run code — use a **full** key with the SDK.

## Errors

```python
from sistemo import (
    APIError,
    AuthError,
    InsufficientBalanceError,
    QuotaExceededError,
    TooManyExecsError,
)

try:
    with Sandbox() as sb:
        sb.run("...")
except QuotaExceededError as e:
    # An account limit was reached. `detail` names which one and how much is in
    # use. Retrying SUCCEEDS once you free that resource — stop or destroy a
    # machine, delete a volume — or once the limit is raised.
    print(e.detail)
except InsufficientBalanceError as e:
    # Prepaid wallet cannot cover this. Top up; freeing machines does not help.
    print(e.detail)
except TooManyExecsError:
    ...          # 409 — this machine already has 16 commands running; wait
except AuthError:
    ...          # 401/403 — key missing/invalid/revoked, or read-only key
except APIError as e:
    print(e.status, e.detail, e.code, e.request_id, e.retryable, e.doc_url)
```

Both `QuotaExceededError` and `AuthError` arrive as HTTP 403, so **catch
`QuotaExceededError` first** — it is a subclass of `APIError`, not of `AuthError`,
precisely because the two need opposite handling: an auth failure never succeeds
on retry, a quota refusal does once you free something. `GET /v1/quotas` reports
every limit alongside current usage. `InsufficientBalanceError` is HTTP 402
(top up); it is not a quota refusal and not an auth failure.

Every request sends `X-Request-Id`. Quote `e.request_id` when you contact support.
On `429`, the client honours `Retry-After` (capped at 60 s) from version 0.1.3.
`e.retryable` and `e.doc_url` come from the API body when it sent them (`None` if it did not).

Zero runtime dependencies (Python stdlib only). Apache-2.0.

## List machines and volumes

Lists are keyset-paginated. `list_volumes()` / `list_machines()` return a
`Page` (a list with `next_cursor`; `None` means the last page). Index it like a
list; walk every page with `iter_volumes()` / `iter_machines()`.

```python
from sistemo import Client

c = Client()
page = c.list_volumes()          # page[0]["id"], page.next_cursor
page = c.list_volumes(cursor=page.next_cursor, limit=50)
for vol in c.iter_volumes():
    print(vol["id"])
```

## Long-running commands

`sb.run()` starts a guest job and waits for it on your machine (default **120
seconds**, maximum 24 hours). Each poll is a short request, so a proxy never
holds a connection for the whole command. Pass a longer `timeout` for installs
and builds:

```python
with Sandbox() as sb:
    r = sb.run("pip install numpy", timeout=180)
```

`sb.start()` returns the handle without waiting, when you want to stream,
cancel, or reconnect:

```python
from sistemo import Sandbox

with Sandbox() as sb:
    job = sb.start("npm ci && npm run build", timeout=3600)

    for chunk in job.stream():        # output as it is produced
        print(chunk, end="")

    result = job.wait()
    print(result.state, result.exit_code)
```

The handle outlives the process that created it. Reconnect with
`Sandbox.connect(machine_id)` (does not provision a new microVM), then
`sb.job(exec_id)` or `sb.jobs()` / `sb.iter_jobs()`. `jobs()` returns a `Page`
(a list with `next_cursor`); the guest keeps at most 64, so the default page
holds them all.

```python
sb = Sandbox.connect(machine_id)   # GET /v1/machines/{id}
job = sb.job(exec_id)
print(job.wait().exit_code)
```

### Three things worth knowing

**`exit_code` is `None` until it is known** — and `None` forever if the job is
`lost`. It is never `0` as a placeholder, because a zero would read as success.
Branch on `state`, or use `job.ok`.

**`lost` is not `failed`.** `failed` means the command ran and returned non-zero.
`lost` means the machine could not account for it — we can say neither that it
ran nor that it did not. Do not retry blindly and do not assume completion.
If the machine stops while `run()` / `wait()` is polling, the result is
`lost=True` (not a raised 409). `start()` on a machine that is already
stopped raises `MachineNotRunningError`.

**A failed `start()` may still have started something.** If it raises
`ExecStartUnconfirmed`, the command may be running; the exception carries the
handle:

```python
from sistemo import ExecStartUnconfirmed

try:
    job = sb.start("./deploy.sh")
except ExecStartUnconfirmed as e:
    job = sb.job(e.exec_id)     # find out what actually happened
    job.wait()
```

You mint `exec_id` (a UUID is generated if you omit it). Re-sending the same id
is a 409 and returns the existing job, not a second run. If start raises
`ExecStartUnconfirmed`, poll that id — do not start again.

The 17th concurrent command is `TooManyExecsError` (`too_many_execs`): wait for
one to finish; a new id does not help. `POST /execs` is never retried — not on
HTTP 5xx/429, and not on a dropped connection. A dropped start is recovered by
GET of the minted id (404 is retried for a few seconds — the API can 404 while
still accepting the POST). If that GET also fails, `ExecStartUnconfirmed` carries it.

## Changelog

### 0.1.7

- GET of a minted `exec_id` after a dropped start retries 404 for a few seconds. The API can 404 for 0.2–0.5 s while still accepting the POST; a single GET treated that as "nothing started" while the command ran to completion.

### 0.1.6

- A dropped `start()` / `run()` is recovered by GET of the minted `exec_id`, never by re-POSTing. If GET also fails, `ExecStartUnconfirmed` still carries the id.
- After a command has already succeeded, unread output is empty stdout — not `lost=True`.

### 0.1.5

- `TooManyExecsError` for the 17th concurrent `run()` / `start()`.
- `ProcessLimitError`, `DiskFullError`, `OutOfMemoryError`, `MachineNotRunningError`.
- `run()` / `wait()` return `lost=True` when the machine stops mid-wait.
- `POST /execs` is never retried, including dropped connections.

### 0.1.4

- Abort kept through the response body (JS no longer hangs 300 s).
- GET retries after a dropped connection; raw stdlib errors wrapped.
- Non-object 2xx JSON is `SistemoError`.
- `retryable` / `doc_url` on `APIError`; `InsufficientBalanceError` for HTTP 402.
- Pagination helpers (`Page`, `iter_*`, JS `nextCursor`); `Sandbox.connect()`; `ExecResult.lost`.

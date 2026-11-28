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
on retry, a quota refusal does once you free something. `Client.get_quotas()`
is `GET /v1/quotas`: `tier`, `quotas` (used and limit per dimension), and
`per_machine`. `InsufficientBalanceError` is HTTP 402
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

## Power, wait, snapshots, images

`stop()` powers the machine off (`POST /v1/machines/{id}/stop`). `boot()`
powers it back on (`POST /v1/machines/{id}/start`). `boot()` is not
`start()`: `start(script)` runs a command inside the guest.

`connect()` is one GET and does not wait. `wait(timeout)` polls
`GET /v1/machines/{id}` until `running`. `timeout` is required (seconds).
A machine that is `stopped`, `stopping`, `error`, `destroyed`, or
`destroying` fails immediately — waiting will not start it; call `boot()`
for a stopped machine. Past the deadline the error is `MachineTimeout`.
Create already returns a running machine.

```python
from sistemo import Client, Sandbox

sb = Sandbox.connect(machine_id)
sb.stop()
sb.boot()
sb.wait(30)

snap = sb.create_snapshot("before")     # POST /v1/machines/{id}/snapshots
for s in sb.list_snapshots():            # Page, same next_cursor contract
    print(s["id"])
sb.restore_snapshot(snap["id"])          # POST /v1/snapshots/{id}/restore
sb.delete_snapshot(snap["id"])           # DELETE /v1/snapshots/{id}

for image in Client().list_images():     # GET /v1/images
    print(image["name"])                 # pass as stack= when creating
```

`root_volume_id` on the instance is the id you passed to `Sandbox(...)`,
or the one a later response named. The create response does not include it.

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
`Sandbox.connect(machine_id)` (one GET; it does not provision a new microVM
and it does not wait for `running` — call `sb.wait(timeout)` first if the
machine may still be starting), then
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
raises `APIError` 409 `exec_id_in_use` and nothing runs — look the first one up
with `job()`. If start raises
`ExecStartUnconfirmed`, poll that id — do not start again.

The 17th concurrent command is `TooManyExecsError` (`too_many_execs`): wait for
one to finish; a new id does not help. `POST /execs` is never retried — not on
HTTP 5xx/429, and not on a dropped connection. A dropped start is recovered by
GET of the minted id (404 is retried for a few seconds — the API can 404 while
still accepting the POST). If that GET also fails, `ExecStartUnconfirmed` carries it.

## Locked network and secrets

By default a machine has the open internet. `network="locked"` refuses every
outbound connection except HTTP and HTTPS (ports 80 and 443) to the hosts in
`allow`. An entry is an exact host (`pypi.org`) or one leading `*.`
(`*.pythonhosted.org`, which matches subdomains but not the name itself).
A locked machine with an empty `allow` has no public outbound at all.

```python
sb = Sandbox(network="locked", allow=["pypi.org", "*.pythonhosted.org"])
sb.run("pip install requests")                      # allowed
sb.run("curl -sS https://example.com")              # refused

sb.set_network("locked", ["api.example.com"])       # replaces the WHOLE list
sb.network, sb.allow                                # -> "locked", ["api.example.com"]

log = sb.egress(action="blocked", limit=50)         # newest first
for e in log["activity"]:
    print(e["at"], e["action"], e["host"])
log["complete"]   # False when rows may be missing; an empty list means
                  # "no outbound calls" only when this is True
```

A refused plain-HTTP call gets `403` with `{"code": "egress_domain_not_allowed"}`;
a refused HTTPS call has its connection closed. A running machine changes
policy in place; a stopped one gets it at its next start.

`set_network` is a full replace, so the SDK retries it on a dropped connection or a
5xx/429. If it still fails, `sb.network` / `sb.allow` keep the old policy even
though the host may already have the new one — send the same call again; it
converges. A machine created from a kept root disk of a locked machine must
say `network` explicitly (`network="locked"` or `"open"`) — the API refuses an implicit one.

**Secrets** are created and rotated in the dashboard; their values are
write-only and no API call ever returns one. The SDK binds them by name, and
each bound secret reaches the machine's commands as an environment variable of
the same name:

```python
sb = Sandbox(secrets=["API_TOKEN"])
sb.run('curl -sS -H "Authorization: Bearer $API_TOKEN" https://api.example.com/v1/orders')

sb.secrets.bind("STRIPE_KEY")       # -> ["API_TOKEN", "STRIPE_KEY"]
sb.secrets.unbind("API_TOKEN")      # -> ["STRIPE_KEY"]
list(sb.secrets)                    # names only

Client().list_secrets()             # names, custody, hosts, bound_machines — never a value
```

**Box or proxy.** A **box** secret's variable holds the value. A **proxy**
secret's variable holds a placeholder (`sist_ph_…`): the real value is added
only on HTTPS to the secret's hosts, in the place it was created for (a bearer
token, basic auth, a header or a query parameter), so code on the machine can
use it but never read it. A proxy secret needs a locked machine with its hosts
in `allow`. Do not set `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`
or `NODE_EXTRA_CA_CERTS` in a command's env there — the server sets them so
HTTPS clients trust the machine's own certificate authority. See
[Proxy secrets](https://docs.sistemo.io/cloud/proxy-secrets/).

⚠ A bound box secret is readable by anything that runs on that machine — keep the
machine's code and its network policy as tight as the secret is sensitive.
Binding changes the NEXT command; one already running keeps its environment.
At most 16 per machine. Passing `env=` with the same name as a bound secret is
a `400`, never a silent override. Every bind and unbind is in the Activity feed
(names only).

## Changelog

### 0.2.5

- `start()` with an `exec_id` that is already in use raises `APIError` 409 `exec_id_in_use`. It returned the earlier job instead, so the new command silently never ran.
- Retries honour `Retry-After` on a `503` as well as a `429`; the API sends it on every retryable `503`.
- `Sandbox(idempotency_key="")` raises `InvalidArgumentError`, as `Client` already did; it was replaced by a generated key.

### 0.2.4

- `Sandbox.close()` keeps trying until the machine is deleted — through rate limits (honouring `Retry-After`), dropped connections and a machine still mid-transition — for up to `timeout` seconds (default `CLOSE_TIMEOUT`, 120), whatever `max_retries` says. A machine already gone counts as closed. Tearing many machines down at once no longer leaves some running and billed.
- A `with Sandbox()` block that could not delete its machine now emits a `RuntimeWarning` naming it, instead of failing silently.
- `idempotency_key=""` is refused (it was silently dropped), and `allow="pypi.org"` raises `InvalidArgumentError` (still a `TypeError` too).

### 0.2.3

- `ExecResult.oom_killed` and `Job.oom_killed`: true when the machine ran out of memory and the kernel killed the command (usually exit code 137). The API has reported it; the SDK dropped it.
- An `idempotency_key` longer than 255 printable ASCII characters is refused before anything is sent (`InvalidArgumentError` (a `SistemoError` and a `ValueError`)). A long one was refused by proxies first — HTML at 8 KiB, and a CDN 520 past ~16 KiB that the client retried.
- Client-side refusals (a bad `network`, `allow` without `"locked"`, a bad key) raise `InvalidArgumentError`, which is also a `ValueError` as before — so they are catchable as `SistemoError`, like the JS SDK.

### 0.2.2

- A 429's `Retry-After` is spread over up to half as long again, so many callers refused at once (parallel creates) do not all retry in the same instant.
- `set_network` / create refuse a `network` other than `"open"` or `"locked"` with that message, before anything is sent.

### 0.2.1

- `set_network` is retried on a dropped connection or a 5xx/429 (a full replace is safe to repeat).
- A bare string for `allow` is a `TypeError` (it was split into characters), and `allow` without `network="locked"` is refused before anything is sent.
- Docs: box vs proxy secrets, and the CA variables not to override.

### 0.2.0

- Locked networks: `Sandbox(network="locked", allow=[...])`, `sb.set_network(network, allow)` and `sb.egress(action=, limit=)`. `sb.network` and `sb.allow` are read back from every machine response. A create without these arguments sends exactly what 0.1.x sent, so the open default is unchanged.
- Secrets by name: `Sandbox(secrets=[...])`, `sb.secrets.bind(...)` / `unbind(...)` / `names`, and `Client.list_secrets()`. Values are never returned.

### 0.1.8

- `Sandbox.stop()` powers a machine off (`POST /v1/machines/{id}/stop`). `Sandbox.boot()` powers it on (`POST /v1/machines/{id}/start`). `start()` still starts a command.
- `Sandbox.wait(timeout)` polls until `running`. The deadline is required. `connect()` does not wait. `stopped`, `stopping`, `error`, `destroyed`, and `destroying` fail immediately. `MachineTimeout` is only the deadline.
- Snapshot create, list, restore, and delete. Lists use `next_cursor`.
- `Client.list_images()` returns the catalog from `GET /v1/images`.
- `Client.get_quotas()` returns the body of `GET /v1/quotas`.

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

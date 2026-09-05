# Contributing

Both SDKs are deliberately small: a thin, **zero-dependency** wrapper over three
REST calls (`POST /v1/machines`, `POST /v1/machines/{id}/exec`,
`DELETE /v1/machines/{id}`), plus a few volume helpers. Nothing here should need
a dependency, and adding one is the change most likely to be turned down — the
value of an SDK you can read in one sitting is that you can also audit it.

## Layout

```
python/   sistemo/{_client,sandbox,errors,_version}.py   tests/test_sdk.py
js/       src/index.ts                                   test/sdk.test.mjs
```

`_client` / `Client` owns HTTP, auth, retries and error mapping. `Sandbox` is the
thing users touch. `errors` maps an HTTP status to a type.

## Running the tests

Both suites spin up a mock control plane on a loopback port — **no API key, no
network, no account needed**.

```bash
# Python (3.9+; no dependencies to install)
cd python && python3 -m unittest discover -s tests -v

# JS / TS (Node 20+)
cd js && npm ci && npm test          # builds dist/, then node --test
```

CI runs Python on 3.9 and 3.13, and Node on 20 and 24.

> ⚠ 3.9 is in the matrix on purpose. `socket.timeout` only became an alias of
> `TimeoutError` in Python 3.10, so a read timeout takes a genuinely different
> `except` branch on the floor this package advertises. Testing only on a modern
> interpreter would leave that path unexercised.

## Testing against a real server

Point the SDK anywhere with `SISTEMO_BASE_URL` — the managed cloud, or your own
self-hosted control plane:

```bash
export SISTEMO_API_KEY=sk_live_…
export SISTEMO_BASE_URL=https://api.sistemo.io   # or your own host

cd python
python3 -m venv .venv && source .venv/bin/activate   # PEP 668: distro Pythons refuse a bare install
pip install -e .
python -c "from sistemo import Sandbox; sb=Sandbox(); print(sb.run('uname -a').stdout); sb.close()"
```

⚠ This boots a **real microVM** and bills real usage. `sb.close()` (or the `with`
block) destroys it — a sandbox left running keeps costing money.

## Things that look removable but are not

- **The `User-Agent` header.** The managed API is behind a WAF that rejects
  generic library user-agents with a 403. Without a real UA the Python SDK is
  100% broken against production, and no local or mock test can reproduce it.
- **The version is single-sourced.** Python derives its UA from
  `sistemo/_version.py`; JS restates it in `src/index.ts`. A `version drift` test
  in each suite fails if either falls out of step with the package manifest — the
  UA carries the version, so a stale one is invisible until it 403s.
- **`QuotaExceededError` is not a subclass of `AuthError`.** Both arrive as HTTP
  403 and they need opposite handling: an auth failure never succeeds on retry, a
  quota refusal does once the caller frees the resource it names. Collapsing them
  sends people off to rotate a perfectly good API key. Each suite has a
  counter-guard asserting a *plain* 403 is still an `AuthError`.
- **`exec` is never retried on an HTTP error.** It carries no `Idempotency-Key`
  because replaying it would re-run the caller's script inside the guest. Create
  *is* retried, and reuses one key so a flaky network cannot double-boot a VM.
- **`py.typed`.** Without that marker (PEP 561) every type checker treats this
  fully annotated package as `Any`. CI asserts it is present in the built wheel.

## Pull requests

Please include a test. Both suites are plain stdlib (`unittest`, `node:test`), so
there is no framework to learn. If you are changing behaviour, the most useful
thing you can do is check the new test **fails without your change** — several
tests here exist because an earlier version passed while guarding nothing.

## Licence

Apache-2.0. By contributing you agree your contribution is licensed under it.

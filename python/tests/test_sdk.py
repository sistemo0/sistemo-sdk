"""Unit tests for the Python SDK against a local mock control plane.

No network / real API required. Run from sdk/python:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import pathlib
import threading
import unittest

from sistemo.errors import (
    APIError,
    AuthError,
    QuotaExceededError,
    api_error,
)
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from sistemo import (
    APIError,
    AuthError,
    Client,
    ExecResult,
    NotFoundError,
    RateLimitError,
    Sandbox,
    SistemoError,
)
import sistemo
from sistemo._client import USER_AGENT


class _MockAPI(BaseHTTPRequestHandler):
    """Minimal control plane: create → exec → delete + error routes."""

    # Shared mutable state for assertions
    log: list[dict[str, Any]] = []
    machines: dict[str, dict] = {}
    fail_next: dict[str, Any] | None = None  # {"status": int, "body": dict}
    # Queue of forced responses (status, body) — consumed FIFO before normal routes.
    fail_queue: list[dict[str, Any]] = []
    # Idempotency: same key on create returns the same machine id.
    idem_create: dict[str, str] = {}
    # Empty volumes list returns null (real API quirk).
    volumes_null: bool = False

    def log_message(self, format: str, *args) -> None:  # quiet
        return

    def _read_json(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return None
        return json.loads(self.rfile.read(n))

    def _send(self, status: int, body: Any = None) -> None:
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        if raw:
            self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        full_path = self.path  # includes ?query for preserve_storage asserts
        path = full_path.split("?", 1)[0]
        body = self._read_json() if method in ("POST", "PUT", "PATCH") else None
        auth = self.headers.get("Authorization", "")
        ua = self.headers.get("User-Agent", "")
        idem = self.headers.get("Idempotency-Key", "")
        _MockAPI.log.append(
            {
                "method": method,
                "path": path,
                "full_path": full_path,
                "body": body,
                "auth": auth,
                "ua": ua,
                "idem": idem,
            }
        )

        if _MockAPI.fail_queue:
            fail = _MockAPI.fail_queue.pop(0)
            self._send(fail["status"], fail.get("body", {"detail": "failed"}))
            return

        if _MockAPI.fail_next is not None:
            fail = _MockAPI.fail_next
            _MockAPI.fail_next = None
            self._send(fail["status"], fail.get("body", {"detail": "failed"}))
            return

        if not auth.startswith("Bearer "):
            self._send(401, {"detail": "missing key", "code": "unauthorized"})
            return

        if method == "POST" and path == "/v1/machines":
            if idem and idem in _MockAPI.idem_create:
                mid = _MockAPI.idem_create[idem]
                self._send(201, _MockAPI.machines[mid])
                return
            mid = "m-" + str(len(_MockAPI.machines) + 1)
            m = {"id": mid, "state": "running", **(body or {})}
            _MockAPI.machines[mid] = m
            if idem:
                _MockAPI.idem_create[idem] = mid
            self._send(201, m)
            return

        if method == "POST" and path.endswith("/exec"):
            mid = path.split("/")[3]
            if mid not in _MockAPI.machines:
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
            script = (body or {}).get("script", "")
            self._send(
                200,
                {
                    "exit_code": 0 if "fail" not in script else 1,
                    "stdout": f"out:{script}",
                    "stderr": "",
                },
            )
            return

        if method == "DELETE" and path.startswith("/v1/machines/"):
            mid = path.split("/")[3]
            _MockAPI.machines.pop(mid, None)
            self._send(200, {"status": "destroyed"})
            return

        if method == "GET" and path == "/v1/volumes":
            if _MockAPI.volumes_null:
                self._send(200, {"volumes": None})
            else:
                self._send(200, {"volumes": [{"id": "v1", "name": "disk", "size_gb": 10}]})
            return

        if method == "POST" and path == "/v1/volumes":
            self._send(201, {"id": "v-new", "name": body.get("name"), "size_gb": body.get("size_gb")})
            return

        self._send(404, {"detail": f"no mock for {method} {path}"})


def _start_server() -> tuple[HTTPServer, str]:
    server = HTTPServer(("127.0.0.1", 0), _MockAPI)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, f"http://127.0.0.1:{port}"


class SDKTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.base = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()

    def setUp(self) -> None:
        _MockAPI.log.clear()
        _MockAPI.machines.clear()
        _MockAPI.fail_next = None
        _MockAPI.fail_queue = []
        _MockAPI.idem_create.clear()
        _MockAPI.volumes_null = False
        # isolate env
        self._env = os.environ.copy()
        os.environ.pop("SISTEMO_API_KEY", None)
        os.environ.pop("SISTEMO_BASE_URL", None)

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._env)

    def test_client_requires_api_key(self) -> None:
        with self.assertRaises(SistemoError) as cm:
            Client(base_url=self.base)
        self.assertIn("API key", str(cm.exception))

    def test_client_env_key_and_base(self) -> None:
        os.environ["SISTEMO_API_KEY"] = "sk_live_env"
        os.environ["SISTEMO_BASE_URL"] = self.base + "/"
        c = Client()
        self.assertEqual(c.base_url, self.base)
        c.request("GET", "/v1/volumes")
        self.assertTrue(_MockAPI.log[-1]["auth"].endswith("sk_live_env"))
        self.assertEqual(_MockAPI.log[-1]["ua"], USER_AGENT)

    def test_sandbox_create_run_close(self) -> None:
        sb = Sandbox(api_key="sk_test", base_url=self.base, name="n1", stack="python")
        self.assertTrue(sb.id.startswith("m-"))
        self.assertEqual(sb.state, "running")
        # create payload
        create = next(x for x in _MockAPI.log if x["path"] == "/v1/machines")
        self.assertEqual(create["body"]["name"], "n1")
        self.assertEqual(create["body"]["vcpus"], 1)
        self.assertEqual(create["body"]["memory_mb"], 1024)
        self.assertEqual(create["body"]["rootfs_size_gb"], 10)
        self.assertEqual(create["body"]["stack"], "python")
        self.assertEqual(create["body"]["metadata"], {})
        # Idempotency-Key is always set on create
        self.assertTrue(create["idem"])
        self.assertGreaterEqual(len(create["idem"]), 8)

        r = sb.run("echo hi")
        self.assertIsInstance(r, ExecResult)
        self.assertEqual(r.exit_code, 0)
        self.assertTrue(r.ok)
        self.assertEqual(r.stdout, "out:echo hi")
        exec_log = next(x for x in _MockAPI.log if x["path"].endswith("/exec"))
        self.assertEqual(exec_log["body"]["timeout_sec"], 30)
        # exec must NOT send Idempotency-Key (replay would re-run the script)
        self.assertEqual(exec_log["idem"], "")

        sb.close()
        self.assertNotIn(sb.id, _MockAPI.machines)
        del_log = next(x for x in _MockAPI.log if x["method"] == "DELETE")
        self.assertIn(sb.id, del_log["path"])

    def test_context_manager_closes(self) -> None:
        with Sandbox(api_key="k", base_url=self.base) as sb:
            mid = sb.id
            self.assertIn(mid, _MockAPI.machines)
        self.assertNotIn(mid, _MockAPI.machines)

    def test_context_manager_swallows_close_error(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        mid = sb.id

        def boom(*_a, **_k):
            raise APIError(500, "gone")

        sb.close = boom  # type: ignore[method-assign]
        # __exit__ must not re-raise close failures
        sb.__exit__(None, None, None)
        # machine still "exists" on mock because close was stubbed
        self.assertIn(mid, _MockAPI.machines)

    def test_preserve_storage_query(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        sb.close(preserve_storage=True)
        del_log = next(x for x in _MockAPI.log if x["method"] == "DELETE")
        self.assertIn("preserve_storage=true", del_log["full_path"])

    def test_root_volume_id_in_create(self) -> None:
        Sandbox(api_key="k", base_url=self.base, root_volume_id="vol-abc")
        create = next(x for x in _MockAPI.log if x["path"] == "/v1/machines")
        self.assertEqual(create["body"]["root_volume_id"], "vol-abc")

    def test_auth_error(self) -> None:
        _MockAPI.fail_next = {"status": 401, "body": {"detail": "bad key", "code": "auth"}}
        with self.assertRaises(AuthError) as cm:
            Sandbox(api_key="bad", base_url=self.base)
        self.assertEqual(cm.exception.status, 401)
        self.assertIn("bad key", cm.exception.detail)

    def test_not_found_and_rate_limit(self) -> None:
        c = Client(api_key="k", base_url=self.base, max_retries=0)
        _MockAPI.fail_next = {"status": 404, "body": {"detail": "nope"}}
        with self.assertRaises(NotFoundError):
            c.request("GET", "/v1/missing")
        _MockAPI.fail_next = {"status": 429, "body": {"detail": "slow down"}}
        with self.assertRaises(RateLimitError):
            c.request("GET", "/v1/missing")

    def test_volumes_helpers(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        vols = c.list_volumes()
        self.assertEqual(vols[0]["id"], "v1")
        created = c.create_volume("data", 20)
        self.assertEqual(created["size_gb"], 20)
        # create_volume sends Idempotency-Key
        vol_create = next(x for x in _MockAPI.log if x["path"] == "/v1/volumes" and x["method"] == "POST")
        self.assertTrue(vol_create["idem"])

    def test_list_volumes_null_safe(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        _MockAPI.volumes_null = True
        self.assertEqual(c.list_volumes(), [])

    def test_connection_error(self) -> None:
        c = Client(api_key="k", base_url="http://127.0.0.1:1", timeout=0.3, max_retries=0)
        with self.assertRaises(SistemoError) as cm:
            c.request("GET", "/v1/volumes")
        self.assertIn("connection error", str(cm.exception))

    def test_exec_nonzero(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        r = sb.run("fail please")
        self.assertEqual(r.exit_code, 1)
        self.assertFalse(r.ok)

    def test_create_missing_id_raises(self) -> None:
        _MockAPI.fail_next = {"status": 201, "body": {"state": "running"}}
        with self.assertRaises(APIError) as cm:
            Sandbox(api_key="k", base_url=self.base)
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(cm.exception.code, "invalid_create_response")
        self.assertIn("missing id", cm.exception.detail)

    def test_create_sends_fixed_idempotency_key(self) -> None:
        sb = Sandbox(
            api_key="k",
            base_url=self.base,
            idempotency_key="fixed-key-abc",
        )
        create = next(x for x in _MockAPI.log if x["path"] == "/v1/machines")
        self.assertEqual(create["idem"], "fixed-key-abc")
        self.assertEqual(sb.id, "m-1")

    def test_retry_get_on_503_then_success(self) -> None:
        c = Client(api_key="k", base_url=self.base, max_retries=2, retry_backoff=0.01)
        _MockAPI.fail_queue = [
            {"status": 503, "body": {"detail": "busy"}},
            {"status": 503, "body": {"detail": "still busy"}},
        ]
        vols = c.list_volumes()
        self.assertEqual(vols[0]["id"], "v1")
        # 2 failures + 1 success
        self.assertEqual(sum(1 for x in _MockAPI.log if x["path"] == "/v1/volumes"), 3)

    def test_retry_post_only_with_idempotency_key(self) -> None:
        c = Client(api_key="k", base_url=self.base, max_retries=2, retry_backoff=0.01)
        # POST without key: 500 is NOT retried
        _MockAPI.fail_next = {"status": 500, "body": {"detail": "boom"}}
        with self.assertRaises(APIError) as cm:
            c.request("POST", "/v1/machines", {"name": "x"})
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(sum(1 for x in _MockAPI.log if x["path"] == "/v1/machines"), 1)

        _MockAPI.log.clear()
        # POST with key: 500 IS retried then succeeds
        _MockAPI.fail_queue = [{"status": 500, "body": {"detail": "boom"}}]
        out = c.request(
            "POST",
            "/v1/machines",
            {"name": "y"},
            idempotency_key="retry-me",
        )
        self.assertEqual(out["id"], "m-1")
        self.assertEqual(sum(1 for x in _MockAPI.log if x["path"] == "/v1/machines"), 2)
        self.assertEqual(_MockAPI.log[0]["idem"], "retry-me")
        self.assertEqual(_MockAPI.log[1]["idem"], "retry-me")

    def test_exec_http_error_not_retried(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.fail_queue = [
            {"status": 503, "body": {"detail": "agent down"}},
            {"status": 200, "body": {"exit_code": 0, "stdout": "should-not-run", "stderr": ""}},
        ]
        with self.assertRaises(APIError) as cm:
            sb.run("echo once")
        self.assertEqual(cm.exception.status, 503)
        # only one attempt — the queued success response was not consumed
        execs = [x for x in _MockAPI.log if x["path"].endswith("/exec")]
        self.assertEqual(len(execs), 1)

    def test_create_retry_reuses_same_idem_key(self) -> None:
        # First attempt 503, second succeeds — same key both times; mock returns one machine.
        _MockAPI.fail_queue = [{"status": 503, "body": {"detail": "busy"}}]
        c = Client(api_key="k", base_url=self.base, max_retries=2, retry_backoff=0.01)
        sb = Sandbox(client=c, idempotency_key="create-once")
        creates = [x for x in _MockAPI.log if x["path"] == "/v1/machines"]
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0]["idem"], "create-once")
        self.assertEqual(creates[1]["idem"], "create-once")
        self.assertEqual(len(_MockAPI.machines), 1)
        self.assertEqual(sb.id, "m-1")


if __name__ == "__main__":
    unittest.main()


class QuotaErrorMappingTest(unittest.TestCase):
    """A quota refusal must NOT surface as an auth failure.

    Both arrive as 403, but they need opposite handling: an AuthError means the
    credential cannot do this and retrying never helps, while a quota refusal
    means the credential is fine and a retry succeeds once a machine is stopped.
    Raising the wrong type sends a caller to rotate a perfectly good API key.
    """

    def test_quota_exceeded_is_its_own_type(self):
        err = api_error(403, "Machines quota reached: 1 of 1 in use.", "quota_exceeded")
        self.assertIsInstance(err, QuotaExceededError)
        self.assertNotIsInstance(err, AuthError)
        self.assertEqual(err.code, "quota_exceeded")

    def test_a_plain_403_is_still_an_auth_error(self):
        # The counter-guard: the obvious over-broad fix routes every 403 to the
        # new type, which hides genuine scope failures.
        err = api_error(403, "this API key is read-only", "forbidden")
        self.assertIsInstance(err, AuthError)
        self.assertNotIsInstance(err, QuotaExceededError)

    def test_both_remain_catchable_as_apierror(self):
        for code in ("quota_exceeded", "forbidden"):
            self.assertIsInstance(api_error(403, "x", code), APIError)


class VersionDriftTest(unittest.TestCase):
    """The version is stated in three places and all three must agree.

    ⚠ The User-Agent is not cosmetic: the production API sits behind Cloudflare,
    whose WAF rejects generic library UAs with a 403, so the UA string is what
    makes the SDK work at all. It carries the version, and before ``_version.py``
    it was an independent literal — while the release job only ever compared the
    git tag to ``pyproject.toml``. A release could therefore ship a package
    versioned 0.2.0 introducing itself to the API as ``sistemo-python/0.1.0``.

    ``_version`` collapses the constant and the UA into one definition; this test
    pins ``pyproject.toml``, which cannot import it.
    """

    def _pyproject_version(self) -> str:
        import re

        root = pathlib.Path(__file__).resolve().parent.parent
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        self.assertIsNotNone(m, "no version= line in pyproject.toml")
        return m.group(1)

    def test_version_matches_pyproject(self) -> None:
        self.assertEqual(
            sistemo.__version__,
            self._pyproject_version(),
            "sistemo.__version__ and pyproject.toml disagree — bump both, or the "
            "published package reports a version nobody can trace to a commit",
        )

    def test_user_agent_carries_the_version(self) -> None:
        from sistemo import _client

        self.assertEqual(
            _client.USER_AGENT,
            f"sistemo-python/{sistemo.__version__}",
            "the User-Agent must be derived from __version__, never restated — "
            "the WAF makes this string load-bearing and a stale one is invisible",
        )

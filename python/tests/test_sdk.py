"""Unit tests for the Python SDK against a local mock control plane.

No network / real API required. Run from sdk/python:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import threading
import unittest

from sistemo.errors import (
    APIError,
    AuthError,
    InsufficientBalanceError,
    QuotaExceededError,
    api_error,
)
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

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
    # Two-page list: first page next_cursor=p2, second page is the last.
    paginate: bool = False
    # Close without a byte of HTTP this many times (RemoteDisconnected).
    drop_count: int = 0
    jobs: dict[str, dict] = {}

    def log_message(self, format: str, *args) -> None:  # quiet
        return

    def _read_json(self) -> Any:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return None
        return json.loads(self.rfile.read(n))

    def _send(self, status: int, body: Any = None, extra_headers: dict | None = None) -> None:
        if isinstance(body, dict):
            body = dict(body)
            rid = self.headers.get("X-Request-Id")
            if rid and "request_id" not in body:
                body["request_id"] = rid
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, str(v))
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
                "req_id": self.headers.get("X-Request-Id", ""),
            }
        )

        if _MockAPI.drop_count > 0:
            _MockAPI.drop_count -= 1
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return

        if _MockAPI.fail_queue:
            fail = _MockAPI.fail_queue.pop(0)
            self._send(
                fail["status"],
                fail.get("body", {"detail": "failed"}),
                fail.get("headers"),
            )
            return

        if _MockAPI.fail_next is not None:
            fail = _MockAPI.fail_next
            _MockAPI.fail_next = None
            self._send(
                fail["status"],
                fail.get("body", {"detail": "failed"}),
                fail.get("headers"),
            )
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

        if method == "POST" and path.endswith("/execs"):
            mid = path.split("/")[3]
            if mid not in _MockAPI.machines:
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
            script = (body or {}).get("script", "")
            eid = (body or {}).get("exec_id") or "e-1"
            rec = {
                "exec_id": eid,
                "machine_id": mid,
                "state": "succeeded",
                "exit_code": 0 if "fail" not in script else 1,
                "truncated": False,
            }
            _MockAPI.jobs[eid] = {"rec": rec, "stdout": f"out:{script}".encode()}
            self._send(202, rec)
            return

        if method == "GET" and path.endswith("/execs"):
            mid = path.split("/")[3]
            cursor = (parse_qs(urlparse(full_path).query).get("cursor") or [None])[0]
            if _MockAPI.paginate:
                if cursor == "p2":
                    self._send(
                        200,
                        {
                            "execs": [
                                {
                                    "exec_id": "e2",
                                    "machine_id": mid,
                                    "state": "succeeded",
                                    "exit_code": 0,
                                }
                            ],
                            "next_cursor": None,
                        },
                    )
                else:
                    self._send(
                        200,
                        {
                            "execs": [
                                {
                                    "exec_id": "e1",
                                    "machine_id": mid,
                                    "state": "succeeded",
                                    "exit_code": 0,
                                }
                            ],
                            "next_cursor": "p2",
                        },
                    )
                return
            items = [
                j["rec"]
                for j in _MockAPI.jobs.values()
                if j["rec"].get("machine_id") == mid
            ]
            self._send(200, {"execs": items, "next_cursor": None})
            return

        if method == "GET" and "/execs/" in path and path.endswith("/output"):
            eid = path.split("/")[5]
            job = _MockAPI.jobs.get(eid, {})
            q = parse_qs(urlparse(full_path).query)
            offset = int(q.get("offset", ["0"])[0])
            data = job.get("stdout", b"") if q.get("stream", ["stdout"])[0] != "stderr" else b""
            if offset > 0:
                data = b""
            self._send(
                200,
                {
                    "stream": "stdout",
                    "offset": offset,
                    "next_offset": offset + len(data),
                    "data": base64.b64encode(data).decode() if data else "",
                    "eof": True,
                },
            )
            return

        if method == "GET" and "/execs/" in path:
            eid = path.split("/")[5]
            job = _MockAPI.jobs.get(eid)
            if not job:
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
            self._send(200, job["rec"])
            return

        if method == "DELETE" and path.startswith("/v1/machines/"):
            mid = path.split("/")[3]
            _MockAPI.machines.pop(mid, None)
            self._send(200, {"status": "destroyed"})
            return

        if method == "GET" and path == "/v1/volumes":
            cursor = (parse_qs(urlparse(full_path).query).get("cursor") or [None])[0]
            if _MockAPI.paginate:
                if cursor == "p2":
                    self._send(
                        200,
                        {
                            "volumes": [{"id": "v2", "name": "disk2", "size_gb": 10}],
                            "next_cursor": None,
                        },
                    )
                else:
                    self._send(
                        200,
                        {
                            "volumes": [{"id": "v1", "name": "disk", "size_gb": 10}],
                            "next_cursor": "p2",
                        },
                    )
                return
            if _MockAPI.volumes_null:
                self._send(200, {"volumes": None, "next_cursor": None})
            else:
                self._send(
                    200,
                    {
                        "volumes": [{"id": "v1", "name": "disk", "size_gb": 10}],
                        "next_cursor": None,
                    },
                )
            return

        if method == "GET" and path == "/v1/machines":
            cursor = (parse_qs(urlparse(full_path).query).get("cursor") or [None])[0]
            if _MockAPI.paginate:
                if cursor == "p2":
                    self._send(
                        200,
                        {"machines": [{"id": "m2", "state": "running"}], "next_cursor": None},
                    )
                else:
                    self._send(
                        200,
                        {"machines": [{"id": "m1", "state": "running"}], "next_cursor": "p2"},
                    )
                return
            items = list(_MockAPI.machines.values())
            self._send(200, {"machines": items, "next_cursor": None})
            return

        if method == "GET" and path.startswith("/v1/machines/") and "/execs" not in path:
            mid = path.split("/")[3]
            m = _MockAPI.machines.get(mid)
            if not m:
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
            self._send(200, m)
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
        _MockAPI.paginate = False
        _MockAPI.drop_count = 0
        _MockAPI.jobs.clear()
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
        exec_log = next(x for x in _MockAPI.log if x["path"].endswith("/execs"))
        self.assertEqual(exec_log["body"]["timeout_sec"], 120)
        self.assertTrue(exec_log["body"].get("exec_id"))
        # start must NOT send Idempotency-Key (replay would re-run the script)
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
        execs = [x for x in _MockAPI.log if x["path"].endswith("/execs")]
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

    def test_run_sends_env_and_cwd(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        sb.run("echo hi", env={"FOO": "bar"}, cwd="/tmp")
        execs = [x for x in _MockAPI.log if x["path"].endswith("/execs") and x["method"] == "POST"]
        self.assertEqual(len(execs), 1)
        self.assertEqual(execs[0]["body"]["env"], {"FOO": "bar"})
        self.assertEqual(execs[0]["body"]["cwd"], "/tmp")

    def test_run_omits_env_when_unset(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        sb.run("echo hi")
        execs = [x for x in _MockAPI.log if x["path"].endswith("/execs") and x["method"] == "POST"]
        self.assertEqual(len(execs), 1)
        self.assertNotIn("env", execs[0]["body"])
        self.assertNotIn("cwd", execs[0]["body"])

    def test_every_request_sends_request_id(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        c.request("GET", "/v1/volumes")
        self.assertTrue(_MockAPI.log[-1]["req_id"])
        self.assertEqual(c.last_request_id, _MockAPI.log[-1]["req_id"])

    def test_error_carries_request_id(self) -> None:
        c = Client(api_key="k", base_url=self.base, max_retries=0)
        _MockAPI.fail_next = {"status": 500, "body": {"detail": "boom"}}
        with self.assertRaises(APIError) as cm:
            c.request("GET", "/v1/volumes")
        self.assertTrue(cm.exception.request_id)
        self.assertEqual(cm.exception.request_id, c.last_request_id)

    def test_retries_reuse_the_same_request_id(self) -> None:
        c = Client(api_key="k", base_url=self.base, max_retries=2, retry_backoff=0.01)
        _MockAPI.fail_queue = [
            {"status": 503, "body": {"detail": "busy"}},
            {"status": 503, "body": {"detail": "still busy"}},
        ]
        c.list_volumes()
        ids = [x["req_id"] for x in _MockAPI.log if x["path"] == "/v1/volumes"]
        self.assertEqual(len(ids), 3)
        self.assertEqual(len(set(ids)), 1)

    def test_close_retries_429(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.fail_queue = [
            {
                "status": 429,
                "body": {"detail": "slow", "code": "rate_limited"},
                "headers": {"Retry-After": "0"},
            }
        ]
        sb.close()
        deletes = [
            x for x in _MockAPI.log if x["method"] == "DELETE" and x["path"].startswith("/v1/machines/")
        ]
        self.assertEqual(len(deletes), 2)

    def test_retry_delay_honours_retry_after(self) -> None:
        from sistemo._client import retry_delay
        from sistemo.errors import RateLimitError

        err = RateLimitError(429, "slow", "rate_limited", retry_after=12)
        d = retry_delay(0, 0.01, 8.0, err, rng=lambda: 0.5)
        self.assertGreaterEqual(d, 12)
        self.assertLessEqual(d, 60)

    def test_retry_after_cap_is_60(self) -> None:
        from sistemo._client import parse_retry_after

        self.assertEqual(parse_retry_after("999"), 60)
        self.assertEqual(parse_retry_after("12"), 12)
        self.assertIsNone(parse_retry_after("nope"))
        self.assertEqual(parse_retry_after("-1"), None)

    def test_error_lifts_retryable_and_doc_url(self) -> None:
        c = Client(api_key="k", base_url=self.base, max_retries=0)
        _MockAPI.fail_next = {
            "status": 409,
            "body": {
                "code": "too_many_execs",
                "detail": "wait",
                "retryable": True,
                "doc_url": "https://docs.sistemo.io/errors/too_many_execs",
            },
        }
        with self.assertRaises(APIError) as cm:
            c.request("GET", "/v1/volumes")
        self.assertIs(cm.exception.retryable, True)
        self.assertEqual(
            cm.exception.doc_url, "https://docs.sistemo.io/errors/too_many_execs"
        )

    def test_error_retryable_absent_is_none(self) -> None:
        # Counter-guard: a missing field is not False. The API did not say.
        c = Client(api_key="k", base_url=self.base, max_retries=0)
        _MockAPI.fail_next = {"status": 500, "body": {"detail": "boom"}}
        with self.assertRaises(APIError) as cm:
            c.request("GET", "/v1/volumes")
        self.assertIsNone(cm.exception.retryable)
        self.assertIsNone(cm.exception.doc_url)

    def test_error_retryable_false_stays_false(self) -> None:
        err = APIError(400, "bad", "validation_error", {"retryable": False})
        self.assertIs(err.retryable, False)

    def test_list_volumes_page_keeps_next_cursor(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        _MockAPI.paginate = True
        page = c.list_volumes()
        self.assertEqual(page[0]["id"], "v1")
        self.assertEqual(page.next_cursor, "p2")
        page2 = c.list_volumes(cursor=page.next_cursor, limit=1)
        self.assertEqual(page2[0]["id"], "v2")
        self.assertIsNone(page2.next_cursor)
        last = _MockAPI.log[-1]
        self.assertIn("cursor=p2", last["full_path"])
        self.assertIn("limit=1", last["full_path"])

    def test_iter_volumes_walks_pages(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        _MockAPI.paginate = True
        self.assertEqual([v["id"] for v in c.iter_volumes()], ["v1", "v2"])

    def test_list_machines_and_iter(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        _MockAPI.paginate = True
        page = c.list_machines()
        self.assertEqual(page[0]["id"], "m1")
        self.assertEqual(page.next_cursor, "p2")
        self.assertEqual([m["id"] for m in c.iter_machines()], ["m1", "m2"])

    def test_remote_disconnected_is_sistemo_error_and_get_retries(self) -> None:
        c = Client(api_key="k", base_url=self.base, max_retries=2, retry_backoff=0.01)
        _MockAPI.drop_count = 1
        vols = c.list_volumes()
        self.assertEqual(vols[0]["id"], "v1")
        self.assertEqual(sum(1 for x in _MockAPI.log if x["path"] == "/v1/volumes"), 2)

    def test_jobs_page_keeps_next_cursor(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.paginate = True
        page = sb.jobs(limit=1)
        self.assertEqual(page[0].id, "e1")
        self.assertEqual(page.next_cursor, "p2")
        page2 = sb.jobs(limit=1, cursor=page.next_cursor)
        self.assertEqual(page2[0].id, "e2")
        self.assertIsNone(page2.next_cursor)
        last = _MockAPI.log[-1]
        self.assertIn("cursor=p2", last["full_path"])
        self.assertIn("limit=1", last["full_path"])

    def test_iter_jobs_walks_pages(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.paginate = True
        self.assertEqual([j.id for j in sb.iter_jobs(limit=1)], ["e1", "e2"])

    def test_connect_does_not_create(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        mid = sb.id
        posts = sum(
            1
            for x in _MockAPI.log
            if x["method"] == "POST" and x["path"] == "/v1/machines"
        )
        sb2 = Sandbox.connect(mid, api_key="k", base_url=self.base)
        self.assertEqual(sb2.id, mid)
        self.assertEqual(sb2.state, "running")
        posts2 = sum(
            1
            for x in _MockAPI.log
            if x["method"] == "POST" and x["path"] == "/v1/machines"
        )
        self.assertEqual(posts2, posts)
        self.assertTrue(
            any(
                x["method"] == "GET" and x["path"] == f"/v1/machines/{mid}"
                for x in _MockAPI.log
            )
        )
        self.assertTrue(sb2.run("echo hi").ok)

    def test_connect_missing_is_not_found(self) -> None:
        with self.assertRaises(NotFoundError):
            Sandbox.connect("nope", api_key="k", base_url=self.base)


class IncompleteBodyTest(unittest.TestCase):
    """urlopen can succeed (headers) and then resp.read() raise IncompleteRead."""

    def test_truncated_body_is_a_connection_error(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"volumes":')
                self.wfile.flush()
                self.close_connection = True

        srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        c = Client(
            api_key="k",
            base_url=f"http://127.0.0.1:{srv.server_port}",
            max_retries=0,
        )
        with self.assertRaises(SistemoError) as cm:
            c.list_volumes()
        self.assertIn("connection error", str(cm.exception))
        self.assertNotIsInstance(cm.exception, APIError)


class InvalidJSONTest(unittest.TestCase):
    """A 200 whose body is not a JSON object must be SistemoError, not a stdlib leak."""

    def _client(self, handler) -> Client:
        srv = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        return Client(
            api_key="k",
            base_url=f"http://127.0.0.1:{srv.server_port}",
            max_retries=0,
        )

    def test_html_200_is_sistemo_error(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_GET(self):
                raw = b"<html>nope</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        c = self._client(Handler)
        with self.assertRaises(SistemoError) as cm:
            c.list_volumes()
        self.assertIn("invalid JSON", str(cm.exception))
        self.assertNotIsInstance(cm.exception, APIError)

    def test_json_null_200_is_sistemo_error(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_GET(self):
                raw = b"null"
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        c = self._client(Handler)
        with self.assertRaises(SistemoError) as cm:
            c.list_volumes()
        self.assertIn("expected an object", str(cm.exception))

    def test_truncated_5xx_body_keeps_the_status(self) -> None:
        # e.read() IncompleteRead used to leak. We still know the status, so
        # this stays an APIError 500 (GET retries) rather than a connection error.
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a):
                return

            def do_GET(self):
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b'{"detail":')
                self.wfile.flush()
                self.close_connection = True

        c = self._client(Handler)
        with self.assertRaises(APIError) as cm:
            c.list_volumes()
        self.assertEqual(cm.exception.status, 500)


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

    def test_insufficient_balance_is_its_own_type(self):
        err = api_error(402, "balance is 0", "insufficient_balance")
        self.assertIsInstance(err, InsufficientBalanceError)
        self.assertNotIsInstance(err, AuthError)
        self.assertNotIsInstance(err, QuotaExceededError)
        self.assertIsInstance(err, APIError)

    def test_a_409_is_not_insufficient_balance(self):
        # Counter-guard: 402 used to be 409 conflict. Branching on "any 409"
        # would catch a reused exec_id as "please top up".
        err = api_error(409, "exec_id already in use", "exec_id_in_use")
        self.assertNotIsInstance(err, InsufficientBalanceError)
        self.assertIsInstance(err, APIError)


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

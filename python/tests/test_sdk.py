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
    DiskFullError,
    InsufficientBalanceError,
    MachineNotRunningError,
    OutOfMemoryError,
    ProcessLimitError,
    QuotaExceededError,
    TooManyExecsError,
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
    ExecStartUnconfirmed,
    MachineNotRunningError,
    MachineTimeout,
    NotFoundError,
    Page,
    RateLimitError,
    Sandbox,
    SistemoError,
    TooManyExecsError,
)
import sistemo
import sistemo.sandbox as sandbox_mod
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
    # When dropping a POST /execs, still store the job (response lost, guest accepted).
    drop_store_exec: bool = False
    # First N GET /execs/{id} answer 404 even if the job is stored (POST still
    # in flight). The SDK must retry those, not treat them as "nothing started".
    exec_lookup_404s: int = 0
    jobs: dict[str, dict] = {}
    # Sticky GET failure for /execs/{id} and /output (machine stopped mid-wait).
    job_gone: dict[str, Any] | None = None
    # Sticky GET failure for /output only (command already succeeded).
    output_gone: dict[str, Any] | None = None
    snapshots: list[dict] = []
    # GET of a not-yet-running machine flips it to running after this many reads.
    promote_after: int = 0
    machine_gets: int = 0

    @staticmethod
    def _accept_exec(path: str, body: Any) -> dict:
        mid = path.split("/")[3]
        script = (body or {}).get("script", "")
        eid = (body or {}).get("exec_id") or "e-1"
        rec = {
            "exec_id": eid,
            "machine_id": mid,
            "state": "succeeded",
            "exit_code": 0 if "fail" not in script else 1,
            "truncated": False,
        }
        if _MockAPI.job_gone is not None:
            rec["state"] = "running"
            rec["exit_code"] = None
        _MockAPI.jobs[eid] = {"rec": rec, "stdout": f"out:{script}".encode()}
        return rec

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
            if (
                _MockAPI.drop_store_exec
                and method == "POST"
                and path.endswith("/execs")
            ):
                _MockAPI._accept_exec(path, body)
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

        if (
            _MockAPI.job_gone is not None
            and method == "GET"
            and "/execs/" in path
        ):
            fail = _MockAPI.job_gone
            self._send(fail["status"], fail.get("body", {"detail": "gone"}))
            return

        if (
            _MockAPI.output_gone is not None
            and method == "GET"
            and path.endswith("/output")
        ):
            fail = _MockAPI.output_gone
            self._send(fail["status"], fail.get("body", {"detail": "gone"}))
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
            # Production create does not echo root_volume_id.
            m.pop("root_volume_id", None)
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
            rec = _MockAPI._accept_exec(path, body)
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
            if _MockAPI.exec_lookup_404s > 0:
                _MockAPI.exec_lookup_404s -= 1
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
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

        parts = path.strip("/").split("/")

        if method == "GET" and path == "/v1/quotas":
            self._send(
                200,
                {
                    "tier": "free",
                    "quotas": [
                        {
                            "name": "machines",
                            "used": 1,
                            "limit": 3,
                            "description": "Machines that exist.",
                        }
                    ],
                    "per_machine": {"vcpus": 2, "memory_mb": 2048},
                    "future_field": "kept",
                },
            )
            return

        if method == "GET" and path == "/v1/images":
            self._send(
                200,
                {
                    "images": [
                        {
                            "name": "base",
                            "template_id": "base_2026",
                            "is_default": True,
                            "status": "ready",
                            "revision": "2026",
                            "future": 1,
                        }
                    ]
                },
            )
            return

        if method == "GET" and path == "/v1/snapshots":
            cursor = (parse_qs(urlparse(full_path).query).get("cursor") or [None])[0]
            if _MockAPI.paginate:
                if cursor == "p2":
                    self._send(
                        200,
                        {"snapshots": [{"id": "s2", "name": "later", "state": "ready"}], "next_cursor": None},
                    )
                else:
                    self._send(
                        200,
                        {
                            "snapshots": [{"id": "s1", "name": "before", "state": "ready"}],
                            "next_cursor": "p2",
                        },
                    )
                return
            self._send(200, {"snapshots": list(_MockAPI.snapshots), "next_cursor": None})
            return

        if method == "POST" and len(parts) == 4 and parts[0] == "v1" and parts[1] == "snapshots" and parts[3] == "restore":
            self._send(200, {"id": parts[2], "state": "ready", "restored": True})
            return

        if method == "DELETE" and len(parts) == 3 and parts[0] == "v1" and parts[1] == "snapshots":
            self._send(200, {"status": "deleted"})
            return

        if method == "POST" and len(parts) == 4 and parts[0] == "v1" and parts[1] == "machines" and parts[3] in ("stop", "start"):
            m = _MockAPI.machines.get(parts[2])
            if not m:
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
            m["state"] = "stopped" if parts[3] == "stop" else "running"
            self._send(200, m)
            return

        if method == "POST" and len(parts) == 4 and parts[0] == "v1" and parts[1] == "machines" and parts[3] == "snapshots":
            snap = {
                "id": "snap-" + str(len(_MockAPI.snapshots) + 1),
                "name": (body or {}).get("name"),
                "state": "ready",
                "machine_id": parts[2],
                "extra": "kept",
            }
            _MockAPI.snapshots.append(snap)
            self._send(201, snap)
            return

        if method == "GET" and len(parts) == 4 and parts[0] == "v1" and parts[1] == "machines" and parts[3] == "snapshots":
            cursor = (parse_qs(urlparse(full_path).query).get("cursor") or [None])[0]
            mine = [s for s in _MockAPI.snapshots if s.get("machine_id") == parts[2]]
            if _MockAPI.paginate and cursor != "p2":
                self._send(200, {"snapshots": mine[:1], "next_cursor": "p2"})
                return
            page = mine[1:] if cursor == "p2" else mine
            self._send(200, {"snapshots": page, "next_cursor": None})
            return

        if method == "GET" and path.startswith("/v1/machines/") and "/execs" not in path and "/egress/" not in path:
            mid = path.split("/")[3]
            if len(parts) != 3:
                self._send(404, {"detail": f"no mock for {method} {path}"})
                return
            m = _MockAPI.machines.get(mid)
            if not m:
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
            if m.get("state") in ("pending", "provisioning", "starting"):
                _MockAPI.machine_gets += 1
                if _MockAPI.promote_after and _MockAPI.machine_gets >= _MockAPI.promote_after:
                    m["state"] = "running"
            self._send(200, m)
            return

        if method == "POST" and path == "/v1/volumes":
            self._send(201, {"id": "v-new", "name": body.get("name"), "size_gb": body.get("size_gb")})
            return

        # Locked network + secrets (0.2.0).
        if method == "POST" and path.endswith("/network"):
            mid = path.split("/")[3]
            m = _MockAPI.machines.get(mid)
            if m is None:
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
            m["network"], m["allow"] = (body or {}).get("network"), (body or {}).get("allow")
            self._send(200, m)
            return
        if method == "POST" and path.endswith("/secrets") and path.startswith("/v1/machines/"):
            mid = path.split("/")[3]
            m = _MockAPI.machines.get(mid)
            if m is None:
                self._send(404, {"detail": "not found", "code": "not_found"})
                return
            names = [n for n in (m.get("secrets") or []) if n not in (body or {}).get("unbind", [])]
            for n in (body or {}).get("bind", []):
                if n == "NOPE":
                    self._send(400, {"detail": "no secret named NOPE", "code": "validation_error"})
                    return
                if n not in names:
                    names.append(n)
            m["secrets"] = sorted(names)
            self._send(200, m)
            return
        if method == "GET" and path.endswith("/egress/activity"):
            self._send(200, {"activity": [{"host": "evil.example", "action": "blocked", "at": "2026-11-21T12:00:00Z"}], "complete": True})
            return
        if method == "GET" and path == "/v1/secrets":
            self._send(200, {"secrets": [{"name": "STRIPE_KEY", "custody": "box", "hosts": [], "bound_machines": 0}]})
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
        _MockAPI.drop_store_exec = False
        _MockAPI.exec_lookup_404s = 0
        _MockAPI.jobs.clear()
        _MockAPI.job_gone = None
        _MockAPI.output_gone = None
        _MockAPI.snapshots = []
        _MockAPI.promote_after = 0
        _MockAPI.machine_gets = 0
        # Production waits a few seconds on GET 404 after a dropped POST. Zero
        # here so a genuine miss (nothing started) stays a fast test.
        sandbox_mod._START_LOOKUP_SECONDS = 0.0
        sandbox_mod._START_LOOKUP_INTERVAL = 0.0
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

    def test_retry_after_is_spread_so_refused_callers_do_not_return_together(self) -> None:
        from sistemo._client import retry_delay
        from sistemo.errors import RateLimitError

        err = RateLimitError(429, "slow", "rate_limited", retry_after=2)
        lo = retry_delay(0, 0.01, 8.0, err, rng=lambda: 0.0)
        hi = retry_delay(0, 0.01, 8.0, err, rng=lambda: 0.999)
        self.assertGreaterEqual(lo, 2)
        self.assertGreater(hi - lo, 0.9, "callers told the same Retry-After must not all wait the same time")
        self.assertLessEqual(hi, 3.0)

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
        with self.assertRaises(TooManyExecsError) as cm:
            c.request("GET", "/v1/volumes")
        self.assertIsInstance(cm.exception, APIError)
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

    def test_exec_connection_drop_is_not_retried(self) -> None:
        """POST /execs must not replay a dropped connection (no idempotency)."""
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.log.clear()
        _MockAPI.drop_count = 2
        with self.assertRaises(SistemoError) as cm:
            sb.run("echo once")
        self.assertIn("connection error", str(cm.exception))
        execs = [
            x
            for x in _MockAPI.log
            if x["method"] == "POST" and x["path"].endswith("/execs")
        ]
        self.assertEqual(len(execs), 1)

    def test_run_recovers_dropped_start_by_exec_id(self) -> None:
        """The guest accepted the POST; the ACK was lost. GET that id, never re-POST."""
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.log.clear()
        _MockAPI.drop_store_exec = True
        _MockAPI.drop_count = 1
        r = sb.run("echo recovered")
        self.assertTrue(r.ok)
        self.assertIn("echo recovered", r.stdout)
        self.assertFalse(r.lost)
        posts = [
            x
            for x in _MockAPI.log
            if x["method"] == "POST" and x["path"].endswith("/execs")
        ]
        self.assertEqual(len(posts), 1)
        self.assertTrue(
            any(
                x["method"] == "GET" and "/execs/" in x["path"]
                for x in _MockAPI.log
            ),
            "a dropped start must GET the minted exec_id",
        )

    def test_run_recovers_dropped_start_after_lookup_404s(self) -> None:
        """The API 404s for a beat while still accepting the POST. GET again, never re-POST."""
        sandbox_mod._START_LOOKUP_SECONDS = 2.0
        sandbox_mod._START_LOOKUP_INTERVAL = 0.0
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.log.clear()
        _MockAPI.drop_store_exec = True
        _MockAPI.drop_count = 1
        _MockAPI.exec_lookup_404s = 2
        r = sb.run("echo recovered")
        self.assertTrue(r.ok)
        self.assertIn("echo recovered", r.stdout)
        self.assertFalse(r.lost)
        posts = [
            x
            for x in _MockAPI.log
            if x["method"] == "POST" and x["path"].endswith("/execs")
        ]
        self.assertEqual(len(posts), 1)
        job_gets = [
            x
            for x in _MockAPI.log
            if x["method"] == "GET"
            and "/execs/" in x["path"]
            and not x["path"].endswith("/output")
        ]
        self.assertGreaterEqual(
            len(job_gets),
            3,
            "two 404s then a hit — a single GET would have reported nothing started",
        )

    def test_dropped_start_lookup_404s_for_the_whole_window_is_original(self) -> None:
        """After the window, a 404 still means nothing started — and we still do not re-POST."""
        sandbox_mod._START_LOOKUP_SECONDS = 0.05
        sandbox_mod._START_LOOKUP_INTERVAL = 0.0
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.log.clear()
        _MockAPI.drop_count = 1
        with self.assertRaises(SistemoError) as cm:
            sb.run("echo once")
        self.assertNotIsInstance(cm.exception, ExecStartUnconfirmed)
        self.assertIn("connection error", str(cm.exception))
        posts = [
            x
            for x in _MockAPI.log
            if x["method"] == "POST" and x["path"].endswith("/execs")
        ]
        self.assertEqual(len(posts), 1)
        job_gets = [
            x
            for x in _MockAPI.log
            if x["method"] == "GET"
            and "/execs/" in x["path"]
            and not x["path"].endswith("/output")
        ]
        self.assertGreaterEqual(
            len(job_gets),
            2,
            "the window must GET more than once before giving up",
        )

    def test_dropped_start_raises_unconfirmed_when_get_also_fails(self) -> None:
        """If GET cannot tell either, the error still carries exec_id."""
        sb = Sandbox(
            client=Client(api_key="k", base_url=self.base, max_retries=0)
        )
        _MockAPI.log.clear()
        _MockAPI.drop_store_exec = True
        _MockAPI.drop_count = 2
        with self.assertRaises(ExecStartUnconfirmed) as cm:
            sb.start("echo hi")
        self.assertTrue(cm.exception.exec_id)
        self.assertTrue(
            cm.exception.request_id,
            "the unconfirmed error must carry the POST request_id, not a blank 503",
        )
        posts = [
            x
            for x in _MockAPI.log
            if x["method"] == "POST" and x["path"].endswith("/execs")
        ]
        self.assertEqual(len(posts), 1)

    def test_run_succeeded_output_404_is_empty_not_lost(self) -> None:
        """A command that already succeeded is not rewritten to lost because logs 404."""
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.output_gone = {
            "status": 404,
            "body": {"code": "not_found", "detail": "gone"},
        }
        r = sb.run("echo hi")
        self.assertFalse(r.lost)
        self.assertTrue(r.ok)
        self.assertEqual(r.exit_code, 0)
        self.assertEqual(r.stdout, "")

    def test_post_without_idempotency_is_not_retried_on_drop(self) -> None:
        """Counter-guard for _retry_safe itself — start() also sets max_retries=0.

        A Client.request POST with the default retry budget and no
        Idempotency-Key must stop after one drop. If this only went through
        start(), deleting _retry_safe would still pass.
        """
        c = Client(
            api_key="k", base_url=self.base, max_retries=2, retry_backoff=0.01
        )
        _MockAPI.drop_count = 2
        with self.assertRaises(SistemoError) as cm:
            c.request(
                "POST",
                "/v1/machines/nope/execs",
                {"exec_id": "e", "script": "x"},
            )
        self.assertIn("connection error", str(cm.exception))
        posts = [x for x in _MockAPI.log if x["method"] == "POST"]
        self.assertEqual(len(posts), 1)

    def test_create_connection_drop_is_retried(self) -> None:
        """Counter-guard: POST /v1/machines carries Idempotency-Key, so it retries."""
        _MockAPI.drop_count = 1
        sb = Sandbox(api_key="k", base_url=self.base)
        creates = [
            x
            for x in _MockAPI.log
            if x["method"] == "POST" and x["path"] == "/v1/machines"
        ]
        self.assertEqual(len(creates), 2)
        self.assertEqual(creates[0]["idem"], creates[1]["idem"])
        self.assertEqual(sb.id, "m-1")

    def test_run_lost_when_machine_stops(self) -> None:
        """A 409 machine_not_running mid-wait is lost, not a raised error."""
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.job_gone = {
            "status": 409,
            "body": {"code": "machine_not_running", "detail": "stopped"},
        }
        r = sb.run("echo hi")
        self.assertTrue(r.lost)
        self.assertEqual(r.exit_code, -1)
        self.assertFalse(r.ok)
        self.assertEqual(r.stdout, "")

    def test_run_lost_when_job_404s(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.job_gone = {
            "status": 404,
            "body": {"code": "not_found", "detail": "gone"},
        }
        r = sb.run("echo hi")
        self.assertTrue(r.lost)
        self.assertEqual(r.exit_code, -1)

    def test_run_lost_when_job_410s(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.job_gone = {
            "status": 410,
            "body": {"code": "gone", "detail": "aged out"},
        }
        r = sb.run("echo hi")
        self.assertTrue(r.lost)
        self.assertEqual(r.exit_code, -1)

    def test_run_does_not_mark_lost_on_too_many_execs_poll(self) -> None:
        """Counter-guard: only unaccountable codes become lost, not every 409."""
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.job_gone = {
            "status": 409,
            "body": {"code": "too_many_execs", "detail": "wait"},
        }
        with self.assertRaises(TooManyExecsError):
            sb.run("echo hi")

    def test_start_on_stopped_machine_raises(self) -> None:
        """start() of a new command on a stopped machine is not lost — nothing started."""
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.fail_next = {
            "status": 409,
            "body": {"code": "machine_not_running", "detail": "stopped"},
        }
        with self.assertRaises(MachineNotRunningError) as cm:
            sb.start("echo hi")
        self.assertEqual(cm.exception.code, "machine_not_running")
        self.assertIsInstance(cm.exception, APIError)
        self.assertFalse(
            any(
                x["method"] == "GET" and "/execs/" in x["path"]
                for x in _MockAPI.log
            ),
            "start() must not GET-recover a refusal — nothing started",
        )

    def test_run_17th_is_too_many_execs(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.fail_next = {
            "status": 409,
            "body": {
                "code": "too_many_execs",
                "detail": "wait",
                "retryable": True,
            },
        }
        with self.assertRaises(TooManyExecsError) as cm:
            sb.run("echo hi")
        self.assertEqual(cm.exception.code, "too_many_execs")
        self.assertIsInstance(cm.exception, APIError)
        self.assertNotIsInstance(cm.exception, MachineNotRunningError)
        self.assertFalse(
            any(
                x["method"] == "GET" and "/execs/" in x["path"]
                for x in _MockAPI.log
            ),
            "a fresh exec_id after too_many_execs must not be GET-recovered",
        )

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

    def test_quotas_keep_unknown_fields(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        quotas = c.get_quotas()
        self.assertEqual(quotas["tier"], "free")
        self.assertEqual(quotas["quotas"][0]["name"], "machines")
        self.assertEqual(quotas["per_machine"]["vcpus"], 2)
        self.assertEqual(quotas["future_field"], "kept")
        self.assertEqual(_MockAPI.log[-1]["path"], "/v1/quotas")

    def test_images_keep_unknown_fields(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        images = c.list_images()
        self.assertEqual(images[0]["name"], "base")
        self.assertEqual(images[0]["future"], 1)
        self.assertEqual(_MockAPI.log[-1]["path"], "/v1/images")

    def test_null_images_is_empty_list(self) -> None:
        # Counter-guard: unwrapping must not throw when the catalog is null.
        c = Client(api_key="k", base_url=self.base)
        _MockAPI.fail_next = {"status": 200, "body": {"images": None}}
        self.assertEqual(c.list_images(), [])

    def test_stop_and_boot_are_not_exec_start(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        sb.stop()
        stop = _MockAPI.log[-1]
        self.assertEqual(stop["method"], "POST")
        self.assertEqual(stop["path"], f"/v1/machines/{sb.id}/stop")
        self.assertEqual(sb.state, "stopped")
        sb.boot()
        boot = _MockAPI.log[-1]
        self.assertEqual(boot["method"], "POST")
        self.assertEqual(boot["path"], f"/v1/machines/{sb.id}/start")
        self.assertEqual(sb.state, "running")
        sb.start("echo hi")
        started = _MockAPI.log[-1]
        self.assertTrue(started["path"].endswith("/execs"))
        self.assertEqual(started["body"]["script"], "echo hi")

    def test_root_volume_id_survives_create_without_echo(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base, root_volume_id="vol-abc")
        self.assertEqual(sb.root_volume_id, "vol-abc")
        self.assertNotIn("root_volume_id", _MockAPI.machines[sb.id])
        # A later read that omits the field must not wipe it.
        sb.wait(5, poll_interval=30)
        self.assertEqual(sb.root_volume_id, "vol-abc")

    def test_connect_keeps_root_volume_and_does_not_wait(self) -> None:
        _MockAPI.machines["m-9"] = {
            "id": "m-9",
            "state": "provisioning",
            "root_volume_id": "vol-keep",
        }
        sb = Sandbox.connect("m-9", api_key="k", base_url=self.base)
        self.assertEqual(sb.state, "provisioning")
        self.assertEqual(sb.root_volume_id, "vol-keep")
        gets = [x for x in _MockAPI.log if x["method"] == "GET"]
        self.assertEqual(len(gets), 1)

    def test_wait_returns_immediately_when_running(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        started = __import__("time").monotonic()
        sb.wait(30, poll_interval=30)
        self.assertLess(__import__("time").monotonic() - started, 2)
        self.assertEqual(sb.state, "running")

    def test_wait_fails_immediately_when_stopped_or_error(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        for state in ("stopped", "error"):
            _MockAPI.machines[sb.id]["state"] = state
            _MockAPI.log.clear()
            started = __import__("time").monotonic()
            with self.assertRaises(SistemoError) as cm:
                sb.wait(30, poll_interval=30)
            self.assertLess(__import__("time").monotonic() - started, 2)
            self.assertIn(state, str(cm.exception))
            self.assertNotIsInstance(cm.exception, MachineTimeout)
            self.assertEqual(len(_MockAPI.log), 1)

    def test_wait_polls_until_running(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.machines[sb.id]["state"] = "starting"
        _MockAPI.machine_gets = 0
        _MockAPI.promote_after = 2
        sb.wait(5, poll_interval=0.01)
        self.assertEqual(sb.state, "running")
        self.assertGreaterEqual(_MockAPI.machine_gets, 2)

    def test_wait_deadline_is_required_and_enforced(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        _MockAPI.machines[sb.id]["state"] = "provisioning"
        with self.assertRaises(SistemoError):
            sb.wait(None)  # type: ignore[arg-type]
        with self.assertRaises(MachineTimeout):
            sb.wait(0, poll_interval=30)
        self.assertEqual(sb.state, "provisioning")

    def test_snapshots_round_trip_and_page(self) -> None:
        sb = Sandbox(api_key="k", base_url=self.base)
        created = sb.create_snapshot("before")
        self.assertEqual(created["name"], "before")
        self.assertEqual(created["extra"], "kept")
        self.assertEqual(_MockAPI.log[-1]["path"], f"/v1/machines/{sb.id}/snapshots")
        page = sb.list_snapshots()
        self.assertIsInstance(page, Page)
        self.assertEqual(page[0]["id"], created["id"])
        restored = sb.restore_snapshot(created["id"])
        self.assertEqual(_MockAPI.log[-1]["path"], f"/v1/snapshots/{created['id']}/restore")
        self.assertTrue(restored["restored"])
        deleted = sb.delete_snapshot(created["id"])
        self.assertEqual(deleted["status"], "deleted")
        c = Client(api_key="k", base_url=self.base)
        _MockAPI.paginate = True
        first = c.list_snapshots(limit=1)
        self.assertEqual(first.next_cursor, "p2")
        second = c.list_snapshots(cursor=first.next_cursor)
        self.assertIsNone(second.next_cursor)
        self.assertEqual([s["id"] for s in c.iter_snapshots()], ["s1", "s2"])

    def test_snapshot_list_null_is_empty(self) -> None:
        c = Client(api_key="k", base_url=self.base)
        _MockAPI.fail_next = {"status": 200, "body": {"snapshots": None, "next_cursor": None}}
        self.assertEqual(c.list_snapshots(), [])


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
        self.assertNotIsInstance(err, TooManyExecsError)
        self.assertIsInstance(err, APIError)

    def test_too_many_execs_is_its_own_type(self):
        err = api_error(409, "wait", "too_many_execs")
        self.assertIsInstance(err, TooManyExecsError)
        self.assertIsInstance(err, APIError)
        self.assertNotIsInstance(err, MachineNotRunningError)

    def test_machine_not_running_is_its_own_type(self):
        err = api_error(409, "stopped", "machine_not_running")
        self.assertIsInstance(err, MachineNotRunningError)
        self.assertNotIsInstance(err, TooManyExecsError)

    def test_process_disk_oom_409s_are_typed(self):
        self.assertIsInstance(api_error(409, "x", "machine_process_limit"), ProcessLimitError)
        self.assertIsInstance(api_error(409, "x", "machine_disk_full"), DiskFullError)
        self.assertIsInstance(api_error(409, "x", "machine_out_of_memory"), OutOfMemoryError)


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


class LockedNetworkAndSecretsTest(unittest.TestCase):
    """0.2.0: locked networks and secrets on Sandbox."""

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
        _MockAPI.drop_count = 0

    def _sb(self, **kw) -> Sandbox:
        return Sandbox(api_key="sk_test", base_url=self.base, **kw)

    def _create_body(self) -> dict:
        return [e for e in _MockAPI.log if e["method"] == "POST" and e["path"] == "/v1/machines"][-1]["body"]

    def test_omitted_network_sends_nothing_so_the_default_stays_open(self) -> None:
        # The open default belongs to the server: a create without these keys
        # is exactly what 0.1.x sent.
        sb = self._sb()
        body = self._create_body()
        for k in ("network", "allow", "secrets"):
            self.assertNotIn(k, body)
        self.assertEqual(sb.network, "open")

    def test_locked_with_an_empty_allow_list_is_sent_as_an_empty_list(self) -> None:
        sb = self._sb(network="locked", allow=[])
        body = self._create_body()
        self.assertEqual(body["network"], "locked")
        self.assertEqual(body["allow"], [])  # [] = no outbound, not "unset"
        self.assertEqual(sb.network, "locked")

    def test_secrets_on_create_and_bind_unbind(self) -> None:
        sb = self._sb(network="locked", allow=["api.example.com"], secrets=["STRIPE_KEY"])
        self.assertEqual(self._create_body()["secrets"], ["STRIPE_KEY"])
        self.assertEqual(sb.secrets.names, ["STRIPE_KEY"])
        self.assertEqual(sb.secrets.bind("DB_URL"), ["DB_URL", "STRIPE_KEY"])
        self.assertEqual(sb.secrets.unbind("STRIPE_KEY"), ["DB_URL"])
        last = _MockAPI.log[-1]
        self.assertEqual(last["path"], f"/v1/machines/{sb.id}/secrets")
        self.assertEqual(last["body"], {"bind": [], "unbind": ["STRIPE_KEY"]})

    def test_bind_of_an_unknown_name_is_an_api_error(self) -> None:
        sb = self._sb()
        with self.assertRaises(APIError) as cm:
            sb.secrets.bind("NOPE")
        self.assertEqual(cm.exception.status, 400)
        self.assertEqual(sb.secrets.names, [])

    def test_set_network_sends_the_whole_list(self) -> None:
        sb = self._sb()
        sb.set_network("locked", ["pypi.org", "files.pythonhosted.org"])
        self.assertEqual(_MockAPI.log[-1]["body"], {"network": "locked", "allow": ["pypi.org", "files.pythonhosted.org"]})
        self.assertEqual((sb.network, sb.allow), ("locked", ["pypi.org", "files.pythonhosted.org"]))

    def test_a_bare_string_allow_is_a_type_error_not_characters(self) -> None:
        sb = self._sb()
        n = len(_MockAPI.log)
        with self.assertRaises(TypeError):
            sb.set_network("locked", "pypi.org")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Sandbox(api_key="sk_test", base_url=self.base, network="locked", allow="pypi.org")  # type: ignore[arg-type]
        self.assertEqual(len(_MockAPI.log), n, "nothing may be sent")

    def test_allow_without_locked_is_refused_locally(self) -> None:
        sb = self._sb()
        n = len(_MockAPI.log)
        with self.assertRaises(ValueError):
            sb.set_network("open", ["pypi.org"])
        with self.assertRaises(ValueError):
            Sandbox(api_key="sk_test", base_url=self.base, allow=["pypi.org"])
        self.assertEqual(len(_MockAPI.log), n, "nothing may be sent")
        sb.set_network("open")  # counter-guard: open with no allow is fine
        self.assertEqual(sb.network, "open")

    def test_a_long_idempotency_key_is_refused_before_sending(self) -> None:
        from sistemo import InvalidArgumentError, SistemoError

        n = len(_MockAPI.log)
        for bad in ("a" * 256, "has space", "x" * 20000):
            with self.assertRaises(InvalidArgumentError) as cm:
                Sandbox(api_key="sk_test", base_url=self.base, idempotency_key=bad)
            self.assertIsInstance(cm.exception, SistemoError)
            self.assertIsInstance(cm.exception, ValueError)
        self.assertEqual(len(_MockAPI.log), n, "nothing may be sent — a 20 KB key would be a retried CDN 520")
        self._sb(idempotency_key="a" * 255)  # the limit itself is fine

    def test_client_side_refusals_are_sistemo_errors_and_value_errors(self) -> None:
        from sistemo import InvalidArgumentError, SistemoError

        sb = self._sb()
        with self.assertRaises(InvalidArgumentError) as cm:
            sb.set_network("closed")
        self.assertIsInstance(cm.exception, SistemoError)
        self.assertIsInstance(cm.exception, ValueError)

    def test_an_unknown_network_value_says_so(self) -> None:
        sb = self._sb()
        n = len(_MockAPI.log)
        for bad in ("closed", "LOCKED"):
            with self.assertRaises(ValueError) as cm:
                sb.set_network(bad, ["pypi.org"])
            self.assertIn('network must be "open" or "locked"', str(cm.exception))
        self.assertEqual(len(_MockAPI.log), n, "nothing may be sent")

    def test_set_network_is_retried_on_a_503(self) -> None:
        sb = self._sb()
        _MockAPI.fail_next = {"status": 503, "body": {"code": "unavailable", "detail": "busy"}}
        sb.set_network("locked", ["pypi.org"])
        posts = [e for e in _MockAPI.log if e["method"] == "POST" and e["path"].endswith("/network")]
        self.assertEqual(len(posts), 2, "a whole-list replace is safe to retry")
        self.assertEqual(sb.allow, ["pypi.org"])

    def test_egress_passes_its_filters(self) -> None:
        sb = self._sb(network="locked", allow=[])
        log = sb.egress(action="blocked", limit=10)
        self.assertEqual(log["activity"][0]["host"], "evil.example")
        self.assertEqual(_MockAPI.log[-1]["full_path"], f"/v1/machines/{sb.id}/egress/activity?action=blocked&limit=10")

    def test_connect_reads_network_and_secrets(self) -> None:
        sb = self._sb(network="locked", allow=["a.example.com"], secrets=["K"])
        again = Sandbox.connect(sb.id, api_key="sk_test", base_url=self.base)
        self.assertEqual((again.network, again.allow, again.secrets.names), ("locked", ["a.example.com"], ["K"]))

    def test_list_secrets_returns_names_only(self) -> None:
        c = Client(api_key="sk_test", base_url=self.base)
        secrets = c.list_secrets()
        self.assertEqual(secrets[0]["name"], "STRIPE_KEY")
        self.assertNotIn("value", secrets[0])

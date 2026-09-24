"""Async exec tests for the Python SDK, against a local mock control plane.

Run from sdk/python:

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from sistemo import Sandbox
from sistemo.errors import (
    AsyncExecUnsupportedError,
    ExecStartUnconfirmed,
    GoneError,
    api_error,
)
from sistemo.jobs import Job, JobTimeout

# The scenario each test asks the mock server to play.
SCENE: dict = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):  # silence
        pass

    def _send(self, status: int, payload: dict):
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        body = {}
        if raw:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
        if self.path.endswith("/execs"):
            SCENE.setdefault("starts", []).append(body.get("exec_id"))
            if SCENE.get("start_status"):
                return self._send(SCENE["start_status"], SCENE.get("start_body", {}))
            rec = dict(SCENE["job"])
            if body.get("exec_id"):
                rec["exec_id"] = body["exec_id"]
            return self._send(202, rec)
        return self._send(201, {"id": "m1", "state": "running"})

    def do_DELETE(self):
        return self._send(200, SCENE.get("cancelled", SCENE["job"]))

    def do_GET(self):
        u = urlparse(self.path)
        if u.path.endswith("/output"):
            q = parse_qs(u.query)
            offset = int(q.get("offset", ["0"])[0])
            return self._send(200, next_page(offset))
        if "/execs/" in u.path:
            return self._send(200, SCENE["job"])
        if u.path.endswith("/execs"):
            return self._send(200, {"execs": [SCENE["job"]]})
        return self._send(200, SCENE["job"])


def next_page(offset: int) -> dict:
    """Serve the scripted output pages, then whatever the scene says comes next."""
    pages = SCENE.get("pages", [])
    for p in pages:
        if p["offset"] == offset:
            return p
    return {"stream": "stdout", "offset": offset, "next_offset": offset, "data": "", "eof": True}


def page(offset: int, data: bytes, eof: bool = False) -> dict:
    return {
        "stream": "stdout",
        "offset": offset,
        "next_offset": offset + len(data),
        "data": base64.b64encode(data).decode(),
        "eof": eof,
    }


def job_rec(state="running", exit_code=None, **kw) -> dict:
    rec = {
        "exec_id": "11111111-2222-3333-4444-555555555555",
        "machine_id": "m1",
        "state": state,
        "exit_code": exit_code,
        "stdout_bytes": 0,
        "stderr_bytes": 0,
        "truncated": False,
    }
    rec.update(kw)
    return rec


class AsyncExecTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        SCENE.clear()
        SCENE["job"] = job_rec()

    def sandbox(self) -> Sandbox:
        return Sandbox(api_key="sk_live_test", base_url=self.base)

    # ── the exit-code contract ──────────────────────────────────────

    def test_running_job_has_no_exit_code(self):
        """⚠ None, never 0. A zero here reads as 'the command succeeded'."""
        j = Job(self.sandbox(), job_rec("running"))
        self.assertIsNone(j.exit_code)
        self.assertFalse(j.done)
        self.assertFalse(j.ok)

    def test_lost_is_not_failed_and_is_never_ok(self):
        """⚠ `lost` means we cannot say it ran OR that it did not."""
        j = Job(self.sandbox(), job_rec("lost"))
        self.assertTrue(j.done)
        self.assertTrue(j.lost)
        self.assertIsNone(j.exit_code)
        self.assertFalse(j.ok, "a lost job must never report ok — we do not know that it worked")

    def test_failed_job_is_done_but_not_ok(self):
        j = Job(self.sandbox(), job_rec("failed", 7))
        self.assertTrue(j.done)
        self.assertFalse(j.ok)
        self.assertEqual(j.exit_code, 7)

    def test_succeeded_job_is_ok(self):
        j = Job(self.sandbox(), job_rec("succeeded", 0))
        self.assertTrue(j.ok)

    # ── streaming ───────────────────────────────────────────────────

    def test_multibyte_character_split_across_pages(self):
        """⚠ The trap this SDK exists to hide.

        '€' is three bytes. Split it across two pages and a decoder that treats
        each page on its own produces two replacement characters instead of one
        euro sign — rare enough to survive testing, common enough to corrupt
        real logs.
        """
        euro = "€".encode()  # b'\xe2\x82\xac'
        SCENE["pages"] = [
            page(0, b"cost: " + euro[:1]),
            page(7, euro[1:] + b"9\n", eof=True),
        ]
        SCENE["job"] = job_rec("succeeded", 0)
        j = Job(self.sandbox(), SCENE["job"])
        self.assertEqual("".join(j.stream(poll_interval=0)), "cost: €9\n")

    def test_empty_page_from_a_running_job_is_not_the_end(self):
        """⚠ Stop on eof, never on an empty page."""
        SCENE["pages"] = [
            page(0, b"start\n"),
            # Nothing new yet — the job is thinking. eof is False.
            {"stream": "stdout", "offset": 6, "next_offset": 6, "data": "", "eof": False},
        ]
        SCENE["job"] = job_rec("running")
        j = Job(self.sandbox(), SCENE["job"])

        got = []
        with self.assertRaises(JobTimeout):
            for chunk in j.stream(poll_interval=0.01, timeout=0.3):
                got.append(chunk)
        self.assertEqual("".join(got), "start\n")

    def test_binary_output_survives(self):
        raw = bytes([0x89, 0x50, 0x4E, 0x47, 0x00, 0xFF, 0xFE])
        SCENE["pages"] = [page(0, raw, eof=True)]
        SCENE["job"] = job_rec("succeeded", 0)
        j = Job(self.sandbox(), SCENE["job"])
        self.assertEqual(b"".join(j.stream_bytes(poll_interval=0)), raw)

    # ── start semantics ─────────────────────────────────────────────

    def test_start_mints_an_exec_id(self):
        """⚠ Without a caller-minted id, a lost start response is unrecoverable."""
        sb = self.sandbox()
        job = sb.start("echo hi")
        keys = [k for k in SCENE.get("starts", []) if k]
        self.assertTrue(keys, "start() sent no exec_id")
        self.assertEqual(job.id, keys[0])

    def test_unconfirmed_start_keeps_the_exec_id(self):
        """⚠ A 503 here is NOT a failure — the command may be running.

        Losing the id would leave it unaddressable, which is the orphan this
        whole design prevents.
        """
        SCENE["start_status"] = 503
        SCENE["start_body"] = {
            "error": "could not confirm the command started",
            "code": "unavailable",
            "exec_id": "abc-123",
        }
        sb = self.sandbox()
        with self.assertRaises(ExecStartUnconfirmed) as cm:
            sb.start("./deploy.sh")
        self.assertEqual(cm.exception.exec_id, "abc-123")

    def test_plain_503_is_not_an_unconfirmed_start(self):
        """⚠ Only a 503 that NAMES an exec means 'it may be running'."""
        e = api_error(503, "no host available", "no_host_available", {})
        self.assertNotIsInstance(e, ExecStartUnconfirmed)

    def test_old_image_is_not_retryable(self):
        e = api_error(501, "async exec is not available on this guest agent", None, {})
        self.assertIsInstance(e, AsyncExecUnsupportedError)

    def test_reaped_output_is_gone_not_missing(self):
        e = api_error(410, "output no longer retained", "gone", {})
        self.assertIsInstance(e, GoneError)
        self.assertNotIsInstance(e, type(api_error(404, "x")))

    # ── waiting ─────────────────────────────────────────────────────

    def test_wait_returns_immediately_when_already_terminal(self):
        j = Job(self.sandbox(), job_rec("succeeded", 0))
        self.assertIs(j.wait(timeout=0.01), j)

    def test_wait_timeout_does_not_stop_the_job(self):
        """⚠ JobTimeout is the CLIENT giving up; the job keeps running."""
        SCENE["job"] = job_rec("running")
        j = Job(self.sandbox(), SCENE["job"])
        with self.assertRaises(JobTimeout) as cm:
            j.wait(timeout=0.2, poll_interval=0.05)
        self.assertIn("still running", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

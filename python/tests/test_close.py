"""close() keeps trying until the machine is deleted (audit AD1).

Tearing down 20 machines in parallel after creating them, 11–13 close() calls
raised RateLimitError and those machines stayed running and billed: close() got
the client's ordinary 2 retries, and it sits in finally blocks nobody retries.
"""

import json
import threading
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer

import sistemo.sandbox as sandbox_mod
from sistemo import Client, InvalidArgumentError, SistemoError
from sistemo.errors import APIError
from sistemo.sandbox import Sandbox

SCRIPT: list = []  # (status, code, retry_after) answered in order to DELETE
SEEN: list = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        return

    def do_DELETE(self):  # noqa: N802
        SEEN.append(self.path)
        status, code, retry_after = SCRIPT.pop(0) if SCRIPT else (200, None, None)
        body = json.dumps({"status": "destroyed"} if status == 200 else {"code": code, "detail": code})
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body.encode())


class CloseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        SCRIPT.clear()
        SEEN.clear()
        # A fake clock: sleeping advances it, nothing really waits.
        self.now = [1000.0]
        self.slept = []
        orig = (sandbox_mod._sleep, sandbox_mod._monotonic)
        sandbox_mod._sleep = lambda s: (self.slept.append(s), self.now.__setitem__(0, self.now[0] + s))
        sandbox_mod._monotonic = lambda: self.now[0]
        self.addCleanup(lambda: setattr(sandbox_mod, "_sleep", orig[0]) or setattr(sandbox_mod, "_monotonic", orig[1]))

    def sandbox(self):
        sb = object.__new__(Sandbox)
        sb._client = Client(api_key="k", base_url=f"http://127.0.0.1:{self.srv.server_port}", max_retries=2)
        sb.id = "m-1"
        return sb

    def test_waits_out_the_rate_limit_however_many_429s(self):
        # Five 429s — more than max_retries=2 — then the delete goes through.
        SCRIPT.extend([(429, "rate_limited", 1)] * 5)
        self.sandbox().close()
        self.assertEqual(len(SEEN), 6)
        self.assertTrue(all(s >= 1 for s in self.slept), f"Retry-After not honoured: {self.slept}")

    def test_already_gone_is_closed(self):
        SCRIPT.append((404, "not_found", None))
        self.sandbox().close()  # no raise

    def test_mid_transition_is_waited_out(self):
        SCRIPT.extend([(409, "invalid_state_transition", None), (200, None, None)])
        self.sandbox().close()
        self.assertEqual(len(SEEN), 2)

    def test_a_real_refusal_raises_at_once(self):
        # 409 because the machine has snapshots: waiting cannot fix it.
        SCRIPT.append((409, "conflict", None))
        with self.assertRaises(APIError):
            self.sandbox().close()
        self.assertEqual(len(SEEN), 1)

    def test_gives_up_at_the_deadline_and_says_so(self):
        SCRIPT.extend([(429, "rate_limited", 30)] * 50)
        with self.assertRaises(APIError) as cm:
            self.sandbox().close(timeout=60)
        self.assertEqual(cm.exception.status, 429)
        self.assertLessEqual(self.now[0] - 1000.0, 60.0, "slept past the deadline")

    def test_with_block_warns_instead_of_hiding_a_billed_machine(self):
        SCRIPT.extend([(409, "conflict", None)])
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            with self.sandbox():
                pass
        msgs = [str(x.message) for x in w if issubclass(x.category, RuntimeWarning)]
        self.assertTrue(any("m-1" in m and "still running" in m for m in msgs), msgs)


class ValidationEdgesTest(unittest.TestCase):
    """Audit AD5: Python and JS refuse the same things, the same way."""

    def test_empty_idempotency_key_is_refused(self):
        c = Client(api_key="k", base_url="http://127.0.0.1:9", max_retries=0)
        with self.assertRaises(InvalidArgumentError):
            c.request("POST", "/v1/machines", {}, idempotency_key="")

    def test_allow_as_a_string_is_an_invalid_argument_and_still_a_typeerror(self):
        with self.assertRaises(InvalidArgumentError) as cm:
            sandbox_mod._check_network_args("locked", "pypi.org")
        self.assertIsInstance(cm.exception, TypeError)
        self.assertIsInstance(cm.exception, SistemoError)


if __name__ == "__main__":
    unittest.main()

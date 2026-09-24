"""Jobs — commands that outlive the HTTP request that started them.

``sb.run(...)`` is start + wait on this handle. ``sb.start(...)`` returns it
without waiting, for stream / cancel / reconnect.
"""

from __future__ import annotations

import base64
import codecs
import time
from typing import TYPE_CHECKING, Iterator

from .errors import SistemoError

if TYPE_CHECKING:  # pragma: no cover
    from .sandbox import Sandbox

#: States that mean nothing further will happen.
TERMINAL = frozenset({"succeeded", "failed", "cancelled", "expired", "lost"})


class JobTimeout(SistemoError):
    """``wait()``/``stream()`` gave up waiting.

    ⚠ The job is **still running** — this is your client's patience running out,
    not the command's. The handle stays valid; poll it again later.
    """


class Job:
    """A handle on a command running inside a sandbox.

    The handle outlives the process that created it, so a crashed client can
    reconnect with ``sb.job(exec_id)`` or list what is running with
    ``sb.jobs()``.
    """

    def __init__(self, sandbox: "Sandbox", record: dict):
        self._sb = sandbox
        self._apply(record)

    # ── state ───────────────────────────────────────────────────────

    def _apply(self, rec: dict) -> "Job":
        self.id: str = str(rec.get("exec_id", "") or "")
        self.machine_id: str = str(rec.get("machine_id", "") or "")
        self.state: str = str(rec.get("state", "") or "")
        # ⚠ None until known, and None FOREVER when state == "lost". Never
        # coerce this to 0 — a zero here reads as "the command succeeded".
        raw = rec.get("exit_code")
        self.exit_code: int | None = None if raw is None else int(raw)
        self.stdout_bytes: int = int(rec.get("stdout_bytes") or 0)
        self.stderr_bytes: int = int(rec.get("stderr_bytes") or 0)
        self.truncated: bool = bool(rec.get("truncated"))
        self.started_at = rec.get("started_at")
        self.finished_at = rec.get("finished_at")
        return self

    @property
    def done(self) -> bool:
        """Whether the job reached a terminal state."""
        return self.state in TERMINAL

    @property
    def ok(self) -> bool:
        """``True`` only for a command that finished and exited 0.

        ⚠ A ``lost`` job is never ``ok``, because we cannot say that it worked —
        and a property that guessed would be worse than no property.
        """
        return self.state == "succeeded" and self.exit_code == 0

    @property
    def lost(self) -> bool:
        """The machine could not account for this job.

        ⚠ Not the same as failed. ``failed`` means it ran and returned non-zero;
        ``lost`` means we can say neither that it ran nor that it did not. Do not
        retry blindly, and do not assume completion.
        """
        return self.state == "lost"

    def __repr__(self) -> str:  # pragma: no cover
        code = "null" if self.exit_code is None else self.exit_code
        return f"<Job {self.id[:8]} {self.state} exit_code={code}>"

    # ── polling ─────────────────────────────────────────────────────

    def refresh(self) -> "Job":
        """Re-read the job's state from the API."""
        return self._apply(
            self._sb._client.request(
                "GET", f"/v1/machines/{self._sb.id}/execs/{self.id}"
            )
        )

    def wait(self, timeout: float | None = None, poll_interval: float = 1.0) -> "Job":
        """Block until the job reaches a terminal state, then return it.

        The blocking happens **client-side**: each poll is a fast request, so no
        proxy between you and the API is ever holding a long connection.

        Raises :class:`JobTimeout` if ``timeout`` elapses first — ⚠ which does
        not stop the job. Call :meth:`cancel` if that is what you meant.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.done:
                return self
            if deadline is not None and time.monotonic() >= deadline:
                raise JobTimeout(
                    f"job {self.id} is still {self.state} after {timeout}s "
                    f"(it is still running; poll it again or cancel it)"
                )
            # Refresh BEFORE sleeping. POST /execs returns running immediately,
            # so a hello-world `sb.run("echo hi")` would otherwise wait a full
            # poll interval for a command that finished in milliseconds.
            self.refresh()
            if self.done:
                return self
            if deadline is None:
                time.sleep(poll_interval)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise JobTimeout(
                        f"job {self.id} is still {self.state} after {timeout}s "
                        f"(it is still running; poll it again or cancel it)"
                    )
                time.sleep(min(poll_interval, remaining))

    def cancel(self) -> "Job":
        """Stop the job: SIGTERM, a short grace period, then SIGKILL.

        Cancelling a job that already finished is a no-op, so a retried cancel
        is safe.
        """
        return self._apply(
            self._sb._client.request(
                "DELETE", f"/v1/machines/{self._sb.id}/execs/{self.id}"
            )
        )

    # ── output ──────────────────────────────────────────────────────

    def stream_bytes(
        self,
        stream: str = "stdout",
        poll_interval: float = 0.5,
        timeout: float | None = None,
        limit: int = 65536,
    ) -> Iterator[bytes]:
        """Yield raw output bytes as they are produced, until the job ends.

        ⚠ Chunks are split on BYTE boundaries and one can end mid-character.
        Use :meth:`stream` unless you actually want bytes.
        """
        offset = 0
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            page = self._sb._client.request(
                "GET",
                f"/v1/machines/{self._sb.id}/execs/{self.id}/output"
                f"?stream={stream}&offset={offset}&limit={limit}",
            )
            offset = int(page.get("next_offset") or offset)
            raw = page.get("data") or ""
            data = base64.b64decode(raw) if raw else b""
            if data:
                yield data
                # ⚠ Loop straight back without sleeping. A producer faster than
                # the poll interval would otherwise be read at one page per
                # interval, turning a 30-second build log into minutes of
                # trickle for no reason.
                continue
            # ⚠ Stop on eof, NEVER on an empty page. An empty page from a running
            # job means "nothing new yet"; treating it as the end truncates the
            # output of every job that pauses to think.
            if page.get("eof"):
                return
            if deadline is not None and time.monotonic() >= deadline:
                raise JobTimeout(f"job {self.id} produced no further output within {timeout}s")
            time.sleep(poll_interval)

    def stream(
        self,
        stream: str = "stdout",
        poll_interval: float = 0.5,
        timeout: float | None = None,
    ) -> Iterator[str]:
        """Yield output as text as it is produced.

        ⚠ Decoding is INCREMENTAL. A page can end in the middle of a multi-byte
        character, and decoding each page on its own would corrupt every
        character that straddles a boundary — which, with a 64 KiB page size, is
        rare enough to survive testing and common enough to corrupt real logs.
        """
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        for chunk in self.stream_bytes(stream, poll_interval, timeout):
            text = decoder.decode(chunk)
            if text:
                yield text
        # Flush whatever partial sequence is left, so a truncated final
        # character surfaces as U+FFFD rather than silently disappearing.
        tail = decoder.decode(b"", final=True)
        if tail:
            yield tail

    def output(self, stream: str = "stdout") -> bytes:
        """Read everything written to ``stream`` so far, without waiting."""
        buf = bytearray()
        offset = 0
        while True:
            page = self._sb._client.request(
                "GET",
                f"/v1/machines/{self._sb.id}/execs/{self.id}/output"
                f"?stream={stream}&offset={offset}&limit=65536",
            )
            raw = page.get("data") or ""
            data = base64.b64decode(raw) if raw else b""
            buf.extend(data)
            nxt = int(page.get("next_offset") or offset)
            if not data or nxt <= offset:
                return bytes(buf)
            offset = nxt

    def logs(self, stream: str = "stdout") -> str:
        """:meth:`output` decoded as text."""
        return self.output(stream).decode("utf-8", errors="replace")

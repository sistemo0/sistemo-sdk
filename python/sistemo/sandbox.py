"""The Sandbox abstraction — a real isolated Firecracker microVM."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterator

from ._client import Client, Page, _qs
from .errors import APIError, ExecStartUnconfirmed, NotFoundError, SistemoError
from .jobs import Job

# 409s that mean the POST did not start this id. GET would 404 a fresh UUID.
_SKIP_GET_409 = frozenset(
    {
        "too_many_execs",
        "machine_process_limit",
        "machine_disk_full",
        "machine_out_of_memory",
        "machine_not_running",
    }
)


@dataclass
class ExecResult:
    """The result of running a command in a sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False
    request_id: str | None = None
    #: The machine could not account for this command.
    #:
    #: ⚠ When this is true, :attr:`exit_code` is a PLACEHOLDER (-1) and means
    #: nothing — we can say neither that the command ran nor that it did not.
    #: Without this flag a machine that died mid-command was indistinguishable
    #: from a command that genuinely exited -1.
    lost: bool = False

    @property
    def ok(self) -> bool:
        """True only for a command that finished and exited 0.

        A lost command is never ok — we do not know that it worked.
        """
        return self.exit_code == 0 and not self.lost


class Sandbox:
    """An isolated microVM you can run code in.

    Creating a Sandbox provisions a real microVM (this blocks until it boots).
    Use it as a context manager so it's always cleaned up::

        from sistemo import Sandbox

        with Sandbox() as sb:
            r = sb.run("python3 -c 'print(2 + 2)'")
            print(r.stdout, r.exit_code)
    """

    def __init__(
        self,
        *,
        client: Client | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        name: str = "",
        vcpus: int = 1,
        memory_mb: int = 1024,
        rootfs_size_gb: int = 10,
        stack: str = "base",
        metadata: dict | None = None,
        root_volume_id: str = "",
        idempotency_key: str | None = None,
    ):
        self._client = client or Client(api_key=api_key, base_url=base_url)
        body: dict = {
            "name": name,
            "vcpus": vcpus,
            "memory_mb": memory_mb,
            "rootfs_size_gb": rootfs_size_gb,
            "stack": stack,
            "metadata": metadata or {},
        }
        # Boot from an existing (detached/preserved) root volume instead of a
        # fresh template clone. Size/stack come from the volume.
        if root_volume_id:
            body["root_volume_id"] = root_volume_id
        # Stable key for this create so client/network retries cannot double-boot.
        key = idempotency_key or str(uuid.uuid4())
        machine = self._client.request(
            "POST", "/v1/machines", body, idempotency_key=key
        )
        mid = machine.get("id") if isinstance(machine, dict) else None
        if not mid:
            raise APIError(
                502,
                "create machine response missing id — refusing to continue",
                code="invalid_create_response",
            )
        self.id: str = str(mid)
        self.state: str = str(machine.get("state", "") or "")
        self.root_volume_id: str = root_volume_id
        self._create_idempotency_key = key

    @classmethod
    def connect(
        cls,
        machine_id: str,
        *,
        client: Client | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> "Sandbox":
        """Re-attach to an existing machine — after a crash, or from a stored id.

        Does **not** provision a new microVM. The machine must still exist
        (a 404 is :class:`~sistemo.errors.NotFoundError`).
        """
        if not machine_id:
            raise SistemoError("machine id is required")
        c = client or Client(api_key=api_key, base_url=base_url)
        rec = c.request("GET", f"/v1/machines/{machine_id}")
        mid = rec.get("id") if isinstance(rec, dict) else None
        if not mid:
            raise APIError(
                502,
                "machine response missing id — refusing to continue",
                code="invalid_machine_response",
            )
        self = object.__new__(cls)
        self._client = c
        self.id = str(mid)
        self.state = str(rec.get("state", "") or "")
        self.root_volume_id = str(rec.get("root_volume_id") or "")
        self._create_idempotency_key = ""
        return self

    def run(
        self,
        script: str,
        timeout: int = 120,
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> ExecResult:
        """Run a shell command/script inside the sandbox and return its output.

        This is start + wait on a guest job (``POST /execs``). The default
        ``timeout`` is 120 seconds; the ceiling is 24 hours. A proxy between you
        and the API never holds the connection for the whole command.

        ``env`` and ``cwd`` are sent only when set, so option-less starts still
        work on older guest agents. HTTP 5xx/429 and dropped connections on
        start are **not** retried (replaying would re-run the script). A dropped
        start is recovered by GET of the minted id. If start is unconfirmed,
        this still waits on that id. If the machine stops while waiting, this
        returns ``lost=True`` rather than raising 409.
        """
        eid = str(uuid.uuid4())
        try:
            job = self.start(script, timeout=timeout, exec_id=eid, env=env, cwd=cwd)
        except ExecStartUnconfirmed as e:
            try:
                job = self.job(eid)
            except SistemoError:
                raise e from None
        job.wait()
        # ⚠ -1 is a placeholder for "unknown", not a real status. `lost` is what
        # carries that fact; exit_code alone cannot, because -1 is also a status
        # a real command can exit with.
        code = -1 if job.exit_code is None else job.exit_code
        return ExecResult(
            exit_code=code,
            stdout=job.logs("stdout"),
            stderr=job.logs("stderr"),
            truncated=job.truncated,
            request_id=job.request_id,
            lost=job.lost,
        )

    def start(
        self,
        script: str,
        timeout: int = 120,
        *,
        exec_id: str | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
    ) -> Job:
        """Start a command and return a handle without waiting.

        :meth:`run` is start + wait — use this when you want to stream, cancel,
        or reconnect. Same guest job, same timeout ceiling (24 hours).

        ⚠ You mint ``exec_id`` (a UUID is generated if you omit it). If the
        start response is lost, poll that id rather than starting again.
        Re-sending the same id is a 409 ``exec_id_in_use``, not a second run.
        ``too_many_execs`` is :class:`~sistemo.errors.TooManyExecsError`: wait
        for a command to finish.

        ⚠ If this raises :class:`~sistemo.errors.ExecStartUnconfirmed`, the
        command **may be running**. The exception carries ``exec_id``; poll it
        with :meth:`job` rather than starting over. A dropped connection on
        POST is recovered the same way: GET that id, never re-POST.
        """
        eid = exec_id or str(uuid.uuid4())
        body: dict = {"exec_id": eid, "script": script, "timeout_sec": timeout}
        if env:
            body["env"] = env
        if cwd:
            body["cwd"] = cwd
        try:
            rec = self._client.request(
                "POST",
                f"/v1/machines/{self.id}/execs",
                body,
                max_retries=0,
            )
        except APIError as e:
            if e.status == 409:
                if e.code in _SKIP_GET_409:
                    raise
                return self._job_after_unconfirmed_start(eid, e)
            raise
        except SistemoError as e:
            # Connection / timeout / invalid JSON. The POST is not retried
            # (no Idempotency-Key); the minted id is how we find out whether
            # the guest accepted it.
            return self._job_after_unconfirmed_start(eid, e)
        return Job(self, rec)

    def _job_after_unconfirmed_start(
        self, eid: str, original: BaseException
    ) -> Job:
        """GET the minted id after a POST whose outcome we cannot trust.

        Found → that job. 404 → the original error (nothing started). Any
        other GET failure → :class:`ExecStartUnconfirmed` so the caller has
        the handle.
        """
        try:
            rec = self._client.request(
                "GET", f"/v1/machines/{self.id}/execs/{eid}"
            )
        except NotFoundError:
            raise original from None
        except SistemoError:
            raise ExecStartUnconfirmed(
                503,
                "connection dropped while starting; the command may be running",
                None,
                {"exec_id": eid},
            ) from original
        return Job(self, rec)

    def job(self, exec_id: str) -> Job:
        """Re-attach to a job by id — after a crash, or after an unconfirmed start."""
        rec = self._client.request("GET", f"/v1/machines/{self.id}/execs/{exec_id}")
        return Job(self, rec)

    def jobs(self, limit: int = 100, *, cursor: str | None = None) -> Page:
        """List this sandbox's async jobs, newest first.

        Returns a :class:`~sistemo.Page` of :class:`~sistemo.jobs.Job`. Index it
        like a list; ``next_cursor`` is ``None`` on the last page. The guest
        keeps at most 64, so the default page holds them all. Walk every page
        with :meth:`iter_jobs`.
        """
        out = self._client.request(
            "GET",
            _qs(f"/v1/machines/{self.id}/execs", limit=limit, cursor=cursor),
        )
        return Page(
            [Job(self, r) for r in (out.get("execs") or [])],
            out.get("next_cursor"),
        )

    def iter_jobs(self, *, limit: int | None = None) -> Iterator[Job]:
        """Yield every job on this sandbox, following ``next_cursor``."""
        cursor = None
        page_limit = 100 if limit is None else limit
        while True:
            page = self.jobs(page_limit, cursor=cursor)
            yield from page
            nxt = page.next_cursor
            if not nxt or nxt == cursor:
                return
            cursor = nxt

    def close(self, preserve_storage: bool = False) -> None:
        """Destroy the sandbox (and its disk unless ``preserve_storage``)."""
        query = "?preserve_storage=true" if preserve_storage else ""
        self._client.request("DELETE", f"/v1/machines/{self.id}{query}")

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *_exc) -> None:
        try:
            self.close()
        except Exception:
            pass  # best-effort cleanup; never mask the original error

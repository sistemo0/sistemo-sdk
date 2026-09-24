"""The Sandbox abstraction — a real isolated Firecracker microVM."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ._client import Client
from .errors import APIError
from .jobs import Job


@dataclass
class ExecResult:
    """The result of running a command in a sandbox."""

    exit_code: int
    stdout: str
    stderr: str
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


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

    def run(self, script: str, timeout: int = 120) -> ExecResult:
        """Run a shell command/script inside the sandbox and return its output.

        This is start + wait on a guest job (``POST /execs``). The default
        ``timeout`` is 120 seconds; the ceiling is 24 hours. A proxy between you
        and the API never holds the connection for the whole command.

        HTTP 5xx/429 on start are **not** retried (replaying would re-run the
        script). If start is unconfirmed, this still waits on the id we minted.
        """
        from .errors import ExecStartUnconfirmed

        eid = str(uuid.uuid4())
        try:
            job = self.start(script, timeout=timeout, exec_id=eid)
        except ExecStartUnconfirmed:
            job = self.job(eid)
        job.wait()
        code = -1 if job.exit_code is None else job.exit_code
        return ExecResult(
            exit_code=code,
            stdout=job.logs("stdout"),
            stderr=job.logs("stderr"),
            truncated=job.truncated,
        )

    def start(
        self,
        script: str,
        timeout: int = 120,
        *,
        exec_id: str | None = None,
    ) -> Job:
        """Start a command and return a handle without waiting.

        :meth:`run` is start + wait — use this when you want to stream, cancel,
        or reconnect. Same guest job, same timeout ceiling (24 hours).

        ⚠ You mint ``exec_id`` (a UUID is generated if you omit it). If the
        start response is lost, poll that id rather than starting again.
        Re-sending the same id is a 409, not a second run.

        ⚠ If this raises :class:`~sistemo.errors.ExecStartUnconfirmed`, the
        command **may be running**. The exception carries ``exec_id``; poll it
        with :meth:`job` rather than starting over.
        """
        from .errors import NotFoundError

        eid = exec_id or str(uuid.uuid4())
        try:
            rec = self._client.request(
                "POST",
                f"/v1/machines/{self.id}/execs",
                {"exec_id": eid, "script": script, "timeout_sec": timeout},
            )
        except APIError as e:
            if e.status == 409:
                try:
                    rec = self._client.request(
                        "GET", f"/v1/machines/{self.id}/execs/{eid}"
                    )
                except NotFoundError:
                    raise e from None
            else:
                raise
        return Job(self, rec)

    def job(self, exec_id: str) -> Job:
        """Re-attach to a job by id — after a crash, or after an unconfirmed start."""
        rec = self._client.request("GET", f"/v1/machines/{self.id}/execs/{exec_id}")
        return Job(self, rec)

    def jobs(self, limit: int = 50) -> list[Job]:
        """List this sandbox's async jobs, newest first."""
        out = self._client.request(
            "GET", f"/v1/machines/{self.id}/execs?limit={int(limit)}"
        )
        return [Job(self, r) for r in (out.get("execs") or [])]

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

"""The Sandbox abstraction — a real isolated Firecracker microVM."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from ._client import Client
from .errors import APIError


@dataclass
class ExecResult:
    """The result of running a command in a sandbox."""

    exit_code: int
    stdout: str
    stderr: str

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

    def run(self, script: str, timeout: int = 30) -> ExecResult:
        """Run a shell command/script inside the sandbox and return its output.

        Connection drops may be retried by the client. HTTP 5xx/429 are **not**
        retried for exec (no Idempotency-Key — replaying would re-run the script).
        """
        out = self._client.request(
            "POST",
            f"/v1/machines/{self.id}/exec",
            {"script": script, "timeout_sec": timeout},
        )
        return ExecResult(
            exit_code=int(out.get("exit_code", -1)),
            stdout=out.get("stdout", "") or "",
            stderr=out.get("stderr", "") or "",
        )

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

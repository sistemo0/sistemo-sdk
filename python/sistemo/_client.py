"""Thin HTTP client for the Sistemo control-plane API (stdlib only, no deps)."""

from __future__ import annotations

import json
import os
import random
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Mapping

from ._version import __version__
from .errors import APIError, SistemoError, api_error

DEFAULT_BASE_URL = "https://api.sistemo.io"

# Sent on every request. A real User-Agent is required: the default urllib UA
# (``Python-urllib/x.y``) is blocked by the production WAF (Cloudflare → 403).
USER_AGENT = f"sistemo-python/{__version__}"

# Transient HTTP statuses worth retrying (with care — see request()).
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class Client:
    """Low-level API client. Most users want :class:`sistemo.Sandbox` instead.

    The API key is read from ``api_key=`` or the ``SISTEMO_API_KEY`` env var.
    The base URL is ``base_url=`` or ``SISTEMO_BASE_URL`` (default production).

    Retries (default max 3 attempts): connection failures always; HTTP
    408/429/5xx only when safe (GET/DELETE, or POST/PUT/PATCH with an
    ``Idempotency-Key``). Exec without an idempotency key is **not** retried
    on HTTP errors (would re-run the guest script).
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 120.0,
        *,
        max_retries: int = 2,
        retry_backoff: float = 0.4,
        retry_max_backoff: float = 8.0,
    ):
        self.api_key = api_key or os.environ.get("SISTEMO_API_KEY")
        if not self.api_key:
            raise SistemoError(
                "no API key — pass api_key= or set the SISTEMO_API_KEY environment variable"
            )
        self.base_url = (
            base_url or os.environ.get("SISTEMO_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.timeout = timeout
        self.max_retries = max(0, max_retries)
        self.retry_backoff = retry_backoff
        self.retry_max_backoff = retry_max_backoff

    def request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        idempotency_key: str | None = None,
        max_retries: int | None = None,
    ) -> Any:
        """HTTP JSON request with optional retries.

        Pass ``idempotency_key`` for create (and other POSTs you may safely
        retry). Pass ``max_retries=0`` to disable retries for one call (e.g. exec).
        """
        retries = self.max_retries if max_retries is None else max(0, max_retries)
        method_u = method.upper()
        last_err: BaseException | None = None

        for attempt in range(retries + 1):
            try:
                return self._once(
                    method_u, path, body, headers=headers, idempotency_key=idempotency_key
                )
            except APIError as e:
                last_err = e
                if attempt >= retries or not self._should_retry_http(
                    method_u, e.status, idempotency_key
                ):
                    raise
                self._sleep(attempt, e)
            except SistemoError as e:
                # connection / timeout
                last_err = e
                if attempt >= retries:
                    raise
                self._sleep(attempt, None)

        assert last_err is not None
        raise last_err

    def _once(
        self,
        method: str,
        path: str,
        body: dict | None,
        *,
        headers: Mapping[str, str] | None,
        idempotency_key: str | None,
    ) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", USER_AGENT)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if idempotency_key:
            req.add_header("Idempotency-Key", idempotency_key)
        if headers:
            for k, v in headers.items():
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read()
            detail, code, parsed = "request failed", None, None
            try:
                parsed = json.loads(raw)
                # Some errors use `error` rather than `detail` (an unconfirmed
                # exec start is the live example), so fall back rather than
                # reporting "request failed" over a message that was right there.
                detail = parsed.get("detail") or parsed.get("error") or detail
                code = parsed.get("code")
            except Exception:
                parsed = None
            raise api_error(
                e.code, detail, code, parsed if isinstance(parsed, dict) else None
            ) from None
        except urllib.error.URLError as e:
            raise SistemoError(f"connection error: {e.reason}") from e
        except (socket.timeout, TimeoutError) as e:
            # ⚠ socket.timeout is only an alias of TimeoutError from Python 3.10.
            # On the 3.8/3.9 floor this package declares, a READ timeout raises
            # socket.timeout directly — urlopen does not wrap it in URLError —
            # so catching TimeoutError alone let a raw stdlib exception escape
            # to the caller and skipped the connection-retry path entirely.
            raise SistemoError(f"connection error: timeout ({e})") from e

    def _should_retry_http(
        self, method: str, status: int, idempotency_key: str | None
    ) -> bool:
        if status not in _RETRY_STATUSES:
            return False
        # Safe methods + DELETE (destroy is idempotent enough for retry).
        if method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return True
        # Mutating methods only when the caller opted into idempotency.
        return bool(idempotency_key)

    def _sleep(self, attempt: int, err: APIError | None) -> None:
        # Exponential backoff + jitter. Honour Retry-After seconds if present later.
        base = min(self.retry_max_backoff, self.retry_backoff * (2**attempt))
        delay = base * (0.5 + random.random())  # full jitter around [0.5x, 1.5x]
        if err is not None and err.status == 429:
            delay = max(delay, min(self.retry_max_backoff, base))
        time.sleep(delay)

    # --- volumes (incl. detachable root volumes) ---------------------------

    def list_volumes(self) -> list[dict]:
        """List the account's volumes (root and data)."""
        # API may return "volumes": null when empty — never hand back None.
        return self.request("GET", "/v1/volumes").get("volumes") or []

    def create_volume(self, name: str, size_gb: int) -> dict:
        """Create a standalone data volume."""
        return self.request(
            "POST",
            "/v1/volumes",
            {"name": name, "size_gb": size_gb},
            idempotency_key=str(uuid.uuid4()),
        )

    def attach_volume(self, machine_id: str, volume_id: str) -> dict:
        """Attach a volume (root or data) to a STOPPED machine."""
        return self.request(
            "POST",
            f"/v1/machines/{machine_id}/volume/attach",
            {"volume_id": volume_id},
            idempotency_key=str(uuid.uuid4()),
        )

    def detach_volume(self, machine_id: str, volume_id: str) -> dict:
        """Detach a volume from a STOPPED machine. A detached root volume's disk
        is preserved and can be re-attached or booted onto a new sandbox."""
        return self.request(
            "POST",
            f"/v1/machines/{machine_id}/volume/detach",
            {"volume_id": volume_id},
            idempotency_key=str(uuid.uuid4()),
        )

    def delete_volume(self, volume_id: str) -> dict:
        """Delete a detached volume."""
        return self.request("DELETE", f"/v1/volumes/{volume_id}")

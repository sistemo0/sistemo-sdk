"""Thin HTTP client for the Sistemo control-plane API (stdlib only, no deps)."""

from __future__ import annotations

import http.client
import json
import os
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator, Mapping

from ._version import __version__
from .errors import APIError, InvalidArgumentError, SistemoError, api_error

# Honour Retry-After, but never sleep more than a minute on a single retry.
_RETRY_AFTER_CAP = 60.0


def parse_retry_after(value: str | None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP-date), capped at 60.

    ``None`` when the header is missing or unparseable. A date in the past is 0.
    """
    if not value:
        return None
    text = value.strip()
    try:
        secs = int(text)
    except ValueError:
        secs = None
    if secs is not None:
        if secs < 0:
            return None
        return float(min(secs, int(_RETRY_AFTER_CAP)))
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delay = (dt - datetime.now(timezone.utc)).total_seconds()
        if delay < 0:
            return 0.0
        return float(min(delay, _RETRY_AFTER_CAP))
    except (TypeError, ValueError, OverflowError):
        return None


def retry_delay(
    attempt: int,
    backoff: float,
    max_backoff: float,
    err: APIError | None = None,
    rng=random.random,
) -> float:
    """Seconds to wait before the next attempt. Tests pass ``rng`` so they do not sleep."""
    base = min(max_backoff, backoff * (2**attempt))
    delay = base * (0.5 + rng())
    # Retry-After is honoured on a 503 as well as a 429: the API sends it on every
    # retryable 503 (a busy host, an unconfirmed answer), and ignoring it there
    # re-asked a host that had just said "not yet".
    if err is not None and err.status == 429 and err.retry_after is None:
        delay = max(delay, min(max_backoff, base))
    elif err is not None and err.status in (429, 503) and err.retry_after is not None:
        # Spread clients over up to half as long again as Retry-After: every
        # caller refused by the same limit is told the same number, and
        # waiting exactly that long brings them all back in the same instant
        # (audit Z5 — 3 of 20 parallel creates gave up that way).
        spread = err.retry_after * (1 + 0.5 * rng())
        delay = min(max(delay, spread), _RETRY_AFTER_CAP)
    return delay

DEFAULT_BASE_URL = "https://api.sistemo.io"

# Sent on every request. A real User-Agent is required: the default urllib UA
# (``Python-urllib/x.y``) is blocked by the production WAF (Cloudflare → 403).
USER_AGENT = f"sistemo-python/{__version__}"

# Transient HTTP statuses worth retrying (with care — see request()).
_RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504})


class Page(list):
    """One page of a keyset-paginated list.

    A list subclass so ``page[0]``, ``for item in page``, and ``== []`` keep
    working. ``next_cursor`` is ``None`` on the last page; pass it back as
    ``cursor=`` for the next one.
    """

    def __init__(self, items=(), next_cursor: str | None = None):
        super().__init__(items)
        self.next_cursor = next_cursor

    def __repr__(self) -> str:  # pragma: no cover
        return f"Page({list.__repr__(self)}, next_cursor={self.next_cursor!r})"


def _json_object(raw: bytes) -> Any:
    """Parse a 2xx JSON object. Empty body → {}. Anything else is a connection-class error.

    ⚠ ``json.loads`` on HTML (a proxy 200, a truncated body that still parsed as
    bytes) used to leak ``JSONDecodeError`` — not a :class:`SistemoError`, and
    GET did not retry. A 200 whose body is ``null`` or an array is the same
    class of lie: this API's 2xx bodies are objects.
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SistemoError(f"invalid JSON response: {e}") from e
    if not isinstance(parsed, dict):
        raise SistemoError("invalid JSON response: expected an object")
    return parsed


def _qs(path: str, **params: Any) -> str:
    """Append ``limit`` / ``cursor`` (and any other query) when they are set."""
    pairs = []
    for k, v in params.items():
        if v is None or v == "":
            continue
        pairs.append((k, str(v)))
    if not pairs:
        return path
    return path + "?" + urllib.parse.urlencode(pairs)


class Client:
    """Low-level API client. Most users want :class:`sistemo.Sandbox` instead.

    The API key is read from ``api_key=`` or the ``SISTEMO_API_KEY`` env var.
    The base URL is ``base_url=`` or ``SISTEMO_BASE_URL`` (default production).

    Retries (default max 3 attempts) only when replaying cannot start a
    second side effect: GET/HEAD/OPTIONS/DELETE, or POST/PUT/PATCH with an
    ``Idempotency-Key``. Connection failures and HTTP 408/429/5xx follow
    that same rule. Exec without an idempotency key is **not** retried
    (would re-run the guest script).
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
        self.last_request_id: str | None = None

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
        _check_idempotency_key(idempotency_key)
        last_err: BaseException | None = None
        hdrs = dict(headers or {})
        req_id = None
        for k, v in hdrs.items():
            if k.lower() == "x-request-id" and v:
                req_id = v
                break
        if not req_id:
            req_id = str(uuid.uuid4())
            hdrs["X-Request-Id"] = req_id
        self.last_request_id = req_id

        for attempt in range(retries + 1):
            try:
                return self._once(
                    method_u, path, body, headers=hdrs, idempotency_key=idempotency_key
                )
            except APIError as e:
                last_err = e
                if attempt >= retries or not self._should_retry_http(
                    method_u, e.status, idempotency_key
                ):
                    raise
                self._sleep(attempt, e)
            except SistemoError as e:
                # connection / timeout / invalid JSON. Same safety rule as
                # HTTP retries: a POST without Idempotency-Key must not replay.
                last_err = e
                if attempt >= retries or not self._retry_safe(
                    method_u, idempotency_key
                ):
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
                return _json_object(raw)
        except urllib.error.HTTPError as e:
            # ⚠ e.read() can IncompleteRead the same way resp.read() can.
            # Leaving it uncaught leaked a stdlib type and skipped retry on 5xx.
            try:
                raw = e.read()
            except (http.client.HTTPException, ConnectionError, OSError):
                raw = b""
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
            retry_after = parse_retry_after(
                e.headers.get("Retry-After") if e.headers else None
            )
            request_id = None
            if isinstance(parsed, dict):
                request_id = parsed.get("request_id")
            if not request_id and e.headers:
                request_id = e.headers.get("X-Request-Id")
            if not request_id:
                request_id = self.last_request_id
            raise api_error(
                e.code,
                detail,
                code,
                parsed if isinstance(parsed, dict) else None,
                retry_after=retry_after,
                request_id=str(request_id) if request_id else None,
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
        except (http.client.HTTPException, ConnectionError) as e:
            # ⚠ RemoteDisconnected / IncompleteRead are HTTPException, not
            # URLError. They fire when the peer closes after headers (or with
            # no response at all) — urlopen can succeed and then resp.read()
            # raises. Leaving them uncaught leaked a stdlib type and skipped
            # the GET retry path.
            raise SistemoError(f"connection error: {e}") from e

    def _retry_safe(self, method: str, idempotency_key: str | None) -> bool:
        """Whether replaying this call cannot start a second side effect.

        ⚠ POST /execs has no Idempotency-Key. A dropped connection after the
        guest accepted the start would re-run the script if we retried. The
        caller-minted exec_id makes a replay a 409, which is safe — and still
        contradicts "exec starts are never retried". GET/HEAD/OPTIONS/DELETE
        and POSTs that opted into idempotency may retry.
        """
        if method in ("GET", "HEAD", "OPTIONS", "DELETE"):
            return True
        return bool(idempotency_key)

    def _should_retry_http(
        self, method: str, status: int, idempotency_key: str | None
    ) -> bool:
        if status not in _RETRY_STATUSES:
            return False
        return self._retry_safe(method, idempotency_key)

    def _sleep(self, attempt: int, err: APIError | None) -> None:
        time.sleep(
            retry_delay(attempt, self.retry_backoff, self.retry_max_backoff, err)
        )

    # --- volumes (incl. detachable root volumes) ---------------------------

    def list_volumes(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> Page:
        """List the account's volumes (root and data).

        Returns a :class:`Page`. Index it like a list; ``next_cursor`` is
        ``None`` on the last page. Walk every page with :meth:`iter_volumes`.
        """
        # API may return "volumes": null when empty — never hand back None.
        out = self.request("GET", _qs("/v1/volumes", cursor=cursor, limit=limit))
        return Page(out.get("volumes") or [], out.get("next_cursor"))

    def iter_volumes(self, *, limit: int | None = None) -> Iterator[dict]:
        """Yield every volume, following ``next_cursor`` until the last page."""
        yield from self._iter_pages(self.list_volumes, limit=limit)

    def list_machines(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> Page:
        """List the account's machines. Same paging contract as :meth:`list_volumes`."""
        out = self.request("GET", _qs("/v1/machines", cursor=cursor, limit=limit))
        return Page(out.get("machines") or [], out.get("next_cursor"))

    def iter_machines(self, *, limit: int | None = None) -> Iterator[dict]:
        """Yield every machine, following ``next_cursor`` until the last page."""
        yield from self._iter_pages(self.list_machines, limit=limit)

    def _iter_pages(self, fetch, *, limit: int | None = None) -> Iterator[dict]:
        cursor = None
        while True:
            page = fetch(cursor=cursor, limit=limit)
            yield from page
            nxt = page.next_cursor
            # Stop on last page, and if a cursor ever repeats — that is a
            # broken page, not a reason to loop forever.
            if not nxt or nxt == cursor:
                return
            cursor = nxt

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

    def list_secrets(self) -> list:
        """The account's secrets — names, custody, hosts and how many machines
        each is bound to (``GET /v1/secrets``). Never a value: values are
        write-only, and secrets are created in the dashboard."""
        rec = self.request("GET", "/v1/secrets")
        return list((rec or {}).get("secrets") or [])

    def get_quotas(self) -> dict:
        """Account limits and current usage (``GET /v1/quotas``).

        The body is returned as the API sent it (``tier``, ``quotas``,
        ``per_machine``). Unknown fields are kept.
        """
        return self.request("GET", "/v1/quotas")

    def list_images(self) -> list:
        """The image catalog (``GET /v1/images``).

        Each item is one catalog image. Pass its ``name`` as ``stack`` when
        creating a sandbox. An empty catalog is ``[]``, never ``None``.
        """
        out = self.request("GET", "/v1/images")
        return list(out.get("images") or [])

    def list_snapshots(
        self, *, cursor: str | None = None, limit: int | None = None
    ) -> Page:
        """List the account's snapshots. Same paging contract as :meth:`list_volumes`."""
        out = self.request("GET", _qs("/v1/snapshots", cursor=cursor, limit=limit))
        return Page(out.get("snapshots") or [], out.get("next_cursor"))

    def iter_snapshots(self, *, limit: int | None = None) -> Iterator[dict]:
        """Yield every snapshot, following ``next_cursor`` until the last page."""
        yield from self._iter_pages(self.list_snapshots, limit=limit)

    def restore_snapshot(self, snapshot_id: str) -> dict:
        """Roll a volume back to this snapshot (``POST /v1/snapshots/{id}/restore``)."""
        return self.request("POST", f"/v1/snapshots/{snapshot_id}/restore")

    def delete_snapshot(self, snapshot_id: str) -> dict:
        """Delete a snapshot (``DELETE /v1/snapshots/{id}``)."""
        return self.request("DELETE", f"/v1/snapshots/{snapshot_id}")


# The API refuses an Idempotency-Key over 255 printable ASCII characters. A
# longer one never even reaches it: proxies answer an HTML 400 from 8 KiB and a
# CDN 520 from ~16 KiB — a 5xx this client would RETRY although it can never
# succeed. So it is refused here, before anything is sent.
MAX_IDEMPOTENCY_KEY_LEN = 255


def _check_idempotency_key(key: str | None) -> None:
    if key is None:
        return
    # ⚠ "" used to pass this check and then be dropped (the header is only sent
    # for a non-empty key), so a caller who meant to make a create retry-safe
    # silently was not. The JS SDK already refused it.
    if len(key) == 0 or len(key) > MAX_IDEMPOTENCY_KEY_LEN or not all(0x21 <= ord(c) <= 0x7E for c in key):
        raise InvalidArgumentError(
            f"idempotency_key must be 1-{MAX_IDEMPOTENCY_KEY_LEN} printable ASCII characters "
            f"with no spaces (got {len(key)}) — a UUID is ideal"
        )

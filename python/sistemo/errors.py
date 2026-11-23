"""Exception types raised by the Sistemo SDK."""

from __future__ import annotations


class SistemoError(Exception):
    """Base class for every error raised by this SDK."""


class InvalidArgumentError(SistemoError, ValueError):
    """A value the SDK refuses before sending anything (the API would refuse it).

    It is a ``SistemoError`` like everything else this SDK raises — the JS SDK
    throws ``SistemoError`` for the same cases — and also a ``ValueError``, so
    code written against 0.2.1/0.2.2's plain ``ValueError`` keeps working.
    """


class APIError(SistemoError):
    """The API returned a non-2xx response."""

    def __init__(
        self,
        status: int,
        detail: str,
        code: str | None = None,
        body: dict | None = None,
        *,
        retry_after: float | None = None,
        request_id: str | None = None,
    ):
        self.status = status
        self.detail = detail
        self.code = code
        #: The parsed response body, when there was one.
        #:
        #: Some errors carry a field the caller genuinely needs — a failed
        #: ``start()`` returns the ``exec_id`` of a command that may well be
        #: running, and throwing that away would leave it unaddressable.
        self.body = body or {}
        #: Seconds from ``Retry-After``, capped at 60. ``None`` when the header
        #: was absent or unparseable.
        self.retry_after = retry_after
        self._request_id = request_id
        #: Whether sending the identical request again later can succeed without
        #: changing anything (from the API; ``None`` when the API did not say).
        raw_retry = self.body.get("retryable")
        self.retryable: bool | None = None if raw_retry is None else bool(raw_retry)
        #: The docs entry for :attr:`code` — what it means and what to do.
        doc = self.body.get("doc_url")
        self.doc_url: str | None = str(doc) if doc else None
        msg = f"[{status}] {detail}"
        if code:
            msg += f" ({code})"
        super().__init__(msg)

    @property
    def request_id(self) -> str | None:
        """Correlation id for this call (body, then the ``X-Request-Id`` we sent)."""
        if self._request_id:
            return self._request_id
        rid = self.body.get("request_id")
        return str(rid) if rid else None


class AuthError(APIError):
    """401/403 — the API key is missing, invalid, revoked, or lacks the scope."""


class QuotaExceededError(APIError):
    """403 `quota_exceeded` — this request would exceed an account limit.

    Deliberately NOT an :class:`AuthError`, even though both arrive as 403. The
    two need opposite handling and conflating them sends callers to the wrong
    place entirely:

    * :class:`AuthError` — the credential cannot do this. Retrying never helps;
      fix the key or its scope.
    * ``QuotaExceededError`` — the credential is fine and the request is valid.
      **Retrying succeeds once the resource is freed** (stop or destroy a
      machine, delete a volume) or the limit is raised.

    :attr:`detail` names the dimension that bound and how much of it is in use.
    ``GET /v1/quotas`` reports every limit alongside current usage.

    A message mentioning "fleet limit" is a rarer variant: a fleet-wide ceiling
    rather than your plan, which topping up will not lift.
    """


class InsufficientBalanceError(APIError):
    """402 `insufficient_balance` — the prepaid wallet cannot cover this.

    Distinct from :class:`QuotaExceededError` (free a resource and retry) and
    from :class:`AuthError` (the key is fine). Top up; freeing machines does
    not help. Anything that creates ongoing cost — a machine create or start,
    a volume, a snapshot — is refused this way.
    """


class NotFoundError(APIError):
    """404 — the resource does not exist (or isn't yours)."""


class GoneError(APIError):
    """410 — it existed and is no longer retained.

    Deliberately NOT a :class:`NotFoundError`. Telling a caller their job never
    existed, when in fact its record or output aged out, invites them to re-run
    work that already ran.
    """


class AsyncExecUnsupportedError(APIError):
    """501 — this machine's image predates async exec.

    **Do not retry.** The in-guest agent is baked into the image, so the answer
    cannot change until the image is rebuilt (or the agent is updated in place).
    This is deliberately distinct from a 502/503, which mean "ask again".
    """


class ExecStartUnconfirmed(APIError):
    """503 — we could not confirm the command started, and it may be running.

    **This is not a failure.** The most dangerous thing a caller can do here is
    treat it as one and start the command again.

    :attr:`exec_id` is the handle. Poll it::

        try:
            job = sb.start("./deploy.sh")
        except ExecStartUnconfirmed as e:
            job = sb.job(e.exec_id)   # find out what actually happened
            job.wait()
    """

    @property
    def exec_id(self) -> str:
        return str(self.body.get("exec_id", "") or "")


class RateLimitError(APIError):
    """429 — slow down."""


class TooManyExecsError(APIError):
    """409 `too_many_execs` — this machine already has 16 commands running.

    Wait for one to finish. Minting a new ``exec_id`` does not help; that is
    :class:`APIError` with ``exec_id_in_use``, which means "this id is taken".
    """


class ProcessLimitError(APIError):
    """409 `machine_process_limit` — the guest's process cap is full."""


class DiskFullError(APIError):
    """409 `machine_disk_full` — the guest has no space left to start this."""


class OutOfMemoryError(APIError):
    """409 `machine_out_of_memory` — the guest refused this for lack of RAM."""


class MachineNotRunningError(APIError):
    """409 `machine_not_running` — the machine is stopped or destroyed.

    Raised by ``start()``. ``wait()`` / ``run()`` on a job that was already
    started map this to ``lost`` instead: the command may have run, and we
    can no longer say.
    """


class MachineTimeout(SistemoError):
    """``Sandbox.wait()`` reached its deadline before the machine was running.

    The machine is unchanged. This is the caller's deadline, not a failure
    reported by the API.
    """


def api_error(
    status: int,
    detail: str,
    code: str | None = None,
    body: dict | None = None,
    *,
    retry_after: float | None = None,
    request_id: str | None = None,
) -> APIError:
    """Map an HTTP status to the most specific APIError subclass."""
    kw = {"retry_after": retry_after, "request_id": request_id}
    # 403 is two different things, told apart by `code` — never by the status.
    if status == 403 and code == "quota_exceeded":
        return QuotaExceededError(status, detail, code, body, **kw)
    if status == 402:
        return InsufficientBalanceError(status, detail, code, body, **kw)
    if status in (401, 403):
        return AuthError(status, detail, code, body, **kw)
    if status == 404:
        return NotFoundError(status, detail, code, body, **kw)
    if status == 410:
        return GoneError(status, detail, code, body, **kw)
    if status == 429:
        return RateLimitError(status, detail, code, body, **kw)
    if status == 409:
        if code == "too_many_execs":
            return TooManyExecsError(status, detail, code, body, **kw)
        if code == "machine_process_limit":
            return ProcessLimitError(status, detail, code, body, **kw)
        if code == "machine_disk_full":
            return DiskFullError(status, detail, code, body, **kw)
        if code == "machine_out_of_memory":
            return OutOfMemoryError(status, detail, code, body, **kw)
        if code == "machine_not_running":
            return MachineNotRunningError(status, detail, code, body, **kw)
        # exec_id_in_use / conflict: start() recovers those by GET.
    if status == 501:
        return AsyncExecUnsupportedError(status, detail, code, body, **kw)
    # ⚠ Only a 503 that NAMES an exec is the unconfirmed-start case. A plain 503
    # (no host available, say) is an ordinary retryable error and must not be
    # dressed up as a command that might be running.
    if status == 503 and body and body.get("exec_id"):
        return ExecStartUnconfirmed(status, detail, code, body, **kw)
    return APIError(status, detail, code, body, **kw)

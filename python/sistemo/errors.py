"""Exception types raised by the Sistemo SDK."""

from __future__ import annotations


class SistemoError(Exception):
    """Base class for every error raised by this SDK."""


class APIError(SistemoError):
    """The API returned a non-2xx response."""

    def __init__(
        self,
        status: int,
        detail: str,
        code: str | None = None,
        body: dict | None = None,
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
        msg = f"[{status}] {detail}"
        if code:
            msg += f" ({code})"
        super().__init__(msg)


class AuthError(APIError):
    """401/403 — the API key is missing, invalid, revoked, or lacks the scope."""


class QuotaExceededError(APIError):
    """403 `quota_exceeded` — an account limit was reached.

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


def api_error(
    status: int, detail: str, code: str | None = None, body: dict | None = None
) -> APIError:
    """Map an HTTP status to the most specific APIError subclass."""
    # 403 is two different things, told apart by `code` — never by the status.
    if status == 403 and code == "quota_exceeded":
        return QuotaExceededError(status, detail, code, body)
    if status in (401, 403):
        return AuthError(status, detail, code, body)
    if status == 404:
        return NotFoundError(status, detail, code, body)
    if status == 410:
        return GoneError(status, detail, code, body)
    if status == 429:
        return RateLimitError(status, detail, code, body)
    if status == 501:
        return AsyncExecUnsupportedError(status, detail, code, body)
    # ⚠ Only a 503 that NAMES an exec is the unconfirmed-start case. A plain 503
    # (no host available, say) is an ordinary retryable error and must not be
    # dressed up as a command that might be running.
    if status == 503 and body and body.get("exec_id"):
        return ExecStartUnconfirmed(status, detail, code, body)
    return APIError(status, detail, code, body)

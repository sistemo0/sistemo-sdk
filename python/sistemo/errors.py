"""Exception types raised by the Sistemo SDK."""

from __future__ import annotations


class SistemoError(Exception):
    """Base class for every error raised by this SDK."""


class APIError(SistemoError):
    """The API returned a non-2xx response."""

    def __init__(self, status: int, detail: str, code: str | None = None):
        self.status = status
        self.detail = detail
        self.code = code
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


class RateLimitError(APIError):
    """429 — slow down."""


def api_error(status: int, detail: str, code: str | None = None) -> APIError:
    """Map an HTTP status to the most specific APIError subclass."""
    # 403 is two different things, told apart by `code` — never by the status.
    if status == 403 and code == "quota_exceeded":
        return QuotaExceededError(status, detail, code)
    if status in (401, 403):
        return AuthError(status, detail, code)
    if status == 404:
        return NotFoundError(status, detail, code)
    if status == 429:
        return RateLimitError(status, detail, code)
    return APIError(status, detail, code)

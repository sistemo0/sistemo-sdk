"""Sistemo — run AI agents and untrusted code in real isolated microVMs.

Quickstart::

    from sistemo import Sandbox

    with Sandbox() as sb:               # reads SISTEMO_API_KEY
        result = sb.run("python3 -c 'print(2 + 2)'")
        print(result.stdout, result.exit_code)

For anything longer than a couple of minutes, start it and keep the handle::

    with Sandbox() as sb:
        job = sb.start("npm ci && npm run build", timeout=3600)
        for chunk in job.stream():
            print(chunk, end="")
        print(job.wait().exit_code)
"""

from ._client import Client
from ._version import __version__
from .errors import (
    APIError,
    AsyncExecUnsupportedError,
    AuthError,
    ExecStartUnconfirmed,
    GoneError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    SistemoError,
)
from .jobs import Job, JobTimeout
from .sandbox import ExecResult, Sandbox

__all__ = [
    "Sandbox",
    "ExecResult",
    "Job",
    "JobTimeout",
    "Client",
    "SistemoError",
    "APIError",
    "AuthError",
    "QuotaExceededError",
    "NotFoundError",
    "GoneError",
    "AsyncExecUnsupportedError",
    "ExecStartUnconfirmed",
    "RateLimitError",
    "__version__",
]

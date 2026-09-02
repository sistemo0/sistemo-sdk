"""Sistemo — run AI agents and untrusted code in real isolated microVMs.

Quickstart::

    from sistemo import Sandbox

    with Sandbox() as sb:               # reads SISTEMO_API_KEY
        result = sb.run("python -c 'print(2 + 2)'")
        print(result.stdout, result.exit_code)
"""

from ._client import Client
from ._version import __version__
from .errors import (
    APIError,
    AuthError,
    NotFoundError,
    QuotaExceededError,
    RateLimitError,
    SistemoError,
)
from .sandbox import ExecResult, Sandbox

__all__ = [
    "Sandbox",
    "ExecResult",
    "Client",
    "SistemoError",
    "APIError",
    "AuthError",
    "QuotaExceededError",
    "NotFoundError",
    "RateLimitError",
    "__version__",
]

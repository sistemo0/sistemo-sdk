"""The single definition of this package's version.

⚠ ONE definition on purpose. The version appears in three places that MUST
agree: this constant, ``pyproject.toml``, and the ``User-Agent`` header — and
the UA is not cosmetic, because the production WAF blocks generic library UAs,
so it is the string that has to be right for the SDK to work at all.

Before this module the three were independent literals and only two of them
were checked by CI (the release job compares the git tag to ``pyproject.toml``).
A release could therefore ship a package versioned 0.2.0 that introduced itself
to the API as ``sistemo-python/0.1.0`` — the exact "two definitions meant to
agree" drift this project keeps getting bitten by. ``_client`` now derives the
UA from here, and ``test_version_matches_pyproject`` pins the third.
"""

__version__ = "0.1.0"

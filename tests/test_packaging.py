"""Packaging invariants.

Named regression (doc 101). `iamf-sentinel-pro 0.3.1` shipped to PyPI with
`pyproject.toml` bumped and `loom/__init__.py` left at the previous
value, so the distribution and the module disagreed about their own version on
a published release — and PyPI never allows a version to be re-uploaded. The
bump was checked against the README (doc 96 §10.5) and not against the module
beside it. This test makes the pair a gate rather than a habit.
"""

from __future__ import annotations

import pytest

import loom


def test_declared_version_matches_module_version():
    from importlib.metadata import PackageNotFoundError, version
    try:
        declared = version("iamf-loom")
    except PackageNotFoundError:            # pragma: no cover
        pytest.skip("iamf-loom is not installed; nothing to compare against")
    assert loom.__version__ == declared, (
        "loom.__version__ is %r but the installed distribution is %r — "
        "bump both, or neither" % (loom.__version__, declared)
    )

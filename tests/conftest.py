"""Shared test setup.

The built-in mock engines register themselves on import. Importing them here
(everything else is loaded explicitly by the tests that need it) keeps the suite
fast while making ``create(..., "mock")`` always resolvable regardless of test
ordering.

The autouse fixture below is about hermeticity: without it, a developer with a
real key in their OS credential store gets different results than CI, and tests
that assert "no key configured" would fail on their machine only.
"""

from __future__ import annotations

import pytest

import fpst.ocr.mock  # noqa: F401 - registers ocr/mock
import fpst.translate.mock  # noqa: F401 - registers translate/mock


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch):
    """Never read the machine's real credential store or key env var.

    Tests that need a store install their own fake via the `fake_keyring`
    fixture, which is applied after this one and therefore wins.
    """
    from fpst.runtime import credentials

    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.delenv("FPST_API_KEY", raising=False)

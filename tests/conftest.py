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

import translator.ocr.mock  # noqa: F401 - registers ocr/mock
import translator.translate.baidu  # noqa: F401 - registers translate/baidu
import translator.translate.mock  # noqa: F401 - registers translate/mock


@pytest.fixture(autouse=True)
def _isolate_credentials(monkeypatch):
    """Never read the machine's real credential store or key env var.

    Tests that need a store install their own fake via the `fake_keyring`
    fixture, which is applied after this one and therefore wins.
    """
    from translator.runtime import credentials

    monkeypatch.setattr(credentials, "_keyring", lambda: None)
    monkeypatch.delenv("TRANSLATOR_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _isolate_config_path(tmp_path, monkeypatch):
    """Never let a test write to the user's real config.toml.

    A test called run_setup(config_path=None), which fell back to
    default_config_path() and overwrote the developer's real regions with an
    empty list. Every test now gets a private app-data directory.
    """
    from translator.core import config as config_mod

    data_dir = tmp_path / "appdata"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(config_mod, "app_data_dir", lambda: data_dir)
    monkeypatch.setattr(config_mod, "default_config_path", lambda: data_dir / "config.toml")
    yield data_dir


def pytest_addoption(parser):
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="also run tests that sleep in real time or load real ONNX models",
    )


def pytest_collection_modifyitems(config, items):
    """Skip `slow` tests unless --runslow is given.

    They are not slow because of bad code: they assert wall-clock behaviour
    (throttling, debounce, starvation) and therefore have to actually wait, which
    accounts for ~8 of the ~10 seconds the whole suite takes. Keeping them out of
    the default loop makes the edit-test cycle ~2 s without losing coverage --
    run `pytest --runslow` (or CI) to exercise them.
    """
    if config.getoption("--runslow"):
        return
    skip = pytest.mark.skip(reason="needs --runslow")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)

"""The packaged entry point.

Each test here corresponds to a way the app can silently fail to open, which from
the outside looks identical: double-click, nothing happens.
"""

from __future__ import annotations

from translator import frozen_entry


def test_frozen_entry_asks_for_run(monkeypatch):
    """Regression: it called main() with no argv. Frozen builds have an empty
    sys.argv[1:], so argparse matched no subcommand, printed help and returned 1
    -- the process exited before creating a window. The log held only \"exit 1\"."""
    seen: list[list[str]] = []

    monkeypatch.setattr(frozen_entry, "note", lambda text: None)
    monkeypatch.setattr(
        "translator.__main__.main", lambda argv=None: (seen.append(list(argv)), 0)[1]
    )

    assert frozen_entry.entry() == 0
    assert seen == [["run"]], "a double-clicked exe must start the tray app"


def test_frozen_entry_passes_explicit_argv_through(monkeypatch):
    seen: list[list[str]] = []
    monkeypatch.setattr(frozen_entry, "note", lambda text: None)
    monkeypatch.setattr(
        "translator.__main__.main", lambda argv=None: (seen.append(list(argv)), 0)[1]
    )
    frozen_entry.entry(["doctor"])
    assert seen == [["doctor"]]


def test_import_failure_returns_nonzero_and_reports(monkeypatch):
    reported: list[str] = []
    monkeypatch.setattr(frozen_entry, "note", lambda text: None)

    def boom(argv=None):
        raise ImportError("no Qt today")

    monkeypatch.setattr("translator.__main__.main", boom)
    monkeypatch.setattr(
        "translator.runtime.report.report_fatal",
        lambda message, detail="": reported.append(message),
    )

    assert frozen_entry.entry() == 3
    assert reported, "a crash before the window must be shown to the user"


def test_nonzero_exit_is_surfaced_not_swallowed(monkeypatch):
    """ "exit 1" alone in a log file is not a diagnosis."""
    reported: list[str] = []
    monkeypatch.setattr(frozen_entry, "note", lambda text: None)
    monkeypatch.setattr("translator.__main__.main", lambda argv=None: 1)
    monkeypatch.setattr(
        "translator.runtime.report.report_fatal",
        lambda message, detail="": reported.append(message),
    )

    assert frozen_entry.entry() == 1
    assert reported and "1" in reported[0]


def test_successful_exit_reports_nothing(monkeypatch):
    reported: list[str] = []
    monkeypatch.setattr(frozen_entry, "note", lambda text: None)
    monkeypatch.setattr("translator.__main__.main", lambda argv=None: 0)
    monkeypatch.setattr(
        "translator.runtime.report.report_fatal",
        lambda message, detail="": reported.append(message),
    )
    assert frozen_entry.entry() == 0
    assert reported == []


def test_note_writes_to_a_file(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.delenv("TEMP", raising=False)
    frozen_entry.note("hello")
    log = tmp_path / "Translator" / "logs" / "startup.log"
    assert log.exists() and "hello" in log.read_text(encoding="utf-8")


def test_note_never_raises_when_paths_are_unwritable(monkeypatch):
    monkeypatch.setenv("APPDATA", "/proc/nope")
    monkeypatch.setenv("TEMP", "/proc/nope")
    frozen_entry.note("still fine")  # must not raise

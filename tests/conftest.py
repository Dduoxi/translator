"""Shared test setup.

The built-in mock engines register themselves on import. Importing them here
(everything else is loaded explicitly by the tests that need it) keeps the suite
fast while making ``create(..., "mock")`` always resolvable regardless of test
ordering.
"""

from __future__ import annotations

import fpst.ocr.mock  # noqa: F401 - registers ocr/mock
import fpst.translate.mock  # noqa: F401 - registers translate/mock

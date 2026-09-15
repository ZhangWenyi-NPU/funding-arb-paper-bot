#!/usr/bin/env python3
"""Tests for resilient parallel fetch behavior."""

from __future__ import annotations

import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from market.parallel_fetch import run_io_parallel  # noqa: E402


def test_run_io_parallel_returns_completed_items_on_timeout():
    errors: list[tuple[str, Exception]] = []

    def _fetch(key: str) -> tuple[str, str]:
        if key == "slow":
            time.sleep(1.0)
        return key, key.upper()

    started = time.perf_counter()
    out = run_io_parallel(
        ["fast", "slow"],
        _fetch,
        max_workers=2,
        swallow_errors=True,
        timeout=0.05,
        on_error=lambda key, exc: errors.append((key, exc)),
    )
    elapsed = time.perf_counter() - started

    assert out == {"fast": "FAST"}
    assert elapsed < 0.5
    assert errors and errors[0][0] == "slow"

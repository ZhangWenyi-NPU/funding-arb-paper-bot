#!/usr/bin/env python3
"""Hermetic tests for paper-bot style historical replay."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
ROOT = SCRIPTS.parent
for p in (str(ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from backtest.backtest_paper_bot import run_paper_bot_backtest  # noqa: E402


def _snap(ts: datetime, rows: list[dict]) -> dict:
    return {
        "timestamp": ts.isoformat(),
        "_ts": ts,
        "venues": ["binance", "okx"],
        "forward": rows,
        "reverse": [],
    }


def _row(ts: datetime, base: str = "BTC", *, edge: float = 0.05, spread: float = 0.15) -> dict:
    settle = int(ts.timestamp() * 1000)
    return {
        "base": base,
        "direction": "forward",
        "long_venue": "binance",
        "short_venue": "okx",
        "long_rate_pct": 0.01,
        "short_rate_pct": 0.06,
        "spread_pct": spread,
        "fee_pct": 0.10,
        "round_trip_fee_pct": 0.20,
        "net_edge_pct": edge,
        "real_edge_pct": edge,
        "mark_spread_pct": 0.0,
        "long_interval_h": 8,
        "short_interval_h": 8,
        "long_settle_ms": settle,
        "short_settle_ms": settle,
        "settle_mismatch": False,
        "same_interval": True,
    }


def test_robot_backtest_scans_then_waits_for_consecutive_hit_before_opening():
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    snapshots = [
        _snap(t0, [_row(t0, "BTC", edge=0.05), _row(t0, "ETH", edge=0.0)]),
        _snap(t0 + timedelta(hours=8), [_row(t0 + timedelta(hours=8), "BTC", edge=0.05)]),
        _snap(t0 + timedelta(hours=16), [_row(t0 + timedelta(hours=16), "BTC", edge=0.0)]),
    ]

    result = run_paper_bot_backtest(
        snapshots,
        initial_capital=100000,
        trade_usd=5000,
        min_spread_pct=0.01,
        min_edge_pct=0.02,
        exit_edge_pct=0.02,
        consecutive_hits=2,
        max_holding_hours=999,
        basis_cost_pct=0.0,
    )

    assert result["scan_count"] == 3
    assert result["scan_journal"][0]["scan_total"] == 2
    assert result["scan_journal"][0]["ready_candidates"] == 0
    assert result["scan_journal"][1]["actions"][0]["action"] == "open"
    assert result["scan_journal"][2]["actions"][0]["action"] == "close"
    assert result["trade_count"] == 1
    assert result["synthetic_settle_windows"] > 0
    assert result["daily_logs"][0]["pairs_scanned"] == 4
    assert result["daily_logs"][0]["consecutive_ok"] == 1
    assert result["daily_logs"][0]["open_actions"] == 1


def test_robot_backtest_filters_depth_when_depth_data_exists():
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    row = _row(t0, edge=0.08)
    row["max_exec_usd"] = 100

    result = run_paper_bot_backtest(
        [_snap(t0, [row])],
        initial_capital=100000,
        trade_usd=100,
        min_spread_pct=0.01,
        min_edge_pct=0.02,
        consecutive_hits=1,
        depth_multiple=3,
    )

    assert result["open_actions"] == 0
    assert result["scan_journal"][0]["candidates_after_filter"] == 0
    assert "depth below configured multiple" in result["scan_journal"][0]["skipped"][0]["reason"]


def test_robot_backtest_opens_best_candidate_with_position_limit():
    t0 = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    snapshots = [
        _snap(
            t0,
            [
                _row(t0, "ETH", edge=0.04),
                _row(t0, "BTC", edge=0.08),
            ],
        )
    ]

    result = run_paper_bot_backtest(
        snapshots,
        initial_capital=100000,
        trade_usd=5000,
        min_spread_pct=0.01,
        min_edge_pct=0.02,
        consecutive_hits=1,
        max_concurrent_pairs=1,
        max_actions_per_run=5,
        basis_cost_pct=0.0,
    )

    assert result["open_actions"] == 1
    assert result["trade_count"] == 1
    assert result["trades"][0]["base"] == "BTC"

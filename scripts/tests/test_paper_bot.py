#!/usr/bin/env python3
"""Hermetic tests for the paper bot minimal loop."""

from __future__ import annotations

import json
import asyncio
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
ROOT = SCRIPTS.parent
for p in (str(ROOT), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from server.routes import paper_bot  # noqa: E402
from server import paper_account  # noqa: E402


class FakeResult:
    ok = True
    state = "simulated"
    position_id = "pf-BTC-okx-bybit-test"
    logs: list[str] = []

    def to_dict(self):
        return {
            "ok": self.ok,
            "state": self.state,
            "position_id": self.position_id,
            "logs": self.logs,
        }


def _row(base: str = "BTC") -> dict:
    settle = int(time.time() * 1000) + 45 * 60 * 1000
    return {
        "base": base,
        "direction": "forward",
        "long_venue": "okx",
        "short_venue": "bybit",
        "spread_pct": 0.08,
        "net_edge_pct": 0.04,
        "real_edge_pct": 0.035,
        "mark_spread_pct": 0.005,
        "long_rate_pct": 0.01,
        "short_rate_pct": 0.05,
        "long_interval_h": 8,
        "short_interval_h": 8,
        "long_settle_ms": settle,
        "short_settle_ms": settle,
        "settle_mismatch": False,
        "depth_ok": True,
        "max_exec_usd": 5000,
    }


def _patch_storage(monkeypatch, tmp_path):
    monkeypatch.setattr(paper_bot, "_CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(paper_bot, "_STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(paper_bot, "_JOURNAL_PATH", tmp_path / "journal.jsonl")
    monkeypatch.setattr(paper_account, "LEDGER_PATH", tmp_path / "ledger.jsonl")


def _patch_strategy(monkeypatch):
    monkeypatch.setattr(
        paper_bot,
        "_load_runner_config",
        lambda cfg: {
            "dry_run": True,
            "pureFuturesArbitrage": {
                "venues": ["okx", "bybit"],
                "workers": 1,
                "minSpreadPct": 0.02,
                "minNetEdgePct": 0.02,
                "exitThresholdPct": 0.01,
                "maxConcurrentPairs": 2,
                "tradeUsdPerPair": 1000,
                "maxMarkSpreadPct": 0.3,
                "allowSettleMismatch": False,
            },
        },
    )
    monkeypatch.setattr(paper_bot, "load_strategy_config", lambda: {})
    monkeypatch.setattr(paper_bot, "strategy_edge_thresholds", lambda strat: (0.02, None, None))
    monkeypatch.setattr(paper_bot, "strategy_fee_policy", lambda strat: {"mode": "auto"})
    monkeypatch.setattr(paper_bot, "effective_trade_usd", lambda trade_usd, row: trade_usd)
    monkeypatch.setattr(
        paper_bot,
        "filter_candidates_with_mismatch",
        lambda candidates, **kwargs: candidates,
    )


def test_run_once_waits_for_consecutive_hits_before_opening(tmp_path, monkeypatch):
    _patch_storage(monkeypatch, tmp_path)
    _patch_strategy(monkeypatch)
    paper_bot._save_config(
        paper_bot.PaperBotConfig(
            consecutiveHits=2,
            minSettleMinutes=10,
            maxSettleMinutes=90,
        )
    )
    monkeypatch.setattr(
        paper_bot,
        "scan_pure_futures_spreads",
        lambda **kwargs: {"forward": [_row()], "reverse": []},
    )
    monkeypatch.setattr(paper_bot, "load_pure_futures_positions", lambda: [])

    opened: list[dict] = []

    def _open(**kwargs):
        opened.append(kwargs)
        return FakeResult()

    monkeypatch.setattr(paper_bot, "open_pure_futures_pair", lambda *args, **kwargs: _open(**kwargs))
    monkeypatch.setattr(paper_bot, "close_pure_futures_pair", lambda *args, **kwargs: FakeResult())

    first = paper_bot._run_once_sync()
    assert first["ready_candidates"] == 0
    assert opened == []

    second = paper_bot._run_once_sync()
    assert second["ready_candidates"] == 1
    assert len(opened) == 1
    assert opened[0]["dry_run"] is True
    assert opened[0]["metadata"]["managed_by"] == "paper_bot"
    assert opened[0]["metadata"]["paper_margin_usd"] == 2000
    ledger = paper_account.read_account_ledger()
    assert len(ledger) == 1
    assert ledger[0]["type"] == "open"
    assert ledger[0]["position_id"] == FakeResult.position_id


def test_run_once_skips_candidates_outside_settle_window(tmp_path, monkeypatch):
    _patch_storage(monkeypatch, tmp_path)
    _patch_strategy(monkeypatch)
    paper_bot._save_config(
        paper_bot.PaperBotConfig(
            consecutiveHits=1,
            minSettleMinutes=10,
            maxSettleMinutes=90,
        )
    )
    row = _row()
    row["long_settle_ms"] = int(time.time() * 1000) + 4 * 60 * 60 * 1000
    row["short_settle_ms"] = row["long_settle_ms"]
    monkeypatch.setattr(
        paper_bot,
        "scan_pure_futures_spreads",
        lambda **kwargs: {"forward": [row], "reverse": []},
    )
    monkeypatch.setattr(paper_bot, "load_pure_futures_positions", lambda: [])
    monkeypatch.setattr(paper_bot, "open_pure_futures_pair", lambda *args, **kwargs: FakeResult())

    out = paper_bot._run_once_sync()
    assert out["candidates_after_filter"] == 0
    assert "too far from settlement" in json.dumps(out["skipped"])


def test_start_stop_auto_toggle_config(tmp_path, monkeypatch):
    _patch_storage(monkeypatch, tmp_path)
    monkeypatch.setattr(paper_bot, "_AUTO_TASK", None)
    monkeypatch.setattr(paper_bot, "_AUTO_WAKE_EVENT", None)
    monkeypatch.setattr(paper_bot, "_IMPORT_ERROR", None)

    started: list[bool] = []
    woke: list[bool] = []
    monkeypatch.setattr(paper_bot, "_ensure_auto_task", lambda: started.append(True))
    monkeypatch.setattr(paper_bot, "_wake_auto_loop", lambda: woke.append(True))

    start = asyncio.run(paper_bot.start_paper_bot_auto())
    assert start["success"] is True
    assert start["data"]["config"]["enabled"] is True
    assert paper_bot._load_config().enabled is True
    assert started == [True]
    assert woke == [True]

    stop = asyncio.run(paper_bot.stop_paper_bot_auto())
    assert stop["success"] is True
    assert stop["data"]["config"]["enabled"] is False
    assert stop["data"]["next_run_at"] is None
    assert paper_bot._load_config().enabled is False


def test_open_paper_positions_only_returns_bot_managed(monkeypatch):
    monkeypatch.setattr(
        paper_bot,
        "load_pure_futures_positions",
        lambda: [
            {
                "id": "manual",
                "status": "open",
                "dry_run": True,
                "managed_by": "manual",
            },
            {
                "id": "legacy",
                "status": "open",
                "dry_run": True,
            },
            {
                "id": "bot",
                "status": "open",
                "dry_run": True,
                "managed_by": "paper_bot",
            },
            {
                "id": "closed-bot",
                "status": "closed",
                "dry_run": True,
                "managed_by": "paper_bot",
            },
        ],
    )

    assert [p["id"] for p in paper_bot._open_paper_positions()] == ["bot"]


def test_paper_account_records_close_components(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_account, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    opened_ms = int(time.time() * 1000) - 70 * 60 * 1000
    next_settle = opened_ms + 30 * 60 * 1000
    pos = {
        "id": "pf-BTC-okx-bybit-test",
        "status": "open",
        "dry_run": True,
        "managed_by": "paper_bot",
        "base": "BTC",
        "direction": "forward",
        "long_venue": "okx",
        "short_venue": "bybit",
        "qty": 10,
        "long_price": 100,
        "short_price": 100,
        "trade_usd": 1000,
        "opened_at": opened_ms,
        "paper_open_fee_usd": 1,
        "paper_close_fee_est_usd": 1,
        "paper_long_rate_pct": 0.01,
        "paper_short_rate_pct": 0.05,
        "paper_long_interval_h": 1,
        "paper_short_interval_h": 1,
        "paper_long_next_settle_ms": next_settle,
        "paper_short_next_settle_ms": next_settle,
    }
    paper_account.record_bot_open(_row(), pos, FakeResult(), 1000)
    closed = {
        **pos,
        "status": "closed",
        "closed_at": int(time.time() * 1000),
        "close_info": {
            "long_price": 101,
            "short_price": 99,
        },
    }

    event = paper_account.record_bot_close(
        pos,
        closed,
        reason="test_close",
        edge=0.01,
        result=FakeResult(),
    )

    assert event is not None
    assert event["type"] == "close"
    assert event["price_pnl_usd"] == 20
    assert event["funding_pnl_usd"] == 0.4
    assert event["total_fee_usd"] == 2
    assert event["net_pnl_usd"] == 18.4

#!/usr/bin/env python3
"""Paper auto-bot API routes.

Minimal loop:
  scan pure-futures spreads -> close stale paper positions -> open ready paper positions.
All execution is forced to dry-run, regardless of environment variables.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.paper_account import (
    BOT_MANAGER_ID,
    build_account_snapshot,
    estimate_open_fee_usd,
    margin_required_usd,
    metadata_for_candidate,
    open_bot_positions,
    read_account_ledger,
    record_bot_close,
    record_bot_open,
    result_position_id,
)

router = APIRouter(tags=["paper-bot"])

_ROOT_DIR = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _ROOT_DIR / "scripts"
for _p in (str(_ROOT_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_DATA_DIR = _SCRIPTS_DIR / "data" / "paper-bot"
_CONFIG_PATH = _DATA_DIR / "config.json"
_STATUS_PATH = _DATA_DIR / "status.json"
_JOURNAL_PATH = _DATA_DIR / "journal.jsonl"
_PURE_FUTURES_TEMPLATE = _ROOT_DIR / "templates" / "config.pure_futures.spread.json"
_DEFAULT_BACKTEST_JSONL = _ROOT_DIR / "data" / "pure_futures_spreads.jsonl"
_BOT_MANAGER_ID = BOT_MANAGER_ID

_IMPORT_ERROR: str | None = None
try:
    from cli.scan_pure_futures_spreads import scan_pure_futures_spreads  # noqa: E402
    from core.strategy_config import (  # noqa: E402
        apply_strategy_to_pure_futures_cfg,
        load_strategy_config,
        min_edge_for_row_factory,
        strategy_edge_thresholds,
        strategy_fee_policy,
    )
    from execution.pure_futures_executor import (  # noqa: E402
        close_pure_futures_pair,
        load_pure_futures_positions,
        open_pure_futures_pair,
    )
    from execution.settle_mismatch_planner import (  # noqa: E402
        effective_trade_usd,
        filter_candidates_with_mismatch,
    )
except Exception as e:  # pragma: no cover - surfaced by status endpoint
    _IMPORT_ERROR = str(e)

_RUN_LOCK = asyncio.Lock()
_AUTO_TASK: asyncio.Task | None = None
_AUTO_WAKE_EVENT: asyncio.Event | None = None


class PaperBotConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    enabled: bool = False
    initial_balance_usdt: float = Field(100000.0, ge=0, alias="initialBalanceUsdt")
    depth_multiple: float = Field(3.0, ge=1.0, alias="depthMultiple")
    consecutive_hits: int = Field(2, ge=1, alias="consecutiveHits")
    min_settle_minutes: int = Field(10, ge=0, alias="minSettleMinutes")
    max_settle_minutes: int = Field(90, ge=1, alias="maxSettleMinutes")
    max_hold_hours: float = Field(9.0, gt=0, alias="maxHoldHours")
    max_actions_per_run: int = Field(5, ge=1, le=20, alias="maxActionsPerRun")
    active_exit_enabled: bool = Field(True, alias="activeExitEnabled")
    active_exit_confirm_minutes: int = Field(2, ge=0, alias="activeExitConfirmMinutes")
    active_exit_window_minutes: int = Field(60, ge=1, alias="activeExitWindowMinutes")
    active_exit_max_mark_spread_pct: float = Field(
        0.15, ge=0, alias="activeExitMaxMarkSpreadPct"
    )

    @model_validator(mode="after")
    def _check_settle_window(self) -> "PaperBotConfig":
        if self.max_settle_minutes <= self.min_settle_minutes:
            raise ValueError("maxSettleMinutes must be greater than minSettleMinutes")
        return self


class PaperBotBacktestRequest(BaseModel):
    jsonl_file: str | None = Field(None, description="Historical scanner JSONL file path")
    history_bases: str | None = Field(
        None, description="Backtest base assets, comma-separated, e.g. BTC,ETH,SOL"
    )
    history_base_limit: int = Field(80, ge=1, le=500, description="Auto-discovered base limit")
    history_venues: str | None = Field(None, description="Venues for history fetch")
    history_days: int = Field(90, ge=1, le=365, description="Backtest period in days")
    capital: float = Field(100000.0, gt=0, description="Initial capital")
    trade_usd: float = Field(5000.0, gt=0, description="Trade size per leg")
    min_spread: float = Field(0.08, ge=0, description="Minimum funding spread")
    min_edge_pct: float = Field(0.01, description="Minimum net/real edge")
    min_edge_1h: float | None = Field(None, description="Optional 1h pair edge threshold")
    min_edge_mismatch: float | None = Field(
        None, description="Optional cross-interval edge threshold"
    )
    exit_edge: float = Field(0.01, description="Close when edge falls below this")
    max_mark_spread_pct: float = Field(1.0, ge=0, description="Maximum mark spread")
    max_positions: int = Field(3, ge=1, le=50, description="Maximum concurrent positions")
    max_holding_hours: float = Field(9.0, gt=0, description="Maximum holding hours")
    allow_mismatch: bool = Field(False, description="Allow cross-interval opportunities")
    consecutive_hits: int = Field(2, ge=1, le=20, description="Required consecutive scans")
    min_settle_minutes: float = Field(10.0, ge=0, description="Nearest settlement minimum")
    max_settle_minutes: float = Field(90.0, gt=0, description="Farthest settlement maximum")
    max_actions_per_run: int = Field(5, ge=1, le=50, description="Open actions per scan")
    depth_multiple: float | None = Field(None, ge=1.0, description="Optional depth multiple for JSONL replays")
    basis_cost_pct: float = Field(0.05, ge=0, description="Estimated basis/slippage cost")
    active_exit_enabled: bool = Field(True, description="Enable post-funding active exits")
    active_exit_confirm_minutes: float = Field(
        2.0, ge=0, description="Wait after funding settlement before active exit"
    )
    active_exit_window_minutes: float = Field(
        60.0, ge=1, description="Post-funding active exit search window"
    )
    active_exit_max_mark_spread_pct: float = Field(
        0.15, ge=0, description="Max mark spread for active low-basis exit"
    )

    @model_validator(mode="after")
    def _check_backtest_settle_window(self) -> "PaperBotBacktestRequest":
        if self.max_settle_minutes <= self.min_settle_minutes:
            raise ValueError("max_settle_minutes must be greater than min_settle_minutes")
        return self


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
        Path(tmp).replace(path)
    except BaseException:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else dict(default)
    except Exception:
        return dict(default)


def _load_config() -> PaperBotConfig:
    return PaperBotConfig.model_validate(_load_json(_CONFIG_PATH, {}))


def _save_config(cfg: PaperBotConfig) -> None:
    _atomic_write_json(_CONFIG_PATH, cfg.model_dump(by_alias=True))


def _default_status() -> dict[str, Any]:
    return {
        "running": False,
        "last_run_at": None,
        "last_finished_at": None,
        "last_error": None,
        "last_summary": None,
        "hit_counts": {},
        "total_runs": 0,
        "next_run_at": None,
        "auto_started_at": None,
        "auto_stopped_at": None,
    }


def _load_status() -> dict[str, Any]:
    status = _default_status()
    status.update(_load_json(_STATUS_PATH, {}))
    if not isinstance(status.get("hit_counts"), dict):
        status["hit_counts"] = {}
    return status


def _save_status(status: dict[str, Any]) -> None:
    _atomic_write_json(_STATUS_PATH, status)


def _append_journal(payload: dict[str, Any]) -> None:
    _JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _JOURNAL_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _read_recent_journal(limit: int) -> list[dict[str, Any]]:
    if not _JOURNAL_PATH.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in _JOURNAL_PATH.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _load_runner_config(bot_cfg: PaperBotConfig) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    if _PURE_FUTURES_TEMPLATE.exists():
        try:
            cfg = json.loads(_PURE_FUTURES_TEMPLATE.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg = apply_strategy_to_pure_futures_cfg(cfg)
    cfg["dry_run"] = True
    pfa = dict(cfg.get("pureFuturesArbitrage") or {})
    pfa["depthCheckEnabled"] = True
    pfa["depthMinMultiple"] = float(bot_cfg.depth_multiple)
    pfa["depthCheckFailOpen"] = False
    cfg["pureFuturesArbitrage"] = pfa
    return cfg


def _candidate_key(row: dict[str, Any]) -> str:
    return ":".join(
        [
            str(row.get("base", "")).upper(),
            str(row.get("direction", "forward")).lower(),
            str(row.get("long_venue", "")).lower(),
            str(row.get("short_venue", "")).lower(),
        ]
    )


def _position_key(pos: dict[str, Any]) -> str:
    return ":".join(
        [
            str(pos.get("base", "")).upper(),
            str(pos.get("direction", "forward")).lower(),
            str(pos.get("long_venue", "")).lower(),
            str(pos.get("short_venue", "")).lower(),
        ]
    )


def _open_paper_positions() -> list[dict[str, Any]]:
    return open_bot_positions(load_pure_futures_positions())


def _paper_position_by_id(position_id: str) -> dict[str, Any] | None:
    for pos in load_pure_futures_positions():
        if str(pos.get("id")) == str(position_id):
            return pos
    return None


def _account_snapshot(positions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    bot_cfg = _load_config()
    rows = positions if positions is not None else load_pure_futures_positions()
    return build_account_snapshot(rows, initial_balance_usdt=bot_cfg.initial_balance_usdt)


def _enriched_account_snapshot() -> dict[str, Any]:
    rows = load_pure_futures_positions()
    try:
        from server.routes.positions import _enrich_positions_with_pnl  # noqa: E402

        rows = _enrich_positions_with_pnl(rows)
    except Exception:
        pass
    return _account_snapshot(rows)


def _row_real_edge(row: dict[str, Any]) -> float:
    if row.get("real_edge_pct") is not None:
        return float(row.get("real_edge_pct") or 0.0)
    return float(row.get("net_edge_pct", 0.0) or 0.0) - abs(
        float(row.get("mark_spread_pct", 0.0) or 0.0)
    )


def _split_csv(value: str | None, *, upper: bool = False, lower: bool = False) -> list[str]:
    if not value:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in value.split(","):
        item = part.strip()
        if not item:
            continue
        if upper:
            item = item.upper()
        if lower:
            item = item.lower()
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _discover_history_bases(venues: list[str], limit: int) -> list[str]:
    """Discover a cross-venue historical universe from current public futures symbols."""
    from backtest.funding_providers import get_funding_provider  # noqa: E402
    from cli.scan_pure_futures_spreads import SYMBOL_BLACKLIST, _base_from_symbol  # noqa: E402
    from market.parallel_fetch import run_io_parallel  # noqa: E402

    def _fetch_one(venue: str) -> tuple[str, set[str]]:
        bases: set[str] = set()
        rows = get_funding_provider(venue).fetch_all("USDT")
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if not symbol.endswith("USDT"):
                continue
            base = _base_from_symbol(symbol)
            if base and base not in SYMBOL_BLACKLIST:
                bases.add(base)
        return venue, bases

    fetched = run_io_parallel(
        venues,
        _fetch_one,
        max_workers=min(8, max(1, len(venues))),
        swallow_errors=True,
        timeout=30.0,
    )
    counts: dict[str, int] = {}
    for bases in fetched.values():
        for base in bases:
            counts[base] = counts.get(base, 0) + 1

    # Cross-venue arbitrage needs at least two venues. If only one venue answered,
    # keep its symbols so the caller still gets a useful "no opportunities" replay.
    min_venues = 2 if len(fetched) >= 2 else 1
    ranked = sorted(
        (base for base, count in counts.items() if count >= min_venues),
        key=lambda base: (-counts[base], base),
    )
    return ranked[:limit]


def _load_paper_bot_backtest_snapshots(req: PaperBotBacktestRequest) -> list[dict[str, Any]]:
    """Load historical replay data for robot-style backtests."""
    venues = _split_csv(req.history_venues, lower=True) or [
        "binance",
        "bitget",
        "bybit",
        "okx",
    ]
    bases = _split_csv(req.history_bases, upper=True)
    if bases:
        from backtest.funding_history_source import fetch_history_snapshots  # noqa: E402

        return fetch_history_snapshots(venues, bases, req.history_days)

    if req.jsonl_file is None:
        bases = _discover_history_bases(venues, req.history_base_limit)
        if bases:
            from backtest.funding_history_source import fetch_history_snapshots  # noqa: E402

            req.history_bases = ",".join(bases)
            return fetch_history_snapshots(venues, bases, req.history_days)

    from backtest.backtest_pure_futures_spread import load_snapshots  # noqa: E402

    jsonl_path = Path(req.jsonl_file) if req.jsonl_file else _DEFAULT_BACKTEST_JSONL
    if not jsonl_path.is_absolute():
        jsonl_path = _ROOT_DIR / jsonl_path
    return load_snapshots(jsonl_path)


def _venues_from_pair_id(pair_id: str) -> tuple[str, str]:
    parts = str(pair_id).split(":")
    if len(parts) >= 4:
        return parts[2], parts[3]
    return "", ""


def _adapt_paper_bot_backtest_result(
    raw: dict[str, Any],
    req: PaperBotBacktestRequest,
) -> dict[str, Any]:
    trades = []
    for trade in raw.get("trades", []):
        long_v, short_v = _venues_from_pair_id(trade.get("pair_id", ""))
        amount = float(trade.get("amount_usd") or req.trade_usd)
        trades.append(
            {
                "base": trade.get("base", ""),
                "direction": trade.get("direction", ""),
                "long_venue": long_v,
                "short_venue": short_v,
                "open_time": trade.get("open_ts", ""),
                "close_time": trade.get("close_ts", ""),
                "hold_days": round(float(trade.get("holding_hours", 0)) / 24.0, 2),
                "pnl_usd": round(float(trade.get("net_pnl_pct", 0)) / 100.0 * amount, 2),
                "close_reason": trade.get("close_reason", ""),
                "amount_usd": round(amount, 2),
                "funding_pct": trade.get("total_funding_pct", 0),
                "fee_pct": trade.get("total_fee_pct", 0),
            }
        )

    result = {
        "id": f"pbot-bt-{int(time.time())}",
        "mode": "paper_bot",
        "params": req.model_dump(),
        "summary": {
            "total_pnl_usd": round(
                float(raw.get("total_return_pct", 0)) / 100.0 * req.capital, 2
            ),
            "total_pnl_pct": raw.get("total_return_pct", 0),
            "annualized_pct": raw.get("annual_return_pct", 0),
            "max_drawdown_pct": raw.get("max_drawdown_pct", 0),
            "sharpe": raw.get("sharpe_ratio", 0),
            "win_rate": float(raw.get("win_rate_pct", 0)) / 100.0,
            "total_trades": raw.get("trade_count", 0),
            "avg_hold_days": round(float(raw.get("avg_holding_hours", 0)) / 24.0, 2),
            "scan_count": raw.get("scan_count", 0),
            "avg_candidates_after_filter": raw.get("avg_candidates_after_filter", 0),
            "avg_ready_candidates": raw.get("avg_ready_candidates", 0),
            "open_actions": raw.get("open_actions", 0),
            "close_actions": raw.get("close_actions", 0),
            "final_equity": raw.get("final_equity", req.capital),
        },
        "trades": trades,
        "equity_curve": raw.get("equity_curve") or [],
        "scan_journal": raw.get("scan_journal") or [],
        "daily_logs": raw.get("daily_logs") or [],
        "data_quality": {
            "synthetic_settle_windows": raw.get("synthetic_settle_windows", 0),
            "note": (
                "Historical exchange funding data is settlement-indexed; settle-window checks "
                "are approximated for those rows."
                if raw.get("synthetic_settle_windows", 0)
                else ""
            ),
        },
        "run_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "live": True,
    }
    return result


def _remaining_minutes(ts_ms: Any) -> float | None:
    try:
        ts = int(ts_ms or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return (ts - int(time.time() * 1000)) / 60000.0


def _settle_window_ok(row: dict[str, Any], cfg: PaperBotConfig) -> tuple[bool, str]:
    minutes = [
        m
        for m in (
            _remaining_minutes(row.get("long_settle_ms")),
            _remaining_minutes(row.get("short_settle_ms")),
        )
        if m is not None
    ]
    if not minutes:
        return False, "settlement time unavailable"
    nearest = min(minutes)
    farthest = max(minutes)
    if nearest < cfg.min_settle_minutes:
        return False, f"too close to settlement: {nearest:.1f}m"
    if farthest > cfg.max_settle_minutes:
        return False, f"too far from settlement: {farthest:.1f}m"
    return True, ""


def _held_hours(pos: dict[str, Any]) -> float:
    opened = pos.get("opened_at") or pos.get("open_time")
    if not opened:
        return 0.0
    try:
        if isinstance(opened, (int, float)):
            start_ms = float(opened)
        else:
            start_ms = datetime.fromisoformat(str(opened).replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return 0.0
    return max(0.0, (time.time() * 1000 - start_ms) / 3600000.0)


def _funding_collected_state(pos: dict[str, Any], cfg: PaperBotConfig) -> dict[str, Any]:
    settle_times = []
    for key in ("paper_long_next_settle_ms", "paper_short_next_settle_ms"):
        try:
            value = int(pos.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            settle_times.append(value)

    if not settle_times:
        return {"known": False, "collected": False, "deadline_passed": False}

    now_ms = int(time.time() * 1000)
    collected_at = max(settle_times) + cfg.active_exit_confirm_minutes * 60000
    deadline_at = collected_at + cfg.active_exit_window_minutes * 60000
    return {
        "known": True,
        "collected": now_ms >= collected_at,
        "deadline_passed": now_ms >= deadline_at,
        "collected_at": collected_at,
        "deadline_at": deadline_at,
    }


def _skip(skipped: list[dict[str, Any]], row: dict[str, Any], reason: str) -> None:
    if len(skipped) >= 100:
        return
    skipped.append(
        {
            "key": _candidate_key(row),
            "base": row.get("base"),
            "direction": row.get("direction", "forward"),
            "long_venue": row.get("long_venue"),
            "short_venue": row.get("short_venue"),
            "reason": reason,
        }
    )


def _filter_candidates(
    rows: list[dict[str, Any]],
    *,
    cfg: dict[str, Any],
    bot_cfg: PaperBotConfig,
    active_keys: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    strat = load_strategy_config()
    pfa = cfg.get("pureFuturesArbitrage") or {}
    min_spread = float(pfa.get("minSpreadPct", 0.05))
    min_edge, min_edge_1h, min_edge_mismatch = strategy_edge_thresholds(strat)
    row_min_edge = min_edge_for_row_factory(min_edge, min_edge_1h, min_edge_mismatch)
    max_mark_spread = float(pfa.get("maxMarkSpreadPct", 1.0))
    trade_usd = float(pfa.get("tradeUsdPerPair", 500.0))
    allow_mismatch = bool(pfa.get("allowSettleMismatch", False))

    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        key = _candidate_key(row)
        if key in active_keys:
            _skip(skipped, row, "position already open")
            continue
        if float(row.get("spread_pct", 0.0) or 0.0) < min_spread:
            _skip(skipped, row, "spread below threshold")
            continue
        threshold = row_min_edge(row)
        if float(row.get("net_edge_pct", 0.0) or 0.0) < threshold:
            _skip(skipped, row, "net edge below threshold")
            continue
        if _row_real_edge(row) < threshold:
            _skip(skipped, row, "real edge below threshold")
            continue
        if abs(float(row.get("mark_spread_pct", 0.0) or 0.0)) > max_mark_spread:
            _skip(skipped, row, "mark spread above threshold")
            continue
        if row.get("depth_ok") is False:
            _skip(skipped, row, "depth check failed")
            continue
        max_exec = row.get("max_exec_usd")
        if max_exec is not None and float(max_exec or 0.0) < trade_usd * bot_cfg.depth_multiple:
            _skip(skipped, row, "depth below configured multiple")
            continue
        settle_ok, settle_reason = _settle_window_ok(row, bot_cfg)
        if not settle_ok:
            _skip(skipped, row, settle_reason)
            continue
        if row.get("settle_mismatch") and not allow_mismatch:
            _skip(skipped, row, "settlement interval mismatch")
            continue
        candidates.append(row)

    candidates = filter_candidates_with_mismatch(
        candidates,
        allow_mismatch=allow_mismatch,
        max_cumulative_outflow_pct=0.5,
        min_adjusted_edge_pct=min_edge,
        min_edge_for_row=row_min_edge,
    )
    candidates.sort(
        key=lambda row: -float(
            row.get("adjusted_net_edge_pct", row.get("real_edge_pct", row.get("net_edge_pct", 0.0))) or 0.0
        )
    )
    return candidates, skipped


def _run_once_sync() -> dict[str, Any]:
    if _IMPORT_ERROR is not None:
        raise RuntimeError(f"paper bot unavailable: {_IMPORT_ERROR}")

    bot_cfg = _load_config()
    cfg = _load_runner_config(bot_cfg)
    strat = load_strategy_config()
    pfa = cfg.get("pureFuturesArbitrage") or {}
    venues = [str(v).lower() for v in pfa.get("venues", ["binance", "bitget", "bybit", "okx"])]
    workers = int(pfa.get("workers", 4))
    max_mark_spread = float(pfa.get("maxMarkSpreadPct", 1.0))
    exit_edge = float(pfa.get("exitThresholdPct", 0.01))
    max_pairs = int(pfa.get("maxConcurrentPairs", 3))
    trade_usd = float(pfa.get("tradeUsdPerPair", 500.0))
    fee_policy = strategy_fee_policy(strat)

    scan = scan_pure_futures_spreads(
        venues=venues,
        min_spread=0.0,
        min_edge=-999.0,
        max_mark_spread_pct=max_mark_spread,
        workers=workers,
        fee_policy=fee_policy,
    )
    rows = list(scan.get("forward", [])) + list(scan.get("reverse", []))
    row_by_key = {_candidate_key(row): row for row in rows}

    actions: list[dict[str, Any]] = []

    for pos in _open_paper_positions():
        key = _position_key(pos)
        row = row_by_key.get(key)
        edge = float(row.get("net_edge_pct", -999.0)) if row else -999.0
        mark_spread = abs(float(row.get("mark_spread_pct", 999.0))) if row else 999.0
        hold_hours = _held_hours(pos)
        active_state = _funding_collected_state(pos, bot_cfg)
        close_reason = ""
        if hold_hours >= bot_cfg.max_hold_hours:
            close_reason = f"max_hold_reached: {hold_hours:.2f}h"
        elif row is not None and edge <= exit_edge:
            close_reason = f"edge_below_exit: {edge:.4f}%"
        elif bot_cfg.active_exit_enabled and active_state.get("collected"):
            if row is not None and mark_spread <= bot_cfg.active_exit_max_mark_spread_pct:
                close_reason = f"active_exit_low_mark_spread: {mark_spread:.4f}%"
            elif active_state.get("deadline_passed"):
                close_reason = "active_exit_timeout" if row is not None else "candidate_missing_after_active_window"
        elif not bot_cfg.active_exit_enabled and row is None:
            close_reason = "candidate_missing"

        if close_reason:
            before = dict(pos)
            res = close_pure_futures_pair(str(pos["id"]), dry_run=True, config=cfg)
            after = _paper_position_by_id(str(pos["id"]))
            account_event = record_bot_close(
                before,
                after,
                reason=close_reason,
                edge=edge,
                result=res,
            )
            actions.append(
                {
                    "action": "close",
                    "position_id": pos.get("id"),
                    "base": pos.get("base"),
                    "reason": close_reason,
                    "edge": edge,
                    "hold_hours": round(hold_hours, 3),
                    "result": res.to_dict() if hasattr(res, "to_dict") else res,
                    "account_event": account_event,
                }
            )

    active = _open_paper_positions()
    active_keys = {_position_key(pos) for pos in active}
    slots = max(0, max_pairs - len(active))
    candidates, skipped = _filter_candidates(rows, cfg=cfg, bot_cfg=bot_cfg, active_keys=active_keys)

    status = _load_status()
    previous_hits = {
        str(k): int(v)
        for k, v in (status.get("hit_counts") or {}).items()
        if isinstance(v, int) or str(v).isdigit()
    }
    hit_counts: dict[str, int] = {}
    ready: list[dict[str, Any]] = []
    for row in candidates:
        key = _candidate_key(row)
        hits = previous_hits.get(key, 0) + 1
        hit_counts[key] = hits
        if hits >= bot_cfg.consecutive_hits:
            ready.append(row)

    available_balance = float(_account_snapshot().get("available_balance_usdt", 0.0) or 0.0)
    for row in ready[: min(slots, bot_cfg.max_actions_per_run)]:
        row_trade_usd = effective_trade_usd(trade_usd, row)
        open_fee = estimate_open_fee_usd(row, row_trade_usd)
        required_balance = margin_required_usd(row_trade_usd) + open_fee
        if available_balance < required_balance:
            _skip(
                skipped,
                row,
                f"paper account insufficient: need {required_balance:.2f} USDT, available {available_balance:.2f}",
            )
            continue
        res = open_pure_futures_pair(
            str(row["base"]),
            str(row["long_venue"]),
            str(row["short_venue"]),
            row_trade_usd,
            dry_run=True,
            direction=str(row.get("direction", "forward")),
            max_mark_spread_pct=max_mark_spread,
            config=cfg,
            capital_buffer_pct=float(row.get("capital_buffer_pct", 0) or 0),
            metadata={
                "managed_by": _BOT_MANAGER_ID,
                "opened_by": _BOT_MANAGER_ID,
                "source": "paper_bot",
                "bot_strategy": "pure_futures_spread",
                **metadata_for_candidate(row, row_trade_usd),
            },
        )
        key = _candidate_key(row)
        hit_counts.pop(key, None)
        position_id = result_position_id(res)
        position = _paper_position_by_id(position_id) if position_id else None
        account_event = record_bot_open(row, position, res, row_trade_usd)
        if account_event is not None:
            available_balance -= required_balance
        actions.append(
            {
                "action": "open",
                "candidate_key": key,
                "candidate": row,
                "result": res.to_dict() if hasattr(res, "to_dict") else res,
                "account_event": account_event,
            }
        )

    open_count = len(_open_paper_positions())
    summary = {
        "ts": _now_iso(),
        "strategy": "pure_futures_spread",
        "dry_run": True,
        "enabled": bot_cfg.enabled,
        "venues": venues,
        "scan_total": len(rows),
        "candidates_after_filter": len(candidates),
        "ready_candidates": len(ready),
        "actions": actions,
        "open_positions": open_count,
        "hit_counts": hit_counts,
        "skipped": skipped,
        "thresholds": {
            "tradeUsdPerPair": trade_usd,
            "maxConcurrentPairs": max_pairs,
            "maxMarkSpreadPct": max_mark_spread,
            "exitThresholdPct": exit_edge,
            "depthMultiple": bot_cfg.depth_multiple,
            "consecutiveHits": bot_cfg.consecutive_hits,
            "settleWindowMinutes": [
                bot_cfg.min_settle_minutes,
                bot_cfg.max_settle_minutes,
            ],
            "maxHoldHours": bot_cfg.max_hold_hours,
            "maxActionsPerRun": bot_cfg.max_actions_per_run,
            "activeExitEnabled": bot_cfg.active_exit_enabled,
            "activeExitConfirmMinutes": bot_cfg.active_exit_confirm_minutes,
            "activeExitWindowMinutes": bot_cfg.active_exit_window_minutes,
            "activeExitMaxMarkSpreadPct": bot_cfg.active_exit_max_mark_spread_pct,
        },
        "account": _account_snapshot(),
    }

    status.update(
        {
            "running": False,
            "last_finished_at": summary["ts"],
            "last_error": None,
            "last_summary": summary,
            "hit_counts": hit_counts,
            "total_runs": int(status.get("total_runs", 0) or 0) + 1,
        }
    )
    _save_status(status)
    _append_journal(summary)
    return summary


def _strategy_scan_interval_seconds() -> int:
    try:
        strat = load_strategy_config()
        return max(10, int(strat.get("scan_interval_sec", 300) or 300))
    except Exception:
        return 300


def _auto_task_running() -> bool:
    return _AUTO_TASK is not None and not _AUTO_TASK.done()


def _wake_auto_loop() -> None:
    if _AUTO_WAKE_EVENT is not None:
        _AUTO_WAKE_EVENT.set()


def _status_payload() -> dict[str, Any]:
    status = _load_status()
    status["available"] = _IMPORT_ERROR is None
    status["import_error"] = _IMPORT_ERROR
    status["config"] = _load_config().model_dump(by_alias=True)
    status["locked"] = _RUN_LOCK.locked()
    status["auto_running"] = _auto_task_running()
    return status


async def _run_once_async() -> dict[str, Any]:
    if _IMPORT_ERROR is not None:
        raise RuntimeError(f"paper bot unavailable: {_IMPORT_ERROR}")
    if _RUN_LOCK.locked():
        raise RuntimeError("Paper bot run already in progress")

    async with _RUN_LOCK:
        status = _load_status()
        status.update(
            {
                "running": True,
                "last_run_at": _now_iso(),
                "last_error": None,
            }
        )
        _save_status(status)

        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, _run_once_sync)
        except Exception as e:
            failed = _load_status()
            failed.update(
                {
                    "running": False,
                    "last_finished_at": _now_iso(),
                    "last_error": str(e),
                }
            )
            _save_status(failed)
            raise


async def _paper_bot_auto_loop() -> None:
    while True:
        cfg = _load_config()
        if not cfg.enabled:
            status = _load_status()
            status["next_run_at"] = None
            _save_status(status)
            return

        try:
            await _run_once_async()
        except RuntimeError as e:
            status = _load_status()
            status.update({"last_error": str(e), "running": False})
            _save_status(status)
        except Exception as e:
            status = _load_status()
            status.update({"last_error": str(e), "running": False})
            _save_status(status)

        interval = _strategy_scan_interval_seconds()
        next_run_at = (datetime.now(timezone.utc) + timedelta(seconds=interval)).isoformat()
        status = _load_status()
        status["next_run_at"] = next_run_at
        _save_status(status)

        if _AUTO_WAKE_EVENT is None:
            await asyncio.sleep(interval)
            continue
        try:
            await asyncio.wait_for(_AUTO_WAKE_EVENT.wait(), timeout=interval)
            _AUTO_WAKE_EVENT.clear()
        except asyncio.TimeoutError:
            pass


def _auto_task_done(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is None:
        return
    status = _load_status()
    status.update(
        {
            "running": False,
            "next_run_at": None,
            "last_error": f"paper bot auto loop crashed: {exc}",
        }
    )
    _save_status(status)


def _ensure_auto_task() -> None:
    global _AUTO_TASK, _AUTO_WAKE_EVENT
    if _auto_task_running():
        return
    _AUTO_WAKE_EVENT = asyncio.Event()
    _AUTO_TASK = asyncio.create_task(_paper_bot_auto_loop(), name="paper-bot-auto-loop")
    _AUTO_TASK.add_done_callback(_auto_task_done)


async def start_paper_bot_if_enabled() -> None:
    if _load_config().enabled and _IMPORT_ERROR is None:
        _ensure_auto_task()


async def shutdown_paper_bot_auto() -> None:
    global _AUTO_TASK, _AUTO_WAKE_EVENT
    task = _AUTO_TASK
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    finally:
        _AUTO_TASK = None
        _AUTO_WAKE_EVENT = None


@router.get("/paper-bot/config")
async def get_paper_bot_config():
    """Return paper-bot specific settings."""
    return {"success": True, "data": _load_config().model_dump(by_alias=True)}


@router.post("/paper-bot/config")
async def update_paper_bot_config(config: PaperBotConfig):
    """Persist paper-bot specific settings."""
    _save_config(config)
    status = _load_status()
    status["config_updated_at"] = _now_iso()
    _save_status(status)
    if config.enabled and _IMPORT_ERROR is None:
        _ensure_auto_task()
        _wake_auto_loop()
    else:
        _wake_auto_loop()
    return {"success": True, "data": config.model_dump(by_alias=True)}


@router.get("/paper-bot/status")
async def get_paper_bot_status():
    """Return current paper-bot status."""
    return {"success": True, "data": _status_payload()}


@router.get("/paper-bot/account")
async def get_paper_bot_account():
    """Return the simulated account summary for paper-bot-managed positions."""
    return {"success": True, "data": _enriched_account_snapshot()}


@router.post("/paper-bot/run-once")
async def run_paper_bot_once():
    """Run one complete paper-bot decision cycle."""
    try:
        summary = await _run_once_async()
        return {"success": True, "data": summary}
    except Exception as e:
        return {"success": False, "error": f"Paper bot run failed: {e}"}


@router.post("/paper-bot/backtest")
async def run_paper_bot_backtest(req: PaperBotBacktestRequest):
    """Run a historical replay that mimics the paper bot's scan/open/close cycle."""
    try:
        import asyncio

        from backtest.backtest_paper_bot import run_paper_bot_backtest as _run_backtest  # noqa: E402

        loop = asyncio.get_running_loop()

        def _run() -> dict[str, Any]:
            snapshots = _load_paper_bot_backtest_snapshots(req)
            if not snapshots:
                raise RuntimeError(
                    "No historical snapshots available. Provide multiple history bases "
                    "or run the scanner in watch mode to produce a JSONL file."
                )
            raw = _run_backtest(
                snapshots,
                initial_capital=req.capital,
                trade_usd=req.trade_usd,
                max_concurrent_pairs=req.max_positions,
                min_spread_pct=req.min_spread,
                min_edge_pct=req.min_edge_pct,
                min_edge_1h=req.min_edge_1h,
                min_edge_mismatch=req.min_edge_mismatch,
                exit_edge_pct=req.exit_edge,
                max_mark_spread_pct=req.max_mark_spread_pct,
                max_holding_hours=req.max_holding_hours,
                allow_mismatch=req.allow_mismatch,
                consecutive_hits=req.consecutive_hits,
                min_settle_minutes=req.min_settle_minutes,
                max_settle_minutes=req.max_settle_minutes,
                max_actions_per_run=req.max_actions_per_run,
                depth_multiple=req.depth_multiple,
                basis_cost_pct=req.basis_cost_pct,
                active_exit_enabled=req.active_exit_enabled,
                active_exit_confirm_minutes=req.active_exit_confirm_minutes,
                active_exit_window_minutes=req.active_exit_window_minutes,
                active_exit_max_mark_spread_pct=req.active_exit_max_mark_spread_pct,
            )
            return _adapt_paper_bot_backtest_result(raw, req)

        result = await loop.run_in_executor(None, _run)
        try:
            from server.routes.backtest import _save_backtest_result  # noqa: E402

            _save_backtest_result(result)
        except Exception:
            pass
        return {"success": True, "data": result, "live": True}
    except Exception as e:
        return {"success": False, "error": f"Paper bot backtest failed: {e}"}


@router.post("/paper-bot/start")
async def start_paper_bot_auto():
    """Enable the paper-bot background loop and run the first cycle immediately."""
    if _IMPORT_ERROR is not None:
        return {"success": False, "error": f"paper bot unavailable: {_IMPORT_ERROR}"}
    cfg = _load_config().model_copy(update={"enabled": True})
    _save_config(cfg)
    now = _now_iso()
    status = _load_status()
    status.update(
        {
            "auto_started_at": now,
            "auto_stopped_at": None,
            "next_run_at": now,
            "config_updated_at": now,
            "last_error": None,
        }
    )
    _save_status(status)
    _ensure_auto_task()
    _wake_auto_loop()
    return {"success": True, "data": _status_payload()}


@router.post("/paper-bot/stop")
async def stop_paper_bot_auto():
    """Disable the paper-bot background loop after the current cycle settles."""
    cfg = _load_config().model_copy(update={"enabled": False})
    _save_config(cfg)
    now = _now_iso()
    status = _load_status()
    status.update(
        {
            "auto_stopped_at": now,
            "next_run_at": None,
            "config_updated_at": now,
        }
    )
    _save_status(status)
    _wake_auto_loop()
    return {"success": True, "data": _status_payload()}


@router.get("/paper-bot/journal")
async def get_paper_bot_journal(limit: int = Query(50, ge=1, le=200)):
    """Return recent paper-bot run summaries."""
    return {"success": True, "data": _read_recent_journal(limit)}


@router.get("/paper-bot/ledger")
async def get_paper_bot_ledger(limit: int = Query(100, ge=1, le=500)):
    """Return recent simulated account ledger entries."""
    return {"success": True, "data": read_account_ledger(limit)}

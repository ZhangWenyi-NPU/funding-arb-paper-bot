#!/usr/bin/env python3
"""Paper-bot style backtest replay.

This module keeps the historical funding replay from the generic pure-futures
backtest, but layers the paper bot's decision rules on top: scan every snapshot,
filter candidates, require consecutive hits, reserve simulated account capital,
open positions, close stale positions, and keep a compact scan journal.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.backtest_pure_futures_spread import (  # noqa: E402
    ClosedTrade,
    OpenPair,
    _accrue_funding,
    _iter_rows,
    _opp_key,
    _parse_ts,
    _update_pair_rates,
)
from execution.settle_mismatch_planner import (  # noqa: E402
    effective_trade_usd,
    filter_candidates_with_mismatch,
)


FEE_RATES = {
    "bitget": 0.06,
    "binance": 0.05,
    "okx": 0.05,
    "bybit": 0.055,
    "hyperliquid": 0.035,
    "dydx": 0.05,
    "lighter": 0.035,
    "aster": 0.05,
    "edgex": 0.05,
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bot_key(row: dict[str, Any]) -> str:
    return ":".join(
        [
            str(row.get("base", "")).upper(),
            str(row.get("direction", "forward")).lower(),
            str(row.get("long_venue", "")).lower(),
            str(row.get("short_venue", "")).lower(),
        ]
    )


def _row_real_edge(row: dict[str, Any]) -> float:
    if row.get("real_edge_pct") is not None:
        return _safe_float(row.get("real_edge_pct"))
    return _safe_float(row.get("net_edge_pct")) - abs(_safe_float(row.get("mark_spread_pct")))


def _edge_for_sort(row: dict[str, Any]) -> float:
    return _safe_float(
        row.get("adjusted_net_edge_pct", row.get("real_edge_pct", row.get("net_edge_pct")))
    )


def _fee_pct(row: dict[str, Any]) -> float:
    fee = _safe_float(row.get("fee_pct"), -1.0)
    if fee >= 0:
        return fee
    long_venue = str(row.get("long_venue", "")).lower()
    short_venue = str(row.get("short_venue", "")).lower()
    return FEE_RATES.get(long_venue, 0.06) + FEE_RATES.get(short_venue, 0.06)


def _open_fee_usd(row: dict[str, Any], trade_usd: float) -> float:
    return trade_usd * _fee_pct(row) / 100.0


def _margin_required_usd(trade_usd: float) -> float:
    return max(0.0, trade_usd) * 2.0


def _row_threshold_fn(
    min_edge_pct: float,
    min_edge_1h: float | None,
    min_edge_mismatch: float | None,
) -> Callable[[dict[str, Any]], float]:
    def _threshold(row: dict[str, Any]) -> float:
        long_h = _safe_float(row.get("long_interval_h"), 8.0)
        short_h = _safe_float(row.get("short_interval_h"), 8.0)
        if min_edge_1h is not None and long_h <= 1.0 and short_h <= 1.0:
            return min_edge_1h
        if min_edge_mismatch is not None and (
            row.get("settle_mismatch") or abs(long_h - short_h) > 0.5
        ):
            return min_edge_mismatch
        return min_edge_pct

    return _threshold


def _settle_minutes(row: dict[str, Any], ts: datetime) -> list[float]:
    out: list[float] = []
    for key in ("long_settle_ms", "short_settle_ms"):
        settle_ms = _safe_int(row.get(key))
        if settle_ms > 0:
            out.append((settle_ms / 1000.0 - ts.timestamp()) / 60.0)
    return out


def _settle_window_ok(
    row: dict[str, Any],
    ts: datetime,
    *,
    min_settle_minutes: float,
    max_settle_minutes: float,
) -> tuple[bool, str, bool]:
    minutes = _settle_minutes(row, ts)
    if not minutes:
        return False, "settlement time unavailable", False

    nearest = min(minutes)
    farthest = max(minutes)
    if farthest <= 0:
        # Exchange history snapshots are settlement-indexed, so the public data
        # often says "next settlement" equals the snapshot timestamp. Treat this
        # as a synthetic scan inside the configured window instead of rejecting
        # every historical opportunity.
        return True, "", True
    if nearest < min_settle_minutes:
        return False, f"too close to settlement: {nearest:.1f}m", False
    if farthest > max_settle_minutes:
        return False, f"too far from settlement: {farthest:.1f}m", False
    return True, "", False


def _candidate_preview(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "base",
        "direction",
        "long_venue",
        "short_venue",
        "spread_pct",
        "net_edge_pct",
        "real_edge_pct",
        "adjusted_net_edge_pct",
        "mark_spread_pct",
        "fee_pct",
        "long_rate_pct",
        "short_rate_pct",
        "long_interval_h",
        "short_interval_h",
        "settle_mismatch",
        "max_exec_usd",
        "depth_ok",
    )
    return {k: row.get(k) for k in keys if k in row}


def _skip(
    skipped: list[dict[str, Any]],
    row: dict[str, Any],
    reason: str,
    *,
    max_items: int,
) -> None:
    if len(skipped) >= max_items:
        return
    skipped.append(
        {
            "key": _bot_key(row),
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
    ts: datetime,
    active_keys: set[str],
    min_spread_pct: float,
    min_edge_for_row: Callable[[dict[str, Any]], float],
    max_mark_spread_pct: float,
    trade_usd: float,
    allow_mismatch: bool,
    depth_multiple: float | None,
    min_settle_minutes: float,
    max_settle_minutes: float,
    max_skipped_per_scan: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, dict[str, int]]:
    candidates: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    synthetic_settle_count = 0
    counts: dict[str, int] = {
        "scan_total": len(rows),
        "direction_rows": 0,
        "not_open": 0,
        "spread_ok": 0,
        "net_edge_ok": 0,
        "real_edge_ok": 0,
        "mark_spread_ok": 0,
        "settle_window_ok": 0,
        "mismatch_ok": 0,
        "candidates_before_mismatch": 0,
        "candidates_after_filter": 0,
    }

    for raw in rows:
        direction = str(raw.get("direction", "forward"))
        if direction not in {"forward", "reverse"}:
            continue
        counts["direction_rows"] += 1
        row = dict(raw)
        key = _bot_key(row)
        if key in active_keys:
            _skip(skipped, row, "position already open", max_items=max_skipped_per_scan)
            continue
        counts["not_open"] += 1
        if _safe_float(row.get("spread_pct")) < min_spread_pct:
            _skip(skipped, row, "spread below threshold", max_items=max_skipped_per_scan)
            continue
        counts["spread_ok"] += 1
        threshold = min_edge_for_row(row)
        if _safe_float(row.get("net_edge_pct")) < threshold:
            _skip(skipped, row, "net edge below threshold", max_items=max_skipped_per_scan)
            continue
        counts["net_edge_ok"] += 1
        if _row_real_edge(row) < threshold:
            _skip(skipped, row, "real edge below threshold", max_items=max_skipped_per_scan)
            continue
        counts["real_edge_ok"] += 1
        if abs(_safe_float(row.get("mark_spread_pct"))) > max_mark_spread_pct:
            _skip(skipped, row, "mark spread above threshold", max_items=max_skipped_per_scan)
            continue
        counts["mark_spread_ok"] += 1
        if row.get("depth_ok") is False:
            _skip(skipped, row, "depth check failed", max_items=max_skipped_per_scan)
            continue
        max_exec = row.get("max_exec_usd")
        if (
            depth_multiple is not None
            and max_exec is not None
            and _safe_float(max_exec) < trade_usd * depth_multiple
        ):
            _skip(skipped, row, "depth below configured multiple", max_items=max_skipped_per_scan)
            continue
        settle_ok, settle_reason, synthetic_settle = _settle_window_ok(
            row,
            ts,
            min_settle_minutes=min_settle_minutes,
            max_settle_minutes=max_settle_minutes,
        )
        if not settle_ok:
            _skip(skipped, row, settle_reason, max_items=max_skipped_per_scan)
            continue
        counts["settle_window_ok"] += 1
        if synthetic_settle:
            row["synthetic_settle_window"] = True
            synthetic_settle_count += 1
        if row.get("settle_mismatch") and not allow_mismatch:
            _skip(skipped, row, "settlement interval mismatch", max_items=max_skipped_per_scan)
            continue
        counts["mismatch_ok"] += 1
        candidates.append(row)

    before_mismatch = {_bot_key(row): row for row in candidates}
    counts["candidates_before_mismatch"] = len(candidates)
    candidates = filter_candidates_with_mismatch(
        candidates,
        allow_mismatch=allow_mismatch,
        max_cumulative_outflow_pct=0.5,
        min_adjusted_edge_pct=0.0,
        min_edge_for_row=min_edge_for_row,
    )
    after_keys = {_bot_key(row) for row in candidates}
    for key, row in before_mismatch.items():
        if key not in after_keys:
            _skip(
                skipped,
                row,
                "settlement mismatch adjusted edge below threshold",
                max_items=max_skipped_per_scan,
            )

    candidates.sort(key=lambda row: -_edge_for_sort(row))
    counts["candidates_after_filter"] = len(candidates)
    return candidates, skipped, synthetic_settle_count, counts


def _close_trade(
    pair: OpenPair,
    *,
    ts: datetime,
    current_edge: float,
    close_reason: str,
    current_row: dict[str, Any] | None,
    basis_cost_pct: float,
) -> tuple[ClosedTrade, float]:
    exit_mark_spread = _safe_float((current_row or {}).get("mark_spread_pct"))
    if exit_mark_spread > 0 and pair.open_mark_spread_pct > 0:
        basis_cost = abs(exit_mark_spread - pair.open_mark_spread_pct)
    else:
        basis_cost = basis_cost_pct

    total_fee = pair.open_fee_pct * 2.0
    net_pnl = (
        pair.accumulated_funding_pct
        - pair.borrow_paid_pct
        - total_fee
        - basis_cost
    )
    pnl_usd = pair.amount_usd * net_pnl / 100.0
    hours_held = (ts - pair.open_ts).total_seconds() / 3600.0
    trade = ClosedTrade(
        pair_id=pair.pair_id,
        base=pair.base,
        direction=pair.direction,
        open_ts=pair.open_ts,
        close_ts=ts,
        holding_hours=hours_held,
        open_edge_pct=pair.open_edge_pct,
        close_edge_pct=current_edge,
        total_funding_pct=round(pair.accumulated_funding_pct, 6),
        total_fee_pct=round(total_fee, 4),
        net_pnl_pct=round(net_pnl, 6),
        amount_usd=pair.amount_usd,
        close_reason=close_reason,
        win=net_pnl > 0,
        long_settlements=pair.long_settlements,
        short_settlements=pair.short_settlements,
        borrow_paid_pct=round(pair.borrow_paid_pct, 6),
    )
    return trade, pnl_usd


def _active_exit_started_at(pair: OpenPair) -> datetime | None:
    value = getattr(pair, "_active_exit_started_at", None)
    return value if isinstance(value, datetime) else None


def _update_active_exit_start(pair: OpenPair, ts: datetime) -> datetime | None:
    started = _active_exit_started_at(pair)
    if started is not None:
        return started
    if pair.long_settlements > 0 and pair.short_settlements > 0:
        setattr(pair, "_active_exit_started_at", ts)
        return ts
    return None


def _trade_to_dict(trade: ClosedTrade) -> dict[str, Any]:
    return {
        "pair_id": trade.pair_id,
        "base": trade.base,
        "direction": trade.direction,
        "open_ts": trade.open_ts.isoformat(),
        "close_ts": trade.close_ts.isoformat(),
        "holding_hours": round(trade.holding_hours, 3),
        "open_edge_pct": round(trade.open_edge_pct, 6),
        "close_edge_pct": round(trade.close_edge_pct, 6),
        "total_funding_pct": round(trade.total_funding_pct, 6),
        "total_fee_pct": round(trade.total_fee_pct, 6),
        "net_pnl_pct": round(trade.net_pnl_pct, 6),
        "amount_usd": round(trade.amount_usd, 2),
        "close_reason": trade.close_reason,
        "win": trade.win,
        "long_settlements": trade.long_settlements,
        "short_settlements": trade.short_settlements,
        "borrow_paid_pct": round(trade.borrow_paid_pct, 6),
    }


def _equity(
    free_cash: float,
    open_pairs: dict[str, OpenPair],
    *,
    basis_cost_pct: float,
) -> float:
    equity = free_cash
    for pair in open_pairs.values():
        liquidation_pct = (
            pair.accumulated_funding_pct
            - pair.borrow_paid_pct
            - pair.open_fee_pct
            - basis_cost_pct
        )
        equity += _margin_required_usd(pair.amount_usd)
        equity += pair.amount_usd * liquidation_pct / 100.0
    return equity


def _aggregate_daily_logs(scan_journal: list[dict[str, Any]]) -> list[dict[str, Any]]:
    daily: dict[str, dict[str, Any]] = {}
    for row in scan_journal:
        date = str(row.get("ts", ""))[:10] or "unknown"
        out = daily.setdefault(
            date,
            {
                "date": date,
                "scan_runs": 0,
                "pairs_scanned": 0,
                "spread_ok": 0,
                "net_edge_ok": 0,
                "real_edge_ok": 0,
                "mark_spread_ok": 0,
                "settle_window_ok": 0,
                "mismatch_ok": 0,
                "final_candidates": 0,
                "consecutive_ok": 0,
                "open_actions": 0,
                "close_actions": 0,
            },
        )
        counts = row.get("filter_counts") or {}
        actions = row.get("actions") or []
        out["scan_runs"] += 1
        out["pairs_scanned"] += int(counts.get("direction_rows", row.get("scan_total", 0)) or 0)
        out["spread_ok"] += int(counts.get("spread_ok", 0) or 0)
        out["net_edge_ok"] += int(counts.get("net_edge_ok", 0) or 0)
        out["real_edge_ok"] += int(counts.get("real_edge_ok", 0) or 0)
        out["mark_spread_ok"] += int(counts.get("mark_spread_ok", 0) or 0)
        out["settle_window_ok"] += int(counts.get("settle_window_ok", 0) or 0)
        out["mismatch_ok"] += int(counts.get("mismatch_ok", 0) or 0)
        out["final_candidates"] += int(row.get("candidates_after_filter", 0) or 0)
        out["consecutive_ok"] += int(row.get("ready_candidates", 0) or 0)
        out["open_actions"] += sum(1 for action in actions if action.get("action") == "open")
        out["close_actions"] += sum(1 for action in actions if action.get("action") == "close")
    return [daily[key] for key in sorted(daily)]


def run_paper_bot_backtest(
    snapshots: list[dict[str, Any]],
    *,
    initial_capital: float = 100000.0,
    trade_usd: float = 5000.0,
    max_concurrent_pairs: int = 3,
    min_spread_pct: float = 0.05,
    min_edge_pct: float = 0.01,
    min_edge_1h: float | None = None,
    min_edge_mismatch: float | None = None,
    exit_edge_pct: float = 0.01,
    max_mark_spread_pct: float = 1.0,
    max_holding_hours: float = 9.0,
    allow_mismatch: bool = False,
    consecutive_hits: int = 2,
    min_settle_minutes: float = 10.0,
    max_settle_minutes: float = 90.0,
    max_actions_per_run: int = 5,
    depth_multiple: float | None = None,
    basis_cost_pct: float = 0.05,
    active_exit_enabled: bool = True,
    active_exit_confirm_minutes: float = 2.0,
    active_exit_window_minutes: float = 60.0,
    active_exit_max_mark_spread_pct: float = 0.15,
    max_journal_rows: int = 1500,
    max_skipped_per_scan: int = 20,
) -> dict[str, Any]:
    """Replay snapshots with the same high-level rules as the paper bot."""
    ordered: list[dict[str, Any]] = []
    for snap in snapshots:
        item = dict(snap)
        ts = item.get("_ts") if isinstance(item.get("_ts"), datetime) else _parse_ts(item.get("timestamp"))
        if ts is None:
            continue
        item["_ts"] = ts.astimezone(timezone.utc)
        ordered.append(item)
    ordered.sort(key=lambda item: item["_ts"])

    free_cash = float(initial_capital)
    open_pairs: dict[str, OpenPair] = {}
    closed_trades: list[ClosedTrade] = []
    equity_curve: list[dict[str, Any]] = []
    scan_journal: list[dict[str, Any]] = []
    hit_counts: dict[str, int] = {}
    min_edge_for_row = _row_threshold_fn(min_edge_pct, min_edge_1h, min_edge_mismatch)
    peak_equity = free_cash
    max_drawdown = 0.0
    daily_returns: list[float] = []
    prev_date: str | None = None
    day_start_equity = free_cash
    total_synthetic_settle_windows = 0
    total_scan_rows = 0
    total_candidates = 0
    total_ready = 0
    total_open_actions = 0
    total_close_actions = 0

    for snap in ordered:
        ts: datetime = snap["_ts"]
        current_date = ts.strftime("%Y-%m-%d")
        rows = _iter_rows(snap)
        total_scan_rows += len(rows)
        all_by_key = {_bot_key(row): row for row in rows}
        actions: list[dict[str, Any]] = []

        to_close: list[str] = []
        for pair_id, pair in list(open_pairs.items()):
            current_row = all_by_key.get(pair_id)
            if current_row is not None:
                _update_pair_rates(pair, current_row)
            _accrue_funding(pair, ts)
            active_start = _update_active_exit_start(pair, ts)
            active_ready_at = (
                active_start
                and active_start + timedelta(minutes=active_exit_confirm_minutes)
            )
            active_deadline = (
                active_ready_at
                and active_ready_at + timedelta(minutes=active_exit_window_minutes)
            )

            current_edge = -999.0
            close_reason = ""
            if current_row is not None:
                current_edge = _safe_float(current_row.get("net_edge_pct"))
                if current_edge <= exit_edge_pct:
                    close_reason = f"edge_below_exit: {current_edge:.4f}%"

            hours_held = (ts - pair.open_ts).total_seconds() / 3600.0
            if hours_held >= max_holding_hours:
                close_reason = f"max_hold_reached: {hours_held:.2f}h"
            elif (
                active_exit_enabled
                and active_ready_at is not None
                and ts >= active_ready_at
                and not close_reason
            ):
                current_mark_spread = abs(_safe_float((current_row or {}).get("mark_spread_pct"), 999.0))
                if current_row is not None and current_mark_spread <= active_exit_max_mark_spread_pct:
                    close_reason = f"active_exit_low_mark_spread: {current_mark_spread:.4f}%"
                elif active_deadline is not None and ts >= active_deadline:
                    close_reason = (
                        "active_exit_timeout"
                        if current_row is not None
                        else "candidate_missing_after_active_window"
                    )
            elif not active_exit_enabled and current_row is None:
                close_reason = "candidate_missing"

            if close_reason:
                trade, pnl_usd = _close_trade(
                    pair,
                    ts=ts,
                    current_edge=current_edge,
                    close_reason=close_reason,
                    current_row=current_row,
                    basis_cost_pct=basis_cost_pct,
                )
                free_cash += _margin_required_usd(pair.amount_usd)
                free_cash += pnl_usd + pair.amount_usd * pair.open_fee_pct / 100.0
                closed_trades.append(trade)
                to_close.append(pair_id)
                total_close_actions += 1
                actions.append(
                    {
                        "action": "close",
                        "candidate_key": pair_id,
                        "base": pair.base,
                        "edge": round(current_edge, 6),
                        "reason": close_reason,
                        "pnl_usd": round(pnl_usd, 2),
                        "net_pnl_pct": trade.net_pnl_pct,
                        "hold_hours": round(hours_held, 3),
                    }
                )

        for pair_id in to_close:
            del open_pairs[pair_id]

        active_keys = set(open_pairs)
        candidates, skipped, synthetic_count, filter_counts = _filter_candidates(
            rows,
            ts=ts,
            active_keys=active_keys,
            min_spread_pct=min_spread_pct,
            min_edge_for_row=min_edge_for_row,
            max_mark_spread_pct=max_mark_spread_pct,
            trade_usd=trade_usd,
            allow_mismatch=allow_mismatch,
            depth_multiple=depth_multiple,
            min_settle_minutes=min_settle_minutes,
            max_settle_minutes=max_settle_minutes,
            max_skipped_per_scan=max_skipped_per_scan,
        )
        total_synthetic_settle_windows += synthetic_count
        total_candidates += len(candidates)

        previous_hits = hit_counts
        hit_counts = {}
        ready: list[dict[str, Any]] = []
        for row in candidates:
            key = _bot_key(row)
            hits = previous_hits.get(key, 0) + 1
            hit_counts[key] = hits
            if hits >= consecutive_hits:
                ready.append(row)
        total_ready += len(ready)
        filter_counts["consecutive_ok"] = len(ready)

        slots = max(0, max_concurrent_pairs - len(open_pairs))
        open_limit = min(slots, max_actions_per_run)
        opened = 0
        for row in ready:
            if opened >= open_limit:
                break
            row_trade_usd = effective_trade_usd(trade_usd, row)
            open_fee = _open_fee_usd(row, row_trade_usd)
            required = _margin_required_usd(row_trade_usd) + open_fee
            if free_cash < required:
                _skip(
                    skipped,
                    row,
                    f"paper account insufficient: need {required:.2f}, available {free_cash:.2f}",
                    max_items=max_skipped_per_scan,
                )
                continue
            key = _bot_key(row)
            pair = OpenPair(
                pair_id=key,
                base=str(row.get("base", "")).upper(),
                long_venue=str(row.get("long_venue", "")).lower(),
                short_venue=str(row.get("short_venue", "")).lower(),
                direction=str(row.get("direction", "forward")).lower(),
                amount_usd=row_trade_usd,
                open_edge_pct=_edge_for_sort(row),
                open_spread_pct=_safe_float(row.get("spread_pct")),
                open_mark_spread_pct=_safe_float(row.get("mark_spread_pct")),
                open_ts=ts,
                open_fee_pct=_fee_pct(row),
                last_accrual_ts=ts,
            )
            _update_pair_rates(pair, row)
            open_pairs[key] = pair
            free_cash -= required
            hit_counts.pop(key, None)
            opened += 1
            total_open_actions += 1
            actions.append(
                {
                    "action": "open",
                    "candidate_key": key,
                    "candidate": _candidate_preview(row),
                    "trade_usd": round(row_trade_usd, 2),
                    "open_fee_usd": round(open_fee, 2),
                    "required_balance_usd": round(required, 2),
                    "edge": round(pair.open_edge_pct, 6),
                }
            )
        filter_counts["open_actions"] = sum(1 for action in actions if action.get("action") == "open")
        filter_counts["close_actions"] = sum(1 for action in actions if action.get("action") == "close")

        equity = _equity(free_cash, open_pairs, basis_cost_pct=basis_cost_pct)
        equity_curve.append(
            {
                "ts": ts.isoformat(),
                "equity": round(equity, 2),
                "open_pairs": len(open_pairs),
                "capital_free": round(free_cash, 2),
            }
        )
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            max_drawdown = max(max_drawdown, (peak_equity - equity) / peak_equity * 100.0)

        if current_date != prev_date:
            if prev_date is not None and day_start_equity > 0:
                daily_returns.append((equity - day_start_equity) / day_start_equity)
            day_start_equity = equity
            prev_date = current_date

        if len(scan_journal) < max_journal_rows:
            scan_journal.append(
                {
                    "ts": ts.isoformat(),
                    "scan_total": len(rows),
                    "candidates_after_filter": len(candidates),
                    "ready_candidates": len(ready),
                    "actions": actions,
                    "open_positions": len(open_pairs),
                    "hit_counts": dict(hit_counts),
                    "skipped": skipped,
                    "filter_counts": filter_counts,
                    "capital_free": round(free_cash, 2),
                    "equity": round(equity, 2),
                    "synthetic_settle_windows": synthetic_count,
                }
            )

    final_ts = ordered[-1]["_ts"] if ordered else datetime.now(timezone.utc)
    for pair_id, pair in list(open_pairs.items()):
        trade, pnl_usd = _close_trade(
            pair,
            ts=final_ts,
            current_edge=-999.0,
            close_reason="backtest_end",
            current_row=None,
            basis_cost_pct=basis_cost_pct,
        )
        free_cash += _margin_required_usd(pair.amount_usd)
        free_cash += pnl_usd + pair.amount_usd * pair.open_fee_pct / 100.0
        closed_trades.append(trade)
        total_close_actions += 1
        if scan_journal:
            scan_journal[-1].setdefault("actions", []).append(
                {
                    "action": "close",
                    "candidate_key": pair_id,
                    "base": pair.base,
                    "edge": -999.0,
                    "reason": "backtest_end",
                    "pnl_usd": round(pnl_usd, 2),
                    "net_pnl_pct": trade.net_pnl_pct,
                    "hold_hours": round(trade.holding_hours, 3),
                }
            )
            scan_journal[-1]["open_positions"] = max(0, scan_journal[-1]["open_positions"] - 1)
            counts = scan_journal[-1].setdefault("filter_counts", {})
            counts["close_actions"] = int(counts.get("close_actions", 0) or 0) + 1
        del open_pairs[pair_id]

    final_equity = free_cash
    if equity_curve:
        equity_curve[-1]["equity"] = round(final_equity, 2)
        equity_curve[-1]["open_pairs"] = 0
        equity_curve[-1]["capital_free"] = round(free_cash, 2)
    peak_equity = max(peak_equity, final_equity)
    if peak_equity > 0:
        max_drawdown = max(max_drawdown, (peak_equity - final_equity) / peak_equity * 100.0)

    total_return = (
        (final_equity - initial_capital) / initial_capital * 100.0
        if initial_capital > 0
        else 0.0
    )
    total_hours = (
        (ordered[-1]["_ts"] - ordered[0]["_ts"]).total_seconds() / 3600.0
        if len(ordered) >= 2
        else 0.0
    )
    annual_return = total_return / total_hours * 8760.0 if total_hours > 0 else 0.0
    wins = sum(1 for trade in closed_trades if trade.win)
    avg_holding = (
        sum(trade.holding_hours for trade in closed_trades) / len(closed_trades)
        if closed_trades
        else 0.0
    )
    avg_pnl = (
        sum(trade.net_pnl_pct for trade in closed_trades) / len(closed_trades)
        if closed_trades
        else 0.0
    )
    total_funding = sum(trade.total_funding_pct for trade in closed_trades)
    total_fees = sum(trade.total_fee_pct for trade in closed_trades)
    sharpe = 0.0
    if len(daily_returns) > 1:
        mean_ret = sum(daily_returns) / len(daily_returns)
        variance = sum((r - mean_ret) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std_dev = math.sqrt(variance) if variance > 0 else 0.0
        if std_dev > 0:
            sharpe = (mean_ret / std_dev) * math.sqrt(365.0)

    return {
        "total_return_pct": round(total_return, 4),
        "annual_return_pct": round(annual_return, 2),
        "max_drawdown_pct": round(max_drawdown, 4),
        "sharpe_ratio": round(sharpe, 2),
        "trade_count": len(closed_trades),
        "win_count": wins,
        "win_rate_pct": round(wins / len(closed_trades) * 100.0, 1) if closed_trades else 0.0,
        "avg_holding_hours": round(avg_holding, 3),
        "avg_pnl_per_trade_pct": round(avg_pnl, 6),
        "total_funding_collected_pct": round(total_funding, 6),
        "total_fees_paid_pct": round(total_fees, 6),
        "trades": [_trade_to_dict(trade) for trade in closed_trades],
        "equity_curve": equity_curve,
        "scan_journal": scan_journal,
        "daily_logs": _aggregate_daily_logs(scan_journal),
        "scan_count": len(ordered),
        "scan_total_rows": total_scan_rows,
        "avg_candidates_after_filter": round(total_candidates / len(ordered), 3) if ordered else 0.0,
        "avg_ready_candidates": round(total_ready / len(ordered), 3) if ordered else 0.0,
        "open_actions": total_open_actions,
        "close_actions": total_close_actions,
        "synthetic_settle_windows": total_synthetic_settle_windows,
        "final_equity": round(final_equity, 2),
    }

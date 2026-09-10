#!/usr/bin/env python3
"""Paper-bot account ledger and simulation accounting helpers."""

from __future__ import annotations

import json
import math
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from core.file_lock import lock_exclusive, unlock  # noqa: E402

DATA_DIR = SCRIPTS_DIR / "data" / "paper-bot"
LEDGER_PATH = DATA_DIR / "ledger.jsonl"
BOT_MANAGER_ID = "paper_bot"
DEFAULT_INITIAL_BALANCE_USDT = 100000.0


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ms() -> int:
    return int(time.time() * 1000)


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


def _to_ms(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return default


def _round_money(value: float) -> float:
    return round(_safe_float(value), 2)


def is_bot_managed_position(pos: dict[str, Any]) -> bool:
    return (
        pos.get("managed_by") == BOT_MANAGER_ID
        or pos.get("opened_by") == BOT_MANAGER_ID
        or pos.get("source") == BOT_MANAGER_ID
    )


def bot_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in positions if p.get("dry_run") is True and is_bot_managed_position(p)]


def open_bot_positions(positions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [p for p in bot_positions(positions) if p.get("status") == "open"]


def _with_ledger_lock():
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = open(LEDGER_PATH.with_suffix(".lock"), "w")
    try:
        lock_exclusive(lock_fd)
    except BaseException:
        lock_fd.close()
        raise
    return lock_fd


def read_account_ledger(limit: int | None = None) -> list[dict[str, Any]]:
    if not LEDGER_PATH.exists():
        return []
    lines = LEDGER_PATH.read_text(encoding="utf-8").splitlines()
    if limit is not None:
        lines = lines[-limit:]
    rows: list[dict[str, Any]] = []
    for line in lines:
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def append_account_event(entry: dict[str, Any]) -> dict[str, Any]:
    payload = dict(entry)
    payload.setdefault("id", uuid.uuid4().hex[:12])
    payload.setdefault("ts", now_iso())
    payload.setdefault("source", BOT_MANAGER_ID)
    lock_fd = _with_ledger_lock()
    try:
        with LEDGER_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    finally:
        unlock(lock_fd)
        lock_fd.close()
    return payload


def ledger_has_event(position_id: str, event_type: str) -> bool:
    return any(
        e.get("position_id") == position_id and e.get("type") == event_type
        for e in read_account_ledger()
    )


def ledger_open_event(position_id: str) -> dict[str, Any] | None:
    for event in read_account_ledger():
        if event.get("position_id") == position_id and event.get("type") == "open":
            return event
    return None


def result_ok(result: Any) -> bool:
    if result is None:
        return False
    if hasattr(result, "ok"):
        return bool(getattr(result, "ok"))
    if isinstance(result, dict) and "ok" in result:
        return bool(result.get("ok"))
    return True


def result_position_id(result: Any) -> str:
    if result is None:
        return ""
    if hasattr(result, "position_id"):
        return str(getattr(result, "position_id") or "")
    if isinstance(result, dict):
        return str(result.get("position_id") or "")
    if hasattr(result, "to_dict"):
        data = result.to_dict()
        if isinstance(data, dict):
            return str(data.get("position_id") or "")
    return ""


def fee_pct_from_row(row: dict[str, Any]) -> float:
    fee_pct = row.get("fee_pct")
    if fee_pct is not None:
        return _safe_float(fee_pct)
    return _safe_float(row.get("long_fee_pct")) + _safe_float(row.get("short_fee_pct"))


def position_trade_usd(pos: dict[str, Any]) -> float:
    trade_usd = _safe_float(pos.get("trade_usd") or pos.get("amount_usd"))
    if trade_usd > 0:
        return trade_usd
    qty = _safe_float(pos.get("qty"))
    long_px = _safe_float(pos.get("long_price"))
    short_px = _safe_float(pos.get("short_price"))
    px = max(long_px, short_px)
    return qty * px if qty > 0 and px > 0 else 0.0


def margin_required_usd(trade_usd: float) -> float:
    # Pure-futures simulation uses one notional-sized collateral bucket per leg.
    return max(0.0, trade_usd) * 2.0


def estimate_open_fee_usd(row: dict[str, Any], trade_usd: float) -> float:
    return max(0.0, trade_usd) * fee_pct_from_row(row) / 100.0


def metadata_for_candidate(row: dict[str, Any], trade_usd: float) -> dict[str, Any]:
    fee_pct = fee_pct_from_row(row)
    open_fee = estimate_open_fee_usd(row, trade_usd)
    return {
        "paper_margin_usd": _round_money(margin_required_usd(trade_usd)),
        "paper_open_fee_usd": _round_money(open_fee),
        "paper_close_fee_est_usd": _round_money(open_fee),
        "paper_round_trip_fee_est_usd": _round_money(open_fee * 2.0),
        "paper_fee_pct": round(fee_pct, 6),
        "paper_long_fee_pct": round(_safe_float(row.get("long_fee_pct")), 6),
        "paper_short_fee_pct": round(_safe_float(row.get("short_fee_pct")), 6),
        "paper_open_spread_pct": round(_safe_float(row.get("spread_pct")), 6),
        "paper_open_net_edge_pct": round(_safe_float(row.get("net_edge_pct")), 6),
        "paper_open_real_edge_pct": round(
            _safe_float(row.get("real_edge_pct", row.get("net_edge_pct"))), 6
        ),
        "paper_long_rate_pct": round(_safe_float(row.get("long_rate_pct")), 6),
        "paper_short_rate_pct": round(_safe_float(row.get("short_rate_pct")), 6),
        "paper_long_interval_h": _safe_float(row.get("long_interval_h"), 8.0),
        "paper_short_interval_h": _safe_float(row.get("short_interval_h"), 8.0),
        "paper_long_next_settle_ms": _safe_int(row.get("long_settle_ms")),
        "paper_short_next_settle_ms": _safe_int(row.get("short_settle_ms")),
    }


def _entry_base(row: dict[str, Any], position: dict[str, Any] | None, result: Any) -> dict[str, Any]:
    pos = position or {}
    position_id = str(pos.get("id") or result_position_id(result))
    trade_usd = position_trade_usd(pos) or _safe_float(row.get("trade_usd") or row.get("amount_usd"))
    return {
        "position_id": position_id,
        "base": str(pos.get("base") or row.get("base") or "").upper(),
        "direction": str(pos.get("direction") or row.get("direction") or "forward"),
        "long_venue": str(pos.get("long_venue") or row.get("long_venue") or ""),
        "short_venue": str(pos.get("short_venue") or row.get("short_venue") or ""),
        "trade_usd": _round_money(trade_usd),
        "gross_notional_usd": _round_money(trade_usd * 2.0),
        "qty": _safe_float(pos.get("qty")),
    }


def record_bot_open(
    row: dict[str, Any],
    position: dict[str, Any] | None,
    result: Any,
    trade_usd: float,
) -> dict[str, Any] | None:
    if not result_ok(result):
        return None
    base = _entry_base({**row, "trade_usd": trade_usd}, position, result)
    position_id = str(base.get("position_id") or "")
    if not position_id or ledger_has_event(position_id, "open"):
        return None
    fee_pct = fee_pct_from_row(row)
    open_fee = estimate_open_fee_usd(row, trade_usd)
    event = {
        "type": "open",
        **base,
        "long_price": _safe_float((position or {}).get("long_price") or row.get("long_mark")),
        "short_price": _safe_float((position or {}).get("short_price") or row.get("short_mark")),
        "fee_pct": round(fee_pct, 6),
        "fee_usd": _round_money(open_fee),
        "open_fee_usd": _round_money(open_fee),
        "close_fee_est_usd": _round_money(open_fee),
        "round_trip_fee_est_usd": _round_money(open_fee * 2.0),
        "margin_usd": _round_money(margin_required_usd(trade_usd)),
        "price_pnl_usd": 0.0,
        "funding_pnl_usd": 0.0,
        "gross_pnl_usd": 0.0,
        "net_pnl_usd": _round_money(-open_fee),
        "long_rate_pct": _safe_float(row.get("long_rate_pct")),
        "short_rate_pct": _safe_float(row.get("short_rate_pct")),
        "real_edge_pct": _safe_float(row.get("real_edge_pct", row.get("net_edge_pct"))),
        "net_edge_pct": _safe_float(row.get("net_edge_pct")),
    }
    return append_account_event(event)


def _settlements_from_next(open_ms: int, close_ms: int, next_ms: int, interval_h: float) -> int:
    if close_ms <= open_ms or interval_h <= 0:
        return 0
    interval_ms = int(interval_h * 3600 * 1000)
    if interval_ms <= 0:
        return 0
    if next_ms <= 0:
        return int(math.floor(close_ms / interval_ms) - math.floor(open_ms / interval_ms))
    first = next_ms
    while first <= open_ms:
        first += interval_ms
    if first > close_ms:
        return 0
    return 1 + int((close_ms - first) // interval_ms)


def estimate_settled_funding_pnl_usd(
    position: dict[str, Any],
    close_ms: int | None = None,
) -> tuple[float, int, int]:
    open_ms = _to_ms(position.get("opened_at") or position.get("open_time"), _now_ms())
    close_ms = close_ms or _now_ms()
    if open_ms is None:
        return 0.0, 0, 0

    qty = _safe_float(position.get("qty"))
    trade_usd = position_trade_usd(position)
    long_notional = qty * _safe_float(position.get("long_price")) if qty > 0 else trade_usd
    short_notional = qty * _safe_float(position.get("short_price")) if qty > 0 else trade_usd
    if long_notional <= 0:
        long_notional = trade_usd
    if short_notional <= 0:
        short_notional = trade_usd

    long_rate = _safe_float(position.get("paper_long_rate_pct"))
    short_rate = _safe_float(position.get("paper_short_rate_pct"))
    long_interval = _safe_float(position.get("paper_long_interval_h"), 8.0)
    short_interval = _safe_float(position.get("paper_short_interval_h"), 8.0)
    long_next = _safe_int(position.get("paper_long_next_settle_ms"))
    short_next = _safe_int(position.get("paper_short_next_settle_ms"))

    long_n = _settlements_from_next(open_ms, close_ms, long_next, long_interval)
    short_n = _settlements_from_next(open_ms, close_ms, short_next, short_interval)

    funding = short_n * short_rate * short_notional / 100.0
    funding -= long_n * long_rate * long_notional / 100.0
    return _round_money(funding), long_n, short_n


def estimate_price_pnl_usd(before: dict[str, Any], after: dict[str, Any] | None = None) -> float:
    pos = after or before
    close_info = pos.get("close_info") if isinstance(pos.get("close_info"), dict) else {}
    qty = _safe_float(before.get("qty") or pos.get("qty"))
    long_open = _safe_float(before.get("long_price"))
    short_open = _safe_float(before.get("short_price"))
    long_close = _safe_float(close_info.get("long_price"), long_open)
    short_close = _safe_float(close_info.get("short_price"), short_open)
    if qty <= 0:
        return 0.0
    return _round_money((long_close - long_open) * qty + (short_open - short_close) * qty)


def _close_fee_usd(position: dict[str, Any], open_event: dict[str, Any] | None) -> float:
    explicit = _safe_float(position.get("paper_close_fee_est_usd"))
    if explicit > 0:
        return explicit
    if open_event is not None:
        return _safe_float(open_event.get("close_fee_est_usd") or open_event.get("fee_usd"))
    return position_trade_usd(position) * _safe_float(position.get("paper_fee_pct")) / 100.0


def _open_fee_usd(position: dict[str, Any], open_event: dict[str, Any] | None) -> float:
    explicit = _safe_float(position.get("paper_open_fee_usd"))
    if explicit > 0:
        return explicit
    if open_event is not None:
        return _safe_float(open_event.get("open_fee_usd") or open_event.get("fee_usd"))
    return 0.0


def record_bot_close(
    before: dict[str, Any],
    after: dict[str, Any] | None,
    *,
    reason: str,
    edge: float | None,
    result: Any,
) -> dict[str, Any] | None:
    if not result_ok(result):
        return None
    position_id = str(before.get("id") or (after or {}).get("id") or result_position_id(result))
    if not position_id or ledger_has_event(position_id, "close"):
        return None

    pos_after = after or before
    open_event = ledger_open_event(position_id)
    closed_ms = _to_ms(pos_after.get("closed_at"), _now_ms()) or _now_ms()
    trade_usd = position_trade_usd(before)
    price_pnl = estimate_price_pnl_usd(before, pos_after)
    funding_pnl, long_settles, short_settles = estimate_settled_funding_pnl_usd(before, closed_ms)
    open_fee = _open_fee_usd(before, open_event)
    close_fee = _close_fee_usd(before, open_event)
    total_fee = open_fee + close_fee
    gross_pnl = price_pnl + funding_pnl
    net_pnl = gross_pnl - total_fee

    event = {
        "type": "close",
        "position_id": position_id,
        "base": str(before.get("base") or "").upper(),
        "direction": str(before.get("direction") or "forward"),
        "long_venue": str(before.get("long_venue") or ""),
        "short_venue": str(before.get("short_venue") or ""),
        "trade_usd": _round_money(trade_usd),
        "gross_notional_usd": _round_money(trade_usd * 2.0),
        "qty": _safe_float(before.get("qty")),
        "fee_usd": _round_money(close_fee),
        "open_fee_usd": _round_money(open_fee),
        "close_fee_usd": _round_money(close_fee),
        "total_fee_usd": _round_money(total_fee),
        "margin_released_usd": _round_money(margin_required_usd(trade_usd)),
        "price_pnl_usd": price_pnl,
        "funding_pnl_usd": funding_pnl,
        "gross_pnl_usd": _round_money(gross_pnl),
        "net_pnl_usd": _round_money(net_pnl),
        "long_settlements": long_settles,
        "short_settlements": short_settles,
        "edge_pct": edge,
        "reason": reason,
    }
    return append_account_event(event)


def build_account_snapshot(
    positions: list[dict[str, Any]],
    *,
    initial_balance_usdt: float = DEFAULT_INITIAL_BALANCE_USDT,
) -> dict[str, Any]:
    entries = read_account_ledger()
    bot_pos = bot_positions(positions)
    open_pos = [p for p in bot_pos if p.get("status") == "open"]
    open_events = [e for e in entries if e.get("type") == "open"]
    close_events = [e for e in entries if e.get("type") == "close"]

    event_open_ids = {str(e.get("position_id")) for e in open_events}
    legacy_open_fee = 0.0
    legacy_trade = 0.0
    for pos in bot_pos:
        if str(pos.get("id")) in event_open_ids:
            continue
        legacy_trade += position_trade_usd(pos)
        legacy_open_fee += _safe_float(pos.get("paper_open_fee_usd"))

    open_fee_paid = sum(_safe_float(e.get("fee_usd")) for e in open_events) + legacy_open_fee
    close_fee_paid = sum(_safe_float(e.get("fee_usd")) for e in close_events)
    fees_paid = open_fee_paid + close_fee_paid

    realized_price = sum(_safe_float(e.get("price_pnl_usd")) for e in close_events)
    realized_funding = sum(_safe_float(e.get("funding_pnl_usd")) for e in close_events)
    realized_gross = realized_price + realized_funding

    open_price = sum(
        _safe_float(p.get("unrealized_pnl_usd", p.get("pnl_usd")))
        for p in open_pos
    )
    open_funding = 0.0
    for pos in open_pos:
        funding, _, _ = estimate_settled_funding_pnl_usd(pos)
        open_funding += funding

    reserved_margin = sum(
        _safe_float(p.get("paper_margin_usd"))
        or margin_required_usd(position_trade_usd(p))
        for p in open_pos
    )
    total_trade = sum(_safe_float(e.get("trade_usd")) for e in open_events) + legacy_trade
    gross_pnl = realized_gross + open_price + open_funding
    net_pnl = gross_pnl - fees_paid
    initial = max(0.0, _safe_float(initial_balance_usdt, DEFAULT_INITIAL_BALANCE_USDT))
    available = initial + realized_gross - fees_paid - reserved_margin
    equity = initial + net_pnl

    closed_count = len(close_events)
    wins = sum(1 for e in close_events if _safe_float(e.get("net_pnl_usd")) > 0)
    losses = sum(1 for e in close_events if _safe_float(e.get("net_pnl_usd")) < 0)
    win_rate = (wins / closed_count * 100.0) if closed_count else 0.0

    return {
        "currency": "USDT",
        "initial_balance_usdt": _round_money(initial),
        "equity_usdt": _round_money(equity),
        "available_balance_usdt": _round_money(available),
        "reserved_margin_usdt": _round_money(reserved_margin),
        "gross_pnl_usdt": _round_money(gross_pnl),
        "net_pnl_usdt": _round_money(net_pnl),
        "realized_pnl_usdt": _round_money(realized_gross - close_fee_paid),
        "unrealized_pnl_usdt": _round_money(open_price + open_funding),
        "price_pnl_usdt": _round_money(realized_price + open_price),
        "funding_pnl_usdt": _round_money(realized_funding + open_funding),
        "fees_paid_usdt": _round_money(fees_paid),
        "open_fee_paid_usdt": _round_money(open_fee_paid),
        "close_fee_paid_usdt": _round_money(close_fee_paid),
        "total_trade_usdt": _round_money(total_trade),
        "open_positions": len(open_pos),
        "closed_trades": closed_count,
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(win_rate, 1),
        "ledger_entries": len(entries),
        "updated_at": now_iso(),
    }

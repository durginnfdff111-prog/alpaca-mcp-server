"""Read-only Alpaca paper-account preview for proposed stock orders.

Never posts orders. Risk facts come from broker APIs, not caller-supplied values.
In production no verified out-of-sample strategy statistics are connected yet,
so the preview deliberately fails closed. All output is indicative only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from math import isfinite
import re
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .crash_guard import evaluate_position

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
_PAPER_URL = "https://paper-api.alpaca.markets"
_DATA_URL = "https://data.alpaca.markets"
_ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class VerifiedEdge:
    """Internal-only, independently validated out-of-sample strategy statistics.

    This object must NEVER be constructed from MCP order arguments.
    Connecting an auditable strategy-statistics store is a separate task.
    """
    win_probability: float
    win_loss_ratio: float
    oos_trades: int
    oos_verified: bool

    def __post_init__(self) -> None:
        if not self.oos_verified or self.oos_trades < 100:
            raise ValueError("at least 100 verified out-of-sample trades required")
        if not (isfinite(self.win_probability) and 0 < self.win_probability < 1):
            raise ValueError("invalid win probability")
        if not (isfinite(self.win_loss_ratio) and self.win_loss_ratio > 0):
            raise ValueError("invalid win/loss ratio")


def _deny(reason: str, **detail: Any) -> dict[str, Any]:
    return {
        "mode": "PAPER_DRY_RUN",
        "dry_run": True,
        "submitted": False,
        "approved": False,
        "reason": reason,
        **detail,
    }


def _decimal(value: Any, name: str) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not number.is_finite() or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _when(value: Any) -> datetime:
    if isinstance(value, (int, float)) and isfinite(value):
        return datetime.fromtimestamp(value, timezone.utc)
    if not isinstance(value, str):
        raise ValueError("missing or invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


async def _get(client: httpx.AsyncClient, url: str, params: dict | None = None) -> Any:
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def _history_peak(history: Any, equity: Decimal, account: dict, now: datetime) -> Decimal:
    """Require full account history and no external cashflow before raw-equity peak."""
    if not isinstance(history, dict):
        raise ValueError("portfolio history malformed")
    equity_series = history.get("equity")
    timestamps = history.get("timestamp")
    if not isinstance(equity_series, list) or not isinstance(timestamps, list):
        raise ValueError("portfolio history missing equity/timestamp")
    if len(equity_series) < 30 or len(equity_series) != len(timestamps):
        raise ValueError("portfolio history too short or misaligned")
    values = [_decimal(v, "historical equity") for v in equity_series]
    dates = [_when(t) for t in timestamps]
    if dates != sorted(dates) or dates[-1] < now - timedelta(days=7):
        raise ValueError("portfolio history stale or unordered")
    cashflow = history.get("cashflow")
    if not isinstance(cashflow, dict):
        raise ValueError("missing cashflow details; drawdown cannot be validated")
    for amounts in cashflow.values():
        if not isinstance(amounts, list) or len(amounts) != len(values):
            raise ValueError("cashflow history malformed")
        if any(Decimal(str(v)) != 0 for v in amounts):
            raise ValueError("cash movements require cashflow-adjusted drawdown")
    created_at = account.get("created_at")
    if not created_at:
        raise ValueError("account inception unknown; lifetime peak unverified")
    created = _when(created_at)
    if created < now - timedelta(days=365):
        raise ValueError("account history exceeds 1-year window; lifetime peak unverified")
    if dates[0] > created + timedelta(days=7):
        raise ValueError("history does not cover account inception")
    return max([equity, *values])


def _closes_from_bars(payload: Any, now: datetime) -> tuple[list[float], datetime]:
    if not isinstance(payload, dict) or payload.get("next_page_token"):
        raise ValueError("incomplete historical bars or unexpected pagination")
    bars = payload.get("bars")
    if not isinstance(bars, list) or len(bars) < 200:
        raise ValueError("at least 200 complete daily bars required")
    times = [_when(bar["t"]) for bar in bars]
    if times != sorted(times) or len(set(times)) != len(times):
        raise ValueError("bars unordered or duplicated")
    if not now - timedelta(days=7) <= times[-1] < now:
        raise ValueError("latest daily bar is stale or incomplete")
    if times[-1].astimezone(_ET).date() >= now.astimezone(_ET).date():
        raise ValueError("current New York session bar may be incomplete")
    closes = [float(_decimal(bar["c"], "close")) for bar in bars]
    return closes, times[-1]


def _supported_order(order: dict) -> tuple[str, Decimal]:
    symbol = str(order.get("symbol", "")).upper().strip()
    if not _SYMBOL.fullmatch(symbol):
        raise ValueError("invalid stock ticker")
    if order.get("side") != "buy":
        raise ValueError("only long stock buy previews are implemented; sells need reduce-only validation")
    if order.get("type") not in {"market", "limit"} or order.get("time_in_force") != "day":
        raise ValueError("only day market/limit orders are supported in preview")
    if order.get("extended_hours") or order.get("advanced_instructions"):
        raise ValueError("extended hours/algorithm orders are not supported")
    if order.get("order_class") not in (None, "simple"):
        raise ValueError("bracket or contingent orders need separate verification")
    if any(key in order for key in ("stop_price", "stop_loss", "take_profit", "trail_price", "trail_percent")):
        raise ValueError("complex orders are not supported")
    if ("qty" in order) == ("notional" in order):
        raise ValueError("specify exactly one of qty and notional")
    amount = _decimal(order.get("qty", order.get("notional")), "qty/notional")
    if order.get("type") == "limit":
        if "notional" in order:
            raise ValueError("notional cannot be used with limit orders")
        _decimal(order.get("limit_price"), "limit_price")
    return symbol, amount


async def preview_stock_order(
    trading_client: httpx.AsyncClient,
    data_client: httpx.AsyncClient | None,
    order: dict,
    *,
    edge: VerifiedEdge | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Evaluate a paper-only stock buy without submitting or altering any order.

    edge is internal/test-only; runtime never supplies it until a separate
    auditable out-of-sample validation path is implemented.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return _deny("time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if str(trading_client.base_url).rstrip("/") != _PAPER_URL:
        return _deny("not connected to official Alpaca paper endpoint")
    if data_client is None or str(data_client.base_url).rstrip("/") != _DATA_URL:
        return _deny("official Alpaca market-data client unavailable")
    try:
        symbol, amount = _supported_order(order)
        account = await _get(trading_client, "/v2/account")
        equity = _decimal(account.get("equity"), "equity")
        cash = _decimal(account.get("cash"), "cash")
        if not isinstance(account, dict) or account.get("status") != "ACTIVE":
            return _deny("account status unverified")
        if any(account.get(k) is True for k in (
            "trading_blocked", "account_blocked", "trade_suspended_by_user"
        )):
            return _deny("account has trading restrictions")
        history = await _get(
            trading_client,
            "/v2/account/portfolio/history",
            {"period": "1A", "timeframe": "1D", "cashflow_types": "ALL"},
        )
        peak_equity = _history_peak(history, equity, account, now)
        open_orders = await _get(trading_client, "/v2/orders", {"status": "open", "limit": 500})
        if not isinstance(open_orders, list) or open_orders:
            return _deny("open orders exist or order inventory unverified")
        positions = await _get(trading_client, "/v2/positions")
        if not isinstance(positions, list):
            return _deny("positions inventory unverified")
        existing = Decimal("0")
        for p in positions:
            if p.get("symbol") == symbol:
                qty = Decimal(str(p.get("qty", "0")))
                if qty < 0:
                    return _deny("short exposure requires separate risk controls")
                existing += max(Decimal("0"), Decimal(str(p.get("market_value", "0"))))
        bars = await _get(
            data_client,
            f"/v2/stocks/{symbol}/bars",
            {
                "timeframe": "1Day",
                "start": (now - timedelta(days=430)).date().isoformat(),
                "end": now.astimezone(_ET).date().isoformat(),
                "limit": 1000,
                "adjustment": "split",
                "feed": "iex",
            },
        )
        closes, asof = _closes_from_bars(bars, now)
        indicative_price = Decimal(str(closes[-1]))
        proposed_notional = amount * indicative_price if "qty" in order else amount
        if order["type"] == "limit":
            # Budget against the higher of price history and the user's limit.
            proposed_notional = amount * max(
                indicative_price, _decimal(order["limit_price"], "limit_price")
            ) if "qty" in order else amount
        if proposed_notional > cash:
            return _deny("insufficient unlevered cash", cash=str(cash))
        if edge is None:
            return _deny(
                "no verified out-of-sample Kelly statistics; fail closed",
                equity=str(equity),
                peak_equity=str(peak_equity),
                latest_completed_bar=asof.isoformat(),
                indicative_price=str(indicative_price),
            )
        decision = evaluate_position(
            equity=float(equity),
            peak_equity=float(peak_equity),
            price=float(indicative_price),
            closes=closes,
            win_probability=edge.win_probability,
            win_loss_ratio=edge.win_loss_ratio,
            paper_mode=True,
        )
        if not decision.approved:
            return _deny(decision.reason, risk=asdict(decision))
        if existing + proposed_notional > Decimal(str(decision.notional)):
            return _deny(
                "proposed total position exceeds volatility/trend/Kelly/drawdown cap",
                allowed_total_notional=decision.notional,
                existing_notional=str(existing),
                proposed_notional=str(proposed_notional),
                risk=asdict(decision),
            )
        return {
            "mode": "PAPER_DRY_RUN",
            "dry_run": True,
            "submitted": False,
            "approved": True,
            "reason": "risk checks passed for paper preview only; order NOT submitted",
            "symbol": symbol,
            "indicative_price": str(indicative_price),
            "proposed_notional": str(proposed_notional),
            "latest_completed_bar": asof.isoformat(),
            "risk": asdict(decision),
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError, OverflowError, ArithmeticError) as exc:
        return _deny(f"risk data/validation unavailable: {type(exc).__name__}: {exc}")

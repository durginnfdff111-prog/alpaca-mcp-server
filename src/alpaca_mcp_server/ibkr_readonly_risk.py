"""IBKR read-only portfolio risk assessment. No broker SDK, network, or orders.

Observed NAV peaks are NOT cash-flow adjusted. Never interpret this module's
risk tier as permission to trade. All outputs are advisory only.
"""
from __future__ import annotations

from math import isfinite
from typing import Mapping, Sequence


def evaluate_ibkr_risk(
    *,
    net_liquidation: float,
    cash: float,
    positions: Sequence[Mapping[str, object]],
    historical_nav: Sequence[float],
    cash_flow_adjusted: bool = False,
) -> dict:
    """Evaluate account-level drawdown and concentration from trusted snapshots.

    Positions must contain market_value. The caller must independently verify
    broker provenance, timestamps, deposits/withdrawals, and stale quotations.
    """
    if not historical_nav:
        raise ValueError("NAV history required")
    if not isfinite(net_liquidation) or net_liquidation <= 0:
        raise ValueError("positive finite net liquidation required")
    if not isfinite(cash):
        raise ValueError("finite cash required")
    if any(not isfinite(v) or v <= 0 for v in historical_nav):
        raise ValueError("NAV history must be positive and finite")
    values = []
    for p in positions:
        value = float(p["market_value"])
        if not isfinite(value):
            raise ValueError("finite position market values required")
        values.append(value)

    observed_peak = max(net_liquidation, *historical_nav)
    drawdown = max(0.0, 1.0 - net_liquidation / observed_peak)
    if drawdown >= 0.25:
        tier, multiplier = "HARD_HALT_REVIEW", 0.0
    elif drawdown >= 0.20:
        tier, multiplier = "NO_NEW_RISK", 0.0
    elif drawdown >= 0.15:
        tier, multiplier = "QUARTER_NEW_RISK", 0.25
    elif drawdown >= 0.10:
        tier, multiplier = "HALF_NEW_RISK", 0.5
    else:
        tier, multiplier = "NORMAL_REVIEW", 1.0

    long_exposure = sum(max(0.0, v) for v in values)
    gross_exposure = sum(abs(v) for v in values)
    top_weight = max((abs(v) / net_liquidation for v in values), default=0.0)
    return {
        "source": "caller_supplied_ibkr_snapshot_unverified",
        "advisory_only": True,
        "execution_authorized": False,
        "submitted": False,
        "cash_flow_adjusted": cash_flow_adjusted,
        "drawdown_is_provisional": not cash_flow_adjusted,
        "observed_peak_nav": observed_peak,
        "observed_drawdown": drawdown,
        "risk_tier": tier,
        "new_risk_multiplier_guidance": multiplier,
        "net_liquidation": net_liquidation,
        "cash": cash,
        "position_count": len(values),
        "gross_exposure": gross_exposure,
        "long_exposure": long_exposure,
        "largest_absolute_position_weight": top_weight,
        "positions_over_10pct": sum(abs(v) / net_liquidation > 0.10 for v in values),
        "nav_reconciliation_difference": net_liquidation - cash - sum(values),
        "requires_verified_market_history_for_vol_trend_kelly": True,
    }

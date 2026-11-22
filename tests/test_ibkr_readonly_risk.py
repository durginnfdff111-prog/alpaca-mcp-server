"""Offline tests for the IBKR read-only risk adapter; no credentials."""
import pytest

from alpaca_mcp_server.ibkr_readonly_risk import evaluate_ibkr_risk


def assess(equity=17895.39):
    return evaluate_ibkr_risk(
        net_liquidation=equity, cash=1.10,
        positions=[{"market_value": 8105.75}, {"market_value": 5635.84},
                   {"market_value": 4106.50}],
        historical_nav=[21210.89, 20500.0],
    )


def test_drawdown_tier_and_no_order_authorization():
    result = assess()
    assert result["risk_tier"] == "QUARTER_NEW_RISK"
    assert result["new_risk_multiplier_guidance"] == 0.25
    assert result["drawdown_is_provisional"]
    assert not result["execution_authorized"]
    assert not result["submitted"]
    assert result["position_count"] == 3
    assert result["positions_over_10pct"] == 3


@pytest.mark.parametrize("equity,tier,multiplier", [
    (21210.89, "NORMAL_REVIEW", 1.0),
    (19000.0, "HALF_NEW_RISK", 0.5),
    (18000.0, "QUARTER_NEW_RISK", 0.25),
    (16500.0, "NO_NEW_RISK", 0.0),
    (15000.0, "HARD_HALT_REVIEW", 0.0),
])
def test_drawdown_tiers(equity, tier, multiplier):
    result = assess(equity)
    assert result["risk_tier"] == tier
    assert result["new_risk_multiplier_guidance"] == multiplier


@pytest.mark.parametrize("bad_nav", [[], [0.0], [float("nan")]])
def test_bad_history_fails_closed(bad_nav):
    with pytest.raises(ValueError):
        evaluate_ibkr_risk(net_liquidation=100, cash=0,
                           positions=[], historical_nav=bad_nav)

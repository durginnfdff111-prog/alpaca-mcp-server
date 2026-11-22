"""Offline paper-mode crash guard and trading transport tests; zero credentials."""
import httpx
import pytest

from alpaca_mcp_server.crash_guard import RiskConfig, evaluate_position
from alpaca_mcp_server.paper_gateway import (
    PAPER_API_URL,
    block_broker_mutations,
    require_paper_endpoint,
)


def rising_closes():
    return [100.0 + index * 0.5 for index in range(220)]


def risk(**kwargs):
    values = {
        "equity": 10000,
        "peak_equity": 10000,
        "price": 200,
        "closes": rising_closes(),
        "win_probability": 0.6,
        "win_loss_ratio": 1.5,
        "paper_mode": True,
    }
    values.update(kwargs)
    return evaluate_position(**values)


def test_four_risk_controls_are_active():
    normal = risk()
    assert normal.approved and normal.mode == "PAPER_ONLY"
    assert 0 < normal.weight <= RiskConfig().max_position_weight
    assert normal.weight <= RiskConfig().fractional_kelly * normal.kelly_fraction
    assert normal.annualized_vol > 0

    ten_percent = risk(equity=9000, peak_equity=10000)
    fifteen_percent = risk(equity=8500, peak_equity=10000)
    twenty_percent = risk(equity=8000, peak_equity=10000)
    hard_limit = risk(equity=7500, peak_equity=10000)
    assert ten_percent.approved and ten_percent.weight <= normal.weight * 0.5
    assert fifteen_percent.approved and fifteen_percent.weight <= normal.weight * 0.25
    assert not twenty_percent.approved and twenty_percent.weight == 0
    assert not hard_limit.approved and "halted" in hard_limit.reason

    assert not risk(closes=list(reversed(rising_closes()))).approved
    assert not risk(win_probability=0.3, win_loss_ratio=1).approved


def test_kelly_requires_finite_inputs_and_paper_only():
    with pytest.raises(ValueError):
        risk(win_probability=float("nan"))
    with pytest.raises(ValueError):
        risk(closes=[float("inf")] * 220)
    with pytest.raises(RuntimeError, match="paper-only"):
        risk(paper_mode=False)


def test_live_trading_endpoint_rejected():
    require_paper_endpoint(PAPER_API_URL)
    with pytest.raises(RuntimeError, match="live trading is disabled"):
        require_paper_endpoint("https://api.alpaca.markets")
    with pytest.raises(RuntimeError):
        require_paper_endpoint("https://paper-api.alpaca.markets.evil.example")


@pytest.mark.asyncio
async def test_all_broker_mutations_are_blocked_before_network_io():
    observed = []

    async def mock_handler(req: httpx.Request):
        observed.append(req.method)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(
        base_url=PAPER_API_URL,
        transport=httpx.MockTransport(mock_handler),
        event_hooks={"request": [block_broker_mutations]},
    ) as client:
        for method, path in [
            ("POST", "/v2/orders"),
            ("PATCH", "/v2/orders/123"),
            ("DELETE", "/v2/orders/123"),
            ("DELETE", "/v2/positions/AAPL"),
            ("POST", "/v2/positions/AAPL/exercise"),
            ("PATCH", "/v2/account/configurations"),
        ]:
            with pytest.raises(PermissionError, match="No request was submitted"):
                await client.request(method, path)
        assert observed == []
        result = await client.get("/v2/account")
        assert result.status_code == 200
        assert observed == ["GET"]

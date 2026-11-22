"""Offline paper-mode crash guard and trading transport tests; zero credentials."""
import httpx
import pytest

from alpaca_mcp_server.crash_guard import RiskConfig, evaluate_position
from alpaca_mcp_server.paper_preview import register_paper_preview_tool
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


@pytest.mark.asyncio
async def test_preview_tool_never_submits_orders_even_if_model_approves():
    class FakeServer:
        def tool(self, **_annotations):
            def decorated(func):
                self.preview = func
                return func
            return decorated

    server = FakeServer()
    register_paper_preview_tool(server)
    result = await server.preview(
        equity=10000,
        peak_equity=10000,
        price=200,
        closes=rising_closes(),
        win_probability=0.6,
        win_loss_ratio=1.5,
    )
    assert result["simulation_valid"] is True
    assert result["decision"]["approved"] is True
    assert result["execution_authorized"] is False
    assert result["submitted"] is False
    assert "unverified" in result["input_provenance"]

    rejected = await server.preview(
        equity=0,
        peak_equity=10000,
        price=200,
        closes=rising_closes(),
        win_probability=0.6,
        win_loss_ratio=1.5,
    )
    assert rejected["simulation_valid"] is False
    assert rejected["execution_authorized"] is False
    assert rejected["submitted"] is False


def test_server_construction_rejects_live_environment(monkeypatch):
    from alpaca_mcp_server.server import build_server

    monkeypatch.setenv("ALPACA_PAPER_TRADE", "false")
    monkeypatch.setenv("ALPACA_TOOLSETS", "account,trading")
    monkeypatch.setenv("ALPACA_API_KEY", "dummy")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "dummy")
    with pytest.raises(RuntimeError, match="live trading is disabled"):
        build_server()


def test_server_attaches_gateway_to_shared_trading_client(monkeypatch):
    from alpaca_mcp_server import server as server_module

    observed = []
    original_factory = server_module._make_api_client

    def capture_client(base_url, headers):
        client = original_factory(base_url, headers)
        observed.append((base_url, client))
        return client

    monkeypatch.setenv("ALPACA_PAPER_TRADE", "true")
    monkeypatch.setenv("ALPACA_TOOLSETS", "account,trading")
    monkeypatch.setenv("ALPACA_API_KEY", "dummy")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "dummy")
    monkeypatch.setattr(server_module, "_make_api_client", capture_client)
    server_module.build_server()
    assert len(observed) == 1
    base_url, trading_client = observed[0]
    assert base_url == PAPER_API_URL
    assert block_broker_mutations in trading_client.event_hooks["request"]


@pytest.mark.parametrize("equity,expected_approved,expected_brake", [
    (9999.0, True, 1.0),
    (9000.0, True, 0.5),
    (8500.0, True, 0.25),
    (8000.0, False, 0.0),
    (7500.0, False, 0.0),
])
def test_drawdown_brake_boundaries(equity, expected_approved, expected_brake):
    decision = risk(equity=equity)
    baseline = risk()
    assert decision.approved is expected_approved
    assert decision.weight <= baseline.weight * expected_brake + 1e-12


@pytest.mark.parametrize("bad_price", [0, -1, float("inf"), float("nan")])
def test_reject_invalid_prices(bad_price):
    with pytest.raises(ValueError):
        risk(price=bad_price)


@pytest.mark.parametrize("method", ["PUT", "TRACE", "CONNECT"])
@pytest.mark.asyncio
async def test_unusual_http_methods_blocked(method):
    observed = []

    async def handler(request: httpx.Request):
        observed.append(request.method)
        return httpx.Response(200)

    async with httpx.AsyncClient(
        base_url=PAPER_API_URL,
        transport=httpx.MockTransport(handler),
        event_hooks={"request": [block_broker_mutations]},
    ) as client:
        with pytest.raises(PermissionError):
            await client.request(method, "/v2/orders")
    assert not observed

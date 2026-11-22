"""Guarded order previews: all broker calls mocked, no real orders or credentials."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from unittest.mock import patch

import httpx
import pytest
from fastmcp.client import Client
from fastmcp import FastMCP

from alpaca_mcp_server.overrides import register_order_tools

from alpaca_mcp_server.crash_guard import evaluate_position
from alpaca_mcp_server.paper_risk import VerifiedEdge, preview_stock_order
from alpaca_mcp_server.server import build_server

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


def fixtures(*, equity=10_000, cash=10_000, highwater=10_000, cashflow=0):
    created = NOW - timedelta(days=40)
    dates = [NOW - timedelta(days=39 - i) for i in range(40)]
    account = {
        "equity": str(equity),
        "cash": str(cash),
        "status": "ACTIVE",
        "created_at": created.isoformat(),
    }
    history = {
        "equity": [highwater] + [equity] * 39,
        "timestamp": [round(v.timestamp()) for v in dates],
        "cashflow": {"CSD": [cashflow] + [0] * 39},
    }
    bars = {
        "bars": [
            {"t": (NOW - timedelta(days=220 - i)).isoformat(), "c": 100 + 0.5 * i}
            for i in range(220)
        ],
        "next_page_token": None,
    }
    return account, history, bars


def fake_clients(account, history, bars):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET", "a preview MUST NEVER submit or mutate"
        routes = {
            "/v2/account": account,
            "/v2/account/portfolio/history": history,
            "/v2/orders": [],
            "/v2/positions": [],
            "/v2/stocks/SPY/bars": bars,
        }
        return httpx.Response(200, json=routes[request.url.path])

    transport = httpx.MockTransport(handler)
    trading = httpx.AsyncClient(transport=transport, base_url="https://paper-api.alpaca.markets")
    market = httpx.AsyncClient(transport=transport, base_url="https://data.alpaca.markets")
    return trading, market, calls


@pytest.mark.asyncio
async def test_runtime_without_verified_kelly_fails_closed_after_broker_reads():
    account, history, bars = fixtures()
    trading, market, calls = fake_clients(account, history, bars)
    async with trading, market:
        result = await preview_stock_order(
            trading, market, {"symbol": "SPY", "side": "buy",
                              "type": "market", "time_in_force": "day", "qty": "1"},
            now=NOW,
        )
    assert result["dry_run"] and not result["submitted"] and not result["approved"]
    assert "verified out-of-sample" in result["reason"]
    assert len(calls) == 5 and all(method == "GET" for method, _ in calls)


@pytest.mark.asyncio
async def test_mock_verified_edge_only_allows_nonexecuting_preview():
    account, history, bars = fixtures()
    trading, market, calls = fake_clients(account, history, bars)
    edge = VerifiedEdge(0.60, 1.5, 250, True)  # test fixture ONLY; no runtime stats connected
    async with trading, market:
        result = await preview_stock_order(
            trading, market, {"symbol": "SPY", "side": "buy",
                              "type": "market", "time_in_force": "day", "qty": "1"},
            edge=edge, now=NOW,
        )
    assert result["approved"] and result["dry_run"] and not result["submitted"]
    assert result["risk"]["weight"] <= 0.10
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.asyncio
async def test_hard_drawdown_rejects_even_with_mock_validated_edge():
    account, history, bars = fixtures(equity=7500, cash=7500, highwater=10000)
    trading, market, calls = fake_clients(account, history, bars)
    async with trading, market:
        result = await preview_stock_order(
            trading, market, {"symbol": "SPY", "side": "buy",
                              "type": "market", "time_in_force": "day", "qty": "1"},
            edge=VerifiedEdge(0.60, 1.5, 250, True), now=NOW,
        )
    assert not result["approved"]
    assert "halted" in result["reason"]
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.asyncio
async def test_cashflow_history_blocks_unadjusted_drawdown():
    account, history, bars = fixtures(cashflow=100)
    trading, market, _ = fake_clients(account, history, bars)
    async with trading, market:
        result = await preview_stock_order(
            trading, market, {"symbol": "SPY", "side": "buy",
                              "type": "market", "time_in_force": "day", "qty": "1"},
            now=NOW,
        )
    assert not result["approved"]
    assert "cash movements" in result["reason"]


@pytest.mark.asyncio
async def test_unknown_lifetime_highwater_rejected():
    account, history, bars = fixtures()
    account.pop("created_at")
    trading, market, _ = fake_clients(account, history, bars)
    async with trading, market:
        result = await preview_stock_order(
            trading, market, {"symbol": "SPY", "side": "buy",
                              "type": "market", "time_in_force": "day", "qty": "1"},
            now=NOW,
        )
    assert not result["approved"]
    assert "inception unknown" in result["reason"]


@pytest.mark.asyncio
async def test_broker_live_url_blocked_without_any_request():
    calls = []
    def handle(req):
        calls.append(req)
        raise AssertionError("should never be called")
    async with httpx.AsyncClient(
        base_url="https://api.alpaca.markets",
        transport=httpx.MockTransport(handle),
    ) as trading:
        result = await preview_stock_order(trading, None, {}, now=NOW)
    assert not result["approved"] and not calls
    assert "official Alpaca paper" in result["reason"]


@pytest.mark.asyncio
async def test_unsupported_orders_fail_before_any_broker_request():
    account, history, bars = fixtures()
    trading, market, calls = fake_clients(account, history, bars)
    async with trading, market:
        result = await preview_stock_order(
            trading, market, {"symbol": "SPY", "side": "sell",
                              "type": "market", "time_in_force": "day", "qty": "1"},
            now=NOW,
        )
    assert not result["approved"] and not calls


def test_live_mode_fails_core_evaluator():
    with pytest.raises(RuntimeError, match="paper-only"):
        evaluate_position(
            equity=10000, peak_equity=10000, price=200,
            closes=[100 + i * 0.5 for i in range(220)],
            win_probability=0.6, win_loss_ratio=1.5, paper_mode=False,
        )


def test_unverified_edge_rejected():
    with pytest.raises(ValueError, match="100 verified"):
        VerifiedEdge(0.60, 1.5, 20, False)


def test_guarded_server_refuses_live_base_url():
    with patch.dict(os.environ, {
        "CRASH_GUARD_PAPER_DRY_RUN": "true",
        "ALPACA_PAPER_TRADE": "false",
        "ALPACA_API_KEY": "test-key",
        "ALPACA_SECRET_KEY": "test-secret",
    }):
        with pytest.raises(ValueError, match="refuses"):
            build_server()


@pytest.mark.asyncio
async def test_guarded_toolset_does_not_expose_unguarded_mutations():
    with patch.dict(os.environ, {
        "CRASH_GUARD_PAPER_DRY_RUN": "true",
        "ALPACA_PAPER_TRADE": "true",
        "ALPACA_TOOLSETS": "account,trading",
        "ALPACA_API_KEY": "test-key",
        "ALPACA_SECRET_KEY": "test-secret",
    }):
        server = build_server()
    async with Client(server) as client:
        tools = await client.list_tools()
    names = {t.name for t in tools}
    assert "place_stock_order" in names
    assert "place_crypto_order" in names
    assert "place_option_order" in names
    assert "get_account_info" in names
    for dangerous in (
        "replace_order_by_id", "close_position", "close_all_positions",
        "exercise_options_position", "update_account_config",
    ):
        assert dangerous not in names


@pytest.mark.asyncio
async def test_all_three_guarded_order_tool_routes_never_post():
    calls = []
    def forbid_network(req):
        calls.append((req.method, req.url.path))
        raise AssertionError("paper preview must not send orders")

    async with httpx.AsyncClient(
        base_url="https://paper-api.alpaca.markets",
        transport=httpx.MockTransport(forbid_network),
    ) as trading:
        server = FastMCP("paper-order-safety")
        register_order_tools(
            server, trading, data_client=None, crash_guard_dry_run=True,
        )
        async with Client(transport=server) as client:
            await client.call_tool("place_stock_order", {
                "symbol": "SPY", "side": "buy", "qty": "1"
            })
            await client.call_tool("place_crypto_order", {
                "symbol": "BTC/USD", "side": "buy", "qty": "0.01"
            })
            await client.call_tool("place_option_order", {
                "symbol": "SPY270115C00500000", "side": "buy", "qty": "1"
            })
    assert calls == []

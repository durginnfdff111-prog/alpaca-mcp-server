"""Explicitly hypothetical, no-order crash guard preview tool.

Caller-supplied statistics are ONLY for deterministic dry-run demonstrations.
They are never accepted as authority to place an actual order. A broker
integration must later use server-side account, price and independently
verified strategy statistics.
"""
from __future__ import annotations

from dataclasses import asdict

from fastmcp import FastMCP
from .crash_guard import evaluate_position


def register_paper_preview_tool(server: FastMCP) -> None:
    @server.tool(
        annotations={
            "title": "Preview Crash Guard (No Orders)",
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    async def preview_crash_guard(
        equity: float,
        peak_equity: float,
        price: float,
        closes: list[float],
        win_probability: float,
        win_loss_ratio: float,
    ) -> dict:
        """Simulate volatility, drawdown, trend and Kelly caps using hypothetical inputs.

        This tool NEVER contacts Alpaca or places/cancels/modifies orders.
        It cannot verify that the supplied equity/peak, closing prices,
        win probability or win/loss ratio are accurate, fresh or out-of-sample.
        Its output is NOT a trading approval, even when approved=True.
        """
        try:
            decision = evaluate_position(
                equity=equity,
                peak_equity=peak_equity,
                price=price,
                closes=closes,
                win_probability=win_probability,
                win_loss_ratio=win_loss_ratio,
                paper_mode=True,
            )
        except (ValueError, RuntimeError) as exc:
            return {
                "simulation_valid": False,
                "execution_authorized": False,
                "submitted": False,
                "error": str(exc),
            }
        return {
            "simulation_valid": True,
            "execution_authorized": False,
            "submitted": False,
            "input_provenance": "unverified_caller_supplied_hypothetical",
            "decision": asdict(decision),
        }

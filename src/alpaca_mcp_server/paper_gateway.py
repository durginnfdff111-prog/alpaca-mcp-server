"""Fail-closed, paper-only network boundary for the Crash Guard preview phase.

This phase supports read-only Alpaca account and market-data queries plus
deterministic offline risk simulations. It NEVER sends simulated or live orders.
The guard applies to the shared httpx trading client, including auto-generated
OpenAPI tools, order replacements and options/crypto order overrides.
"""
from __future__ import annotations

import httpx

PAPER_API_URL = "https://paper-api.alpaca.markets"


def require_paper_endpoint(url: str) -> None:
    """Reject a server configured for any other trading API origin."""
    if url != PAPER_API_URL:
        raise RuntimeError(
            "Crash Guard preview only supports the Alpaca paper endpoint; "
            "live trading is disabled"
        )


async def block_broker_mutations(request: httpx.Request) -> None:
    """Block *all* state-changing Alpaca trading API calls before network I/O."""
    if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
        raise PermissionError(
            "Crash Guard dry-run: Alpaca trading mutations are blocked "
            "(orders, replacements, cancellations, position closes, exercise, "
            "and account configuration). No request was submitted."
        )

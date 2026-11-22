"""Credential-backed Alpaca PAPER account probe. Strictly GET-only, no orders.

This test is excluded from PR CI and must be launched manually. It never logs
credentials, holdings, account numbers, buying power, or account equity.
"""
import os

import httpx
import pytest

from alpaca_mcp_server.paper_gateway import (
    PAPER_API_URL,
    block_broker_mutations,
    require_paper_endpoint,
)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_paper_account_readonly_connection():
    key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    assert key and secret, "Paper API credentials are required"
    assert os.environ.get("ALPACA_PAPER_TRADE") == "true"
    require_paper_endpoint(PAPER_API_URL)

    async with httpx.AsyncClient(
        base_url=PAPER_API_URL,
        headers={
            "APCA-API-KEY-ID": key,
            "APCA-API-SECRET-KEY": secret,
        },
        event_hooks={"request": [block_broker_mutations]},
        timeout=15,
    ) as client:
        response = await client.get("/v2/account")
        # No response body in assertion messages; avoids leaking account details.
        assert response.status_code == 200, (
            f"Paper account GET failed with HTTP {response.status_code}"
        )
        data = response.json()
        assert isinstance(data, dict), "Unexpected paper account response"
        assert data.get("status") in {"ACTIVE", "INACTIVE"}, (
            "Paper account returned an unexpected status"
        )

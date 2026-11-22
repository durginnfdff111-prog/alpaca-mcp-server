# Crash Guard — Alpaca paper-only order preview

**Activation:** set `CRASH_GUARD_PAPER_DRY_RUN=true` in a **non-production test deployment** with `ALPACA_PAPER_TRADE=true`. The feature is **off by default** to avoid changing the existing production service during code review. Do **not** merge or deploy before reviewing the integration tests and the permissions of every order route.

## Implemented controls

- 20-return historical log-volatility estimate targeting 12% annualized volatility (provisional).
- A 50-day vs 200-day moving-average trend filter.
- Quarter Kelly and a 10%-of-equity per-position maximum.
- High-water drawdown brake: at 10% size × 0.5; 15% size × 0.25; 20% no new risk; 25% hard stop.
- No API-originated order submission in guarded mode. The stock order tool is a **GET-only preview**; crypto/option order overrides reject. OpenAPI-generated trading operations are limited to an explicit read-only allowlist, preventing bypass through order replace, position close, account mutations, and option exercise.
- Hard-fails server startup if guarded mode is requested with `ALPACA_PAPER_TRADE=false`.

## Broker-sourced checks

The preview reads account equity/cash, portfolio daily history and cashflow series, open orders, positions, and split-adjusted historical IEX daily bars. It blocks whenever history is stale, incomplete, has cash movements, or does not cover account inception. This deliberately conservative restriction means many accounts **cannot** pass yet. The portfolio-history lookback is only one year: older accounts fail closed rather than silently treating the one-year high as a lifetime high.

It uses the latest completed daily bar only as an **indicative** share price. It does not pretend this is an executable quote. All previews remain paper-only, even if the risk checks pass.

**Critical missing piece:** There is no independently audited out-of-sample trade-statistics store connected to the runtime. Kelly needs verified win probability and average win/loss ratio; these must not come from user-supplied order arguments or invented defaults. As a result, **all runtime stock-buy previews currently fail closed** with a missing-statistics reason. Tests may inject an internal `VerifiedEdge` fixture solely to validate the full calculation; runtime never does so.

## Execution and API safety

- NO `POST /v2/orders`, `PATCH`, `DELETE`, or live orders can be performed through guarded order tools.
- SELL/short, option, crypto, bracket, trailing, and extended-hours orders are unimplemented in this preview and blocked.
- It rejects risk-increasing exposure if available cash is insufficient, existing holdings plus requested notional exceed the computed position cap, or another order is already open.
- The existing unguarded behavior is retained when `CRASH_GUARD_PAPER_DRY_RUN` is not enabled; do not treat this feature branch or an unmodified deployment as protected.

## Outstanding work before any real paper submission

1. Verify the canonical `https://hft.md/crash/llms.txt` rules. They were not retrievable in this environment; all numeric defaults above are **provisional engineering settings**, not attributed to hft.md.
2. Connect independently validated sample-out-of-sample trade statistics (at least 100 audited trades), and independently verify history completeness, cashflow normalization, data feed/quotes, per-portfolio exposure and Kelly estimation.
3. Integrate reduce-only sells, order replacement, position close and other risk-affecting routes into a unified authorization gate. **Do not simply re-enable them**.
4. Review network credentials, secret handling, order idempotency, and stale-price limits before even enabling a real Alpaca paper `POST` path.
5. Do not touch Railway production or submit a real/paper order without explicit user confirmation.

## Test command

```bash
pytest tests/test_crash_guard_paper_preview.py -v
```

Tests use `httpx.MockTransport` and fake account data. That is an **offline paper simulation**, not an order submitted to an Alpaca paper account. CI executes those tests without account credentials.

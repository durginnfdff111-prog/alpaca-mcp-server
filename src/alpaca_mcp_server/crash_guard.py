"""Paper-only Crash Guard sizing, shared with the isolated research prototype.

IMPORTANT: Pure risk preview, NEVER authorises or executes a broker order.
These numeric defaults are provisional pending verification of hft.md/crash/llms.txt.
Alpaca account equity, price history, cash flows, and validated out-of-sample Kelly
metrics MUST be sourced and checked by a separate trusted integration before trading.
"""

from __future__ import annotations
from dataclasses import dataclass
from math import isfinite, log, sqrt
from statistics import stdev
from typing import Sequence

@dataclass(frozen=True)
class RiskConfig:
    annual_vol_target: float = 0.12
    base_weight: float = 0.20
    max_position_weight: float = 0.10
    fractional_kelly: float = 0.25
    vol_lookback: int = 20
    fast_sma: int = 50
    slow_sma: int = 200
    brake_1: float = 0.10
    brake_2: float = 0.15
    brake_3: float = 0.20
    hard_drawdown: float = 0.25
    def __post_init__(self):
        if not (0 < self.annual_vol_target <= 1): raise ValueError("annual_vol_target must be in (0, 1]")
        if not (0 < self.base_weight <= 1 and 0 < self.max_position_weight <= 1): raise ValueError("weight limits must be in (0, 1]")
        if not (0 < self.fractional_kelly <= 1): raise ValueError("fractional_kelly must be in (0, 1]")
        if self.vol_lookback < 2 or self.fast_sma < 2 or self.slow_sma <= self.fast_sma: raise ValueError("invalid lookback lengths")
        if not (0 < self.brake_1 < self.brake_2 < self.brake_3 < self.hard_drawdown <= 1): raise ValueError("drawdown thresholds must be strictly increasing and <= 1")

@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    mode: str
    weight: float
    notional: float
    quantity: float
    annualized_vol: float
    drawdown: float
    kelly_fraction: float
    reason: str

def _kelly_fraction(win_probability: float, win_loss_ratio: float) -> float:
    if not 0 < win_probability < 1 or win_loss_ratio <= 0: raise ValueError("win_probability must be in (0, 1) and win_loss_ratio > 0")
    return max(0.0, win_probability - (1.0 - win_probability) / win_loss_ratio)

def _annualized_volatility(closes: Sequence[float], lookback: int) -> float:
    sample = closes[-(lookback + 1):]
    if len(sample) != lookback + 1 or any(x <= 0 for x in sample): raise ValueError(f"at least {lookback + 1} positive closing prices are required")
    returns = [log(sample[i] / sample[i - 1]) for i in range(1, len(sample))]
    return stdev(returns) * sqrt(252)

def evaluate_position(*, equity: float, peak_equity: float, price: float, closes: Sequence[float], win_probability: float, win_loss_ratio: float, config: RiskConfig = RiskConfig(), paper_mode: bool = True) -> RiskDecision:
    """Return paper-only sizing. This function never connects to a broker or places an order."""
    if not paper_mode: raise RuntimeError("Crash Guard is paper-only; live mode is disabled")
    scalar_inputs = (equity, peak_equity, price, win_probability, win_loss_ratio)
    if not all(isfinite(x) for x in scalar_inputs):
        raise ValueError("all numeric inputs must be finite")
    if equity <= 0 or peak_equity <= 0 or price <= 0:
        raise ValueError("equity, peak_equity and price must be positive")
    if equity > peak_equity: raise ValueError("peak_equity cannot be below current equity")
    if len(closes) < config.slow_sma:
        raise ValueError(f"at least {config.slow_sma} positive closes are required")
    if any(not isfinite(x) or x <= 0 for x in closes):
        raise ValueError("all closing prices must be finite and positive")
    drawdown = max(0.0, 1.0 - equity / peak_equity)
    vol = _annualized_volatility(closes, config.vol_lookback)
    kelly = _kelly_fraction(win_probability, win_loss_ratio)
    if drawdown + 1e-12 >= config.hard_drawdown:
        return RiskDecision(False, "PAPER_ONLY", 0, 0, 0, vol, drawdown, kelly, "hard drawdown limit reached; trading halted")
    if drawdown + 1e-12 >= config.brake_3: brake = 0.0
    elif drawdown + 1e-12 >= config.brake_2: brake = 0.25
    elif drawdown + 1e-12 >= config.brake_1: brake = 0.50
    else: brake = 1.0
    fast = sum(closes[-config.fast_sma:]) / config.fast_sma
    slow = sum(closes[-config.slow_sma:]) / config.slow_sma
    if not (closes[-1] > slow and fast > slow):
        return RiskDecision(False, "PAPER_ONLY", 0, 0, 0, vol, drawdown, kelly, "trend filter failed: close must exceed slow SMA and fast SMA must exceed slow SMA")
    if brake == 0:
        return RiskDecision(False, "PAPER_ONLY", 0, 0, 0, vol, drawdown, kelly, "drawdown brake at 20%: new risk disabled")
    if not isfinite(vol) or vol <= 0:
        return RiskDecision(False, "PAPER_ONLY", 0, 0, 0, vol, drawdown, kelly, "volatility estimate is zero or invalid")
    if kelly <= 0: return RiskDecision(False, "PAPER_ONLY", 0, 0, 0, vol, drawdown, kelly, "Kelly estimate is non-positive; no statistically positive edge")
    vol_weight = config.base_weight * min(1.0, config.annual_vol_target / vol)
    base_weight = min(vol_weight, config.fractional_kelly * kelly, config.max_position_weight)
    weight = base_weight * brake
    notional = equity * weight
    return RiskDecision(True, "PAPER_ONLY", weight, notional, notional / price, vol, drawdown, kelly, "approved for paper simulation only; no order was submitted")

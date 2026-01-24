# -*- coding: utf-8 -*-
"""
Pure functions for RangeTwo strategy (no IO, no exchange, no threading).
Commit #3 split.
"""
from __future__ import annotations

from typing import Optional, Tuple


def normalize_range(price_a: float, price_b: float) -> Tuple[float, float, float]:
    """Return (low, high, diff)."""
    a = float(price_a)
    b = float(price_b)
    lo, hi = (a, b) if a <= b else (b, a)
    return lo, hi, hi - lo


def plan_orders(
    *,
    side: str,
    low: float,
    high: float,
    diff: float,
    mark: float,
    second_entry_offset_pct: float,
) -> Tuple[float, float, float, float]:
    """
    Compute (sl, tp, a1_price, a2_price).
    - A1 always at mid
    - A2 is mark +/- offset
    """
    mid = (low + high) / 2.0
    if side == "long":
        sl = low
        tp = high + diff / 2.0
        a2 = mark * (1.0 - float(second_entry_offset_pct))
    else:
        sl = high
        tp = low - diff / 2.0
        a2 = mark * (1.0 + float(second_entry_offset_pct))
    return float(sl), float(tp), float(mid), float(a2)


def calc_current_bep(
    *,
    a1_filled: bool,
    a2_filled: bool,
    a1_entry_price: Optional[float],
    a2_entry_price: Optional[float],
    a1_limit_price: Optional[float],
    a2_limit_price: Optional[float],
    qty1: float,
    qty2: float,
) -> Optional[float]:
    """Compute weighted average entry price for current position legs (BEP)."""
    parts = []
    if a1_filled:
        p1 = float(a1_entry_price or 0.0) or float(a1_limit_price or 0.0)
        q1 = float(qty1 or 0.0)
        if p1 > 0 and q1 > 0:
            parts.append((p1, q1))
    if a2_filled:
        p2 = float(a2_entry_price or 0.0) or float(a2_limit_price or 0.0)
        q2 = float(qty2 or 0.0)
        if p2 > 0 and q2 > 0:
            parts.append((p2, q2))
    if not parts:
        return None
    num = sum(p * q for p, q in parts)
    den = sum(q for _, q in parts)
    if den <= 0:
        return None
    return num / den

# -*- coding: utf-8 -*-
"""
Pure functions for Ladder strategy (no IO, no exchange, no threading).
Commit #3 split.
"""
from __future__ import annotations

from typing import Optional


def calc_next_add_price(last_entry: float, side: str, step: float) -> float:
    """short: last*(1+step); long: last*(1-step)"""
    if side == "short":
        return last_entry * (1.0 + step)
    return last_entry * (1.0 - step)


def entry_limit_price(mark: float, side: str, offset: float) -> float:
    """short: sell slightly higher; long: buy slightly lower"""
    if side == "short":
        return mark * (1.0 + offset)
    return mark * (1.0 - offset)


def should_add(mark: float, next_add_price: Optional[float], side: str) -> bool:
    if next_add_price is None:
        return False
    if side == "short":
        return mark >= float(next_add_price)
    return mark <= float(next_add_price)


def tp_price_from_entry(entry_price: float, side: str, tp_pct: float) -> float:
    """Return TP price (reduce-only opposite side placed elsewhere)."""
    if side == "short":
        return entry_price * (1.0 - tp_pct)
    return entry_price * (1.0 + tp_pct)

"""Deterministic cumulative XP thresholds and logarithmic level lookup."""

from __future__ import annotations

import math
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from functools import lru_cache


@lru_cache(maxsize=1024)
def cumulative_xp(level: int, curve: str, mult: float, base: float, inc: float) -> int:
    """Compute one threshold without allocating a table of every preceding level."""
    if level <= 0:
        return 0
    if not all(math.isfinite(v) and v >= 0 for v in (mult, base, inc)):
        raise ValueError("Curve coefficients must be finite and nonnegative.")
    with localcontext() as ctx:
        # Enough precision to retain integer boundaries for very large exponential XP.
        ctx.prec = max(50, level // 10 + 40) if curve == "exponential" else 60
        factor = Decimal(str(mult))
        if curve == "constant":
            total = Decimal(100) * level * factor
        elif curve == "exponential":
            total = Decimal(400) * factor * (Decimal("1.25") ** level - 1)
        else:
            total = factor * (
                Decimal(str(base)) * level + Decimal(str(inc)) * level * (level - 1) / 2
            )
        return int(total.to_integral_value(rounding=ROUND_HALF_EVEN))


def level_from_xp(xp: int, curve: str, mult: float, max_level: int, base: float, inc: float) -> int:
    """Find the greatest reached threshold in O(log level), with no 5,000-level cap."""
    xp = max(0, int(xp))
    curve = (curve or "linear").lower()
    if not all(math.isfinite(v) and v >= 0 for v in (mult, base, inc)):
        raise ValueError("Curve coefficients must be finite and nonnegative.")
    if mult <= 0 or (curve not in {"constant", "exponential"} and base == inc == 0):
        return max(0, max_level)
    hi = 1
    while cumulative_xp(hi, curve, mult, base, inc) <= xp:
        if max_level > 0 and hi >= max_level:
            return max_level
        hi = min(hi * 2, max_level) if max_level > 0 else hi * 2
    lo = 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cumulative_xp(mid, curve, mult, base, inc) <= xp:
            lo = mid
        else:
            hi = mid - 1
    return lo


def level_thresholds(
    curve: str, mult: float, max_level: int, linear_base: float, linear_inc: float
) -> list[int]:
    """Compatibility helper for callers that need an explicit threshold table."""
    cap = max_level if max_level > 0 else 5000
    return [
        cumulative_xp(n, (curve or "linear").lower(), mult, linear_base, linear_inc)
        for n in range(cap + 1)
    ]

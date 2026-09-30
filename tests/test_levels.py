import math

import pytest

from levelplus.levels import cumulative_xp, level_from_xp


@pytest.mark.parametrize("curve", ["constant", "linear", "exponential"])
@pytest.mark.parametrize("level", [1, 5, 100, 5000, 6000])
def test_exact_threshold_and_previous_xp(curve, level):
    threshold = cumulative_xp(level, curve, 1.0, 83.2, 100.433)
    assert level_from_xp(threshold, curve, 1.0, 0, 83.2, 100.433) == level
    assert level_from_xp(threshold - 1, curve, 1.0, 0, 83.2, 100.433) == level - 1


def test_default_thresholds_and_deterministic_half_even_rounding():
    assert [cumulative_xp(n, "linear", 1.0, 83.2, 100.433) for n in range(4)] == [0, 83, 267, 551]
    assert cumulative_xp(3000, "linear", 1.0, 83.2, 100.433) == 452047450
    assert cumulative_xp(2201, "linear", 1.0, 83.2, 100.433) == 243341460


def test_cap_and_large_cap_with_small_xp():
    assert level_from_xp(100000, "constant", 1.0, 5, 83.2, 100.433) == 5
    # A huge configured cap must not cause huge exponential computations for a new user.
    assert level_from_xp(100, "exponential", 1.0, 10**12, 83.2, 100.433) == 1


def test_zero_cost_curve_and_negative_xp_terminate():
    assert level_from_xp(0, "linear", 1.0, 20, 0, 0) == 20
    assert level_from_xp(0, "linear", 1.0, 0, 0, 0) == 0
    assert level_from_xp(-5, "constant", 1.0, 0, 0, 0) == 0


@pytest.mark.parametrize("value", [math.nan, math.inf, -1.0])
def test_invalid_coefficients_rejected(value):
    with pytest.raises(ValueError):
        level_from_xp(10, "linear", value, 0, 83.2, 100.433)

"""Integer math tests and reference vectors from v3-core v1.0.0."""

from decimal import Decimal, localcontext

import pytest

from lp_pilot.uniswap import (
    MAX_SQRT_RATIO, MAX_TICK, MAX_UINT128, MAX_UINT160, MAX_UINT256,
    MIN_SQRT_RATIO, MIN_TICK, Q96, amount0_delta, amount1_delta,
    compute_swap_step, get_sqrt_ratio_at_tick, get_tick_at_sqrt_ratio,
    liquidity_for_capital, mul_div, next_sqrt_price_from_input,
    next_sqrt_price_from_output, position_amounts, price_from_sqrt,
    sqrt_from_price, tick_bounds_for_price,
)


def encode_ratio(numerator, denominator):
    with localcontext() as context:
        context.prec = 100
        return int((Decimal(numerator) / Decimal(denominator)).sqrt() * Q96)


@pytest.mark.parametrize("tick,expected", [
    (MIN_TICK, MIN_SQRT_RATIO), (MIN_TICK + 1, 4295343490),
    (0, Q96), (1, 79232123823359799118286999568),
    (-1, 79224201403219477170569942574),
    (MAX_TICK - 1, 1461373636630004318706518188784493106690254656249),
    (MAX_TICK, MAX_SQRT_RATIO),
])
def test_official_tickmath_vectors(tick, expected):
    assert get_sqrt_ratio_at_tick(tick) == expected


@pytest.mark.parametrize("tick", [-887272, -500000, -100000, -250, -1, 0, 1, 250, 100000, 500000, 887271])
def test_tickmath_inverse_and_adjacent_interval(tick):
    lower = get_sqrt_ratio_at_tick(tick)
    upper = get_sqrt_ratio_at_tick(tick + 1)
    assert get_tick_at_sqrt_ratio(lower) == tick
    assert get_tick_at_sqrt_ratio(upper - 1) == tick


@pytest.mark.parametrize("tick", [MIN_TICK - 1, MAX_TICK + 1, 1.0, True])
def test_tickmath_rejects_invalid_ticks(tick):
    with pytest.raises(ValueError):
        get_sqrt_ratio_at_tick(tick)


@pytest.mark.parametrize("sqrt", [0, MIN_SQRT_RATIO - 1, MAX_SQRT_RATIO, MAX_UINT160 + 1])
def test_tickmath_inverse_excludes_upper_bound(sqrt):
    with pytest.raises(ValueError):
        get_tick_at_sqrt_ratio(sqrt)


def test_price_orientation_and_decimals():
    sqrt = sqrt_from_price("2500")
    assert abs(price_from_sqrt(sqrt) - Decimal(2500)) < Decimal("1e-20")
    assert abs(price_from_sqrt(sqrt, invert=False) - Decimal("0.0004")) < Decimal("1e-28")
    assert price_from_sqrt(Q96, 6, 18) == Decimal(10) ** 12
    assert price_from_sqrt(Q96, 18, 6, invert=False) == Decimal(10) ** 12


@pytest.mark.parametrize("invert", [False, True])
def test_range_ticks_round_outward_with_spacing(invert):
    center, width = Decimal("2500"), Decimal("0.005")
    lower, upper = tick_bounds_for_price(center, width, 10, invert=invert)
    requested = sorted([sqrt_from_price(center / (1 + width), invert=invert),
                        sqrt_from_price(center * (1 + width), invert=invert)])
    assert lower % 10 == upper % 10 == 0
    assert get_sqrt_ratio_at_tick(lower) <= requested[0]
    assert get_sqrt_ratio_at_tick(upper) >= requested[1]
    assert get_sqrt_ratio_at_tick(lower + 10) > requested[0]
    assert get_sqrt_ratio_at_tick(upper - 10) < requested[1]


def test_delta_direction_and_rounding():
    assert amount0_delta(Q96, 2 * Q96, 3) == 1
    assert amount0_delta(2 * Q96, Q96, 3, True) == 2
    assert amount1_delta(Q96, 2 * Q96, 3) == 3
    assert amount1_delta(Q96, Q96 + 1, 3) == 0
    assert amount1_delta(Q96, Q96 + 1, 3, True) == 1
    assert amount0_delta(Q96, Q96, 3, True) == 0


@pytest.mark.parametrize("zero,expected", [
    (False, 87150978765690771352898345369),
    (True, 72025602285694852357767227579),
])
def test_official_sqrtprice_input_vectors(zero, expected):
    assert next_sqrt_price_from_input(Q96, 10**18, 10**17, zero) == expected


@pytest.mark.parametrize("zero,expected", [
    (False, 88031291682515930659493278152),
    (True, 71305346262837903834189555302),
])
def test_official_sqrtprice_output_vectors(zero, expected):
    assert next_sqrt_price_from_output(Q96, 10**18, 10**17, zero) == expected


def test_sqrtprice_solidity_overflow_fallback():
    maximum = MAX_UINT256 - ((MAX_UINT128 << 96) // MAX_UINT160)
    assert next_sqrt_price_from_input(MAX_UINT160, MAX_UINT128, maximum, True) == 1
    with pytest.raises(ValueError):
        next_sqrt_price_from_input(MAX_UINT160, 1024, 1024, False)
    with pytest.raises(ValueError):
        next_sqrt_price_from_output(Q96, 1, 1, True)
    with pytest.raises(ValueError):
        next_sqrt_price_from_input(Q96, 0, 1, True)


@pytest.mark.parametrize("amount", [10**18, -(10**18)])
def test_official_swapmath_target_capped_vectors(amount):
    target = encode_ratio(101, 100)
    step = compute_swap_step(Q96, target, 2 * 10**18, amount, 600)
    assert step.sqrt_price_next_x96 == target
    assert (step.amount_in, step.amount_out, step.fee_amount) == (
        9975124224178055, 9925619580021728, 5988667735148)


def test_official_swapmath_fully_spent_input():
    step = compute_swap_step(Q96, encode_ratio(10, 1), 2 * 10**18, 10**18, 600)
    assert (step.amount_in, step.amount_out, step.fee_amount) == (
        999400000000000000, 666399946655997866, 600000000000000)
    assert step.amount_in + step.fee_amount == 10**18


def test_official_swapmath_fully_received_output():
    step = compute_swap_step(Q96, encode_ratio(100, 1), 2 * 10**18, -10**18, 600)
    assert (step.amount_in, step.amount_out, step.fee_amount) == (
        2000000000000000000, 10**18, 1200720432259356)


def test_official_exact_output_one_raw_unit_cap():
    step = compute_swap_step(417332158212080721273783715441582,
                             1452870262520218020823638996,
                             159344665391607089467575320103, -1, 1)
    assert step.sqrt_price_next_x96 == 417332158212080721273783715441581
    assert (step.amount_in, step.amount_out, step.fee_amount) == (1, 1, 1)


def test_official_tiny_input_all_fees_and_zero_liquidity_crossing():
    step = compute_swap_step(2413, 79887613182836312, 1985041575832132834610021537970, 10, 1872)
    assert (step.sqrt_price_next_x96, step.amount_in, step.amount_out, step.fee_amount) == (2413, 0, 0, 10)
    empty = compute_swap_step(Q96, Q96 + 1000, 0, 100, 500)
    assert (empty.sqrt_price_next_x96, empty.amount_in, empty.amount_out, empty.fee_amount) == (Q96 + 1000, 0, 0, 0)


@pytest.mark.parametrize("zero", [False, True])
@pytest.mark.parametrize("exact_input", [False, True])
def test_swap_direction_rounding_and_conservation(zero, exact_input):
    target = get_sqrt_ratio_at_tick(-100 if zero else 100)
    amount = 10**12 if exact_input else -(10**12)
    step = compute_swap_step(Q96, target, 10**18, amount, 500)
    assert min(Q96, target) <= step.sqrt_price_next_x96 <= max(Q96, target)
    if exact_input:
        assert step.amount_in + step.fee_amount == amount
    else:
        assert step.amount_out == -amount
    assert step.fee_amount > 0


def test_positions_below_inside_and_above_range():
    lower, upper, liquidity = -100, 100, 10**18
    below = position_amounts(get_sqrt_ratio_at_tick(-200), lower, upper, liquidity)
    middle = position_amounts(Q96, lower, upper, liquidity)
    above = position_amounts(get_sqrt_ratio_at_tick(200), lower, upper, liquidity)
    assert below[0] > 0 and below[1] == 0
    assert above[0] == 0 and above[1] > 0
    assert middle[0] > 0 and middle[1] > 0
    assert below == position_amounts(get_sqrt_ratio_at_tick(lower), lower, upper, liquidity)
    assert above == position_amounts(get_sqrt_ratio_at_tick(upper), lower, upper, liquidity)


def test_capital_liquidity_uses_external_value_and_mint_rounding():
    price, capital = Decimal(2500), Decimal(10000)
    sqrt = sqrt_from_price(price)
    lower, upper = tick_bounds_for_price(price, ".01", 10)
    liquidity = liquidity_for_capital(capital, price, sqrt, lower, upper)
    def cost(value):
        a0, a1 = position_amounts(sqrt, lower, upper, value, True)
        return Decimal(a0) / 10**6 + Decimal(a1) / 10**18 * price
    assert cost(liquidity) <= capital < cost(liquidity + 1)


def test_fullmath_512_bit_product_and_result_bounds():
    assert mul_div(MAX_UINT256, MAX_UINT256, MAX_UINT256) == MAX_UINT256
    assert mul_div(5, 5, 3, True) == 9
    with pytest.raises(ValueError):
        mul_div(MAX_UINT256, MAX_UINT256, 1)
    with pytest.raises(ValueError):
        mul_div(1, 1, 0)

# SPDX-License-Identifier: GPL-2.0-or-later
# Reference work: Uniswap v3-core, Copyright (c) 2021 Uniswap Labs.
"""Integer Uniswap v3 math, using v3-core v1.0.0 as the reference.

TickMath constants and rounding rules follow the official implementation
(GPL-2.0-or-later for TickMath; BUSL-1.1 source for SqrtPriceMath/SwapMath).
The underlying integer formulas are implemented in Python with explicit bounds.
See docs/protocol.md for provenance and the limits of event-only reconstruction.
"""

from dataclasses import dataclass
from decimal import Decimal, localcontext

Q96 = 1 << 96
Q128 = 1 << 128
MAX_UINT128 = (1 << 128) - 1
MAX_UINT160 = (1 << 160) - 1
MAX_UINT256 = (1 << 256) - 1
MIN_TICK = -887272
MAX_TICK = 887272
MIN_SQRT_RATIO = 4295128739
MAX_SQRT_RATIO = 1461446703485210103287273052203988822378723970342
FEE_DENOMINATOR = 1_000_000

_TICK_MULTIPLIERS = (
    0xFFFCB933BD6FAD37AA2D162D1A594001,
    0xFFF97272373D413259A46990580E213A,
    0xFFF2E50F5F656932EF12357CF3C7FDCC,
    0xFFE5CACA7E10E4E61C3624EAA0941CD0,
    0xFFCB9843D60F6159C9DB58835C926644,
    0xFF973B41FA98C081472E6896DFB254C0,
    0xFF2EA16466C96A3843EC78B326B52861,
    0xFE5DEE046A99A2A811C461F1969C3053,
    0xFCBE86C7900A88AEDCFFC83B479AA3A4,
    0xF987A7253AC413176F2B074CF7815E54,
    0xF3392B0822B70005940C7A398E4B70F3,
    0xE7159475A2C29B7443B29C7FA6E889D9,
    0xD097F3BDFD2022B8845AD8F792AA5825,
    0xA9F746462D870FDF8A65DC1F90E061E5,
    0x70D869A156D2A1B890BB3DF62BAF32F7,
    0x31BE135F97D08FD981231505542FCFA6,
    0x9AA508B5B7A84E1C677DE54F3E99BC9,
    0x5D6AF8DEDB81196699C329225EE604,
    0x2216E584F5FA1EA926041BEDFE98,
    0x48A170391F7DC42444E8FA2,
)


def _uint(value: int, bits: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 1 << bits:
        raise ValueError(f"{name} must be uint{bits}")
    return value


def div_rounding_up(numerator: int, denominator: int) -> int:
    if numerator < 0 or denominator <= 0:
        raise ValueError("division requires nonnegative numerator and positive denominator")
    return (numerator + denominator - 1) // denominator


def mul_div(a: int, b: int, denominator: int, round_up: bool = False) -> int:
    """FullMath semantics: a 512-bit product and uint256 result."""
    _uint(a, 256, "a")
    _uint(b, 256, "b")
    _uint(denominator, 256, "denominator")
    if denominator == 0:
        raise ValueError("denominator must be positive")
    result = div_rounding_up(a * b, denominator) if round_up else a * b // denominator
    return _uint(result, 256, "result")


def get_sqrt_ratio_at_tick(tick: int) -> int:
    if not isinstance(tick, int) or isinstance(tick, bool) or not MIN_TICK <= tick <= MAX_TICK:
        raise ValueError("tick outside TickMath bounds")
    absolute = abs(tick)
    ratio = Q128
    for bit, multiplier in enumerate(_TICK_MULTIPLIERS):
        if absolute & (1 << bit):
            ratio = ratio * multiplier >> 128
    if tick > 0:
        ratio = MAX_UINT256 // ratio
    return div_rounding_up(ratio, 1 << 32)


def get_tick_at_sqrt_ratio(sqrt_price_x96: int) -> int:
    """Greatest tick whose sqrt ratio is <= input; exact integer binary search."""
    _uint(sqrt_price_x96, 160, "sqrt_price_x96")
    if not MIN_SQRT_RATIO <= sqrt_price_x96 < MAX_SQRT_RATIO:
        raise ValueError("sqrt price outside TickMath inverse bounds")
    low, high = MIN_TICK, MAX_TICK
    while low < high:
        middle = (low + high + 1) // 2
        if get_sqrt_ratio_at_tick(middle) <= sqrt_price_x96:
            low = middle
        else:
            high = middle - 1
    return low


def amount0_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool = False) -> int:
    _uint(sqrt_a, 160, "sqrt_a")
    _uint(sqrt_b, 160, "sqrt_b")
    _uint(liquidity, 128, "liquidity")
    low, high = sorted((sqrt_a, sqrt_b))
    if low == 0:
        raise ValueError("sqrt price must be positive")
    numerator = liquidity << 96
    if round_up:
        return div_rounding_up(mul_div(numerator, high - low, high, True), low)
    return mul_div(numerator, high - low, high) // low


def amount1_delta(sqrt_a: int, sqrt_b: int, liquidity: int, round_up: bool = False) -> int:
    _uint(sqrt_a, 160, "sqrt_a")
    _uint(sqrt_b, 160, "sqrt_b")
    _uint(liquidity, 128, "liquidity")
    return mul_div(liquidity, abs(sqrt_a - sqrt_b), Q96, round_up)


def next_sqrt_price_from_amount0(sqrt_price_x96: int, liquidity: int, amount: int, add: bool) -> int:
    _uint(sqrt_price_x96, 160, "sqrt_price_x96")
    _uint(liquidity, 128, "liquidity")
    _uint(amount, 256, "amount")
    if not sqrt_price_x96 or not liquidity:
        raise ValueError("sqrt price and liquidity must be positive")
    if amount == 0:
        return sqrt_price_x96
    numerator = liquidity << 96
    product = amount * sqrt_price_x96
    if add:
        # Preserve the Solidity overflow fallback; arbitrary precision alone
        # would choose a different rounding path for overflowing products.
        if product <= MAX_UINT256 and numerator + product <= MAX_UINT256:
            result = mul_div(numerator, sqrt_price_x96, numerator + product, True)
        else:
            denominator = numerator // sqrt_price_x96 + amount
            _uint(denominator, 256, "amount0 denominator")
            result = div_rounding_up(numerator, denominator)
    else:
        if product > MAX_UINT256 or product >= numerator:
            raise ValueError("insufficient virtual token0 reserves")
        result = mul_div(numerator, sqrt_price_x96, numerator - product, True)
    return _uint(result, 160, "next sqrt price")


def next_sqrt_price_from_amount1(sqrt_price_x96: int, liquidity: int, amount: int, add: bool) -> int:
    _uint(sqrt_price_x96, 160, "sqrt_price_x96")
    _uint(liquidity, 128, "liquidity")
    _uint(amount, 256, "amount")
    if not sqrt_price_x96 or not liquidity:
        raise ValueError("sqrt price and liquidity must be positive")
    quotient = mul_div(amount, Q96, liquidity, not add)
    if not add and quotient >= sqrt_price_x96:
        raise ValueError("insufficient virtual token1 reserves")
    result = sqrt_price_x96 + quotient if add else sqrt_price_x96 - quotient
    return _uint(result, 160, "next sqrt price")


def next_sqrt_price_from_input(sqrt_price_x96: int, liquidity: int, amount: int, zero_for_one: bool) -> int:
    return (next_sqrt_price_from_amount0 if zero_for_one else next_sqrt_price_from_amount1)(
        sqrt_price_x96, liquidity, amount, True
    )


def next_sqrt_price_from_output(sqrt_price_x96: int, liquidity: int, amount: int, zero_for_one: bool) -> int:
    return (next_sqrt_price_from_amount1 if zero_for_one else next_sqrt_price_from_amount0)(
        sqrt_price_x96, liquidity, amount, False
    )


@dataclass(frozen=True)
class SwapStep:
    sqrt_price_next_x96: int
    amount_in: int
    amount_out: int
    fee_amount: int


def compute_swap_step(sqrt_current: int, sqrt_target: int, liquidity: int,
                      amount_remaining: int, fee_pips: int) -> SwapStep:
    """Reference SwapMath for one step; negative amount means exact output."""
    _uint(sqrt_current, 160, "sqrt_current")
    _uint(sqrt_target, 160, "sqrt_target")
    _uint(liquidity, 128, "liquidity")
    if not 0 <= fee_pips < FEE_DENOMINATOR:
        raise ValueError("invalid fee_pips")
    if not isinstance(amount_remaining, int) or not -(1 << 255) < amount_remaining < 1 << 255:
        raise ValueError("amount_remaining must fit int256 and be safely negatable")
    if sqrt_current <= 0 or sqrt_target <= 0:
        raise ValueError("sqrt prices must be positive")
    zero_for_one = sqrt_current >= sqrt_target
    exact_in = amount_remaining >= 0
    input_delta = amount0_delta if zero_for_one else amount1_delta
    output_delta = amount1_delta if zero_for_one else amount0_delta
    amount_in = input_delta(sqrt_current, sqrt_target, liquidity, True)
    amount_out = output_delta(sqrt_current, sqrt_target, liquidity, False)
    if exact_in:
        available = amount_remaining * (FEE_DENOMINATOR - fee_pips) // FEE_DENOMINATOR
        next_price = sqrt_target if available >= amount_in else next_sqrt_price_from_input(
            sqrt_current, liquidity, available, zero_for_one)
    else:
        next_price = sqrt_target if -amount_remaining >= amount_out else next_sqrt_price_from_output(
            sqrt_current, liquidity, -amount_remaining, zero_for_one)
    reached = next_price == sqrt_target
    if not (reached and exact_in):
        amount_in = input_delta(sqrt_current, next_price, liquidity, True)
    if not (reached and not exact_in):
        amount_out = output_delta(sqrt_current, next_price, liquidity, False)
    if not exact_in:
        amount_out = min(amount_out, -amount_remaining)
    fee = amount_remaining - amount_in if exact_in and not reached else div_rounding_up(
        amount_in * fee_pips, FEE_DENOMINATOR - fee_pips)
    return SwapStep(next_price, amount_in, amount_out, fee)


def position_amounts(sqrt_price_x96: int, lower_tick: int, upper_tick: int,
                     liquidity: int, round_up: bool = False) -> tuple[int, int]:
    _uint(sqrt_price_x96, 160, "sqrt_price_x96")
    if sqrt_price_x96 == 0:
        raise ValueError("sqrt price must be positive")
    if lower_tick >= upper_tick:
        raise ValueError("lower_tick must be below upper_tick")
    lower, upper = get_sqrt_ratio_at_tick(lower_tick), get_sqrt_ratio_at_tick(upper_tick)
    clamped = min(upper, max(lower, sqrt_price_x96))
    return (amount0_delta(clamped, upper, liquidity, round_up),
            amount1_delta(lower, clamped, liquidity, round_up))


def price_from_sqrt(sqrt_price_x96: int, decimals0: int = 6, decimals1: int = 18,
                    invert: bool = True) -> Decimal:
    """Human token1/token0 price, or reciprocal (USDC/WETH for this pool)."""
    if sqrt_price_x96 <= 0:
        raise ValueError("sqrt price must be positive")
    with localcontext() as context:
        context.prec = 90
        ratio = Decimal(sqrt_price_x96) ** 2 / Decimal(Q96) ** 2
        ratio *= Decimal(10) ** (decimals0 - decimals1)
        return +(Decimal(1) / ratio if invert else ratio)


def sqrt_from_price(price: Decimal | str | int, decimals0: int = 6,
                    decimals1: int = 18, invert: bool = True) -> int:
    with localcontext() as context:
        context.prec = 100
        human = Decimal(price)
        if human <= 0:
            raise ValueError("price must be positive")
        ratio = (Decimal(1) / human if invert else human) * Decimal(10) ** (decimals1 - decimals0)
        value = int(ratio.sqrt() * Q96)
    if not MIN_SQRT_RATIO <= value < MAX_SQRT_RATIO:
        raise ValueError("price outside TickMath bounds")
    return value


def tick_bounds_for_price(center: Decimal | str | int, width: Decimal | str,
                          tick_spacing: int, decimals0: int = 6, decimals1: int = 18,
                          invert: bool = True) -> tuple[int, int]:
    """Round outward in raw token1/token0 price and preserve valid spacing."""
    if not isinstance(tick_spacing, int) or tick_spacing <= 0:
        raise ValueError("tick_spacing must be positive")
    with localcontext() as context:
        context.prec = 90
        center, width = Decimal(center), Decimal(width)
        if center <= 0 or width <= 0:
            raise ValueError("center and width must be positive")
        prices = [sqrt_from_price(center / (1 + width), decimals0, decimals1, invert),
                  sqrt_from_price(center * (1 + width), decimals0, decimals1, invert)]
    low_sqrt, high_sqrt = sorted(prices)
    low_tick = get_tick_at_sqrt_ratio(low_sqrt)
    high_tick = get_tick_at_sqrt_ratio(high_sqrt)
    if get_sqrt_ratio_at_tick(high_tick) < high_sqrt:
        high_tick += 1
    lower = (low_tick // tick_spacing) * tick_spacing
    upper = -((-high_tick) // tick_spacing) * tick_spacing
    if lower < MIN_TICK or upper > MAX_TICK or lower >= upper:
        raise ValueError("rounded range outside valid TickMath bounds")
    return lower, upper


def liquidity_for_capital(capital_usdc: Decimal | str | int,
                          reference_price: Decimal | str | int, sqrt_price_x96: int,
                          lower_tick: int, upper_tick: int,
                          decimals0: int = 6, decimals1: int = 18) -> int:
    """Largest mint liquidity whose rounded token cost fits USDC capital.

    This convenience function assumes token0=USDC, token1=WETH. It values
    WETH using the external reference and includes mint's upward rounding.
    """
    capital, reference = Decimal(capital_usdc), Decimal(reference_price)
    if capital <= 0 or reference <= 0:
        raise ValueError("capital and reference price must be positive")
    with localcontext() as context:
        context.prec = 90
        def cost(liquidity: int) -> Decimal:
            a0, a1 = position_amounts(sqrt_price_x96, lower_tick, upper_tick, liquidity, True)
            return Decimal(a0) / Decimal(10) ** decimals0 + Decimal(a1) / Decimal(10) ** decimals1 * reference
        low, high = 0, 1
        while high < MAX_UINT128 and cost(high) <= capital:
            low, high = high, min(MAX_UINT128, high * 2)
        if cost(high) <= capital:
            return high
        while low + 1 < high:
            middle = (low + high) // 2
            if cost(middle) <= capital:
                low = middle
            else:
                high = middle
        return low

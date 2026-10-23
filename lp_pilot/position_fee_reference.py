# SPDX-License-Identifier: GPL-2.0-or-later
# Fee formulas: Uniswap Labs, v3-core v1.0.0, commit
# e3589b192d0be27e100cd0daaf6c97204fdb1899, Tick.sol and Position.sol.
# https://github.com/Uniswap/v3-core/tree/e3589b192d0be27e100cd0daaf6c97204fdb1899/contracts/libraries
"""Independent integer fee reference and Ethereum Core-position keys.

The caller must separately establish continuous boundary initialization and
constant position liquidity. These arithmetic helpers cannot establish either
fact from endpoint numbers alone.
"""

_Q128 = 1 << 128
_UINT256 = 1 << 256
_MASK64 = (1 << 64) - 1
_MIN_TICK, _MAX_TICK = -887272, 887272

# Keccak-f[1600], 24 rounds. Ethereum uses Keccak's 0x01 delimiter,
# whereas hashlib.sha3_256 uses the different SHA-3 delimiter.
_ROTATIONS = (
    0, 1, 62, 28, 27,
    36, 44, 6, 55, 20,
    3, 10, 43, 25, 39,
    41, 45, 15, 21, 8,
    18, 2, 61, 56, 14,
)
_ROUND_CONSTANTS = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)


def _rotate(value: int, count: int) -> int:
    return ((value << count) | (value >> ((64 - count) % 64))) & _MASK64


def _permute(state: list[int]) -> None:
    for constant in _ROUND_CONSTANTS:
        columns = [state[x] ^ state[x + 5] ^ state[x + 10]
                   ^ state[x + 15] ^ state[x + 20] for x in range(5)]
        for x in range(5):
            difference = columns[(x - 1) % 5] ^ _rotate(columns[(x + 1) % 5], 1)
            for y in range(5):
                state[x + 5 * y] ^= difference
        moved = [0] * 25
        for x in range(5):
            for y in range(5):
                moved[y + 5 * ((2 * x + 3 * y) % 5)] = _rotate(
                    state[x + 5 * y], _ROTATIONS[x + 5 * y])
        for x in range(5):
            for y in range(5):
                state[x + 5 * y] = moved[x + 5 * y] ^ (
                    ~moved[(x + 1) % 5 + 5 * y] & moved[(x + 2) % 5 + 5 * y])
        state[0] ^= constant


def keccak256(data: bytes) -> bytes:
    """Ethereum Keccak-256, used only for read-only ABI identifiers here."""
    if not isinstance(data, bytes):
        raise ValueError("Keccak input must be bytes")
    rate = 136
    padded = bytearray(data)
    padded.append(0x01)
    padded.extend(b"\x00" * (-len(padded) % rate))
    padded[-1] |= 0x80
    state = [0] * 25
    for offset in range(0, len(padded), rate):
        for lane in range(rate // 8):
            start = offset + lane * 8
            state[lane] ^= int.from_bytes(padded[start:start + 8], "little")
        _permute(state)
    return b"".join(lane.to_bytes(8, "little") for lane in state)[:32]


def _integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def _range(lower: int, upper: int) -> None:
    _integer(lower, "lower tick", _MIN_TICK, _MAX_TICK)
    _integer(upper, "upper tick", _MIN_TICK, _MAX_TICK)
    if lower >= upper:
        raise ValueError("lower tick must precede upper tick")


def position_key(owner: str, lower: int, upper: int) -> str:
    """Core key: keccak256(abi.encodePacked(address, int24, int24))."""
    _range(lower, upper)
    if not isinstance(owner, str) or not owner.startswith("0x") or len(owner) != 42:
        raise ValueError("owner must be a 20-byte hexadecimal address")
    try:
        address = bytes.fromhex(owner[2:])
    except ValueError as error:
        raise ValueError("owner must be a 20-byte hexadecimal address") from error
    if len(address) != 20:
        raise ValueError("owner must be a 20-byte hexadecimal address")
    packed = address + lower.to_bytes(3, "big", signed=True) + upper.to_bytes(3, "big", signed=True)
    return "0x" + keccak256(packed).hex()


def fee_growth_inside(current_tick: int, lower: int, upper: int,
                      global_growth: int, outside_lower: int, outside_upper: int) -> int:
    """Tick.getFeeGrowthInside for one token, with uint256 wraparound."""
    _range(lower, upper)
    _integer(current_tick, "current tick", _MIN_TICK, _MAX_TICK)
    for name, value in (("global growth", global_growth), ("lower outside", outside_lower),
                        ("upper outside", outside_upper)):
        _integer(value, name, 0, _UINT256 - 1)
    below = outside_lower if current_tick >= lower else global_growth - outside_lower
    above = outside_upper if current_tick < upper else global_growth - outside_upper
    return (global_growth - below - above) % _UINT256


def interval_claim(start_inside: int, end_inside: int, liquidity: int) -> dict[str, int]:
    """Isolated interval accrual, rounded once as in Position.update.

    This is not a stored tokensOwed update. If an existing position carries a
    fractional remainder from before the interval, the difference of its two
    pending claims may be one raw unit larger.
    """
    _integer(start_inside, "start inside growth", 0, _UINT256 - 1)
    _integer(end_inside, "end inside growth", 0, _UINT256 - 1)
    _integer(liquidity, "position liquidity", 0, _Q128 - 1)
    delta = (end_inside - start_inside) % _UINT256
    return {"growth_delta_x128": delta, "fees_raw": delta * liquidity // _Q128}


def pending_claim(inside: int, last_inside: int, liquidity: int) -> int:
    """Pending accrued fees before the uint128 storage cast/addition."""
    return interval_claim(last_inside, inside, liquidity)["fees_raw"]

from __future__ import annotations

from .models import Candle, Direction, FVG, TradeSetup


def _calc_tp(entry: float, sl: float, r_multiple: float, direction: Direction) -> float:
    sl_dist = abs(entry - sl)
    if direction == Direction.BULLISH:
        return entry + sl_dist * r_multiple
    return entry - sl_dist * r_multiple


def _highest_high(candles: list[Candle]) -> float:
    return max(c.high for c in candles)


def _lowest_low(candles: list[Candle]) -> float:
    return min(c.low for c in candles)


# ─── Setup 1: Double Gap ──────────────────────────────────────────────────────

def check_double_gap(
    candles: list[Candle],
    first_fvg: FVG,
    bar_index: int,
    r_multiple: float,
) -> TradeSetup | None:
    """Second consecutive FVG in the same direction."""
    if len(candles) < 3 or bar_index <= first_fvg.bar_index:
        return None

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    if first_fvg.direction == Direction.BULLISH and c3.low > c1.high:
        entry = c3.close
        sl = first_fvg.candle1.low
        if entry <= sl:
            return None
        tp = _calc_tp(entry, sl, r_multiple, Direction.BULLISH)
        return TradeSetup(1, "Double Gap", Direction.BULLISH, entry, sl, tp, first_fvg)

    if first_fvg.direction == Direction.BEARISH and c3.high < c1.low:
        entry = c3.close
        sl = first_fvg.candle1.high
        if entry >= sl:
            return None
        tp = _calc_tp(entry, sl, r_multiple, Direction.BEARISH)
        return TradeSetup(1, "Double Gap", Direction.BEARISH, entry, sl, tp, first_fvg)

    return None


# ─── Setup 2: Reversal ────────────────────────────────────────────────────────

def check_reversal(
    candles: list[Candle],
    first_fvg: FVG,
    bar_index: int,
    r_multiple: float,
    lookback: int = 30,
) -> TradeSetup | None:
    """First FVG is inverted — candle closes beyond it in the opposing direction."""
    if bar_index <= first_fvg.bar_index or len(candles) < 2:
        return None

    current = candles[-1]
    lookback_candles = candles[-lookback:] if len(candles) >= lookback else candles

    # Bullish FVG inverted → go SHORT
    if first_fvg.direction == Direction.BULLISH and current.close < first_fvg.zone_lo:
        entry = current.close
        sl = _highest_high(lookback_candles)
        if entry >= sl:
            return None
        tp = _calc_tp(entry, sl, r_multiple, Direction.BEARISH)
        return TradeSetup(2, "Reversal", Direction.BEARISH, entry, sl, tp, first_fvg)

    # Bearish FVG inverted → go LONG
    if first_fvg.direction == Direction.BEARISH and current.close > first_fvg.zone_hi:
        entry = current.close
        sl = _lowest_low(lookback_candles)
        if entry <= sl:
            return None
        tp = _calc_tp(entry, sl, r_multiple, Direction.BULLISH)
        return TradeSetup(2, "Reversal", Direction.BULLISH, entry, sl, tp, first_fvg)

    return None


# ─── Setup 3: Continuation (FVG tapped + CISD) ────────────────────────────────

def check_continuation(
    candles: list[Candle],
    first_fvg: FVG,
    bar_index: int,
    r_multiple: float,
    candles_since_tap: list[Candle] | None = None,
) -> TradeSetup | None:
    """FVG tapped but not inverted, then CISD confirms continuation."""
    if not first_fvg.tapped or bar_index <= first_fvg.bar_index or len(candles) < 2:
        return None

    current = candles[-1]
    prev = candles[-2]

    # Check FVG NOT inverted
    if first_fvg.direction == Direction.BULLISH and current.close < first_fvg.zone_lo:
        return None
    if first_fvg.direction == Direction.BEARISH and current.close > first_fvg.zone_hi:
        return None

    # CISD detection (body-based, ignore wicks)
    # Bullish CISD: prev candle is bearish, current closes above prev's open
    bullish_cisd = prev.is_bearish and current.close > prev.open
    # Bearish CISD: prev candle is bullish, current closes below prev's open
    bearish_cisd = prev.is_bullish and current.close < prev.open

    tap_candles = candles_since_tap or candles[-10:]

    if first_fvg.direction == Direction.BULLISH and bullish_cisd:
        entry = current.close
        sl = _lowest_low(tap_candles)
        if entry <= sl:
            return None
        tp = _calc_tp(entry, sl, r_multiple, Direction.BULLISH)
        return TradeSetup(3, "Continuation", Direction.BULLISH, entry, sl, tp, first_fvg)

    if first_fvg.direction == Direction.BEARISH and bearish_cisd:
        entry = current.close
        sl = _highest_high(tap_candles)
        if entry >= sl:
            return None
        tp = _calc_tp(entry, sl, r_multiple, Direction.BEARISH)
        return TradeSetup(3, "Continuation", Direction.BEARISH, entry, sl, tp, first_fvg)

    return None


# ─── Setup 4: Tap & Engulf ────────────────────────────────────────────────────

def check_tap_engulf(
    candles: list[Candle],
    first_fvg: FVG,
    bar_index: int,
    r_multiple: float,
    candles_since_tap: list[Candle] | None = None,
) -> TradeSetup | None:
    """FVG tapped, candle closes beyond the full 3-candle formation (wicks included)."""
    if not first_fvg.tapped or bar_index <= first_fvg.bar_index or len(candles) < 1:
        return None

    current = candles[-1]
    tap_candles = candles_since_tap or candles[-10:]

    # Bullish continuation: close above formation's highest high
    if first_fvg.direction == Direction.BULLISH and current.close > first_fvg.formation_hh:
        entry = current.close
        sl = _lowest_low(tap_candles)
        if entry <= sl:
            return None
        tp = _calc_tp(entry, sl, r_multiple, Direction.BULLISH)
        return TradeSetup(4, "Tap & Engulf", Direction.BULLISH, entry, sl, tp, first_fvg)

    # Bearish continuation: close below formation's lowest low
    if first_fvg.direction == Direction.BEARISH and current.close < first_fvg.formation_ll:
        entry = current.close
        sl = _highest_high(tap_candles)
        if entry >= sl:
            return None
        tp = _calc_tp(entry, sl, r_multiple, Direction.BEARISH)
        return TradeSetup(4, "Tap & Engulf", Direction.BEARISH, entry, sl, tp, first_fvg)

    return None


# ─── Combined checker ─────────────────────────────────────────────────────────

def check_all_setups(
    candles: list[Candle],
    first_fvg: FVG,
    bar_index: int,
    r_multiple: float,
    lookback: int = 30,
    candles_since_tap: list[Candle] | None = None,
    enabled: tuple[bool, bool, bool, bool] = (True, True, True, True),
) -> TradeSetup | None:
    """Check setups in priority order (1→2→3→4). First match wins."""
    if enabled[0]:
        s = check_double_gap(candles, first_fvg, bar_index, r_multiple)
        if s:
            return s
    if enabled[1]:
        s = check_reversal(candles, first_fvg, bar_index, r_multiple, lookback)
        if s:
            return s
    if enabled[2]:
        s = check_continuation(candles, first_fvg, bar_index, r_multiple, candles_since_tap)
        if s:
            return s
    if enabled[3]:
        s = check_tap_engulf(candles, first_fvg, bar_index, r_multiple, candles_since_tap)
        if s:
            return s
    return None

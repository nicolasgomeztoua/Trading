from __future__ import annotations

from datetime import time

from .models import Candle, Direction, FVG


def detect_fvg(candles: list[Candle], bar_index: int) -> FVG | None:
    """Check last 3 candles for a Fair Value Gap. Returns FVG or None."""
    if len(candles) < 3:
        return None

    c1, c2, c3 = candles[-3], candles[-2], candles[-1]

    # Bullish FVG: gap between candle 1 high and candle 3 low (price jumped up)
    if c3.low > c1.high:
        return FVG(
            direction=Direction.BULLISH,
            zone_hi=c3.low,       # top of gap
            zone_lo=c1.high,      # bottom of gap
            candle1=c1,
            candle2=c2,
            candle3=c3,
            bar_index=bar_index,
        )

    # Bearish FVG: gap between candle 1 low and candle 3 high (price dropped)
    if c3.high < c1.low:
        return FVG(
            direction=Direction.BEARISH,
            zone_hi=c1.low,       # top of gap
            zone_lo=c3.high,      # bottom of gap
            candle1=c1,
            candle2=c2,
            candle3=c3,
            bar_index=bar_index,
        )

    return None


def is_valid_session_fvg(fvg: FVG, session_start: time) -> bool:
    """Check if FVG was confirmed during the session.

    The scan only runs while in-window, so any detected FVG is valid.
    This function is kept for API compatibility but always returns True
    when called from the state machine (which already gates on in-window).
    """
    return True

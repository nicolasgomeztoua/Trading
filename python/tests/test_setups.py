"""Tests for setup detection logic."""

from datetime import datetime

import pytz

from etrades_scalp.models import Candle, Direction, FVG
from etrades_scalp.setups import (
    check_continuation,
    check_double_gap,
    check_reversal,
    check_tap_engulf,
)

NY = pytz.timezone("America/New_York")


def _candle(ts_str: str, o: float, h: float, l: float, c: float) -> Candle:
    ts = NY.localize(datetime.strptime(ts_str, "%Y-%m-%d %H:%M"))
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c)


def _make_bull_fvg(bar_index: int = 3) -> FVG:
    """Create a bullish FVG: c1.high=102, c3.low=103, zone=[102, 103]."""
    c1 = _candle("2026-03-28 09:30", 100, 102, 99, 101)
    c2 = _candle("2026-03-28 09:31", 102, 105, 101, 104)
    c3 = _candle("2026-03-28 09:32", 104, 107, 103, 106)
    return FVG(
        direction=Direction.BULLISH,
        zone_hi=103, zone_lo=102,
        candle1=c1, candle2=c2, candle3=c3,
        bar_index=bar_index,
    )


def _make_bear_fvg(bar_index: int = 3) -> FVG:
    """Create a bearish FVG: c1.low=98, c3.high=97, zone=[97, 98]."""
    c1 = _candle("2026-03-28 09:30", 100, 101, 98, 99)
    c2 = _candle("2026-03-28 09:31", 99, 100, 96, 97)
    c3 = _candle("2026-03-28 09:32", 97, 97, 94, 95)
    return FVG(
        direction=Direction.BEARISH,
        zone_hi=98, zone_lo=97,
        candle1=c1, candle2=c2, candle3=c3,
        bar_index=bar_index,
    )


class TestDoubleGap:
    def test_bullish_double_gap(self):
        fvg = _make_bull_fvg(bar_index=3)
        # Second bullish FVG: c1.high=106, c3.low=108
        candles = [
            _candle("2026-03-28 09:33", 106, 108, 105, 107),  # becomes [2]
            _candle("2026-03-28 09:34", 108, 111, 107, 110),  # becomes [1]
            _candle("2026-03-28 09:35", 110, 113, 109, 112),  # becomes [0], low=109 > 108=c1.high
        ]
        setup = check_double_gap(candles, fvg, bar_index=6, r_multiple=0.2)
        assert setup is not None
        assert setup.setup_number == 1
        assert setup.direction == Direction.BULLISH
        assert setup.stop_loss == fvg.candle1.low  # 99

    def test_no_double_gap_wrong_direction(self):
        fvg = _make_bull_fvg(bar_index=3)
        # Bearish FVG after bullish — not a double gap
        candles = [
            _candle("2026-03-28 09:33", 106, 107, 104, 105),
            _candle("2026-03-28 09:34", 105, 106, 102, 103),
            _candle("2026-03-28 09:35", 103, 103, 99, 100),  # high=103 < c1.low=104
        ]
        setup = check_double_gap(candles, fvg, bar_index=6, r_multiple=0.2)
        assert setup is None


class TestReversal:
    def test_bullish_fvg_inverted_to_short(self):
        fvg = _make_bull_fvg(bar_index=3)
        candles = [
            _candle("2026-03-28 09:33", 105, 106, 104, 105),
            _candle("2026-03-28 09:34", 105, 105, 100, 101),  # closes at 101 < zone_lo=102
        ]
        setup = check_reversal(candles, fvg, bar_index=5, r_multiple=0.2)
        assert setup is not None
        assert setup.setup_number == 2
        assert setup.direction == Direction.BEARISH
        assert setup.entry_price == 101

    def test_bearish_fvg_inverted_to_long(self):
        fvg = _make_bear_fvg(bar_index=3)
        candles = [
            _candle("2026-03-28 09:33", 96, 97, 95, 96),
            _candle("2026-03-28 09:34", 96, 100, 96, 99),  # closes at 99 > zone_hi=98
        ]
        setup = check_reversal(candles, fvg, bar_index=5, r_multiple=0.2)
        assert setup is not None
        assert setup.setup_number == 2
        assert setup.direction == Direction.BULLISH


class TestContinuation:
    def test_bullish_continuation_with_cisd(self):
        fvg = _make_bull_fvg(bar_index=3)
        fvg.tapped = True
        fvg.tap_bar = 4
        candles = [
            _candle("2026-03-28 09:33", 104, 104, 102.5, 103),  # taps FVG, goes down
            _candle("2026-03-28 09:34", 103, 103, 102, 102.5),  # bearish candle (close < open)
            _candle("2026-03-28 09:35", 102.5, 105, 102, 104),  # bullish CISD: close=104 > prev open=103
        ]
        setup = check_continuation(
            candles, fvg, bar_index=6, r_multiple=0.2,
            candles_since_tap=candles,
        )
        assert setup is not None
        assert setup.setup_number == 3
        assert setup.direction == Direction.BULLISH

    def test_no_continuation_if_not_tapped(self):
        fvg = _make_bull_fvg(bar_index=3)
        # fvg.tapped is False by default
        candles = [
            _candle("2026-03-28 09:34", 103, 103, 102, 102.5),
            _candle("2026-03-28 09:35", 102.5, 105, 102, 104),
        ]
        setup = check_continuation(candles, fvg, bar_index=5, r_multiple=0.2)
        assert setup is None


class TestTapEngulf:
    def test_bullish_tap_and_engulf(self):
        fvg = _make_bull_fvg(bar_index=3)
        # formation_hh = max(102, 105, 107) = 107
        fvg.tapped = True
        fvg.tap_bar = 4
        candles = [
            _candle("2026-03-28 09:33", 104, 104, 102.5, 103),
            _candle("2026-03-28 09:34", 103, 108, 102, 108),  # close=108 > formation_hh=107
        ]
        setup = check_tap_engulf(
            candles, fvg, bar_index=5, r_multiple=0.2,
            candles_since_tap=candles,
        )
        assert setup is not None
        assert setup.setup_number == 4
        assert setup.direction == Direction.BULLISH

    def test_no_engulf_if_close_below_hh(self):
        fvg = _make_bull_fvg(bar_index=3)
        fvg.tapped = True
        fvg.tap_bar = 4
        candles = [
            _candle("2026-03-28 09:33", 104, 104, 102.5, 103),
            _candle("2026-03-28 09:34", 103, 106, 102, 106),  # close=106 < formation_hh=107
        ]
        setup = check_tap_engulf(
            candles, fvg, bar_index=5, r_multiple=0.2,
            candles_since_tap=candles,
        )
        assert setup is None

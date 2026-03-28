"""Tests for FVG detection logic."""

from datetime import datetime, time

import pytz

from etrades_scalp.fvg_detector import detect_fvg, is_valid_session_fvg
from etrades_scalp.models import Candle, Direction

NY = pytz.timezone("America/New_York")


def _candle(ts_str: str, o: float, h: float, l: float, c: float) -> Candle:
    ts = NY.localize(datetime.strptime(ts_str, "%Y-%m-%d %H:%M"))
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c)


class TestDetectFVG:
    def test_bullish_fvg(self):
        candles = [
            _candle("2026-03-28 09:30", 100, 102, 99, 101),   # c1: high=102
            _candle("2026-03-28 09:31", 102, 105, 101, 104),   # c2
            _candle("2026-03-28 09:32", 104, 107, 103, 106),   # c3: low=103 > c1.high=102
        ]
        fvg = detect_fvg(candles, bar_index=3)
        assert fvg is not None
        assert fvg.direction == Direction.BULLISH
        assert fvg.zone_lo == 102.0  # c1 high
        assert fvg.zone_hi == 103.0  # c3 low

    def test_bearish_fvg(self):
        candles = [
            _candle("2026-03-28 09:30", 100, 101, 98, 99),    # c1: low=98
            _candle("2026-03-28 09:31", 99, 100, 96, 97),      # c2
            _candle("2026-03-28 09:32", 97, 97, 94, 95),       # c3: high=97 < c1.low=98
        ]
        fvg = detect_fvg(candles, bar_index=3)
        assert fvg is not None
        assert fvg.direction == Direction.BEARISH
        assert fvg.zone_hi == 98.0   # c1 low
        assert fvg.zone_lo == 97.0   # c3 high

    def test_no_fvg_overlapping(self):
        candles = [
            _candle("2026-03-28 09:30", 100, 102, 99, 101),
            _candle("2026-03-28 09:31", 101, 103, 100, 102),
            _candle("2026-03-28 09:32", 102, 104, 101, 103),   # low=101 < c1.high=102
        ]
        fvg = detect_fvg(candles, bar_index=3)
        assert fvg is None

    def test_not_enough_candles(self):
        candles = [_candle("2026-03-28 09:30", 100, 102, 99, 101)]
        assert detect_fvg(candles, bar_index=1) is None
        assert detect_fvg([], bar_index=0) is None


class TestSessionValidation:
    def test_always_valid_when_scan_is_in_window(self):
        # Session validation is now handled by the state machine's in-window check.
        # is_valid_session_fvg always returns True — the gate is upstream.
        candles = [
            _candle("2026-03-28 09:29", 100, 102, 99, 101),
            _candle("2026-03-28 09:30", 102, 105, 101, 104),
            _candle("2026-03-28 09:31", 104, 107, 103, 106),
        ]
        fvg = detect_fvg(candles, bar_index=3)
        assert fvg is not None
        assert is_valid_session_fvg(fvg, time(9, 30)) is True

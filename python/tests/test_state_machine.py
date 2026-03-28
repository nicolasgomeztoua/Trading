"""Tests for the state machine."""

from datetime import datetime

import pytz

from etrades_scalp.config import BotConfig
from etrades_scalp.models import Candle, State
from etrades_scalp.state_machine import ScalpStateMachine

NY = pytz.timezone("America/New_York")


def _candle(ts_str: str, o: float, h: float, l: float, c: float) -> Candle:
    ts = NY.localize(datetime.strptime(ts_str, "%Y-%m-%d %H:%M"))
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c)


class TestStateMachine:
    def _make_sm(self) -> ScalpStateMachine:
        config = BotConfig(tp_r_multiple=0.2)
        return ScalpStateMachine(config)

    def test_starts_in_wait_open(self):
        sm = self._make_sm()
        assert sm.state == State.WAIT_OPEN

    def test_transitions_to_scan_on_window(self):
        sm = self._make_sm()
        # Before window
        sm.on_candle(_candle("2026-03-28 09:29", 100, 101, 99, 100))
        assert sm.state == State.WAIT_OPEN
        # At window start
        sm.on_candle(_candle("2026-03-28 09:30", 100, 101, 99, 100))
        assert sm.state == State.SCAN_FVG

    def test_detects_fvg_and_transitions(self):
        sm = self._make_sm()
        # Get into scanning state
        sm.on_candle(_candle("2026-03-28 09:29", 100, 101, 99, 100))
        sm.on_candle(_candle("2026-03-28 09:30", 100, 102, 99, 101))
        sm.on_candle(_candle("2026-03-28 09:31", 102, 105, 101, 104))
        sm.on_candle(_candle("2026-03-28 09:32", 104, 107, 103, 106))  # bullish FVG
        assert sm.state == State.FVG_FOUND
        assert sm.first_fvg is not None
        assert sm.first_fvg.zone_lo == 102  # c1 high
        assert sm.first_fvg.zone_hi == 103  # c3 low

    def test_done_when_window_expires_no_fvg(self):
        sm = self._make_sm()
        sm.on_candle(_candle("2026-03-28 09:30", 100, 101, 99, 100))
        assert sm.state == State.SCAN_FVG
        # Jump to after window (no FVG found)
        sm.on_candle(_candle("2026-03-28 12:00", 100, 101, 99, 100))
        assert sm.state == State.DONE

    def test_daily_reset(self):
        sm = self._make_sm()
        sm.on_candle(_candle("2026-03-28 09:30", 100, 101, 99, 100))
        assert sm.state == State.SCAN_FVG
        # New day
        sm.on_candle(_candle("2026-03-29 09:00", 100, 101, 99, 100))
        assert sm.state == State.WAIT_OPEN

    def test_double_gap_triggers_trade(self):
        sm = self._make_sm()
        # Pre-session candle
        sm.on_candle(_candle("2026-03-28 09:29", 100, 101, 99, 100))
        # FVG candles: bullish FVG
        sm.on_candle(_candle("2026-03-28 09:30", 100, 102, 99, 101))   # c1
        sm.on_candle(_candle("2026-03-28 09:31", 102, 105, 101, 104))  # c2
        sm.on_candle(_candle("2026-03-28 09:32", 104, 107, 103, 106))  # c3 -> FVG found

        assert sm.state == State.FVG_FOUND

        # Second bullish FVG (double gap)
        result = sm.on_candle(_candle("2026-03-28 09:33", 106, 108, 105, 107))  # new c1
        # This alone isn't an FVG yet (need 3 candles checked from current window)
        # The check_double_gap looks at candles[-3:] so we need the sequence
        sm.on_candle(_candle("2026-03-28 09:34", 108, 111, 107, 110))  # new c2
        result = sm.on_candle(_candle("2026-03-28 09:35", 110, 113, 109, 112))  # new c3: low=109 > 108

        assert result is not None
        assert result.setup_number == 1
        assert result.setup_name == "Double Gap"
        assert sm.state == State.IN_TRADE
        assert sm.traded_today is True

from __future__ import annotations

import logging
from datetime import datetime, time

import pytz

from .config import BotConfig
from .fvg_detector import detect_fvg, is_valid_session_fvg
from .models import Candle, Direction, FVG, State, TradeSetup
from .setups import check_all_setups

logger = logging.getLogger(__name__)

NY_TZ = pytz.timezone("America/New_York")


class ScalpStateMachine:
    def __init__(self, config: BotConfig):
        self.config = config
        self.candles: list[Candle] = []
        self.bar_index: int = 0
        self.state: State = State.WAIT_OPEN
        self.first_fvg: FVG | None = None
        self.traded_today: bool = False
        self._current_date: str | None = None

    def reset_day(self) -> None:
        logger.info("Daily reset")
        self.state = State.WAIT_OPEN
        self.first_fvg = None
        self.traded_today = False
        self.candles.clear()
        self.bar_index = 0

    def _ny_time(self, ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return NY_TZ.localize(ts)
        return ts.astimezone(NY_TZ)

    def _in_window(self, ny_dt: datetime) -> bool:
        t = ny_dt.time()
        return self.config.session_start <= t < self.config.session_end

    def _is_blocked(self, ny_dt: datetime) -> bool:
        mmdd = f"{ny_dt.month:02d}{ny_dt.day:02d}"
        return mmdd in self.config.blocked_dates

    def _update_fvg_tap(self, candle: Candle) -> None:
        fvg = self.first_fvg
        if fvg is None or fvg.tapped or self.bar_index <= fvg.bar_index:
            return
        if fvg.direction == Direction.BULLISH and candle.low <= fvg.zone_hi:
            fvg.tapped = True
            fvg.tap_bar = self.bar_index
            logger.info("FVG tapped (bullish) at bar %d", self.bar_index)
        elif fvg.direction == Direction.BEARISH and candle.high >= fvg.zone_lo:
            fvg.tapped = True
            fvg.tap_bar = self.bar_index
            logger.info("FVG tapped (bearish) at bar %d", self.bar_index)

    def _candles_since_tap(self) -> list[Candle] | None:
        if self.first_fvg is None or self.first_fvg.tap_bar is None:
            return None
        tap_idx = self.first_fvg.tap_bar
        offset = self.bar_index - tap_idx
        if offset <= 0 or offset > len(self.candles):
            return self.candles[-10:]
        return self.candles[-offset:]

    def on_candle(self, candle: Candle) -> TradeSetup | None:
        """Process a completed 1-min candle. Returns TradeSetup if triggered."""
        ny_dt = self._ny_time(candle.timestamp)

        # Daily reset on new date
        date_str = ny_dt.strftime("%Y-%m-%d")
        if self._current_date is not None and date_str != self._current_date:
            self.reset_day()
        self._current_date = date_str

        self.candles.append(candle)
        self.bar_index += 1

        if self._is_blocked(ny_dt):
            return None

        # STATE 0: Wait for session open
        if self.state == State.WAIT_OPEN:
            if self._in_window(ny_dt):
                self.state = State.SCAN_FVG
                logger.info("Session open — scanning for FVG")

        # STATE 1: Scan for first valid FVG
        # Note: elif prevents cascading into FVG_FOUND on the same bar the FVG forms.
        # The formation candles are part of the FVG itself — setups are checked starting next bar.
        elif self.state == State.SCAN_FVG and self._in_window(ny_dt):
            fvg = detect_fvg(self.candles, self.bar_index)
            if fvg and is_valid_session_fvg(fvg, self.config.session_start):
                self.first_fvg = fvg
                self.state = State.FVG_FOUND
                logger.info(
                    "First FVG detected: %s zone=[%.2f, %.2f] at bar %d",
                    fvg.direction.name, fvg.zone_lo, fvg.zone_hi, self.bar_index,
                )

        # STATE 2: FVG found, monitor for setups
        elif self.state == State.FVG_FOUND and not self.traded_today:
            self._update_fvg_tap(candle)

            setup = check_all_setups(
                candles=self.candles,
                first_fvg=self.first_fvg,
                bar_index=self.bar_index,
                r_multiple=self.config.tp_r_multiple,
                lookback=self.config.reversal_lookback,
                candles_since_tap=self._candles_since_tap(),
                enabled=(
                    self.config.enable_setup_1,
                    self.config.enable_setup_2,
                    self.config.enable_setup_3,
                    self.config.enable_setup_4,
                ),
            )

            if setup:
                self.state = State.IN_TRADE
                self.traded_today = True
                logger.info(
                    "Setup %d (%s) triggered: %s @ %.2f SL=%.2f TP=%.2f",
                    setup.setup_number, setup.setup_name,
                    setup.direction.name, setup.entry_price,
                    setup.stop_loss, setup.take_profit,
                )
                return setup

            if not self._in_window(ny_dt):
                self.state = State.DONE
                logger.info("Window expired — no setup triggered today")

        # STATE 1 window expiry
        if self.state == State.SCAN_FVG and not self._in_window(ny_dt):
            self.state = State.DONE
            logger.info("Window expired — no FVG found today")

        return None

    def on_trade_closed(self) -> None:
        """Call when the position is closed (SL or TP hit)."""
        self.state = State.DONE
        logger.info("Trade closed — done for the day")

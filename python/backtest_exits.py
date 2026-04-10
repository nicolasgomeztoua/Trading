#!/usr/bin/env python3
"""
ETrades Scalp Model — Exit Strategy Optimizer

Downloads NQ futures 1-min data and backtests 10+ exit strategies
to find the best trailing/TP approach.

Usage:
    python backtest_exits.py                    # last 30 days
    python backtest_exits.py --days 60          # last 60 days
    python backtest_exits.py --csv data.csv     # from CSV file
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
import pytz

# Add parent so we can import etrades_scalp
sys.path.insert(0, str(Path(__file__).parent))

from etrades_scalp.models import Candle, Direction, FVG, State, TradeSetup
from etrades_scalp.fvg_detector import detect_fvg, is_valid_session_fvg
from etrades_scalp.setups import check_all_setups

NY = pytz.timezone("America/New_York")
MNQ_POINT_VALUE = 2.0  # $2 per point for MNQ

# ══════════════════════════════════════════════════════════════════════════════
# DATA LOADING
# ══════════════════════════════════════════════════════════════════════════════

def load_yfinance(symbol: str = "NQ=F", days: int = 30) -> pd.DataFrame:
    """Download 1-min bars from Yahoo Finance."""
    import yfinance as yf

    # yfinance limits 1m data to 7 days per request, max 30 days back
    end = datetime.now()
    all_frames = []

    # Download in 7-day chunks
    chunks = min(days, 30)
    for i in range(0, chunks, 7):
        chunk_end = end - timedelta(days=i)
        chunk_start = chunk_end - timedelta(days=7)
        try:
            df = yf.download(symbol, start=chunk_start, end=chunk_end,
                             interval="1m", progress=False)
            if len(df) > 0:
                all_frames.append(df)
        except Exception:
            pass

    if not all_frames:
        raise RuntimeError(f"No data downloaded for {symbol}")

    df = pd.concat(all_frames).sort_index()
    df = df[~df.index.duplicated(keep='first')]
    # Flatten multi-level columns from yfinance
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def load_file(path: str) -> pd.DataFrame:
    """Load CSV or Parquet file."""
    if path.endswith(".parquet"):
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path, parse_dates=True, index_col=0)
    # Normalize column names to title case
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ("open", "high", "low", "close", "volume"):
            col_map[c] = cl.capitalize()
    df = df.rename(columns=col_map)
    for col in ["Open", "High", "Low", "Close"]:
        if col not in df.columns:
            raise ValueError(f"File must have {col} column")
    return df.sort_index()


def df_to_candles(df: pd.DataFrame) -> list[Candle]:
    candles = []
    for ts, row in df.iterrows():
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        candles.append(Candle(
            timestamp=ts.to_pydatetime(),
            open=float(row["Open"]),
            high=float(row["High"]),
            low=float(row["Low"]),
            close=float(row["Close"]),
            volume=int(row.get("Volume", 0)),
        ))
    return candles


# ══════════════════════════════════════════════════════════════════════════════
# TRADE RESULT
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class TradeResult:
    setup_name: str
    direction: Direction
    entry_price: float
    stop_loss: float
    exit_price: float
    sl_distance: float  # initial R in price terms
    pnl_r: float        # P&L in R multiples
    pnl_dollars: float   # per contract
    bars_held: int
    exit_reason: str


# ══════════════════════════════════════════════════════════════════════════════
# STATE MACHINE (simplified for backtesting — finds entries only)
# ══════════════════════════════════════════════════════════════════════════════

SESSION_START = time(9, 30)
SESSION_END = time(12, 0)
BLOCKED_DATES = {"1225", "0101", "0704"}
MIN_INVERSION_PCT = 50.0


def find_swing_low(candles: list[Candle], max_lookback: int = 50) -> float | None:
    if len(candles) < 3:
        return None
    for i in range(len(candles) - 2, max(0, len(candles) - 2 - max_lookback), -1):
        if i <= 0 or i >= len(candles) - 1:
            continue
        if candles[i].low < candles[i-1].low and candles[i].low < candles[i+1].low:
            return candles[i].low
    return None


def find_swing_high(candles: list[Candle], max_lookback: int = 50) -> float | None:
    if len(candles) < 3:
        return None
    for i in range(len(candles) - 2, max(0, len(candles) - 2 - max_lookback), -1):
        if i <= 0 or i >= len(candles) - 1:
            continue
        if candles[i].high > candles[i-1].high and candles[i].high > candles[i+1].high:
            return candles[i].high
    return None


def _in_window(ts: datetime) -> bool:
    ny = ts.astimezone(NY) if ts.tzinfo else NY.localize(ts)
    t = ny.time()
    return SESSION_START <= t < SESSION_END


def _is_blocked(ts: datetime) -> bool:
    ny = ts.astimezone(NY) if ts.tzinfo else NY.localize(ts)
    mmdd = f"{ny.month:02d}{ny.day:02d}"
    return mmdd in BLOCKED_DATES


def _ny_date(ts: datetime) -> str:
    ny = ts.astimezone(NY) if ts.tzinfo else NY.localize(ts)
    return ny.strftime("%Y-%m-%d")


@dataclass
class EntrySignal:
    """Raw entry from state machine — exit strategy applied separately."""
    setup_number: int
    setup_name: str
    direction: Direction
    entry_price: float
    stop_loss: float
    sl_distance: float  # abs(entry - sl)
    entry_bar: int      # session-local bar index
    global_bar: int     # index into the full candles list
    fvg: FVG


def find_entries(candles: list[Candle]) -> list[EntrySignal]:
    """Run state machine over candles, collect all entry signals."""
    entries: list[EntrySignal] = []

    state = State.WAIT_OPEN
    first_fvg: FVG | None = None
    traded_today = False
    current_date: str | None = None
    buf: list[Candle] = []
    bar_idx = 0

    for global_idx, candle in enumerate(candles):
        ny_date = _ny_date(candle.timestamp)

        # Daily reset
        if current_date is not None and ny_date != current_date:
            state = State.WAIT_OPEN
            first_fvg = None
            traded_today = False
            buf.clear()
            bar_idx = 0
        current_date = ny_date

        buf.append(candle)
        bar_idx += 1

        if _is_blocked(candle.timestamp):
            continue

        in_win = _in_window(candle.timestamp)

        if state == State.WAIT_OPEN:
            if in_win:
                state = State.SCAN_FVG

        elif state == State.SCAN_FVG and in_win:
            fvg = detect_fvg(buf, bar_idx)
            if fvg and is_valid_session_fvg(fvg, SESSION_START):
                first_fvg = fvg
                state = State.FVG_FOUND

        elif state == State.FVG_FOUND and not traded_today:
            # Update tap
            if first_fvg and not first_fvg.tapped and bar_idx > first_fvg.bar_index:
                if first_fvg.direction == Direction.BULLISH and candle.low <= first_fvg.zone_hi:
                    first_fvg.tapped = True
                    first_fvg.tap_bar = bar_idx
                elif first_fvg.direction == Direction.BEARISH and candle.high >= first_fvg.zone_lo:
                    first_fvg.tapped = True
                    first_fvg.tap_bar = bar_idx

            # Candles since tap
            tap_candles = None
            if first_fvg and first_fvg.tap_bar:
                offset = bar_idx - first_fvg.tap_bar
                if 0 < offset <= len(buf):
                    tap_candles = buf[-offset:]

            # Check setups (with swing detection for Setup 2)
            setup = _check_setups_with_swing(buf, first_fvg, bar_idx, tap_candles, global_idx)

            if setup:
                state = State.IN_TRADE
                traded_today = True
                entries.append(setup)

            if not in_win:
                state = State.DONE

        if state == State.SCAN_FVG and not in_win:
            state = State.DONE

    return entries


def _check_setups_with_swing(
    candles: list[Candle], fvg: FVG | None, bar_idx: int,
    tap_candles: list[Candle] | None, global_idx: int = 0,
) -> EntrySignal | None:
    """Check all 4 setups with swing-based SL for Setup 2."""
    if fvg is None:
        return None

    c = candles[-1]

    # Setup 1: Double Gap
    if len(candles) >= 3 and bar_idx > fvg.bar_index:
        c1, c3 = candles[-3], candles[-1]
        if fvg.direction == Direction.BULLISH and c3.low > c1.high:
            sl = fvg.candle1.low
            if c.close > sl:
                return EntrySignal(1, "Double Gap", Direction.BULLISH, c.close, sl,
                                   c.close - sl, bar_idx, global_idx, fvg)
        if fvg.direction == Direction.BEARISH and c3.high < c1.low:
            sl = fvg.candle1.high
            if c.close < sl:
                return EntrySignal(1, "Double Gap", Direction.BEARISH, c.close, sl,
                                   sl - c.close, bar_idx, global_idx, fvg)

    # Setup 2: Reversal (with swing detection + inversion depth filter)
    if bar_idx > fvg.bar_index:
        zone_width = fvg.zone_hi - fvg.zone_lo
        inv_buffer = zone_width * MIN_INVERSION_PCT / 100.0

        if fvg.direction == Direction.BEARISH and c.close > fvg.zone_hi + inv_buffer:
            swing = find_swing_low(candles)
            sl = swing if swing else min(x.low for x in candles[-30:])
            if c.close > sl:
                return EntrySignal(2, "Reversal", Direction.BULLISH, c.close, sl,
                                   c.close - sl, bar_idx, global_idx, fvg)
        if fvg.direction == Direction.BULLISH and c.close < fvg.zone_lo - inv_buffer:
            swing = find_swing_high(candles)
            sl = swing if swing else max(x.high for x in candles[-30:])
            if c.close < sl:
                return EntrySignal(2, "Reversal", Direction.BEARISH, c.close, sl,
                                   sl - c.close, bar_idx, global_idx, fvg)

    # Setup 3: Continuation
    if fvg.tapped and bar_idx > fvg.bar_index and len(candles) >= 2:
        prev = candles[-2]
        if fvg.direction == Direction.BULLISH and c.close >= fvg.zone_lo:
            if prev.is_bearish and c.close > prev.open:
                tc = tap_candles or candles[-10:]
                sl = min(x.low for x in tc)
                if c.close > sl:
                    return EntrySignal(3, "Continuation", Direction.BULLISH, c.close, sl,
                                       c.close - sl, bar_idx, global_idx, fvg)
        if fvg.direction == Direction.BEARISH and c.close <= fvg.zone_hi:
            if prev.is_bullish and c.close < prev.open:
                tc = tap_candles or candles[-10:]
                sl = max(x.high for x in tc)
                if c.close < sl:
                    return EntrySignal(3, "Continuation", Direction.BEARISH, c.close, sl,
                                       sl - c.close, bar_idx, global_idx, fvg)

    # Setup 4: Tap & Engulf
    if fvg.tapped and bar_idx > fvg.bar_index:
        if fvg.direction == Direction.BULLISH and c.close > fvg.formation_hh:
            tc = tap_candles or candles[-10:]
            sl = min(x.low for x in tc)
            if c.close > sl:
                return EntrySignal(4, "Tap & Engulf", Direction.BULLISH, c.close, sl,
                                   c.close - sl, bar_idx, global_idx, fvg)
        if fvg.direction == Direction.BEARISH and c.close < fvg.formation_ll:
            tc = tap_candles or candles[-10:]
            sl = max(x.high for x in tc)
            if c.close < sl:
                return EntrySignal(4, "Tap & Engulf", Direction.BEARISH, c.close, sl,
                                   sl - c.close, bar_idx, global_idx, fvg)

    return None


# ══════════════════════════════════════════════════════════════════════════════
# EXIT STRATEGIES
# ══════════════════════════════════════════════════════════════════════════════

def simulate_exit(
    entry: EntrySignal,
    candles: list[Candle],
    exit_fn: Callable,
) -> TradeResult | None:
    """Simulate a trade from entry through candles using exit_fn.

    exit_fn(entry, bar_candles, bar_num) -> (exit_price, reason) or None
    Returns None each bar to continue, or (price, reason) to exit.
    """
    start = entry.global_bar + 1  # start from the candle AFTER entry
    remaining = candles[start:]
    if not remaining:
        return None

    sl = entry.stop_loss
    is_long = entry.direction == Direction.BULLISH
    bar_num = 0

    for candle in remaining:
        bar_num += 1

        # Check SL hit intra-bar (use high/low)
        if is_long and candle.low <= sl:
            exit_price = sl
            return _make_result(entry, exit_price, bar_num, "SL Hit")
        if not is_long and candle.high >= sl:
            exit_price = sl
            return _make_result(entry, exit_price, bar_num, "SL Hit")

        # Run exit strategy on candle close
        result = exit_fn(entry, candle, bar_num, sl)
        if result is not None:
            new_sl, exit_price, reason = result
            if exit_price is not None:
                return _make_result(entry, exit_price, bar_num, reason)
            sl = new_sl  # updated trailing SL

        # End of day — force exit at close
        if not _in_window(candle.timestamp):
            return _make_result(entry, candle.close, bar_num, "EOD")

    # Data ended
    if remaining:
        return _make_result(entry, remaining[-1].close, bar_num, "Data End")
    return None


def _make_result(entry: EntrySignal, exit_price: float, bars: int, reason: str) -> TradeResult:
    is_long = entry.direction == Direction.BULLISH
    pnl_price = (exit_price - entry.entry_price) if is_long else (entry.entry_price - exit_price)
    pnl_r = pnl_price / entry.sl_distance if entry.sl_distance > 0 else 0
    pnl_dollars = pnl_price * MNQ_POINT_VALUE
    return TradeResult(
        setup_name=entry.setup_name,
        direction=entry.direction,
        entry_price=entry.entry_price,
        stop_loss=entry.stop_loss,
        exit_price=exit_price,
        sl_distance=entry.sl_distance,
        pnl_r=pnl_r,
        pnl_dollars=pnl_dollars,
        bars_held=bars,
        exit_reason=reason,
    )


# ─── Strategy 1: Fixed R:R ───────────────────────────────────────────────────

def make_fixed_rr(tp_r: float):
    """Exit at fixed R:R target."""
    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        is_long = entry.direction == Direction.BULLISH
        tp = entry.entry_price + entry.sl_distance * tp_r if is_long \
            else entry.entry_price - entry.sl_distance * tp_r
        if is_long and candle.high >= tp:
            return (sl, tp, f"TP {tp_r}R")
        if not is_long and candle.low <= tp:
            return (sl, tp, f"TP {tp_r}R")
        return None
    return exit_fn


# ─── Strategy 2: Trail per bar + BE ──────────────────────────────────────────

def make_trail_per_bar(trail_r: float, be_r: float):
    """Trail SL by trail_r per bar after price reaches be_r."""
    state = {"at_be": False, "current_sl": None}

    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        is_long = entry.direction == Direction.BULLISH
        if state["current_sl"] is None:
            state["current_sl"] = sl

        profit = (candle.close - entry.entry_price) if is_long else (entry.entry_price - candle.close)
        be_dist = entry.sl_distance * be_r
        trail_step = entry.sl_distance * trail_r

        # Check BE
        if not state["at_be"] and profit >= be_dist:
            state["at_be"] = True
            if is_long:
                state["current_sl"] = max(state["current_sl"], entry.entry_price)
            else:
                state["current_sl"] = min(state["current_sl"], entry.entry_price)

        # Trail after BE
        if state["at_be"]:
            if is_long:
                new_sl = state["current_sl"] + trail_step
                if new_sl < candle.close:
                    state["current_sl"] = new_sl
            else:
                new_sl = state["current_sl"] - trail_step
                if new_sl > candle.close:
                    state["current_sl"] = new_sl

        return (state["current_sl"], None, None)

    def wrapper(entry, candle, bar_num, sl):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl
        return exit_fn(entry, candle, bar_num, sl)

    return wrapper


# ─── Strategy 3: BE at X R, then fixed TP ────────────────────────────────────

def make_be_then_tp(be_r: float, tp_r: float):
    """Move to BE at be_r, then hold for tp_r."""
    state = {"at_be": False, "current_sl": None}

    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl

        is_long = entry.direction == Direction.BULLISH
        profit = (candle.close - entry.entry_price) if is_long else (entry.entry_price - candle.close)

        # TP check
        tp = entry.entry_price + entry.sl_distance * tp_r if is_long \
            else entry.entry_price - entry.sl_distance * tp_r
        if is_long and candle.high >= tp:
            return (sl, tp, f"BE+TP {tp_r}R")
        if not is_long and candle.low <= tp:
            return (sl, tp, f"BE+TP {tp_r}R")

        # BE
        if not state["at_be"] and profit >= entry.sl_distance * be_r:
            state["at_be"] = True
            state["current_sl"] = entry.entry_price

        return (state["current_sl"] if state["at_be"] else sl, None, None)

    return exit_fn


# ─── Strategy 4: Swing TP ────────────────────────────────────────────────────

def make_swing_tp(lookback: int = 90):
    """TP at previous swing high/low within lookback bars."""
    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        # We only have the current candle, not history context here.
        # So we store the TP from the entry context.
        return None
    return exit_fn


def find_swing_tp(candles: list[Candle], entry_bar: int, direction: Direction,
                  lookback: int = 90) -> float | None:
    """Find swing high/low TP from candles before entry."""
    start = max(0, entry_bar - lookback)
    window = candles[start:entry_bar]
    if len(window) < 3:
        return None

    if direction == Direction.BULLISH:
        # Find highest swing high before entry
        best = None
        for i in range(1, len(window) - 1):
            if window[i].high > window[i-1].high and window[i].high > window[i+1].high:
                if best is None or window[i].high > best:
                    best = window[i].high
        return best
    else:
        # Find lowest swing low before entry
        best = None
        for i in range(1, len(window) - 1):
            if window[i].low < window[i-1].low and window[i].low < window[i+1].low:
                if best is None or window[i].low < best:
                    best = window[i].low
        return best


def make_swing_tp_exit(candles: list[Candle], entry: EntrySignal, lookback: int = 90):
    """Create exit fn that targets swing high/low."""
    tp = find_swing_tp(candles, entry.global_bar, entry.direction, lookback)
    state = {"at_be": False, "current_sl": None}

    def exit_fn(e: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl

        is_long = e.direction == Direction.BULLISH

        # Check TP
        if tp is not None:
            if is_long and candle.high >= tp:
                return (sl, tp, f"Swing TP {lookback}")
            if not is_long and candle.low <= tp:
                return (sl, tp, f"Swing TP {lookback}")

        # BE at 0.2R
        profit = (candle.close - e.entry_price) if is_long else (e.entry_price - candle.close)
        if not state["at_be"] and profit >= e.sl_distance * 0.2:
            state["at_be"] = True
            state["current_sl"] = e.entry_price

        return (state["current_sl"] if state["at_be"] else sl, None, None)

    return exit_fn


# ─── Strategy 5: Previous N-min high/low TP ──────────────────────────────────

def find_range_tp(candles: list[Candle], entry_bar: int, direction: Direction,
                  lookback_bars: int = 90) -> float | None:
    """TP at previous N-bar high/low."""
    start = max(0, entry_bar - lookback_bars)
    window = candles[start:entry_bar]
    if not window:
        return None
    if direction == Direction.BULLISH:
        return max(c.high for c in window)
    return min(c.low for c in window)


def make_range_tp_exit(candles: list[Candle], entry: EntrySignal, lookback: int = 90):
    """TP at previous N-bar range high/low + BE at 0.2R."""
    tp = find_range_tp(candles, entry.global_bar, entry.direction, lookback)
    state = {"at_be": False, "current_sl": None}

    def exit_fn(e: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl

        is_long = e.direction == Direction.BULLISH

        if tp is not None:
            if is_long and candle.high >= tp:
                return (sl, tp, f"Range {lookback} TP")
            if not is_long and candle.low <= tp:
                return (sl, tp, f"Range {lookback} TP")

        profit = (candle.close - e.entry_price) if is_long else (e.entry_price - candle.close)
        if not state["at_be"] and profit >= e.sl_distance * 0.2:
            state["at_be"] = True
            state["current_sl"] = e.entry_price

        return (state["current_sl"] if state["at_be"] else sl, None, None)

    return exit_fn


# ─── Strategy 6: Candle structure trail ───────────────────────────────────────

def make_candle_trail():
    """Trail SL to previous candle's low (long) or high (short) after BE."""
    state = {"at_be": False, "current_sl": None, "prev_candle": None}

    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl
            state["prev_candle"] = None

        is_long = entry.direction == Direction.BULLISH
        profit = (candle.close - entry.entry_price) if is_long else (entry.entry_price - candle.close)

        # BE at 0.2R
        if not state["at_be"] and profit >= entry.sl_distance * 0.2:
            state["at_be"] = True
            state["current_sl"] = entry.entry_price

        # Trail to prev candle low/high
        if state["at_be"] and state["prev_candle"] is not None:
            pc = state["prev_candle"]
            if is_long:
                new_sl = pc.low
                if new_sl > state["current_sl"]:
                    state["current_sl"] = new_sl
            else:
                new_sl = pc.high
                if new_sl < state["current_sl"]:
                    state["current_sl"] = new_sl

        state["prev_candle"] = candle
        return (state["current_sl"], None, None)

    return exit_fn


# ─── Strategy 7: Live swing trail ─────────────────────────────────────────────

def make_live_swing_trail(pivot_len: int = 3, be_r: float = 0.2):
    """Trail SL to each confirmed swing low (long) or swing high (short).
    A swing is confirmed when pivot_len bars have passed since the pivot.
    Only ratchets — never moves SL backwards."""
    state = {"at_be": False, "current_sl": None, "history": []}

    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl
            state["history"] = []

        state["history"].append(candle)
        is_long = entry.direction == Direction.BULLISH
        profit = (candle.close - entry.entry_price) if is_long else (entry.entry_price - candle.close)

        # BE gate
        if not state["at_be"] and profit >= entry.sl_distance * be_r:
            state["at_be"] = True
            if is_long:
                state["current_sl"] = max(state["current_sl"], entry.entry_price)
            else:
                state["current_sl"] = min(state["current_sl"], entry.entry_price)

        # Check for confirmed swing pivot in recent history
        hist = state["history"]
        if state["at_be"] and len(hist) >= pivot_len * 2 + 1:
            mid = len(hist) - 1 - pivot_len  # the candidate pivot bar
            if mid >= pivot_len:
                if is_long:
                    # Swing low: mid bar low < surrounding bars
                    is_swing = all(hist[mid].low <= hist[mid - j].low for j in range(1, pivot_len + 1)) and \
                               all(hist[mid].low <= hist[mid + j].low for j in range(1, pivot_len + 1))
                    if is_swing and hist[mid].low > state["current_sl"]:
                        state["current_sl"] = hist[mid].low
                else:
                    # Swing high: mid bar high > surrounding bars
                    is_swing = all(hist[mid].high >= hist[mid - j].high for j in range(1, pivot_len + 1)) and \
                               all(hist[mid].high >= hist[mid + j].high for j in range(1, pivot_len + 1))
                    if is_swing and hist[mid].high < state["current_sl"]:
                        state["current_sl"] = hist[mid].high

        return (state["current_sl"], None, None)

    return exit_fn


# ─── Strategy 8: Swing TP (nearest) ──────────────────────────────────────────

def make_nearest_swing_tp(candles: list[Candle], entry: EntrySignal, lookback: int = 60):
    """TP at the nearest swing high (long) / swing low (short) before entry.
    Not the highest/lowest — the CLOSEST one in the direction of the trade."""
    tp = _find_nearest_swing(candles, entry.global_bar, entry.direction, lookback)
    state = {"at_be": False, "current_sl": None}

    def exit_fn(e: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl

        is_long = e.direction == Direction.BULLISH

        # TP hit
        if tp is not None:
            if is_long and candle.high >= tp:
                return (sl, tp, f"NearSwing {lookback}")
            if not is_long and candle.low <= tp:
                return (sl, tp, f"NearSwing {lookback}")

        # BE
        profit = (candle.close - e.entry_price) if is_long else (e.entry_price - candle.close)
        if not state["at_be"] and profit >= e.sl_distance * 0.2:
            state["at_be"] = True
            state["current_sl"] = e.entry_price

        return (state["current_sl"] if state["at_be"] else sl, None, None)

    return exit_fn


def _find_nearest_swing(candles: list[Candle], entry_bar: int, direction: Direction,
                        lookback: int) -> float | None:
    """Find the nearest swing high/low (closest to entry price) in trade direction."""
    start = max(0, entry_bar - lookback)
    window = candles[start:entry_bar]
    if len(window) < 3:
        return None
    entry_price = candles[entry_bar].close if entry_bar < len(candles) else None
    if entry_price is None:
        return None

    if direction == Direction.BULLISH:
        # Nearest swing high ABOVE entry
        best = None
        for i in range(1, len(window) - 1):
            if window[i].high > window[i-1].high and window[i].high > window[i+1].high:
                if window[i].high > entry_price:
                    if best is None or window[i].high < best:
                        best = window[i].high
        return best
    else:
        # Nearest swing low BELOW entry
        best = None
        for i in range(1, len(window) - 1):
            if window[i].low < window[i-1].low and window[i].low < window[i+1].low:
                if window[i].low < entry_price:
                    if best is None or window[i].low > best:
                        best = window[i].low
        return best


# ─── Strategy 9: Swing trail + fixed max TP cap ──────────────────────────────

def make_swing_trail_capped(pivot_len: int = 3, max_r: float = 2.0, be_r: float = 0.2):
    """Live swing trail with a hard TP cap at max_r."""
    state = {"at_be": False, "current_sl": None, "history": []}

    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl
            state["history"] = []

        state["history"].append(candle)
        is_long = entry.direction == Direction.BULLISH
        profit = (candle.close - entry.entry_price) if is_long else (entry.entry_price - candle.close)

        # Hard TP cap
        tp_price = entry.entry_price + entry.sl_distance * max_r if is_long \
            else entry.entry_price - entry.sl_distance * max_r
        if is_long and candle.high >= tp_price:
            return (sl, tp_price, f"SwingTrail cap {max_r}R")
        if not is_long and candle.low <= tp_price:
            return (sl, tp_price, f"SwingTrail cap {max_r}R")

        # BE
        if not state["at_be"] and profit >= entry.sl_distance * be_r:
            state["at_be"] = True
            if is_long:
                state["current_sl"] = max(state["current_sl"], entry.entry_price)
            else:
                state["current_sl"] = min(state["current_sl"], entry.entry_price)

        # Swing trail
        hist = state["history"]
        if state["at_be"] and len(hist) >= pivot_len * 2 + 1:
            mid = len(hist) - 1 - pivot_len
            if mid >= pivot_len:
                if is_long:
                    is_swing = all(hist[mid].low <= hist[mid - j].low for j in range(1, pivot_len + 1)) and \
                               all(hist[mid].low <= hist[mid + j].low for j in range(1, pivot_len + 1))
                    if is_swing and hist[mid].low > state["current_sl"]:
                        state["current_sl"] = hist[mid].low
                else:
                    is_swing = all(hist[mid].high >= hist[mid - j].high for j in range(1, pivot_len + 1)) and \
                               all(hist[mid].high >= hist[mid + j].high for j in range(1, pivot_len + 1))
                    if is_swing and hist[mid].high < state["current_sl"]:
                        state["current_sl"] = hist[mid].high

        return (state["current_sl"], None, None)

    return exit_fn


# ─── Strategy 10: FVG formation TP ───────────────────────────────────────────

def make_fvg_formation_tp(r_multiple: float = 1.0):
    """TP at the FVG formation's full range (formation_hh or formation_ll).
    For longs: TP = formation HH + r_multiple * formation range.
    For shorts: TP = formation LL - r_multiple * formation range."""
    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        is_long = entry.direction == Direction.BULLISH
        fvg = entry.fvg
        form_range = fvg.formation_hh - fvg.formation_ll
        if is_long:
            tp = fvg.formation_hh + form_range * r_multiple
            if candle.high >= tp:
                return (sl, tp, f"FVG form {r_multiple}x")
        else:
            tp = fvg.formation_ll - form_range * r_multiple
            if candle.low <= tp:
                return (sl, tp, f"FVG form {r_multiple}x")
        return None
    return exit_fn


# ─── Strategy 11: Swing trail + partial at nearest swing ─────────────────────

def make_swing_partial_trail(candles: list[Candle], entry: EntrySignal,
                             pivot_len: int = 3, lookback: int = 60):
    """Take profit at nearest swing target, then trail remainder via live swings."""
    tp = _find_nearest_swing(candles, entry.global_bar, entry.direction, lookback)
    state = {"partial_taken": False, "at_be": False, "current_sl": None, "history": []}

    def exit_fn(e: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["partial_taken"] = False
            state["at_be"] = False
            state["current_sl"] = sl
            state["history"] = []

        state["history"].append(candle)
        is_long = e.direction == Direction.BULLISH
        profit = (candle.close - e.entry_price) if is_long else (e.entry_price - candle.close)

        # Partial at nearest swing (simulated as lock-in 50% of the R at that point)
        if not state["partial_taken"] and tp is not None:
            if is_long and candle.high >= tp:
                state["partial_taken"] = True
                state["at_be"] = True
                state["current_sl"] = e.entry_price
            elif not is_long and candle.low <= tp:
                state["partial_taken"] = True
                state["at_be"] = True
                state["current_sl"] = e.entry_price

        # BE without partial
        if not state["at_be"] and profit >= e.sl_distance * 0.3:
            state["at_be"] = True
            if is_long:
                state["current_sl"] = max(state["current_sl"], e.entry_price)
            else:
                state["current_sl"] = min(state["current_sl"], e.entry_price)

        # Live swing trail after BE
        hist = state["history"]
        if state["at_be"] and len(hist) >= pivot_len * 2 + 1:
            mid = len(hist) - 1 - pivot_len
            if mid >= pivot_len:
                if is_long:
                    is_swing = all(hist[mid].low <= hist[mid - j].low for j in range(1, pivot_len + 1)) and \
                               all(hist[mid].low <= hist[mid + j].low for j in range(1, pivot_len + 1))
                    if is_swing and hist[mid].low > state["current_sl"]:
                        state["current_sl"] = hist[mid].low
                else:
                    is_swing = all(hist[mid].high >= hist[mid - j].high for j in range(1, pivot_len + 1)) and \
                               all(hist[mid].high >= hist[mid + j].high for j in range(1, pivot_len + 1))
                    if is_swing and hist[mid].high < state["current_sl"]:
                        state["current_sl"] = hist[mid].high

        return (state["current_sl"], None, None)

    return exit_fn


# ─── Strategy 12: Swing OR fixed (whichever is closer) ────────────────────────

def make_swing_or_fixed(candles: list[Candle], entry: EntrySignal,
                        lookback: int = 30, fixed_r: float = 1.0):
    """Use nearest swing as TP if it's closer than fixed_r, else use fixed_r."""
    swing_tp = _find_nearest_swing(candles, entry.global_bar, entry.direction, lookback)
    is_long = entry.direction == Direction.BULLISH
    fixed_tp = entry.entry_price + entry.sl_distance * fixed_r if is_long \
        else entry.entry_price - entry.sl_distance * fixed_r

    # Pick the closer target
    if swing_tp is not None:
        if is_long:
            tp = min(swing_tp, fixed_tp) if swing_tp > entry.entry_price else fixed_tp
        else:
            tp = max(swing_tp, fixed_tp) if swing_tp < entry.entry_price else fixed_tp
    else:
        tp = fixed_tp

    state = {"at_be": False, "current_sl": None}

    def exit_fn(e: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl

        il = e.direction == Direction.BULLISH
        if il and candle.high >= tp:
            return (sl, tp, f"Sw|{fixed_r}R")
        if not il and candle.low <= tp:
            return (sl, tp, f"Sw|{fixed_r}R")

        profit = (candle.close - e.entry_price) if il else (e.entry_price - candle.close)
        if not state["at_be"] and profit >= e.sl_distance * 0.2:
            state["at_be"] = True
            state["current_sl"] = e.entry_price
        return (state["current_sl"] if state["at_be"] else sl, None, None)

    return exit_fn


# ─── Strategy 13: Time-based profit exit ─────────────────────────────────────

def make_time_profit(max_bars: int = 10, be_r: float = 0.2):
    """If in profit after max_bars, exit. Otherwise keep trailing."""
    state = {"at_be": False, "current_sl": None}

    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl

        is_long = entry.direction == Direction.BULLISH
        profit = (candle.close - entry.entry_price) if is_long else (entry.entry_price - candle.close)

        # BE
        if not state["at_be"] and profit >= entry.sl_distance * be_r:
            state["at_be"] = True
            state["current_sl"] = entry.entry_price

        # Time exit: if past max_bars and in profit, take it
        if bar_num >= max_bars and profit > 0:
            return (sl, candle.close, f"TimeProfit@{max_bars}")

        # After BE, trail with candle lows/highs
        if state["at_be"]:
            if is_long:
                # Use current candle low as potential trail (lagging by 1 would be better but close enough)
                trail = candle.low
                if trail > state["current_sl"] and trail < candle.close:
                    state["current_sl"] = trail
            else:
                trail = candle.high
                if trail < state["current_sl"] and trail > candle.close:
                    state["current_sl"] = trail

        return (state["current_sl"] if state["at_be"] else sl, None, None)

    return exit_fn


# ─── Strategy 14: FVG zone edge TP ───────────────────────────────────────────

def make_fvg_zone_tp():
    """For longs: TP at FVG formation HH. For shorts: TP at FVG formation LL.
    Simple — just get to the other side of what created the FVG."""
    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        is_long = entry.direction == Direction.BULLISH
        fvg = entry.fvg
        if is_long:
            tp = fvg.formation_hh
            if candle.high >= tp:
                return (sl, tp, "FVG zone edge")
        else:
            tp = fvg.formation_ll
            if candle.low <= tp:
                return (sl, tp, "FVG zone edge")
        return None
    return exit_fn


# ─── Strategy 15: Smart swing — use swing if close, else fixed R ─────────────

def make_smart_swing(candles: list[Candle], entry: EntrySignal,
                     lookback: int = 30, max_r: float = 1.0):
    """Use nearest swing TP if within max_r distance. If swing is further or
    doesn't exist, fall back to fixed max_r TP. Always with BE at 0.2R."""
    swing_tp = _find_nearest_swing(candles, entry.global_bar, entry.direction, lookback)
    is_long = entry.direction == Direction.BULLISH
    fixed_tp = entry.entry_price + entry.sl_distance * max_r if is_long \
        else entry.entry_price - entry.sl_distance * max_r

    # Use swing if it's within max_r, otherwise fixed
    if swing_tp is not None:
        swing_dist = abs(swing_tp - entry.entry_price)
        max_dist = entry.sl_distance * max_r
        tp = swing_tp if swing_dist <= max_dist else fixed_tp
    else:
        tp = fixed_tp

    state = {"at_be": False, "current_sl": None}

    def exit_fn(e: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["at_be"] = False
            state["current_sl"] = sl

        il = e.direction == Direction.BULLISH
        if il and candle.high >= tp:
            return (sl, tp, f"Smart {max_r}R")
        if not il and candle.low <= tp:
            return (sl, tp, f"Smart {max_r}R")

        profit = (candle.close - e.entry_price) if il else (e.entry_price - candle.close)
        if not state["at_be"] and profit >= e.sl_distance * 0.2:
            state["at_be"] = True
            state["current_sl"] = e.entry_price
        return (state["current_sl"] if state["at_be"] else sl, None, None)

    return exit_fn


# ─── Strategy 16: Partial + trail ─────────────────────────────────────────────

def make_partial_then_trail(partial_r: float, trail_r: float):
    """Take half off at partial_r, trail rest at trail_r per bar."""
    state = {"partial_taken": False, "at_be": False, "current_sl": None}

    def exit_fn(entry: EntrySignal, candle: Candle, bar_num: int, sl: float):
        if bar_num == 1:
            state["partial_taken"] = False
            state["at_be"] = False
            state["current_sl"] = sl

        is_long = entry.direction == Direction.BULLISH
        profit = (candle.close - entry.entry_price) if is_long else (entry.entry_price - candle.close)
        profit_r = profit / entry.sl_distance if entry.sl_distance > 0 else 0

        # Partial at partial_r (simulate as 50% exit, report blended)
        if not state["partial_taken"] and profit_r >= partial_r:
            state["partial_taken"] = True
            state["at_be"] = True
            state["current_sl"] = entry.entry_price

        # Trail after partial
        if state["partial_taken"]:
            trail_step = entry.sl_distance * trail_r
            if is_long:
                new_sl = state["current_sl"] + trail_step
                if new_sl < candle.close:
                    state["current_sl"] = new_sl
            else:
                new_sl = state["current_sl"] - trail_step
                if new_sl > candle.close:
                    state["current_sl"] = new_sl

        return (state["current_sl"], None, None)

    return exit_fn


# ══════════════════════════════════════════════════════════════════════════════
# BACKTESTER
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class StrategyStats:
    name: str
    trades: int = 0
    wins: int = 0
    losses: int = 0
    total_r: float = 0.0
    total_dollars: float = 0.0
    max_drawdown_r: float = 0.0
    avg_bars: float = 0.0
    results: list[TradeResult] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades * 100 if self.trades > 0 else 0

    @property
    def avg_r(self) -> float:
        return self.total_r / self.trades if self.trades > 0 else 0

    @property
    def profit_factor(self) -> float:
        gross_win = sum(r.pnl_r for r in self.results if r.pnl_r > 0)
        gross_loss = abs(sum(r.pnl_r for r in self.results if r.pnl_r < 0))
        return gross_win / gross_loss if gross_loss > 0 else float('inf')

    @property
    def expectancy_r(self) -> float:
        """Expectancy in R = (win_rate * avg_win) - (loss_rate * avg_loss)."""
        if not self.results:
            return 0
        wins = [r.pnl_r for r in self.results if r.pnl_r > 0]
        losses = [abs(r.pnl_r) for r in self.results if r.pnl_r <= 0]
        avg_win = np.mean(wins) if wins else 0
        avg_loss = np.mean(losses) if losses else 0
        wr = len(wins) / len(self.results)
        return wr * avg_win - (1 - wr) * avg_loss


def run_backtest(candles: list[Candle], entries: list[EntrySignal]) -> dict[str, StrategyStats]:
    """Run all exit strategies against the same entries."""

    strategies: dict[str, Callable] = {
        # Baselines (top 3 from previous run)
        "Fixed 2.0R TP":     ("fixed", 2.0),
        "BE@0.2R → TP 2.0R":  ("be_tp", 0.2, 2.0),
        "BE@0.5R → TP 2.0R":  ("be_tp", 0.5, 2.0),

        # ── SWING STRATEGIES ──

        # Live swing trail: SL ratchets to each confirmed swing pivot
        "SwingTrail 3bar BE@0.2R":     ("swing_trail", 3, 0.2),
        "SwingTrail 5bar BE@0.2R":     ("swing_trail", 5, 0.2),
        "SwingTrail 3bar BE@0.5R":     ("swing_trail", 3, 0.5),
        "SwingTrail 5bar BE@0.5R":     ("swing_trail", 5, 0.5),

        # Swing trail + hard TP cap (best of both worlds?)
        "SwingTrail 3bar cap@2R":      ("swing_trail_cap", 3, 2.0, 0.2),
        "SwingTrail 3bar cap@3R":      ("swing_trail_cap", 3, 3.0, 0.2),
        "SwingTrail 5bar cap@2R":      ("swing_trail_cap", 5, 2.0, 0.2),
        "SwingTrail 5bar cap@3R":      ("swing_trail_cap", 5, 3.0, 0.2),

        # Nearest swing TP (closest target, not furthest)
        "NearestSwing TP 60bar":       ("nearest_swing", 60),
        "NearestSwing TP 90bar":       ("nearest_swing", 90),
        "NearestSwing TP 30bar":       ("nearest_swing", 30),

        # Partial at nearest swing, then swing trail remainder
        "Partial@swing + SwingTrail":  ("swing_partial", 3, 60),

        # FVG formation range as TP
        "FVG formation 1x":            ("fvg_form", 1.0),
        "FVG formation 0.5x":          ("fvg_form", 0.5),
        "FVG formation 2x":            ("fvg_form", 2.0),

        # Candle trail for reference
        "Candle Trail (prev low/high)": ("candle_trail",),

        # Old trail for reference
        "Trail 0.05R/bar BE@0.2R":     ("trail", 0.05, 0.2),

        # ── HIGH WIN RATE STRATEGIES ──

        # Nearest swing with tight lookback
        "NearestSwing TP 15bar":       ("nearest_swing", 15),
        "NearestSwing TP 10bar":       ("nearest_swing", 10),

        # Nearest swing OR fixed R — whichever is closer (tighter target)
        "NearSwing30 OR 1R (closer)":  ("swing_or_fixed", 30, 1.0),
        "NearSwing30 OR 0.5R (closer)":("swing_or_fixed", 30, 0.5),
        "NearSwing60 OR 1R (closer)":  ("swing_or_fixed", 60, 1.0),

        # Time-based: take profit if in profit after N bars, else trail
        "Profit@10bar or trail":       ("time_profit", 10, 0.2),
        "Profit@20bar or trail":       ("time_profit", 20, 0.2),
        "Profit@5bar or trail":        ("time_profit", 5, 0.2),

        # Scalp: tight fixed TP + wider SL tolerance (asymmetric)
        "Fixed 0.3R TP":              ("fixed", 0.3),
        "Fixed 0.4R TP":              ("fixed", 0.4),

        # FVG zone as TP (just to other side of the FVG zone)
        "FVG zone edge TP":           ("fvg_zone",),

        # Combo: nearest swing if close, else 1R, with BE
        "Smart: swing<1R else 1R":    ("smart_swing", 30, 1.0),
        "Smart: swing<1.5R else 1.5R":("smart_swing", 30, 1.5),
    }

    stats: dict[str, StrategyStats] = {name: StrategyStats(name=name) for name in strategies}

    for entry in entries:
        for name, params in strategies.items():
            kind = params[0]

            if kind == "fixed":
                exit_fn = make_fixed_rr(params[1])
            elif kind == "trail":
                exit_fn = make_trail_per_bar(params[1], params[2])
            elif kind == "be_tp":
                exit_fn = make_be_then_tp(params[1], params[2])
            elif kind == "swing_trail":
                exit_fn = make_live_swing_trail(pivot_len=params[1], be_r=params[2])
            elif kind == "swing_trail_cap":
                exit_fn = make_swing_trail_capped(pivot_len=params[1], max_r=params[2], be_r=params[3])
            elif kind == "nearest_swing":
                exit_fn = make_nearest_swing_tp(candles, entry, lookback=params[1])
            elif kind == "swing_partial":
                exit_fn = make_swing_partial_trail(candles, entry, pivot_len=params[1], lookback=params[2])
            elif kind == "fvg_form":
                exit_fn = make_fvg_formation_tp(r_multiple=params[1])
            elif kind == "candle_trail":
                exit_fn = make_candle_trail()
            elif kind == "swing_or_fixed":
                exit_fn = make_swing_or_fixed(candles, entry, lookback=params[1], fixed_r=params[2])
            elif kind == "time_profit":
                exit_fn = make_time_profit(max_bars=params[1], be_r=params[2])
            elif kind == "fvg_zone":
                exit_fn = make_fvg_zone_tp()
            elif kind == "smart_swing":
                exit_fn = make_smart_swing(candles, entry, lookback=params[1], max_r=params[2])
            else:
                continue

            result = simulate_exit(entry, candles, exit_fn)
            if result:
                s = stats[name]
                s.trades += 1
                s.total_r += result.pnl_r
                s.total_dollars += result.pnl_dollars
                s.results.append(result)
                if result.pnl_r > 0:
                    s.wins += 1
                else:
                    s.losses += 1

    # Compute avg bars and max drawdown
    for s in stats.values():
        if s.results:
            s.avg_bars = np.mean([r.bars_held for r in s.results])
            # Max drawdown in R
            cum = np.cumsum([r.pnl_r for r in s.results])
            peak = np.maximum.accumulate(cum)
            dd = peak - cum
            s.max_drawdown_r = float(np.max(dd)) if len(dd) > 0 else 0

    return stats


# ══════════════════════════════════════════════════════════════════════════════
# REPORTING
# ══════════════════════════════════════════════════════════════════════════════

def print_report(stats: dict[str, StrategyStats]):
    from tabulate import tabulate

    # Sort by total R descending
    sorted_stats = sorted(stats.values(), key=lambda s: s.total_r, reverse=True)

    rows = []
    for s in sorted_stats:
        rows.append([
            s.name,
            s.trades,
            f"{s.win_rate:.0f}%",
            f"{s.total_r:+.2f}R",
            f"${s.total_dollars:+.0f}",
            f"{s.avg_r:+.3f}R",
            f"{s.expectancy_r:+.3f}R",
            f"{s.profit_factor:.2f}",
            f"{s.max_drawdown_r:.2f}R",
            f"{s.avg_bars:.0f}",
        ])

    headers = ["Strategy", "Trades", "Win%", "Total R", "Total $", "Avg R",
               "Expect", "PF", "MaxDD", "Avg Bars"]

    print("\n" + "=" * 100)
    print("ETrades Scalp Model — Exit Strategy Comparison")
    print("=" * 100)
    print(tabulate(rows, headers=headers, tablefmt="simple"))
    print("=" * 100)

    # Top 3
    print("\n🏆 TOP 3 BY TOTAL R:")
    for i, s in enumerate(sorted_stats[:3]):
        print(f"  {i+1}. {s.name}: {s.total_r:+.2f}R | WR {s.win_rate:.0f}% | PF {s.profit_factor:.2f}")

    # Top 3 by expectancy
    by_expect = sorted(stats.values(), key=lambda s: s.expectancy_r, reverse=True)
    print("\n🎯 TOP 3 BY EXPECTANCY:")
    for i, s in enumerate(by_expect[:3]):
        print(f"  {i+1}. {s.name}: {s.expectancy_r:+.3f}R/trade | WR {s.win_rate:.0f}%")

    # Setup breakdown for best strategy
    best = sorted_stats[0]
    print(f"\n📊 SETUP BREAKDOWN FOR BEST ({best.name}):")
    setup_groups: dict[str, list[TradeResult]] = {}
    for r in best.results:
        setup_groups.setdefault(r.setup_name, []).append(r)
    for setup_name, results in sorted(setup_groups.items()):
        total_r = sum(r.pnl_r for r in results)
        wr = sum(1 for r in results if r.pnl_r > 0) / len(results) * 100
        print(f"  {setup_name}: {len(results)} trades, {total_r:+.2f}R, WR {wr:.0f}%")

    # Exit reason breakdown for best
    print(f"\n📋 EXIT REASONS ({best.name}):")
    reason_groups: dict[str, list[TradeResult]] = {}
    for r in best.results:
        reason_groups.setdefault(r.exit_reason, []).append(r)
    for reason, results in sorted(reason_groups.items(), key=lambda x: len(x[1]), reverse=True):
        total_r = sum(r.pnl_r for r in results)
        print(f"  {reason}: {len(results)} trades, {total_r:+.2f}R")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="ETrades Scalp Model — Exit Strategy Optimizer")
    parser.add_argument("--days", type=int, default=30, help="Days of data to download (max 30 for 1m)")
    parser.add_argument("--csv", type=str, help="Load data from CSV/Parquet instead of yfinance")
    parser.add_argument("--symbol", type=str, default="NQ=F", help="Yahoo Finance symbol")
    args = parser.parse_args()

    print("Loading data...")
    if args.csv:
        df = load_file(args.csv)
    else:
        df = load_yfinance(args.symbol, args.days)

    print(f"Loaded {len(df)} bars from {df.index[0]} to {df.index[-1]}")

    candles = df_to_candles(df)
    print(f"Converted to {len(candles)} candles")

    print("Finding entries...")
    entries = find_entries(candles)
    print(f"Found {len(entries)} entry signals")

    if not entries:
        print("No entries found — check data range and session times.")
        return

    # Show entry breakdown
    setup_counts: dict[str, int] = {}
    for e in entries:
        setup_counts[e.setup_name] = setup_counts.get(e.setup_name, 0) + 1
    for name, count in sorted(setup_counts.items()):
        print(f"  {name}: {count}")

    print("\nRunning 20 exit strategies...")
    stats = run_backtest(candles, entries)

    print_report(stats)


if __name__ == "__main__":
    main()

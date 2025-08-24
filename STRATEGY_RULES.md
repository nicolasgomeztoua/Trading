## Three-Step 9:30 Range Scalping — Strategy Rules

This document describes the complete trading rules implemented in `orbrange.pine` for the “Three-Step 9:30 Range Scalping” strategy.

### Scope and Philosophy
- **Markets**: Works on futures, stocks, crypto, and forex.
- **Execution timeframe**: 1-minute chart for entries and trade management.
- **Core idea**: Use the first 5-minute candle starting at 9:30 a.m. New York time to define a range. Take mechanical entries from three setups: Break, Trap, and Reversal. Manage risk with a fixed R multiple take-profit and precise stop-loss rules.

## Session and Time Rules
- **Time zone**: Strategy logic assumes chart time is New York time.
- **Range candle**: Use the 5-minute candle that STARTS at 9:30 (locks on its close at ~9:35).
- **Trading window**: First hour only — from 9:30:00 to 10:29:59.
- **Holiday blackout**: No trading from Dec 15 to Jan 15.
- **One trade per day (optional)**: Controlled by `oneTradePerDay`.

## First 5-Minute Range
- **Range High** = high of the 9:30–9:34:59 5-minute candle.
- **Range Low**  = low of the 9:30–9:34:59 5-minute candle.
- The range becomes available immediately when that candle closes.

## Fair Value Gap (FVG) Definition
Use a strict three-candle pattern with bars A, B, C = `[2], [1], [0]` relative to the current bar:

- **Bullish FVG** exists if: `low[B] > high[A]` AND `low[C] > high[A]`.
- **Bearish FVG** exists if: `high[B] < low[A]` AND `high[C] < low[A]`.

Note: This definition requires the gap to remain intact through candle C.

## Entry Gating (applies to all setups)
- Range must be locked (the 9:30 5m candle has closed).
- Must be within the trading window and not in the blackout window.
- Must be flat (no open position). If `oneTradePerDay` is true, must not have traded yet that day.

## Setups

### 1) Break Entry
Goal: Trade a breakout of the 9:30 range when a strong imbalance forms.

Requirements (Long):
- A valid bullish FVG (A,B,C as defined above), and
- At least one of A, B, or C has a body close above the Range High.

Entry (Long):
- Enter at the close of candle C when the conditions are true.

Stop-Loss (Long):
- Place SL at the low of the very first 1-minute candle that closed above the Range High (the initial break candle).

Take-Profit (Long):
- Fixed `R` multiple: `TP = entry + rr * (entry - stop)`.

Requirements (Short):
- A valid bearish FVG, and
- At least one of A, B, or C has a body close below the Range Low.

Entry (Short):
- Enter at the close of candle C when the conditions are true.

Stop-Loss (Short):
- Place SL at the high of the very first 1-minute candle that closed below the Range Low (the initial break candle).

Take-Profit (Short):
- Fixed `R` multiple: `TP = entry - rr * (stop - entry)`.

Notes:
- This setup allows “any of the three FVG candles” to be the one that closes outside the range. The SL, however, is anchored to the initial outside-close candle, not restricted to the FVG trio.

### 2) Trap Entry
Goal: Trade after a confirmed break, a retest back inside the range, and a re-close outside.

Requirements (Long):
1) First body close above Range High (records the initial break).
2) A retest inside the range (at least one body close back inside).
3) A subsequent body close back above the Range High.
   - No FVG required for Trap entries.

Entry (Long):
- Enter at the re-close above the Range High.

Stop-Loss (Long):
- Place SL at the low of the very first candle that closed above the Range High (the initial break candle). If that is not available, use current bar low.

Take-Profit (Long):
- Fixed `R` multiple: `TP = entry + rr * (entry - stop)`.

Requirements (Short):
1) First body close below Range Low (records the initial break).
2) A retest inside the range (at least one body close back inside).
3) A subsequent body close back below the Range Low.

Entry (Short):
- Enter at the re-close below the Range Low.

Stop-Loss (Short):
- Place SL at the high of the very first candle that closed below the Range Low (the initial break candle). If that is not available, use current bar high.

Take-Profit (Short):
- Fixed `R` multiple: `TP = entry - rr * (stop - entry)`.

Consumption behavior:
- Each direction’s Trap entry can only trigger once per fresh break/retest sequence.

### 3) Reversal Entry
Goal: Fade a failed breakout when an opposite-direction imbalance forms back into the range.

Requirements (Long):
- A prior bearish break was recorded (first body close below the Range Low), and
- A bullish FVG forms while price is closing back inside the range.

Entry (Long):
- Enter at the close of candle C of the bullish FVG while price is inside the range.

Stop-Loss (Long):
- Place SL at the low of candle A (the first candle of the FVG pattern).

Take-Profit (Long):
- Fixed `R` multiple: `TP = entry + rr * (entry - stop)`.

Requirements (Short):
- A prior bullish break was recorded (first body close above the Range High), and
- A bearish FVG forms while price is closing back inside the range.

Entry (Short):
- Enter at the close of candle C of the bearish FVG while price is inside the range.

Stop-Loss (Short):
- Place SL at the high of candle A (the first candle of the FVG pattern).

Take-Profit (Short):
- Fixed `R` multiple: `TP = entry - rr * (stop - entry)`.

Consumption behavior:
- Each direction’s Reversal entry can only trigger once per day.

## Position Sizing
Sizing can be configured via inputs:

- **Mode**: `sizeMode` ∈ {`Risk %`, `Fixed contracts`}.
- **Risk %**: `riskPct` of current equity is allocated to the distance between entry and stop.
  - Contracts = `floor( (equity * riskPct/100) / ((abs(entry - stop) * syminfo.pointvalue)) )`.
- **Fixed contracts**: Use `fixedContracts` regardless of stop distance.

## Parameters (Key Inputs and Defaults)
- `enableBreak = true`, `enableTrap = true`, `enableReversal = true`.
- `rr = 2.0` (take-profit R multiple).
- `oneTradePerDay = true`.
- `riskPct = 1.5` (used when `sizeMode = Risk %`).
- `fixedContracts = 1` (used when `sizeMode = Fixed contracts`).
- Visuals: `showRange = true`, `showSL = true`, `showTP = true`.
- Alerts: `enableAlerts = true`.
- Strategy settings: `pyramiding = 0`, `process_orders_on_close = true`, `calc_on_every_tick = false`.

## Visuals and Alerts
- **Plots**: Range High/Low, active SL/TP lines.
- **Markers**: Setup markers for Break, Trap, and Reversal when conditions are satisfied.
- **Alerts**: Message includes entry, stop, TP, and risk/TP distances.

## Quick Start Checklist
1. Set chart to 1-minute on your instrument; ensure chart time is New York time.
2. Wait for the 9:30–9:34:59 5-minute candle to close; confirm the range is drawn.
3. Trade only between 9:30 and 10:29:59 (unless you intentionally modify this).
4. Look for one of the three setups:
   - Break: FVG present; at least one of A/B/C closes beyond the range.
   - Trap: Break → retest inside → re-close outside (no FVG required).
   - Reversal: Failed break; opposite-direction FVG back into the range.
5. Place SL exactly per setup rule; TP at `rr` times risk (default 2R).
6. Size position via `Risk %` or `Fixed contracts`.

## Assumptions and Notes
- **Fills and slippage**: Backtest uses bar-close execution (`process_orders_on_close`). Real fills will differ; incorporate slippage and fees in live use.
- **FVG definition**: This script uses a strict three-candle gap that persists through candle C; definitions may vary elsewhere.
- **Range candle source**: Range is explicitly the 5-minute bar that begins at 9:30. If your chart isn’t NY time, adjust expectations accordingly.
- **One trade per day**: Enabled by default; disable to allow multiple trades, but consumption guards still limit per-direction triggers.

## Customization Ideas
- Tighten Break confirmation to require the third candle (C) to be the one that closes outside the range.
- Add optional break-even or partial take-profit logic at 1R.
- Parameterize the trading window or remove the holiday blackout.



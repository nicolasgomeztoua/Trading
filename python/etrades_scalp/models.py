from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum


class Direction(IntEnum):
    BEARISH = -1
    NONE = 0
    BULLISH = 1


class State(IntEnum):
    WAIT_OPEN = 0
    SCAN_FVG = 1
    FVG_FOUND = 2
    IN_TRADE = 3
    DONE = 4


@dataclass
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open


@dataclass
class FVG:
    direction: Direction
    zone_hi: float          # top of the FVG zone
    zone_lo: float          # bottom of the FVG zone
    candle1: Candle         # oldest candle ([2] in Pine)
    candle2: Candle         # middle candle ([1] in Pine)
    candle3: Candle         # newest candle ([0] in Pine)
    bar_index: int          # index when FVG was detected

    @property
    def formation_hh(self) -> float:
        return max(self.candle1.high, self.candle2.high, self.candle3.high)

    @property
    def formation_ll(self) -> float:
        return min(self.candle1.low, self.candle2.low, self.candle3.low)

    tapped: bool = False
    tap_bar: int | None = None


@dataclass
class TradeSetup:
    setup_number: int       # 1-4
    setup_name: str
    direction: Direction    # +1 long, -1 short
    entry_price: float
    stop_loss: float
    take_profit: float
    fvg: FVG
    contracts: int = 0

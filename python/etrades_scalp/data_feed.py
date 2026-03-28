from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Callable

import pytz

from .models import Candle

logger = logging.getLogger(__name__)

NY_TZ = pytz.timezone("America/New_York")


class CandleAggregator:
    """Aggregates tick/quote data into 1-minute candles."""

    def __init__(self, on_candle_complete: Callable[[Candle], None]):
        self.on_candle_complete = on_candle_complete
        self.current_candle: Candle | None = None

    def on_tick(self, timestamp: datetime, price: float, volume: int = 0) -> None:
        """Process a tick. Emits completed candle on minute boundary."""
        minute = timestamp.replace(second=0, microsecond=0)

        if self.current_candle is None or self.current_candle.timestamp != minute:
            # New minute — emit previous candle if exists
            if self.current_candle is not None:
                self.on_candle_complete(self.current_candle)
            self.current_candle = Candle(
                timestamp=minute,
                open=price,
                high=price,
                low=price,
                close=price,
                volume=volume,
            )
        else:
            self.current_candle.high = max(self.current_candle.high, price)
            self.current_candle.low = min(self.current_candle.low, price)
            self.current_candle.close = price
            self.current_candle.volume += volume

    def on_chart_bar(self, bar_data: dict) -> None:
        """Process a completed chart bar from Tradovate WebSocket.

        Tradovate chart subscription delivers bars with fields:
        - timestamp (epoch ms)
        - open, high, low, close
        - upVolume, downVolume
        """
        ts = datetime.fromtimestamp(bar_data["timestamp"] / 1000.0, tz=NY_TZ)
        candle = Candle(
            timestamp=ts,
            open=bar_data["open"],
            high=bar_data["high"],
            low=bar_data["low"],
            close=bar_data["close"],
            volume=bar_data.get("upVolume", 0) + bar_data.get("downVolume", 0),
        )
        self.on_candle_complete(candle)

    def parse_ws_message(self, raw: str) -> None:
        """Parse Tradovate WebSocket message and route to on_chart_bar."""
        # Tradovate WS messages have format: event\nid\n\njson
        parts = raw.split("\n", 3)
        if len(parts) < 4:
            return
        try:
            data = json.loads(parts[3])
        except (json.JSONDecodeError, IndexError):
            return

        # Chart data comes as {"charts": [{"id": ..., "bars": [...]}]}
        if isinstance(data, dict) and "charts" in data:
            for chart in data["charts"]:
                for bar in chart.get("bars", []):
                    self.on_chart_bar(bar)

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pytz

from .config import BotConfig
from .data_feed import CandleAggregator
from .models import Candle, State
from .order_manager import OrderManager
from .state_machine import ScalpStateMachine
from .tradovate_client import TradovateClient

logger = logging.getLogger(__name__)

NY_TZ = pytz.timezone("America/New_York")


class ETradesScalpBot:
    def __init__(self, config: BotConfig):
        self.config = config
        self.client = TradovateClient(use_demo=config.use_demo)
        self.state_machine = ScalpStateMachine(config)
        self.order_manager = OrderManager(self.client, config)
        self.aggregator = CandleAggregator(self._on_candle)
        self.running = False
        self._equity: float = 0.0

    def _on_candle(self, candle: Candle) -> None:
        """Callback when a 1-min candle completes."""
        setup = self.state_machine.on_candle(candle)
        if setup:
            asyncio.create_task(self._execute_setup(setup))

    async def _execute_setup(self, setup) -> None:
        success = await self.order_manager.execute_setup(setup, self._equity)
        if not success:
            logger.error("Failed to execute setup — marking day as done")
            self.state_machine.state = State.DONE

    async def _monitor_position(self) -> None:
        """Periodically check if position is still open."""
        while self.running:
            if self.order_manager.in_position:
                still_open = await self.order_manager.check_position()
                if not still_open:
                    self.state_machine.on_trade_closed()
            await asyncio.sleep(5)

    async def _fetch_equity(self) -> None:
        """Fetch current account equity."""
        try:
            bal = await self.client.get_cash_balance(self.config.account_id)
            self._equity = bal.get("totalCashValue", 0.0)
            logger.info("Account equity: $%.2f", self._equity)
        except Exception:
            logger.exception("Failed to fetch equity")

    async def start(self) -> None:
        """Start the bot: authenticate, subscribe, run."""
        logger.info("Starting ETrades Scalp Bot (demo=%s)", self.config.use_demo)

        # Authenticate
        auth = await self.client.authenticate(
            username=self.config.tradovate_username,
            password=self.config.tradovate_password,
            app_id=self.config.tradovate_app_id,
            app_version=self.config.tradovate_app_version,
            cid=self.config.tradovate_cid,
            secret=self.config.tradovate_secret,
        )
        if not self.client.access_token:
            logger.error("Authentication failed — exiting")
            return

        # Get account info
        accounts = await self.client.get_accounts()
        if accounts:
            if self.config.account_id == 0:
                self.config.account_id = accounts[0]["id"]
                logger.info("Using account: %s (id=%d)", accounts[0].get("name", ""), self.config.account_id)

        await self._fetch_equity()

        # Connect WebSocket and subscribe to chart data
        await self.client.connect_ws()

        self.running = True

        # Start position monitor in background
        monitor_task = asyncio.create_task(self._monitor_position())

        # Subscribe to chart data (this blocks until WS closes)
        logger.info("Subscribing to %s 1-min chart data...", self.config.symbol)
        await self.client.subscribe_chart(
            symbol=self.config.symbol,
            timeframe=1,
            callback=self.aggregator.parse_ws_message,
        )

        self.running = False
        monitor_task.cancel()
        logger.info("Bot stopped")

    async def stop(self) -> None:
        """Graceful shutdown."""
        self.running = False
        await self.client.close()
        logger.info("Bot shut down")

from __future__ import annotations

import logging

from .config import BotConfig
from .models import Direction, TradeSetup
from .risk_manager import MNQ_POINT_VALUE, calculate_contracts
from .tradovate_client import TradovateClient

logger = logging.getLogger(__name__)


class OrderManager:
    def __init__(self, client: TradovateClient, config: BotConfig):
        self.client = client
        self.config = config
        self.active_order_id: int | None = None
        self.in_position: bool = False

    async def execute_setup(self, setup: TradeSetup, equity: float = 0.0) -> bool:
        """Place bracket order for a setup. Returns True on success."""
        contracts = calculate_contracts(setup, self.config, equity)
        setup.contracts = contracts

        action = "Buy" if setup.direction == Direction.BULLISH else "Sell"
        symbol = self.config.symbol

        sl_dist = abs(setup.entry_price - setup.stop_loss)
        risk_per = sl_dist * MNQ_POINT_VALUE
        total_risk = risk_per * contracts

        logger.info(
            "Executing %s: %s %d %s @ market | SL=%.2f TP=%.2f | "
            "Risk/contract=$%.2f Total=$%.2f",
            setup.setup_name, action, contracts, symbol,
            setup.stop_loss, setup.take_profit,
            risk_per, total_risk,
        )

        try:
            result = await self.client.place_oso(
                account_id=self.config.account_id,
                action=action,
                symbol=symbol,
                qty=contracts,
                sl_price=setup.stop_loss,
                tp_price=setup.take_profit,
            )
            if "orderId" in result:
                self.active_order_id = result["orderId"]
                self.in_position = True
                logger.info("Order placed: ID=%s", self.active_order_id)
                return True
            else:
                logger.error("Order failed: %s", result)
                return False
        except Exception:
            logger.exception("Failed to place order")
            return False

    async def check_position(self) -> bool:
        """Check if we still have an open position."""
        try:
            positions = await self.client.get_positions(self.config.account_id)
            has_position = any(
                p.get("netPos", 0) != 0
                for p in positions
                if self.config.symbol in p.get("contractId", "")
            )
            if self.in_position and not has_position:
                self.in_position = False
                self.active_order_id = None
                logger.info("Position closed (SL/TP hit)")
            return has_position
        except Exception:
            logger.exception("Failed to check position")
            return self.in_position

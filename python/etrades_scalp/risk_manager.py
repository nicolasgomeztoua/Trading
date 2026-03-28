from __future__ import annotations

import math

from .config import BotConfig
from .models import TradeSetup

# MNQ futures: $2 per point, $0.50 per tick (tick = 0.25)
MNQ_POINT_VALUE = 2.0


def calculate_contracts(setup: TradeSetup, config: BotConfig, equity: float = 0.0) -> int:
    """Calculate number of contracts based on risk config."""
    sl_distance = abs(setup.entry_price - setup.stop_loss)
    risk_per_contract = sl_distance * MNQ_POINT_VALUE

    if risk_per_contract <= 0:
        return 1

    if config.size_mode == "Fixed contracts":
        qty = config.fixed_contracts
    elif config.size_mode == "Fixed $":
        qty = max(1, math.floor(config.risk_dollars / risk_per_contract))
    else:  # "Risk %"
        if equity <= 0:
            return 1
        risk_budget = equity * config.risk_pct / 100.0
        qty = max(1, math.floor(risk_budget / risk_per_contract))

    return min(qty, config.max_contracts)

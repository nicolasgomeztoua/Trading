from __future__ import annotations

import math

from .config import BotConfig
from .models import TradeSetup

# NQ futures: $20 per point, $5 per tick (tick = 0.25)
NQ_POINT_VALUE = 20.0


def calculate_contracts(setup: TradeSetup, config: BotConfig, equity: float = 0.0) -> int:
    """Calculate number of contracts based on risk config."""
    sl_distance = abs(setup.entry_price - setup.stop_loss)
    risk_per_contract = sl_distance * NQ_POINT_VALUE

    if risk_per_contract <= 0:
        return 1

    if config.size_mode == "Fixed contracts":
        return config.fixed_contracts
    elif config.size_mode == "Fixed $":
        return max(1, math.floor(config.risk_dollars / risk_per_contract))
    else:  # "Risk %"
        if equity <= 0:
            return 1
        risk_budget = equity * config.risk_pct / 100.0
        return max(1, math.floor(risk_budget / risk_per_contract))

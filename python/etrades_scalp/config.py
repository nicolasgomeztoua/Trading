from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class BotConfig:
    # Tradovate credentials
    tradovate_username: str = ""
    tradovate_password: str = ""
    tradovate_app_id: str = ""
    tradovate_app_version: str = "1.0"
    tradovate_cid: int = 0
    tradovate_secret: str = ""
    use_demo: bool = True

    # Trading parameters
    symbol: str = "MNQ"
    max_contracts: int = 60
    account_id: int = 0
    risk_dollars: float = 250.0
    risk_pct: float = 1.0
    size_mode: str = "Fixed $"  # "Risk %", "Fixed $", "Fixed contracts"
    fixed_contracts: int = 1
    tp_r_multiple: float = 0.2
    reversal_lookback: int = 30

    # Session (NY time)
    session_start: time = field(default_factory=lambda: time(9, 30))
    session_end: time = field(default_factory=lambda: time(12, 0))

    # Setup toggles
    enable_setup_1: bool = True
    enable_setup_2: bool = True
    enable_setup_3: bool = True
    enable_setup_4: bool = True

    # Blocked dates (MMDD format)
    blocked_dates: list[str] = field(default_factory=lambda: ["1225", "0101", "0704"])

    @classmethod
    def from_env(cls, env_path: str | Path | None = None) -> BotConfig:
        if env_path:
            load_dotenv(env_path)
        else:
            load_dotenv()

        return cls(
            tradovate_username=os.getenv("TRADOVATE_USERNAME", ""),
            tradovate_password=os.getenv("TRADOVATE_PASSWORD", ""),
            tradovate_app_id=os.getenv("TRADOVATE_APP_ID", ""),
            tradovate_app_version=os.getenv("TRADOVATE_APP_VERSION", "1.0"),
            tradovate_cid=int(os.getenv("TRADOVATE_CID", "0")),
            tradovate_secret=os.getenv("TRADOVATE_SECRET", ""),
            use_demo=os.getenv("TRADOVATE_USE_DEMO", "true").lower() == "true",
            symbol=os.getenv("SYMBOL", "NQ"),
            risk_dollars=float(os.getenv("RISK_DOLLARS", "250.0")),
            risk_pct=float(os.getenv("RISK_PCT", "1.0")),
            size_mode=os.getenv("SIZE_MODE", "Fixed $"),
            fixed_contracts=int(os.getenv("FIXED_CONTRACTS", "1")),
            tp_r_multiple=float(os.getenv("TP_R_MULTIPLE", "0.2")),
            reversal_lookback=int(os.getenv("REVERSAL_LOOKBACK", "30")),
        )

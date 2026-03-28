#!/usr/bin/env python3
"""ETrades Scalp Model — Tradovate Bot entry point."""

import asyncio
import logging
import signal
import sys
from pathlib import Path

# Add parent to path so we can import the package
sys.path.insert(0, str(Path(__file__).parent))

from etrades_scalp.bot import ETradesScalpBot
from etrades_scalp.config import BotConfig


def setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("etrades_scalp.log"),
        ],
    )


async def main() -> None:
    setup_logging()
    logger = logging.getLogger("etrades_scalp")

    # Load config from .env
    env_path = Path(__file__).parent / ".env"
    config = BotConfig.from_env(env_path if env_path.exists() else None)

    logger.info("=" * 60)
    logger.info("ETrades Scalp Model — Tradovate Bot")
    logger.info("=" * 60)
    logger.info("Symbol: %s", config.symbol)
    logger.info("Mode: %s", "DEMO" if config.use_demo else "LIVE")
    logger.info("Risk: %s", config.size_mode)
    logger.info("TP Multiple: %.2fR", config.tp_r_multiple)
    logger.info("Session: %s - %s NY", config.session_start, config.session_end)
    logger.info("Setups enabled: S1=%s S2=%s S3=%s S4=%s",
                config.enable_setup_1, config.enable_setup_2,
                config.enable_setup_3, config.enable_setup_4)
    logger.info("=" * 60)

    bot = ETradesScalpBot(config)

    # Graceful shutdown on SIGINT/SIGTERM
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop()))

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
ETrades Scalp Model — TradingView Webhook → Tradovate Bridge

Receives webhook alerts from TradingView and places orders on Tradovate.
Run locally with ngrok for a public URL.

Usage:
    1. Copy .env.example to .env, fill in Tradovate credentials
    2. pip install aiohttp python-dotenv
    3. python webhook_server.py
    4. In another terminal: ngrok http 8765
    5. Copy the ngrok URL into your TradingView alert webhook URL
"""

import asyncio
import json
import logging
import os
import math
from datetime import datetime
from aiohttp import web, ClientSession
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("webhook")

# ─── Config ──────────────────────────────────────────────────────────────────

TRADOVATE_USER = os.getenv("TRADOVATE_USERNAME", "")
TRADOVATE_PASS = os.getenv("TRADOVATE_PASSWORD", "")
TRADOVATE_APP_ID = os.getenv("TRADOVATE_APP_ID", "ETradesScalp")
TRADOVATE_APP_VERSION = os.getenv("TRADOVATE_APP_VERSION", "1.0")
TRADOVATE_CID = int(os.getenv("TRADOVATE_CID", "0"))
TRADOVATE_SECRET = os.getenv("TRADOVATE_SECRET", "")
USE_DEMO = os.getenv("TRADOVATE_USE_DEMO", "true").lower() == "true"

BASE_URL = "https://demo.tradovateapi.com/v1" if USE_DEMO else "https://live.tradovateapi.com/v1"
RISK_DOLLARS = float(os.getenv("RISK_DOLLARS", "2000"))
MAX_CONTRACTS = int(os.getenv("MAX_CONTRACTS", "60"))
MNQ_POINT_VALUE = 2.0

PORT = int(os.getenv("WEBHOOK_PORT", "8765"))
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # optional: validate incoming webhooks

# ─── Tradovate Client ────────────────────────────────────────────────────────

class TradovateClient:
    def __init__(self):
        self.session: ClientSession | None = None
        self.access_token: str | None = None
        self.account_id: int | None = None
        self.account_spec: str | None = None

    async def ensure_session(self):
        if self.session is None or self.session.closed:
            self.session = ClientSession()

    def headers(self):
        h = {"Content-Type": "application/json"}
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    async def authenticate(self):
        await self.ensure_session()
        payload = {
            "name": TRADOVATE_USER,
            "password": TRADOVATE_PASS,
            "appId": TRADOVATE_APP_ID,
            "appVersion": TRADOVATE_APP_VERSION,
            "cid": TRADOVATE_CID,
            "sec": TRADOVATE_SECRET,
        }
        async with self.session.post(
            f"{BASE_URL}/auth/accesstokenrequest",
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            data = await resp.json()
            if "accessToken" in data:
                self.access_token = data["accessToken"]
                log.info("Authenticated with Tradovate (%s)", "DEMO" if USE_DEMO else "LIVE")
            else:
                log.error("Auth failed: %s", data)
                raise Exception(f"Tradovate auth failed: {data}")

    async def get_account(self):
        await self.ensure_session()
        async with self.session.get(
            f"{BASE_URL}/account/list", headers=self.headers()
        ) as resp:
            accounts = await resp.json()
            if accounts:
                self.account_id = accounts[0]["id"]
                self.account_spec = accounts[0].get("name", str(self.account_id))
                log.info("Using account: %s (id=%d)", self.account_spec, self.account_id)
            else:
                raise Exception("No accounts found")

    async def place_bracket_order(self, action: str, symbol: str, qty: int, sl_price: float, tp_price: float):
        """Place market entry with bracket SL/TP."""
        await self.ensure_session()

        # Use placeOrder for entry, then placeOSO for bracket
        # Actually, use the simpler approach: place market order + two exit orders

        exit_action = "Sell" if action == "Buy" else "Buy"

        # Entry order (market)
        entry_payload = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "timeInForce": "Day",
            "isAutomated": True,
        }

        log.info("Placing %s %d %s @ Market", action, qty, symbol)
        async with self.session.post(
            f"{BASE_URL}/order/placeorder",
            json=entry_payload,
            headers=self.headers(),
        ) as resp:
            entry_result = await resp.json()
            log.info("Entry result: %s", entry_result)

        # Stop loss order
        sl_payload = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": exit_action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Stop",
            "stopPrice": sl_price,
            "timeInForce": "GTC",
            "isAutomated": True,
        }

        log.info("Placing SL %s %d %s @ %.2f", exit_action, qty, symbol, sl_price)
        async with self.session.post(
            f"{BASE_URL}/order/placeorder",
            json=sl_payload,
            headers=self.headers(),
        ) as resp:
            sl_result = await resp.json()
            log.info("SL result: %s", sl_result)

        # Take profit order
        tp_payload = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": exit_action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Limit",
            "price": tp_price,
            "timeInForce": "GTC",
            "isAutomated": True,
        }

        log.info("Placing TP %s %d %s @ %.2f", exit_action, qty, symbol, tp_price)
        async with self.session.post(
            f"{BASE_URL}/order/placeorder",
            json=tp_payload,
            headers=self.headers(),
        ) as resp:
            tp_result = await resp.json()
            log.info("TP result: %s", tp_result)

        return entry_result

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


# ─── Webhook Handler ─────────────────────────────────────────────────────────

client = TradovateClient()


async def handle_webhook(request: web.Request):
    """Handle incoming TradingView webhook alert."""
    try:
        body = await request.text()
        log.info("Webhook received: %s", body)

        # Parse JSON payload from TradingView alert
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.warning("Invalid JSON: %s", body)
            return web.json_response({"error": "invalid json"}, status=400)

        # Validate webhook secret if configured
        if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
            log.warning("Invalid webhook secret")
            return web.json_response({"error": "unauthorized"}, status=401)

        # Extract trade parameters
        action_str = data.get("action", "").lower()  # "buy" or "sell"
        symbol = data.get("symbol", "MNQM6")
        entry = float(data.get("entry", 0))
        sl = float(data.get("sl", 0))
        tp = float(data.get("tp", 0))
        setup = data.get("setup", "unknown")

        if action_str not in ("buy", "sell"):
            log.warning("Invalid action: %s", action_str)
            return web.json_response({"error": "invalid action"}, status=400)

        if entry == 0 or sl == 0 or tp == 0:
            log.warning("Missing entry/sl/tp")
            return web.json_response({"error": "missing prices"}, status=400)

        action = "Buy" if action_str == "buy" else "Sell"

        # Calculate position size
        sl_dist = abs(entry - sl)
        risk_per_contract = sl_dist * MNQ_POINT_VALUE
        if risk_per_contract > 0:
            qty = min(MAX_CONTRACTS, max(1, math.floor(RISK_DOLLARS / risk_per_contract)))
        else:
            qty = 1

        actual_risk = qty * risk_per_contract

        log.info(
            "TRADE: %s | %s %d MNQ @ %.2f | SL=%.2f TP=%.2f | Risk=$%.2f",
            setup, action, qty, entry, sl, tp, actual_risk,
        )

        # Place the order
        result = await client.place_bracket_order(action, symbol, qty, sl, tp)

        return web.json_response({
            "status": "ok",
            "setup": setup,
            "action": action,
            "qty": qty,
            "entry": entry,
            "sl": sl,
            "tp": tp,
            "risk": actual_risk,
        })

    except Exception as e:
        log.exception("Webhook handler error")
        return web.json_response({"error": str(e)}, status=500)


async def handle_health(request: web.Request):
    return web.json_response({
        "status": "ok",
        "authenticated": client.access_token is not None,
        "account": client.account_spec,
        "mode": "DEMO" if USE_DEMO else "LIVE",
        "time": datetime.now().isoformat(),
    })


# ─── Server ──────────────────────────────────────────────────────────────────

async def on_startup(app):
    log.info("Authenticating with Tradovate...")
    await client.authenticate()
    await client.get_account()
    log.info("Ready. Waiting for webhooks on port %d", PORT)


async def on_cleanup(app):
    await client.close()


def main():
    app = web.Application()
    app.router.add_post("/webhook", handle_webhook)
    app.router.add_get("/health", handle_health)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    log.info("=" * 50)
    log.info("ETrades Scalp — Webhook Server")
    log.info("=" * 50)
    log.info("Mode: %s", "DEMO" if USE_DEMO else "LIVE")
    log.info("Risk: $%.0f per trade", RISK_DOLLARS)
    log.info("Max contracts: %d MNQ", MAX_CONTRACTS)
    log.info("Port: %d", PORT)
    log.info("=" * 50)

    web.run_app(app, port=PORT)


if __name__ == "__main__":
    main()

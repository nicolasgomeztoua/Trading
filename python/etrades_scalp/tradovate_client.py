from __future__ import annotations

import json
import logging
import time as time_mod
from typing import Any, Callable

import aiohttp

logger = logging.getLogger(__name__)


class TradovateClient:
    """Async Tradovate REST + WebSocket client."""

    DEMO_BASE = "https://demo.tradovateapi.com/v1"
    LIVE_BASE = "https://live.tradovateapi.com/v1"
    DEMO_WS = "wss://md.tradovateapi.com/v1/websocket"
    LIVE_WS = "wss://md.tradovateapi.com/v1/websocket"

    def __init__(self, use_demo: bool = True):
        self.base_url = self.DEMO_BASE if use_demo else self.LIVE_BASE
        self.ws_url = self.DEMO_WS if use_demo else self.LIVE_WS
        self.access_token: str | None = None
        self.session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        return h

    # ─── Authentication ──────────────────────────────────────────────────

    async def authenticate(
        self,
        username: str,
        password: str,
        app_id: str,
        app_version: str,
        cid: int,
        secret: str,
    ) -> dict[str, Any]:
        """POST /auth/accesstokenrequest"""
        session = await self._ensure_session()
        payload = {
            "name": username,
            "password": password,
            "appId": app_id,
            "appVersion": app_version,
            "cid": cid,
            "sec": secret,
        }
        async with session.post(
            f"{self.base_url}/auth/accesstokenrequest",
            json=payload,
            headers={"Content-Type": "application/json"},
        ) as resp:
            data = await resp.json()
            if "accessToken" in data:
                self.access_token = data["accessToken"]
                logger.info("Authenticated successfully")
            else:
                logger.error("Auth failed: %s", data)
            return data

    # ─── Account ─────────────────────────────────────────────────────────

    async def get_accounts(self) -> list[dict[str, Any]]:
        """GET /account/list"""
        session = await self._ensure_session()
        async with session.get(
            f"{self.base_url}/account/list", headers=self._headers()
        ) as resp:
            return await resp.json()

    async def get_cash_balance(self, account_id: int) -> dict[str, Any]:
        """GET /cashBalance/getCashBalanceSnapshot?accountId=..."""
        session = await self._ensure_session()
        async with session.get(
            f"{self.base_url}/cashBalance/getCashBalanceSnapshot",
            params={"accountId": account_id},
            headers=self._headers(),
        ) as resp:
            return await resp.json()

    # ─── Orders ──────────────────────────────────────────────────────────

    async def place_order(
        self,
        account_id: int,
        action: str,  # "Buy" or "Sell"
        symbol: str,
        order_type: str,  # "Market", "Limit", "Stop", "StopLimit"
        qty: int,
        price: float | None = None,
        stop_price: float | None = None,
    ) -> dict[str, Any]:
        """POST /order/placeorder"""
        session = await self._ensure_session()
        payload: dict[str, Any] = {
            "accountSpec": str(account_id),
            "accountId": account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": order_type,
            "isAutomated": True,
        }
        if price is not None:
            payload["price"] = price
        if stop_price is not None:
            payload["stopPrice"] = stop_price

        async with session.post(
            f"{self.base_url}/order/placeorder",
            json=payload,
            headers=self._headers(),
        ) as resp:
            data = await resp.json()
            logger.info("Order placed: %s", data)
            return data

    async def place_oso(
        self,
        account_id: int,
        action: str,
        symbol: str,
        qty: int,
        sl_price: float,
        tp_price: float,
    ) -> dict[str, Any]:
        """Place bracket order: market entry + stop loss + take profit (OSO)."""
        session = await self._ensure_session()

        # Entry order (market)
        entry_order = {
            "accountSpec": str(account_id),
            "accountId": account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
        }

        # Opposite action for exits
        exit_action = "Sell" if action == "Buy" else "Buy"

        # Stop loss
        sl_order = {
            "accountSpec": str(account_id),
            "accountId": account_id,
            "action": exit_action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Stop",
            "stopPrice": sl_price,
            "isAutomated": True,
        }

        # Take profit
        tp_order = {
            "accountSpec": str(account_id),
            "accountId": account_id,
            "action": exit_action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Limit",
            "price": tp_price,
            "isAutomated": True,
        }

        payload = {
            "accountSpec": str(account_id),
            "accountId": account_id,
            "action": action,
            "symbol": symbol,
            "orderQty": qty,
            "orderType": "Market",
            "isAutomated": True,
            "bracket1": sl_order,
            "bracket2": tp_order,
        }

        async with session.post(
            f"{self.base_url}/order/placeoso",
            json=payload,
            headers=self._headers(),
        ) as resp:
            data = await resp.json()
            logger.info("OSO bracket placed: %s", data)
            return data

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        """POST /order/cancelorder"""
        session = await self._ensure_session()
        async with session.post(
            f"{self.base_url}/order/cancelorder",
            json={"orderId": order_id},
            headers=self._headers(),
        ) as resp:
            return await resp.json()

    # ─── Positions ───────────────────────────────────────────────────────

    async def get_positions(self, account_id: int) -> list[dict[str, Any]]:
        """GET /position/list"""
        session = await self._ensure_session()
        async with session.get(
            f"{self.base_url}/position/list",
            headers=self._headers(),
        ) as resp:
            return await resp.json()

    # ─── WebSocket (market data) ─────────────────────────────────────────

    async def connect_ws(self) -> None:
        """Open WebSocket connection for market data."""
        session = await self._ensure_session()
        self._ws = await session.ws_connect(self.ws_url)
        # Authenticate on WS
        auth_msg = f"authorize\n0\n\n{self.access_token}"
        await self._ws.send_str(auth_msg)
        logger.info("WebSocket connected and authenticated")

    async def subscribe_chart(
        self,
        symbol: str,
        timeframe: int = 1,  # minutes
        callback: Callable | None = None,
    ) -> None:
        """Subscribe to chart data (1-min bars)."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")

        sub_msg = json.dumps({
            "symbol": symbol,
            "chartDescription": {
                "underlyingType": "MinuteBar",
                "elementSize": timeframe,
                "elementSizeUnit": "UnderlyingUnits",
                "withHistogram": False,
            },
            "timeRange": {
                "asMuchAsElements": 10,
            },
        })
        req = f"md/subscribechart\n1\n\n{sub_msg}"
        await self._ws.send_str(req)
        logger.info("Subscribed to %s %dmin chart", symbol, timeframe)

        if callback:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    callback(msg.data)
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("WebSocket closed/error: %s", msg)
                    break

    # ─── Cleanup ─────────────────────────────────────────────────────────

    async def close(self) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self.session and not self.session.closed:
            await self.session.close()

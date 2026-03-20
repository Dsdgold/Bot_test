"""
MEXC Futures API client.
Handles all communication with MEXC exchange.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from .config import MEXCConfig
from .models import Candle, Ticker, OrderStatus, Side

logger = logging.getLogger("mexc_client")


class MEXCClient:
    """MEXC Futures API client."""

    def __init__(self, config: MEXCConfig):
        self.config = config
        self.base_url = config.base_url
        self.client = httpx.AsyncClient(timeout=10.0)

    def _sign(self, params: str) -> str:
        """Create HMAC SHA256 signature."""
        return hmac.new(
            self.config.api_secret.encode("utf-8"),
            params.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _headers(self, timestamp: str, sign: str) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "ApiKey": self.config.api_key,
            "Request-Time": timestamp,
            "Signature": sign,
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        signed: bool = False,
    ) -> Any:
        """Make API request."""
        url = f"{self.base_url}{path}"
        timestamp = str(int(time.time() * 1000))

        if signed:
            param_str = ""
            if params and method == "POST":
                param_str = json.dumps(params, separators=(",", ":"))
            # For GET requests, params in query string are NOT included in signature
            sign_str = f"{self.config.api_key}{timestamp}{param_str}"
            signature = self._sign(sign_str)
            headers = self._headers(timestamp, signature)
        else:
            headers = {"Content-Type": "application/json"}

        try:
            logger.debug(f"API {method} {path} params={params} signed={signed}")
            if method == "GET":
                resp = await self.client.get(url, params=params, headers=headers)
            else:
                # Send raw JSON string to ensure signature matches body exactly
                body = json.dumps(params, separators=(",", ":")) if params else ""
                resp = await self.client.post(url, content=body, headers=headers)

            logger.debug(f"API response status={resp.status_code}")
            data = resp.json()
            if data.get("success") is False:
                logger.error(f"MEXC API error on {path}: code={data.get('code')} msg={data.get('message', data)}")
            return data
        except Exception as e:
            logger.error(f"Request failed {method} {path}: {e}")
            return {"success": False, "message": str(e)}

    # ── Market Data ──────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> Optional[Ticker]:
        """Get current ticker for symbol."""
        data = await self._request("GET", f"/api/v1/contract/ticker?symbol={symbol}")
        if not data.get("success") or not data.get("data"):
            return None

        t = data["data"]
        return Ticker(
            symbol=symbol,
            last_price=float(t.get("lastPrice", 0)),
            bid=float(t.get("bid1", 0)),
            ask=float(t.get("ask1", 0)),
            volume_24h=float(t.get("volume24", 0)),
            change_24h=float(t.get("riseFallRate", 0)),
            high_24h=float(t.get("high24Price", 0)),
            low_24h=float(t.get("low24Price", 0)),
            timestamp=int(t.get("timestamp", 0)),
        )

    async def get_klines(
        self, symbol: str, interval: str = "Min1", limit: int = 200
    ) -> List[Candle]:
        """Get candlestick data."""
        # MEXC futures kline endpoint: symbol in path, params in query
        data = await self._request(
            "GET", f"/api/v1/contract/kline/{symbol}",
            params={"interval": interval, "limit": limit}
        )

        if not data.get("success") or not data.get("data"):
            # Fallback: try index price kline
            data = await self._request(
                "GET", "/api/v1/contract/kline/index_price",
                params={"symbol": symbol, "interval": interval, "limit": limit}
            )

        candles = []
        if data.get("success") and data.get("data"):
            raw = data["data"]
            # Handle different response formats
            if isinstance(raw, dict) and "time" in raw:
                # Single dict with arrays: {time: [...], open: [...], ...}
                times = raw.get("time", [])
                opens = raw.get("open", [])
                highs = raw.get("high", [])
                lows = raw.get("low", [])
                closes = raw.get("close", [])
                vols = raw.get("vol", [])
                for i in range(len(times)):
                    candles.append(
                        Candle(
                            timestamp=int(times[i]),
                            open=float(opens[i]),
                            high=float(highs[i]),
                            low=float(lows[i]),
                            close=float(closes[i]),
                            volume=float(vols[i]) if i < len(vols) else 0,
                        )
                    )
            elif isinstance(raw, list):
                for k in raw:
                    if isinstance(k, dict):
                        # List of dicts: [{time: ..., open: ...}, ...]
                        candles.append(
                            Candle(
                                timestamp=int(k.get("time", 0)),
                                open=float(k.get("open", 0)),
                                high=float(k.get("high", 0)),
                                low=float(k.get("low", 0)),
                                close=float(k.get("close", 0)),
                                volume=float(k.get("vol", 0)),
                            )
                        )
                    elif isinstance(k, (list, tuple)) and len(k) >= 6:
                        # List of arrays: [[time, open, close, high, low, vol], ...]
                        candles.append(
                            Candle(
                                timestamp=int(k[0]),
                                open=float(k[1]),
                                high=float(k[3]),
                                low=float(k[4]),
                                close=float(k[2]),
                                volume=float(k[5]),
                            )
                        )
            if candles:
                logger.debug(f"Parsed {len(candles)} candles, latest close={candles[-1].close}")
            else:
                logger.warning(f"Could not parse kline data format: {type(raw)}, sample={str(raw)[:200]}")
        return candles

    async def get_depth(self, symbol: str, limit: int = 20) -> Dict:
        """Get order book depth."""
        data = await self._request(
            "GET", f"/api/v1/contract/depth/{symbol}",
            params={"limit": limit}
        )
        return data.get("data", {})

    async def get_funding_rate(self, symbol: str) -> Dict:
        """Get current funding rate for symbol."""
        data = await self._request(
            "GET", f"/api/v1/contract/funding_rate/{symbol}"
        )
        if data.get("success") and data.get("data"):
            return data["data"]
        return {}

    async def get_open_interest(self, symbol: str) -> Dict:
        """Get open interest for symbol."""
        # Try different endpoint formats
        data = await self._request(
            "GET", "/api/v1/contract/open_interest",
            params={"symbol": symbol}
        )
        if data.get("success") and data.get("data"):
            return data["data"]
        # Fallback: try ticker which includes OI
        data = await self._request(
            "GET", f"/api/v1/contract/ticker?symbol={symbol}"
        )
        if data.get("success") and data.get("data"):
            t = data["data"]
            return {"openInterest": t.get("holdVol", 0)}
        return {}

    async def get_klines_multi(self, symbol: str, intervals: List[str], limit: int = 100) -> Dict[str, List[Candle]]:
        """Get klines for multiple timeframes."""
        result = {}
        for interval in intervals:
            candles = await self.get_klines(symbol, interval, limit)
            if candles:
                result[interval] = candles
        return result

    # ── Account ──────────────────────────────────────────────────

    async def get_account_info(self) -> Any:
        """Get futures account information."""
        data = await self._request("GET", "/api/v1/private/account/assets", signed=True)
        if not data.get("success"):
            logger.error(f"Failed to get account info: {data}")
            return []
        result = data.get("data", [])
        logger.debug(f"Account info response type: {type(result)}, len: {len(result) if isinstance(result, list) else 'N/A'}")
        return result

    async def get_balance(self) -> float:
        """Get USDT balance."""
        info = await self.get_account_info()
        if isinstance(info, list):
            for asset in info:
                if asset.get("currency") == "USDT":
                    balance = float(asset.get("availableBalance", 0))
                    logger.debug(f"Found USDT balance: {balance}")
                    return balance
            logger.warning("USDT not found in assets list")
        elif isinstance(info, dict):
            # Handle case where data is a single object
            if info.get("currency") == "USDT":
                balance = float(info.get("availableBalance", 0))
                logger.info(f"Found USDT balance (dict): {balance}")
                return balance
            # Maybe it's a nested structure with equity/availableBalance at top level
            if "availableBalance" in info:
                balance = float(info.get("availableBalance", 0))
                logger.info(f"Found balance from top-level: {balance}")
                return balance
            if "equity" in info:
                balance = float(info.get("equity", 0))
                logger.info(f"Found equity balance: {balance}")
                return balance
            logger.warning(f"Unexpected account info structure: {info}")
        else:
            logger.warning(f"Unexpected account info type: {type(info)} = {info}")
        return 0.0

    # ── Trading ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol."""
        params = {"symbol": symbol, "leverage": leverage}
        data = await self._request(
            "POST", "/api/v1/private/position/change_leverage", params, signed=True
        )
        return data.get("success", False)

    async def open_position(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        leverage: int,
        price: Optional[float] = None,
    ) -> Optional[str]:
        """Open a futures position. Returns order ID."""
        # Side: 1=open long, 2=close short, 3=open short, 4=close long
        open_type = 1 if side == Side.LONG else 3
        order_type = 5  # Market order
        if price:
            order_type = 1  # Limit order

        params = {
            "symbol": symbol,
            "price": price or 0,
            "vol": quantity,
            "side": open_type,
            "type": order_type,
            "openType": 2,  # Cross margin
            "leverage": leverage,
        }

        data = await self._request(
            "POST", "/api/v1/private/order/submit", params, signed=True
        )

        if data.get("success") and data.get("data"):
            order_id = str(data["data"])
            logger.info(f"Order placed: {order_id} ({side.value} {quantity} {symbol})")
            return order_id

        logger.error(f"Failed to open position: {data}")
        return None

    async def close_position(
        self,
        symbol: str,
        side: Side,
        quantity: float,
    ) -> Optional[str]:
        """Close a futures position. Returns order ID."""
        # 2=close short (close a long), 4=close long (close a short)
        close_type = 4 if side == Side.LONG else 2

        params = {
            "symbol": symbol,
            "price": 0,
            "vol": quantity,
            "side": close_type,
            "type": 5,  # Market order
            "openType": 2,
        }

        data = await self._request(
            "POST", "/api/v1/private/order/submit", params, signed=True
        )

        if data.get("success") and data.get("data"):
            order_id = str(data["data"])
            logger.info(f"Close order placed: {order_id}")
            return order_id

        logger.error(f"Failed to close position: {data}")
        return None

    async def get_open_positions(self, symbol: str) -> List[Dict]:
        """Get open positions for symbol."""
        data = await self._request(
            "GET",
            "/api/v1/private/position/open_positions",
            params={"symbol": symbol},
            signed=True,
        )
        if data.get("success") and data.get("data"):
            return data["data"]
        return []

    async def set_stop_loss_take_profit(
        self,
        symbol: str,
        position_id: str,
        stop_loss: float,
        take_profit: float,
    ) -> bool:
        """Set stop-loss and take-profit for a position."""
        params = {
            "symbol": symbol,
            "positionId": position_id,
            "stopLossPrice": stop_loss,
            "takeProfitPrice": take_profit,
        }
        data = await self._request(
            "POST",
            "/api/v1/private/position/change_margin",
            params,
            signed=True,
        )
        return data.get("success", False)

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for symbol."""
        data = await self._request(
            "POST",
            "/api/v1/private/order/cancel_all",
            {"symbol": symbol},
            signed=True,
        )
        return data.get("success", False)

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

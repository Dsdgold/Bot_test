"""
Bybit V5 Unified API client.
Handles all communication with Bybit exchange for USDT perpetual futures.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from .config import BybitConfig
from .models import Candle, Ticker, OrderStatus, Side

logger = logging.getLogger("bybit_client")

# Bybit kline interval mapping
INTERVAL_MAP = {
    "Min1": "1",
    "Min3": "3",
    "Min5": "5",
    "Min15": "15",
    "Min30": "30",
    "Min60": "60",
    "Hour4": "240",
    "Day1": "D",
    "Week1": "W",
    # Also accept raw Bybit intervals
    "1": "1",
    "3": "3",
    "5": "5",
    "15": "15",
    "60": "60",
    "240": "240",
    "D": "D",
}


class BybitClient:
    """Bybit V5 Unified API client for USDT perpetual futures."""

    def __init__(self, config: BybitConfig):
        self.config = config
        self.base_url = config.base_url
        self.recv_window = "5000"
        self.client = httpx.AsyncClient(timeout=10.0)

    def _sign(self, timestamp: str, params_str: str) -> str:
        """Create HMAC SHA256 signature for Bybit V5."""
        sign_payload = f"{timestamp}{self.config.api_key}{self.recv_window}{params_str}"
        return hmac.new(
            self.config.api_secret.encode("utf-8"),
            sign_payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, timestamp: str, signature: str) -> Dict[str, str]:
        return {
            "X-BAPI-API-KEY": self.config.api_key,
            "X-BAPI-SIGN": signature,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": self.recv_window,
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict] = None,
        signed: bool = False,
    ) -> Any:
        """Make API request to Bybit V5."""
        url = f"{self.base_url}{path}"
        timestamp = str(int(time.time() * 1000))

        if signed:
            if method == "GET":
                query_str = urlencode(params) if params else ""
                signature = self._sign(timestamp, query_str)
            else:
                body_str = json.dumps(params, separators=(",", ":")) if params else ""
                signature = self._sign(timestamp, body_str)
            headers = self._auth_headers(timestamp, signature)
        else:
            headers = {"Content-Type": "application/json"}

        try:
            logger.debug(f"API {method} {path} params={params} signed={signed}")
            if method == "GET":
                resp = await self.client.get(url, params=params, headers=headers)
            else:
                body = json.dumps(params, separators=(",", ":")) if params else ""
                resp = await self.client.post(url, content=body, headers=headers)

            logger.debug(f"API response status={resp.status_code}")

            try:
                data = resp.json()
            except Exception:
                logger.error(f"Non-JSON response from {path}: status={resp.status_code} body={resp.text[:200]}")
                return {"retCode": -1, "retMsg": f"Non-JSON response: {resp.status_code}"}

            ret_code = data.get("retCode", -1)
            if ret_code != 0:
                logger.error(f"Bybit API error on {path}: code={ret_code} msg={data.get('retMsg', '')}")

            return data

        except Exception as e:
            logger.error(f"Request failed {method} {path}: {e}")
            return {"retCode": -1, "retMsg": str(e)}

    def _ok(self, data: dict) -> bool:
        """Check if Bybit response is successful."""
        return data.get("retCode") == 0

    # ── Market Data ──────────────────────────────────────────────

    async def get_ticker(self, symbol: str) -> Optional[Ticker]:
        """Get current ticker for symbol."""
        data = await self._request("GET", "/v5/market/tickers", {
            "category": "linear",
            "symbol": symbol,
        })
        if not self._ok(data):
            return None

        items = data.get("result", {}).get("list", [])
        if not items:
            return None

        t = items[0]
        return Ticker(
            symbol=symbol,
            last_price=float(t.get("lastPrice", 0)),
            bid=float(t.get("bid1Price", 0)),
            ask=float(t.get("ask1Price", 0)),
            volume_24h=float(t.get("volume24h", 0)),
            change_24h=float(t.get("price24hPcnt", 0)),
            high_24h=float(t.get("highPrice24h", 0)),
            low_24h=float(t.get("lowPrice24h", 0)),
            timestamp=int(time.time() * 1000),
        )

    async def get_klines(
        self, symbol: str, interval: str = "Min1", limit: int = 200
    ) -> List[Candle]:
        """Get candlestick data."""
        bybit_interval = INTERVAL_MAP.get(interval, "1")

        data = await self._request("GET", "/v5/market/kline", {
            "category": "linear",
            "symbol": symbol,
            "interval": bybit_interval,
            "limit": min(limit, 1000),
        })

        candles = []
        if self._ok(data):
            items = data.get("result", {}).get("list", [])
            # Bybit returns: [startTime, openPrice, highPrice, lowPrice, closePrice, volume, turnover]
            # Sorted newest first, so we reverse
            for k in reversed(items):
                if isinstance(k, list) and len(k) >= 6:
                    candles.append(
                        Candle(
                            timestamp=int(k[0]),
                            open=float(k[1]),
                            high=float(k[2]),
                            low=float(k[3]),
                            close=float(k[4]),
                            volume=float(k[5]),
                        )
                    )

            if candles:
                logger.debug(f"Parsed {len(candles)} candles, latest close={candles[-1].close}")
            else:
                logger.warning(f"No candles parsed from Bybit response")

        return candles

    async def get_depth(self, symbol: str, limit: int = 25) -> Dict:
        """Get order book depth."""
        data = await self._request("GET", "/v5/market/orderbook", {
            "category": "linear",
            "symbol": symbol,
            "limit": limit,
        })
        if self._ok(data):
            result = data.get("result", {})
            return {
                "bids": result.get("b", []),  # [[price, size], ...]
                "asks": result.get("a", []),
            }
        return {}

    async def get_funding_rate(self, symbol: str) -> Dict:
        """Get current funding rate for symbol."""
        data = await self._request("GET", "/v5/market/funding/history", {
            "category": "linear",
            "symbol": symbol,
            "limit": 1,
        })
        if self._ok(data):
            items = data.get("result", {}).get("list", [])
            if items:
                return {
                    "fundingRate": float(items[0].get("fundingRate", 0)),
                    "fundingRateTimestamp": items[0].get("fundingRateTimestamp", ""),
                }
        return {}

    async def get_open_interest(self, symbol: str) -> Dict:
        """Get open interest for symbol."""
        data = await self._request("GET", "/v5/market/open-interest", {
            "category": "linear",
            "symbol": symbol,
            "intervalTime": "5min",
            "limit": 1,
        })
        if self._ok(data):
            items = data.get("result", {}).get("list", [])
            if items:
                return {"openInterest": float(items[0].get("openInterest", 0))}
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
        """Get unified account wallet balance."""
        data = await self._request("GET", "/v5/account/wallet-balance", {
            "accountType": "UNIFIED",
        }, signed=True)

        if not self._ok(data):
            logger.error(f"Failed to get account info: {data.get('retMsg', '')}")
            return {}

        return data.get("result", {})

    async def get_balance(self) -> float:
        """Get USDT balance from unified account."""
        info = await self.get_account_info()
        accounts = info.get("list", [])
        for account in accounts:
            coins = account.get("coin", [])
            for coin in coins:
                if coin.get("coin") == "USDT":
                    # availableToWithdraw is the free balance
                    balance = float(coin.get("availableToWithdraw", 0))
                    if balance == 0:
                        # Fallback to walletBalance
                        balance = float(coin.get("walletBalance", 0))
                    logger.debug(f"Found USDT balance: {balance}")
                    return balance
        logger.warning("USDT not found in wallet")
        return 0.0

    # ── Trading ──────────────────────────────────────────────────

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage for a symbol."""
        data = await self._request("POST", "/v5/position/set-leverage", {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }, signed=True)

        if self._ok(data):
            logger.info(f"Leverage set to {leverage}x for {symbol}")
            return True

        # Error 110043 means leverage not modified (already set)
        if data.get("retCode") == 110043:
            logger.info(f"Leverage already at {leverage}x for {symbol}")
            return True

        logger.warning(f"Set leverage response: {data.get('retMsg', '')}")
        return False

    async def get_instrument_info(self, symbol: str) -> Optional[Dict]:
        """Get instrument info (lot size, min order qty, etc.)."""
        data = await self._request("GET", "/v5/market/instruments-info", {
            "category": "linear",
            "symbol": symbol,
        })
        if self._ok(data):
            items = data.get("result", {}).get("list", [])
            if items:
                info = items[0]
                lot_filter = info.get("lotSizeFilter", {})
                logger.info(
                    f"Instrument {symbol}: minQty={lot_filter.get('minOrderQty')} "
                    f"maxQty={lot_filter.get('maxOrderQty')} "
                    f"qtyStep={lot_filter.get('qtyStep')}"
                )
                return info
        return None

    async def open_position(
        self,
        symbol: str,
        side: Side,
        quantity: float,
        leverage: int,
        price: Optional[float] = None,
        use_limit: bool = False,
    ) -> Optional[str]:
        """Open a futures position. Quantity is in base asset (e.g. BTC)."""
        # Get instrument info to know qty precision
        instrument = await self.get_instrument_info(symbol)
        qty_step = 0.001  # Default for BTC
        min_qty = 0.001
        if instrument:
            lot_filter = instrument.get("lotSizeFilter", {})
            qty_step = float(lot_filter.get("qtyStep", 0.001))
            min_qty = float(lot_filter.get("minOrderQty", 0.001))

        # Round quantity to step size
        qty = max(min_qty, round(quantity / qty_step) * qty_step)
        # Format without trailing zeros
        qty_str = f"{qty:.{self._decimal_places(qty_step)}f}"

        order_side = "Buy" if side == Side.LONG else "Sell"

        params = {
            "category": "linear",
            "symbol": symbol,
            "side": order_side,
            "orderType": "Market",
            "qty": qty_str,
        }

        if use_limit and price and price > 0:
            tick_size = 0.1  # Default
            if instrument:
                price_filter = instrument.get("priceFilter", {})
                tick_size = float(price_filter.get("tickSize", 0.1))
            rounded_price = round(price / tick_size) * tick_size
            params["orderType"] = "Limit"
            params["price"] = f"{rounded_price:.{self._decimal_places(tick_size)}f}"
            params["timeInForce"] = "GTC"

        data = await self._request("POST", "/v5/order/create", params, signed=True)

        if self._ok(data):
            order_id = data.get("result", {}).get("orderId", "")
            logger.info(f"Order placed: {order_id} ({side.value} {qty_str} {symbol} {params['orderType']})")
            return order_id

        # Fallback to market if limit fails
        if use_limit and params["orderType"] == "Limit":
            logger.warning("Limit order failed, falling back to market order")
            params["orderType"] = "Market"
            params.pop("price", None)
            params.pop("timeInForce", None)
            data = await self._request("POST", "/v5/order/create", params, signed=True)
            if self._ok(data):
                order_id = data.get("result", {}).get("orderId", "")
                logger.info(f"Market fallback order placed: {order_id}")
                return order_id

        logger.error(f"Failed to open position: {data.get('retMsg', '')}")
        return None

    async def close_position(
        self,
        symbol: str,
        side: Side,
        quantity: float,
    ) -> Optional[str]:
        """Close a futures position with market order."""
        # Close = opposite side with reduceOnly
        close_side = "Sell" if side == Side.LONG else "Buy"

        instrument = await self.get_instrument_info(symbol)
        qty_step = 0.001
        if instrument:
            lot_filter = instrument.get("lotSizeFilter", {})
            qty_step = float(lot_filter.get("qtyStep", 0.001))

        qty = round(quantity / qty_step) * qty_step
        qty_str = f"{qty:.{self._decimal_places(qty_step)}f}"

        params = {
            "category": "linear",
            "symbol": symbol,
            "side": close_side,
            "orderType": "Market",
            "qty": qty_str,
            "reduceOnly": True,
        }

        data = await self._request("POST", "/v5/order/create", params, signed=True)

        if self._ok(data):
            order_id = data.get("result", {}).get("orderId", "")
            logger.info(f"Close order placed: {order_id}")
            return order_id

        logger.error(f"Failed to close position: {data.get('retMsg', '')}")
        return None

    async def close_position_partial(
        self,
        symbol: str,
        side: Side,
        quantity: float,
    ) -> Optional[str]:
        """Partially close a position."""
        return await self.close_position(symbol, side, quantity)

    async def get_open_positions(self, symbol: str) -> List[Dict]:
        """Get open positions for symbol."""
        data = await self._request("GET", "/v5/position/list", {
            "category": "linear",
            "symbol": symbol,
        }, signed=True)

        if self._ok(data):
            positions = data.get("result", {}).get("list", [])
            # Filter out zero-size positions
            return [p for p in positions if float(p.get("size", 0)) > 0]
        return []

    async def set_stop_loss_take_profit(
        self,
        symbol: str,
        position_id: str,
        stop_loss: float,
        take_profit: float,
    ) -> bool:
        """Set stop-loss and take-profit for a position via trading-stop."""
        params = {
            "category": "linear",
            "symbol": symbol,
            "stopLoss": str(round(stop_loss, 2)),
            "takeProfit": str(round(take_profit, 2)),
            "positionIdx": 0,  # One-way mode
        }
        data = await self._request("POST", "/v5/position/trading-stop", params, signed=True)
        return self._ok(data)

    async def cancel_all_orders(self, symbol: str) -> bool:
        """Cancel all open orders for symbol."""
        data = await self._request("POST", "/v5/order/cancel-all", {
            "category": "linear",
            "symbol": symbol,
        }, signed=True)
        return self._ok(data)

    @staticmethod
    def _decimal_places(step: float) -> int:
        """Get number of decimal places from step size."""
        s = f"{step:.10f}".rstrip("0")
        if "." in s:
            return len(s.split(".")[1])
        return 0

    async def close(self):
        """Close HTTP client."""
        await self.client.aclose()

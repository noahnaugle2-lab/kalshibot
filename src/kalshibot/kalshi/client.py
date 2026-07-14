"""Async REST client for the Kalshi Trade API v2.

- Public market-data endpoints work unauthenticated (SHADOW mode reads prod
  read-only). Portfolio/order endpoints require a KalshiSigner.
- All requests flow through shared token buckets (reads and writes separately)
  so nine asset loops can't collectively breach rate limits.
- Retries 429/5xx with exponential backoff; everything else raises.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from typing import Any

import httpx

from kalshibot.kalshi.auth import KalshiSigner
from kalshibot.kalshi.models import (
    Balance,
    CancelResponse,
    Fill,
    Market,
    Order,
    Orderbook,
    OrderRequest,
    OrderResponse,
    Series,
    Settlement,
)
from kalshibot.kalshi.ratelimit import TokenBucket

logger = logging.getLogger(__name__)

API_PREFIX = "/trade-api/v2"
RETRYABLE_STATUS = {429, 502, 503, 504}
MAX_RETRIES = 3


class KalshiEnvironment(str, enum.Enum):
    PROD = "https://api.elections.kalshi.com"
    DEMO = "https://demo-api.kalshi.co"


class KalshiAPIError(Exception):
    def __init__(self, status_code: int, body: str, path: str) -> None:
        self.status_code = status_code
        self.body = body
        self.path = path
        super().__init__(f"Kalshi API {status_code} on {path}: {body[:300]}")


class KalshiAuthError(Exception):
    pass


class KalshiClient:
    def __init__(
        self,
        environment: KalshiEnvironment = KalshiEnvironment.PROD,
        signer: KalshiSigner | None = None,
        read_rate_per_sec: float = 8.0,
        write_rate_per_sec: float = 4.0,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.environment = environment
        self.signer = signer
        self._read_bucket = TokenBucket(read_rate_per_sec)
        self._write_bucket = TokenBucket(write_rate_per_sec)
        self._http = httpx.AsyncClient(
            base_url=environment.value + API_PREFIX, timeout=timeout_seconds
        )

    async def __aenter__(self) -> "KalshiClient":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        auth_required: bool = False,
        write: bool = False,
    ) -> dict[str, Any]:
        if auth_required and self.signer is None:
            raise KalshiAuthError(
                f"{method} {path} requires credentials; set KALSHI_KEY_ID and "
                "KALSHI_PRIVATE_KEY_PATH in .env"
            )
        bucket = self._write_bucket if write else self._read_bucket
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            await bucket.acquire()
            headers = (
                self.signer.auth_headers(method, API_PREFIX + path) if self.signer else {}
            )
            try:
                response = await self._http.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.TransportError as exc:
                last_error = exc
                logger.warning("transport error on %s %s: %s", method, path, exc)
            else:
                if response.status_code < 300:
                    return response.json() if response.content else {}
                last_error = KalshiAPIError(response.status_code, response.text, path)
                if response.status_code not in RETRYABLE_STATUS:
                    raise last_error
                logger.warning("retryable %s on %s %s", response.status_code, method, path)
            if attempt < MAX_RETRIES:
                await asyncio.sleep(0.5 * 2**attempt)
        assert last_error is not None
        raise last_error

    # ----------------------------------------------------------- market data

    async def get_exchange_status(self) -> dict[str, Any]:
        return await self._request("GET", "/exchange/status")

    async def get_series_list(self, category: str) -> list[Series]:
        data = await self._request("GET", "/series", params={"category": category})
        return [Series.model_validate(s) for s in data.get("series") or []]

    async def get_series(self, series_ticker: str) -> Series:
        data = await self._request("GET", f"/series/{series_ticker}")
        return Series.model_validate(data["series"])

    async def get_markets(
        self,
        *,
        series_ticker: str | None = None,
        event_ticker: str | None = None,
        status: str | None = None,
        tickers: list[str] | None = None,
        max_results: int = 1000,
    ) -> list[Market]:
        """Fetch markets, following cursor pagination up to max_results."""
        markets: list[Market] = []
        cursor: str | None = None
        while len(markets) < max_results:
            params: dict[str, Any] = {"limit": min(1000, max_results - len(markets))}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if event_ticker:
                params["event_ticker"] = event_ticker
            if status:
                params["status"] = status
            if tickers:
                params["tickers"] = ",".join(tickers)
            if cursor:
                params["cursor"] = cursor
            data = await self._request("GET", "/markets", params=params)
            batch = data.get("markets") or []
            markets.extend(Market.model_validate(m) for m in batch)
            cursor = data.get("cursor") or None
            if not cursor or not batch:
                break
        return markets

    async def get_market(self, ticker: str) -> Market:
        data = await self._request("GET", f"/markets/{ticker}")
        return Market.model_validate(data["market"])

    async def get_orderbook(self, ticker: str, depth: int = 32) -> Orderbook:
        data = await self._request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth}
        )
        return Orderbook.from_api(data)

    async def get_trades(
        self, ticker: str, *, limit: int = 100, cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"ticker": ticker, "limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/markets/trades", params=params)

    # ------------------------------------------------------------- portfolio

    async def get_balance(self) -> Balance:
        data = await self._request("GET", "/portfolio/balance", auth_required=True)
        return Balance.model_validate(data)

    async def get_api_keys(self) -> dict[str, Any]:
        """Return the authenticated account's API key metadata.

        The live executor uses this read-only endpoint to confirm that the
        configured production key has write scope before it can submit an
        order.  It deliberately does not infer scope from local config.
        """
        return await self._request("GET", "/api_keys", auth_required=True)

    async def get_positions(self, **params: Any) -> dict[str, Any]:
        return await self._request(
            "GET", "/portfolio/positions", params=params or None, auth_required=True
        )

    async def get_fills(self, **params: Any) -> list[Fill]:
        data = await self._request(
            "GET", "/portfolio/fills", params=params or None, auth_required=True
        )
        return [Fill.model_validate(f) for f in data.get("fills") or []]

    async def get_settlements(self, **params: Any) -> list[Settlement]:
        data = await self._request(
            "GET", "/portfolio/settlements", params=params or None, auth_required=True
        )
        return [Settlement.model_validate(s) for s in data.get("settlements") or []]

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Create an order via the V2 endpoint (legacy create 410s since 2026-05)."""
        data = await self._request(
            "POST",
            "/portfolio/events/orders",
            json_body=order.body(),
            auth_required=True,
            write=True,
        )
        return OrderResponse.model_validate(data.get("order") or data)

    async def cancel_order(self, order_id: str) -> CancelResponse:
        data = await self._request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            auth_required=True,
            write=True,
        )
        return CancelResponse.model_validate(data.get("order") or data)

    async def get_orders(self, **params: Any) -> list[Order]:
        data = await self._request(
            "GET", "/portfolio/orders", params=params or None, auth_required=True
        )
        return [Order.model_validate(o) for o in data.get("orders") or []]

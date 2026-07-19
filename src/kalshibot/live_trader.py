"""Fail-closed production order executor.

This is intentionally separate from :class:`ShadowTrader`: simulation rows
and funded exchange activity must never share persistence or a process.  It
accepts only a signal already approved by ``RiskManager`` and submits a BUY
IOC order with a deterministic client id.  Any ambiguous network outcome is
recorded as ``submit_unknown`` and is *never* retried automatically.
"""

from __future__ import annotations

import hashlib
import time
from decimal import Decimal
from typing import Protocol

from kalshibot.config import Mode, Settings
from kalshibot.features.engine import FeatureSnapshot
from kalshibot.kalshi.models import OrderIntent, OrderRequest, OrderResponse
from kalshibot.persistence.db import Database, dump_json
from kalshibot.strategies.base import StrategySignal


LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_ORDERS"


class OrderClient(Protocol):
    async def place_order(self, order: OrderRequest) -> OrderResponse: ...

    async def get_orders(self, **params: object) -> list[object]: ...

    async def get_api_keys(self) -> dict[str, object]: ...


class LiveTradingRefused(RuntimeError):
    """Raised before any exchange write when a live safety boundary is unmet."""


class LiveTrader:
    """Durable, minimal production execution boundary.

    It intentionally contains no strategy loop. The separately deployed funded
    supervisor may call ``submit_approved`` only after its own startup, control,
    risk, and reconciliation gates pass.
    """

    def __init__(self, settings: Settings, db: Database, client: OrderClient) -> None:
        self.settings = settings
        self.db = db
        self.client = client
        self._write_scope_verified = False

    @property
    def allowed_assets(self) -> set[str]:
        return {
            asset.strip().upper()
            for asset in self.settings.live_allowed_assets.split(",")
            if asset.strip()
        }

    def assert_armed(self, asset: str | None = None) -> None:
        if self.settings.mode is not Mode.LIVE:
            raise LiveTradingRefused("MODE must be LIVE")
        if not self.settings.live_trading_enabled:
            raise LiveTradingRefused("LIVE_TRADING_ENABLED is not true")
        if self.settings.live_trading_confirmation != LIVE_CONFIRMATION:
            raise LiveTradingRefused("LIVE_TRADING_CONFIRMATION is not exact")
        if not self.settings.kalshi_key_id or not self.settings.kalshi_private_key_path:
            raise LiveTradingRefused("production Kalshi credentials are missing")
        if not self.allowed_assets:
            raise LiveTradingRefused("LIVE_ALLOWED_ASSETS is empty")
        if asset and asset.upper() not in self.allowed_assets:
            raise LiveTradingRefused(f"{asset} is not allowlisted for live trading")

    async def prepare(self) -> None:
        """Perform the mandatory read-only production-key scope check."""
        self.assert_armed()
        payload = await self.client.get_api_keys()
        keys = payload.get("api_keys") or payload.get("keys") or []
        if not isinstance(keys, list):
            raise LiveTradingRefused("could not parse production API key metadata")
        key = next((item for item in keys if isinstance(item, dict) and (
            item.get("api_key_id") == self.settings.kalshi_key_id
            or item.get("key_id") == self.settings.kalshi_key_id
            or item.get("id") == self.settings.kalshi_key_id
        )), None)
        if key is None:
            raise LiveTradingRefused("configured production API key was not returned")
        scope = key.get("scope", key.get("scopes", ""))
        scope_text = " ".join(scope) if isinstance(scope, list) else str(scope)
        if "write" not in scope_text.lower():
            raise LiveTradingRefused("configured production API key lacks write scope")
        self._write_scope_verified = True

    @staticmethod
    def client_order_id(asset: str, signal: StrategySignal, market_ticker: str) -> str:
        """Stable retry identity for the same market proposal, max 27 chars."""
        material = "|".join((
            asset.upper(), market_ticker, signal.intent.value,
            f"{signal.limit_price:.4f}", f"{signal.contracts:.2f}",
        ))
        return "kb-live-" + hashlib.sha256(material.encode()).hexdigest()[:19]

    async def submit_approved(
        self, asset: str, snapshot: FeatureSnapshot, signal: StrategySignal
    ) -> OrderResponse:
        """Persist intent then submit one IOC entry, never retry an ambiguity."""
        self.assert_armed(asset)
        if signal.intent not in (OrderIntent.BUY_YES, OrderIntent.BUY_NO):
            raise LiveTradingRefused("only BUY settlement entries are supported")
        if signal.execution != "taker":
            raise LiveTradingRefused("live maker orders are not implemented")
        if signal.contracts <= 0:
            raise LiveTradingRefused("contracts must be positive")
        contracts = min(int(signal.contracts), self.settings.live_max_contracts_per_order)
        if contracts < 1:
            raise LiveTradingRefused("contract cap rounds to zero")
        if not self._write_scope_verified:
            raise LiveTradingRefused("production write scope has not been verified")

        client_order_id = self.client_order_id(asset, signal, snapshot.market_ticker)
        existing = self.db.query(
            "SELECT status FROM live_orders WHERE client_order_id = ?", (client_order_id,)
        )
        if existing:
            raise LiveTradingRefused(
                f"duplicate or unresolved live proposal ({existing[0]['status']})"
            )

        now = time.time()
        self.db.write_now("live_orders", {
            "client_order_id": client_order_id,
            "created_ts": now, "updated_ts": now,
            "asset": asset.upper(), "market_ticker": snapshot.market_ticker,
            "intent": signal.intent.value, "limit_price": signal.limit_price,
            "requested_contracts": contracts, "status": "submit_unknown",
            "reason": signal.reason,
            "raw": dump_json({"phase": "intent_persisted"}),
        })
        order = OrderRequest.from_intent(
            snapshot.market_ticker, signal.intent, Decimal(contracts),
            Decimal(str(signal.limit_price)), client_order_id=client_order_id,
            time_in_force="immediate_or_cancel", cancel_order_on_pause=True,
        )
        try:
            response = await self.client.place_order(order)
        except Exception as exc:
            # Unknown means unknown. Reconciliation is manual/read-only before
            # another order can be contemplated, preventing duplicate exposure.
            self.db.write_now_sql(
                "UPDATE live_orders SET status='submit_unknown', updated_ts=?, raw=? "
                "WHERE client_order_id=?",
                (time.time(), dump_json({"submit_error": str(exc)}), client_order_id),
            )
            raise

        filled = float(response.fill_count or 0)
        remaining = float(response.remaining_count or 0)
        status = "filled" if filled >= contracts else "partial" if filled else "unfilled"
        wire_avg_price = (
            float(response.average_fill_price)
            if response.average_fill_price is not None else None
        )
        # V2 quotes every response in YES-leg terms. Convert an ASK fill back
        # to the BUY_NO contract cost used everywhere in our position ledger.
        avg_price = wire_avg_price
        if wire_avg_price is not None and signal.intent is OrderIntent.BUY_NO:
            avg_price = round(1.0 - wire_avg_price, 4)
        # The V2 response is a volume-weighted fee *per contract*.
        fees = (
            float(response.average_fee_paid) * filled
            if response.average_fee_paid is not None else None
        )
        self.db.write_now_sql(
            "UPDATE live_orders SET order_id=?, updated_ts=?, filled_contracts=?, "
            "avg_fill_price=?, fees=?, status=?, raw=? WHERE client_order_id=?",
            (response.order_id, time.time(), filled, avg_price, fees, status,
             dump_json({"response": response.model_dump(mode="json"), "remaining": remaining}),
             client_order_id),
        )
        if filled > 0 and avg_price is not None:
            self.db.write_now("live_positions", {
                "market_ticker": snapshot.market_ticker, "asset": asset.upper(),
                "intent": signal.intent.value, "contracts": filled,
                "avg_price": avg_price, "fees": fees or 0.0, "entry_ts": time.time(),
                "source_client_order_id": client_order_id, "status": "open",
                "updated_ts": time.time(),
            })
        return response

    async def reconcile_open_orders(self) -> int:
        """Read-only reconciliation for durable unresolved/resting order records.

        This method never submits or cancels. It returns the number of local
        records that could be correlated to currently visible exchange orders.
        """
        self.assert_armed()
        exchange_orders = await self.client.get_orders()
        by_client_id = {
            getattr(order, "client_order_id", ""): order for order in exchange_orders
        }
        updated = 0
        for row in self.db.query(
            "SELECT client_order_id FROM live_orders WHERE status = 'submit_unknown'"
        ):
            order = by_client_id.get(row["client_order_id"])
            if order is None:
                continue
            self.db.write_now_sql(
                "UPDATE live_orders SET order_id=?, updated_ts=?, raw=? WHERE client_order_id=?",
                (getattr(order, "order_id", None), time.time(),
                 dump_json({"reconciled_order": getattr(order, "model_dump", lambda: {})()}),
                 row["client_order_id"]),
            )
            updated += 1
        return updated

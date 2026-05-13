import os
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

POLL_INTERVAL = 3   # seconds between fill checks
POLL_TIMEOUT  = 60  # max seconds to wait for fill (increased for market-open volatility)


class Broker:
    def __init__(self, api_key: str, secret_key: str):
        paper = os.getenv("ALPACA_PAPER", "true").lower() != "false"
        self.trading = TradingClient(api_key, secret_key, paper=paper)
        self.data = StockHistoricalDataClient(api_key, secret_key)

    def get_account(self) -> dict:
        acc = self.trading.get_account()
        return {
            "cash": float(acc.cash),
            "portfolio_value": float(acc.portfolio_value),
            "buying_power": float(acc.buying_power),
        }

    def is_market_open(self) -> bool:
        return self.trading.get_clock().is_open

    def get_current_price(self, symbol: str) -> float:
        request = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = self.data.get_stock_latest_quote(request)[symbol]
        ask = float(quote.ask_price or 0)
        bid = float(quote.bid_price or 0)
        if ask > 0 and bid > 0:
            return (ask + bid) / 2
        return ask or bid

    def _poll_for_fill(self, order_id: str) -> tuple[float | None, float | None]:
        """Poll until order fills. Returns (filled_qty, filled_avg_price) or (None, None)."""
        for _ in range(POLL_TIMEOUT // POLL_INTERVAL):
            time.sleep(POLL_INTERVAL)
            o = self.trading.get_order_by_id(order_id)
            status = str(o.status)
            if status in ("filled", "partially_filled"):
                qty = float(o.filled_qty or 0)
                price = float(o.filled_avg_price or 0)
                if qty > 0 and price > 0:
                    return qty, price
        return None, None

    def _try_oco(self, symbol: str, qty: float, tp_price: float, sl_price: float) -> tuple[str | None, str | None]:
        """Place OCO sell. Alpaca requires limit_price + take_profit.limit_price + stop_loss."""
        try:
            oco = self.trading.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.OCO,
                    limit_price=tp_price,
                    take_profit=TakeProfitRequest(limit_price=tp_price),
                    stop_loss=StopLossRequest(stop_price=sl_price),
                )
            )
            return str(oco.id), None
        except Exception as e:
            return None, str(e)

    def place_order(
        self,
        symbol: str,
        notional: float,
        take_profit_pct: float,
        stop_loss_pct: float,
    ) -> tuple[dict | None, str | None]:
        try:
            price = self.get_current_price(symbol)
            if price <= 0:
                return None, f"Could not fetch price for {symbol}"

            whole_shares = int(notional / price)
            frac_shares = round(notional / price, 9)

            tp_price = round(price * (1 + take_profit_pct / 100), 2)
            sl_price = round(price * (1 - stop_loss_pct / 100), 2)

            # ── Whole shares: bracket order ───────────────────────────────────────
            if whole_shares >= 1:
                order = self.trading.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        qty=whole_shares,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.GTC,
                        order_class=OrderClass.BRACKET,
                        take_profit=TakeProfitRequest(limit_price=tp_price),
                        stop_loss=StopLossRequest(stop_price=sl_price),
                    )
                )
                return {
                    "id": str(order.id),
                    "oco_id": None,
                    "symbol": symbol,
                    "shares": whole_shares,
                    "entry_price": price,
                    "take_profit_price": tp_price,
                    "stop_loss_price": sl_price,
                    "fractional": False,
                    "oco_protected": True,
                }, None

            # ── Fractional: market buy → poll fill → OCO exit ─────────────────────
            buy_order = self.trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=frac_shares,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            order_id = str(buy_order.id)

            filled_qty, filled_price = self._poll_for_fill(order_id)

            # Timed out — try to cancel to avoid unprotected position
            if filled_qty is None:
                try:
                    self.trading.cancel_order_by_id(order_id)
                    return None, f"{symbol} order timed out and was cancelled — try again"
                except Exception:
                    # Cancel failed: order already filled, fetch fill and place OCO
                    o = self.trading.get_order_by_id(order_id)
                    filled_qty = float(o.filled_qty or 0)
                    filled_price = float(o.filled_avg_price or 0)
                    if not filled_qty or not filled_price:
                        return None, f"{symbol} in unknown state — check Alpaca manually"

            # Place OCO with actual fill price
            tp_price = round(filled_price * (1 + take_profit_pct / 100), 2)
            sl_price = round(filled_price * (1 - stop_loss_pct / 100), 2)
            oco_id, oco_error = self._try_oco(symbol, filled_qty, tp_price, sl_price)

            return {
                "id": order_id,
                "oco_id": oco_id,
                "symbol": symbol,
                "shares": filled_qty,
                "entry_price": filled_price,
                "take_profit_price": tp_price,
                "stop_loss_price": sl_price,
                "fractional": True,
                "oco_protected": oco_id is not None,
            }, oco_error

        except Exception as e:
            return None, str(e)

    def place_bracket_order(self, symbol, notional, take_profit_pct, stop_loss_pct):
        return self.place_order(symbol, notional, take_profit_pct, stop_loss_pct)

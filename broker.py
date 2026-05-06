from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest


class Broker:
    def __init__(self, api_key: str, secret_key: str):
        self.trading = TradingClient(api_key, secret_key, paper=True)
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

    def place_bracket_order(
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

            take_profit_price = round(price * (1 + take_profit_pct / 100), 2)
            stop_loss_price = round(price * (1 - stop_loss_pct / 100), 2)

            if whole_shares >= 1:
                order = self.trading.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        qty=whole_shares,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.GTC,
                        order_class=OrderClass.BRACKET,
                        take_profit=TakeProfitRequest(limit_price=take_profit_price),
                        stop_loss=StopLossRequest(stop_price=stop_loss_price),
                    )
                )
                return {
                    "id": str(order.id),
                    "symbol": symbol,
                    "shares": whole_shares,
                    "entry_price": price,
                    "take_profit_price": take_profit_price,
                    "stop_loss_price": stop_loss_price,
                    "actual_notional": whole_shares * price,
                    "fractional": False,
                }, None
            else:
                order = self.trading.submit_order(
                    MarketOrderRequest(
                        symbol=symbol,
                        qty=frac_shares,
                        side=OrderSide.BUY,
                        time_in_force=TimeInForce.DAY,
                    )
                )
                return {
                    "id": str(order.id),
                    "symbol": symbol,
                    "shares": frac_shares,
                    "entry_price": price,
                    "take_profit_price": take_profit_price,
                    "stop_loss_price": stop_loss_price,
                    "actual_notional": notional,
                    "fractional": True,
                }, None

        except Exception as e:
            return None, str(e)

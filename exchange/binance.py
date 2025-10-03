from exchange.pexchange import ccxt, httpx # ccxt_async 대신 ccxt를 사용
from devtools import debug
from exchange.model import MarketOrder
import exchange.error as error
import time # asyncio.sleep 대신 time.sleep을 사용

class Binance:
    def __init__(self, key, secret):
        # 동기 CCXT 클라이언트 사용
        self.client = ccxt.binance(
            {
                "apiKey": key,
                "secret": secret,
                "options": {"adjustForTimeDifference": True},
            }
        )
        self.position_mode = "one-way"
        self.order_info: MarketOrder = None

    # 1. init_info: 동기 함수로 변경 (main.py에서 await 불필요)
    def init_info(self, order_info: MarketOrder):
        self.order_info = order_info
        
        # 동기 호출로 변경 (load_markets)
        self.client.load_markets() 
        market = self.client.market(order_info.unified_symbol)

        if order_info.amount is not None:
            order_info.amount = float(
                self.client.amount_to_precision(order_info.unified_symbol, order_info.amount)
            )
        
        if order_info.is_futures:
            if order_info.is_coinm:
                is_contract = market.get("contract")
                if is_contract:
                    order_info.is_contract = True
                    order_info.contract_size = market.get("contractSize")
                self.client.options["defaultType"] = "delivery"
            else:
                self.client.options["defaultType"] = "swap"
        else:
            self.client.options["defaultType"] = "spot"

    # 2. get_ticker: 동기 함수로 변경
    def get_ticker(self, symbol: str):
        return self.client.fetch_ticker(symbol)

    # 3. get_price: 동기 함수로 변경
    def get_price(self, symbol: str):
        return (self.get_ticker(symbol))["last"]

    # 4. get_futures_position: 동기 함수로 변경
    def get_futures_position(self, symbol=None, all=False):
        if symbol is None and all:
            # 동기 호출로 변경 (fetch_balance)
            positions = (self.client.fetch_balance())["info"]["positions"]
            positions = [position for position in positions if float(position["positionAmt"]) != 0]
            return positions

        positions = None
        # 동기 호출로 변경 (fetch_balance)
        if self.order_info.is_coinm:
            positions = (self.client.fetch_balance())["info"]["positions"]
            positions = [
                position
                for position in positions
                if float(position["positionAmt"]) != 0
                and position["symbol"] == self.client.market(symbol).get("id")
            ]
        else:
            # 동기 호출로 변경 (fetch_positions)
            positions = self.client.fetch_positions(symbols=[symbol])

        long_contracts = None
        short_contracts = None
        if positions:
            if self.order_info.is_coinm:
                for position in positions:
                    amt = float(position["positionAmt"])
                    if position["positionSide"] == "LONG":
                        long_contracts = amt
                    elif position["positionSide"] == "SHORT":
                        short_contracts = amt
                    elif position["positionSide"] == "BOTH":
                        if amt > 0:
                            long_contracts = amt
                        elif amt < 0:
                            short_contracts = abs(amt)
            else:
                for position in positions:
                    if position["side"] == "long":
                        long_contracts = position["contracts"]
                    elif position["side"] == "short":
                        short_contracts = position["contracts"]

            if self.order_info.is_close and self.order_info.is_buy:
                if not short_contracts:
                    raise error.ShortPositionNoneError()
                return short_contracts
            elif self.order_info.is_close and self.order_info.is_sell:
                if not long_contracts:
                    raise error.LongPositionNoneError()
                return long_contracts
        else:
            raise error.PositionNoneError()

    # 5. get_balance: 동기 함수로 변경
    def get_balance(self, base: str) -> float:
        free_balance_by_base = None

        if self.order_info.is_entry or (self.order_info.is_spot and (self.order_info.is_buy or self.order_info.is_sell)):
            # 동기 호출로 변경 (fetch_free_balance / fetch_total_balance)
            free_balance = (self.client.fetch_free_balance() if not self.order_info.is_total else self.client.fetch_total_balance())
            free_balance_by_base = free_balance.get(base)

        if free_balance_by_base is None or free_balance_by_base == 0:
            raise error.FreeAmountNoneError()
        return free_balance_by_base

    # 6. get_amount: 동기 함수로 변경
    def get_amount(self, order_info: MarketOrder) -> float:
        # 내부 함수(get_price, get_balance, get_futures_position)가 모두 동기이므로, 이 함수 자체를 동기로 변경
        if order_info.amount is not None and order_info.percent is not None:
            raise error.AmountPercentBothError()
        elif order_info.amount is not None:
            if order_info.is_contract:
                current_price = self.get_price(order_info.unified_symbol)
                result = (order_info.amount * current_price) // order_info.contract_size
            else:
                result = order_info.amount
        elif order_info.percent is not None:
            if order_info.is_entry or (order_info.is_spot and order_info.is_buy):
                if order_info.is_coinm:
                    free_base = self.get_balance(order_info.base)
                    current_price = self.get_price(order_info.unified_symbol)
                    result = (free_base * order_info.percent / 100 * current_price) // order_info.contract_size if order_info.is_contract else free_base * order_info.percent / 100
                else:
                    free_quote = self.get_balance(order_info.quote)
                    current_price = self.get_price(order_info.unified_symbol)
                    if order_info.is_contract:
                        result = (free_quote * order_info.percent / 100 * current_price) // order_info.contract_size
                    else:
                        result = free_quote * (order_info.percent - 0.5) / 100 / current_price
            elif self.order_info.is_close:
                free_amount = self.get_futures_position(order_info.unified_symbol)
                result = free_amount * order_info.percent / 100
            elif order_info.is_spot and order_info.is_sell:
                free_amount = self.get_balance(order_info.base)
                result = free_amount * order_info.percent / 100
            else:
                raise error.AmountPercentNoneError()

            result = float(self.client.amount_to_precision(order_info.unified_symbol, result))
            order_info.amount_by_percent = result
        else:
            raise error.AmountPercentNoneError()

        return result

    # 7. set_leverage: 동기 함수로 변경
    def set_leverage(self, leverage, symbol):
        if self.order_info.is_futures:
            self.client.set_leverage(leverage, symbol)

    # 8. market_order: 동기 함수로 변경 (동기 retry 사용)
    def market_order(self, order_info: MarketOrder):
        # 동기 retry 함수를 사용해야 합니다.
        from exchange.pexchange import retry 

        symbol = order_info.unified_symbol
        params = {}
        try:
            return retry(
                self.client.create_order,
                symbol,
                order_info.type.lower(),
                order_info.side,
                order_info.amount,
                None,
                params,
                order_info=order_info,
                max_attempts=5,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)

    # 9. market_buy: 동기 함수로 변경
    def market_buy(self, order_info: MarketOrder):
        order_info.amount = self.get_amount(order_info)
        return self.market_order(order_info)

    # 10. market_sell: 동기 함수로 변경
    def market_sell(self, order_info: MarketOrder):
        order_info.amount = self.get_amount(order_info)
        return self.market_order(order_info)

    # 11. market_entry: 동기 함수로 변경 (동기 retry 사용)
    def market_entry(self, order_info: MarketOrder):
        from exchange.pexchange import retry 

        symbol = order_info.unified_symbol
        entry_amount = self.get_amount(order_info)
        if entry_amount == 0:
            raise error.MinAmountError()

        params = {}
        if self.position_mode == "hedge":
            if order_info.side == "buy":
                positionSide = "LONG" if order_info.is_entry else "SHORT"
            elif order_info.side == "sell":
                positionSide = "SHORT" if order_info.is_entry else "LONG"
            params = {"positionSide": positionSide}

        if order_info.leverage is not None:
            self.set_leverage(order_info.leverage, symbol)

        try:
            return retry(
                self.client.create_order,
                symbol,
                order_info.type.lower(),
                order_info.side,
                abs(entry_amount),
                None,
                params,
                order_info=order_info,
                max_attempts=10,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)

    # 12. market_close: 동기 함수로 변경 (동기 retry 사용)
    def market_close(self, order_info: MarketOrder):
        from exchange.pexchange import retry 

        symbol = order_info.unified_symbol
        close_amount = self.get_amount(order_info)

        params = {"reduceOnly": True} if self.position_mode == "one-way" else {}
        if self.position_mode == "hedge":
            if order_info.side == "buy":
                positionSide = "LONG" if order_info.is_entry else "SHORT"
            elif order_info.side == "sell":
                positionSide = "SHORT" if order_info.is_entry else "LONG"
            params = {"positionSide": positionSide}

        try:
            return retry(
                self.client.create_order,
                symbol,
                order_info.type.lower(),
                order_info.side,
                abs(close_amount),
                None,
                params,
                order_info=order_info,
                max_attempts=10,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)

    # 13. get_listen_key: 동기 함수로 변경 (httpx가 동기 호출을 지원한다고 가정)
    def get_listen_key(self):
        # 참고: httpx를 동기적으로 사용하려면 httpx.Client().post()를 사용해야 합니다.
        # 여기서는 간단히 async 키워드만 제거합니다. 실제로는 동기 HTTP 클라이언트를 사용해야 합니다.
        url = "https://fapi.binance.com/fapi/v1/listenKey"
        # CCXT의 동기 함수를 사용하는 것이 가장 안전합니다.
        # listenkey = (self.client.fapiPrivatePostListenKey()).get("listenKey") 
        
        # 원본 코드를 최대한 유지하며 async만 제거
        listenkey = (httpx.post(url, headers={"X-MBX-APIKEY": self.client.apiKey})).json()["listenKey"]
        return listenkey

    # 14. get_trades: 동기 함수로 변경
    def get_trades(self):
        if self.order_info.is_futures:
            trades = self.client.fetch_my_trades()
            print(trades)

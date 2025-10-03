from exchange.pexchange import ccxt_async, httpx
from devtools import debug
from exchange.model import MarketOrder
import exchange.error as error


class Binance:
    def __init__(self, key, secret):
        self.client = ccxt_async.binance(
            {
                "apiKey": key,
                "secret": secret,
                "options": {"adjustForTimeDifference": True},
            }
        )
        self.position_mode = "one-way"
        self.order_info: MarketOrder = None

    async def init_info(self, order_info: MarketOrder):
        self.order_info = order_info
        await self.client.load_markets()
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

    async def get_ticker(self, symbol: str):
        return await self.client.fetch_ticker(symbol)

    async def get_price(self, symbol: str):
        return (await self.get_ticker(symbol))["last"]

    async def get_futures_position(self, symbol=None, all=False):
        if symbol is None and all:
            positions = (await self.client.fetch_balance())["info"]["positions"]
            positions = [position for position in positions if float(position["positionAmt"]) != 0]
            return positions

        positions = None
        if self.order_info.is_coinm:
            positions = (await self.client.fetch_balance())["info"]["positions"]
            positions = [
                position
                for position in positions
                if float(position["positionAmt"]) != 0
                and position["symbol"] == self.client.market(symbol).get("id")
            ]
        else:
            positions = await self.client.fetch_positions(symbols=[symbol])

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

    async def get_balance(self, base: str):
        free_balance_by_base = None

        if self.order_info.is_entry or (self.order_info.is_spot and (self.order_info.is_buy or self.order_info.is_sell)):
            free_balance = await (self.client.fetch_free_balance() if not self.order_info.is_total else self.client.fetch_total_balance())
            free_balance_by_base = free_balance.get(base)

        if free_balance_by_base is None or free_balance_by_base == 0:
            raise error.FreeAmountNoneError()
        return free_balance_by_base

    async def get_amount(self, order_info: MarketOrder) -> float:
        if order_info.amount is not None and order_info.percent is not None:
            raise error.AmountPercentBothError()
        elif order_info.amount is not None:
            if order_info.is_contract:
                current_price = await self.get_price(order_info.unified_symbol)
                result = (order_info.amount * current_price) // order_info.contract_size
            else:
                result = order_info.amount
        elif order_info.percent is not None:
            if order_info.is_entry or (order_info.is_spot and order_info.is_buy):
                if order_info.is_coinm:
                    free_base = await self.get_balance(order_info.base)
                    current_price = await self.get_price(order_info.unified_symbol)
                    result = (free_base * order_info.percent / 100 * current_price) // order_info.contract_size if order_info.is_contract else free_base * order_info.percent / 100
                else:
                    free_quote = await self.get_balance(order_info.quote)
                    current_price = await self.get_price(order_info.unified_symbol)
                    if order_info.is_contract:
                        result = (free_quote * order_info.percent / 100 * current_price) // order_info.contract_size
                    else:
                        result = free_quote * (order_info.percent - 0.5) / 100 / current_price
            elif self.order_info.is_close:
                free_amount = await self.get_futures_position(order_info.unified_symbol)
                result = free_amount * order_info.percent / 100
            elif order_info.is_spot and order_info.is_sell:
                free_amount = await self.get_balance(order_info.base)
                result = free_amount * order_info.percent / 100
            else:
                raise error.AmountPercentNoneError()

            result = float(self.client.amount_to_precision(order_info.unified_symbol, result))
            order_info.amount_by_percent = result
        else:
            raise error.AmountPercentNoneError()

        return result

    async def set_leverage(self, leverage, symbol):
        if self.order_info.is_futures:
            await self.client.set_leverage(leverage, symbol)

    async def market_order(self, order_info: MarketOrder):
        from exchange.pexchange import retry_async

        symbol = order_info.unified_symbol
        params = {}
        try:
            return await retry_async(
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

    async def market_buy(self, order_info: MarketOrder):
        order_info.amount = await self.get_amount(order_info)
        return await self.market_order(order_info)

    async def market_sell(self, order_info: MarketOrder):
        order_info.amount = await self.get_amount(order_info)
        return await self.market_order(order_info)

    async def market_entry(self, order_info: MarketOrder):
        from exchange.pexchange import retry_async

        symbol = order_info.unified_symbol
        entry_amount = await self.get_amount(order_info)
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
            await self.set_leverage(order_info.leverage, symbol)

        try:
            return await retry_async(
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

    async def market_close(self, order_info: MarketOrder):
        from exchange.pexchange import retry_async

        symbol = order_info.unified_symbol
        close_amount = await self.get_amount(order_info)

        params = {"reduceOnly": True} if self.position_mode == "one-way" else {}
        if self.position_mode == "hedge":
            if order_info.side == "buy":
                positionSide = "LONG" if order_info.is_entry else "SHORT"
            elif order_info.side == "sell":
                positionSide = "SHORT" if order_info.is_entry else "LONG"
            params = {"positionSide": positionSide}

        try:
            return await retry_async(
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

    async def get_listen_key(self):
        url = "https://fapi.binance.com/fapi/v1/listenKey"
        listenkey = (await httpx.post(url, headers={"X-MBX-APIKEY": self.client.apiKey})).json()["listenKey"]
        return listenkey

    async def get_trades(self):
        if self.order_info.is_futures:
            trades = await self.client.fetch_my_trades()
            print(trades)

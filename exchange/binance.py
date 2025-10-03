import asyncio
from exchange.pexchange import ccxt_async, httpx
from exchange.model import MarketOrder
import exchange.error as error


class Binance:
    def __init__(self, key, secret):
        self.client = ccxt_async.binance({
            "apiKey": key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {"adjustForTimeDifference": True}
        })
        self.position_mode = "one-way"
        self.order_info: MarketOrder = None

    async def init_info(self, order_info: MarketOrder):
        """마켓 정보 초기화"""
        self.order_info = order_info
        await self.client.load_markets()

        unified_symbol = order_info.unified_symbol
        market = self.client.market(unified_symbol)

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
        ticker = await self.get_ticker(symbol)
        return ticker["last"]

    async def get_balance(self, base: str) -> float:
        free_balance = await self.client.fetch_free_balance()
        free_balance_by_base = free_balance.get(base)

        if free_balance_by_base is None or free_balance_by_base == 0:
            raise error.FreeAmountNoneError()
        return free_balance_by_base

    async def get_amount(self, order_info: MarketOrder) -> float:
        """수량/퍼센트 기반 계산"""
        if order_info.amount is not None and order_info.percent is not None:
            raise error.AmountPercentBothError()
        elif order_info.amount is not None:
            return order_info.amount
        elif order_info.percent is not None:
            if order_info.is_buy:
                free_quote = await self.get_balance(order_info.quote)
                cash = free_quote * order_info.percent / 100
                current_price = await self.get_price(order_info.unified_symbol)
                return cash / current_price
            elif order_info.is_sell:
                free_amount = await self.get_balance(order_info.base)
                return free_amount * order_info.percent / 100
        else:
            raise error.AmountPercentNoneError()

    async def market_order(self, order_info: MarketOrder):
        """시장가 주문"""
        try:
            return await self.client.create_order(
                order_info.unified_symbol,
                order_info.type.lower(),
                order_info.side,
                order_info.amount
            )
        except Exception as e:
            raise error.OrderError(e, order_info)

    async def market_buy(self, order_info: MarketOrder):
        """시장가 매수 (분할 주문 포함)"""
        buy_amount = await self.get_amount(order_info)
        price = await self.get_price(order_info.unified_symbol)
        total_price = price * buy_amount

        # 분할 기준 (예: 30만 달러 단위)
        split_count = max(1, round(total_price / 50000)) + 1
        split_amount = buy_amount / split_count

        results = []
        for _ in range(split_count):
            order_info.amount = float(
                self.client.amount_to_precision(order_info.unified_symbol, split_amount)
            )
            order_info.price = price
            result = await self.market_order(order_info)
            results.append(result)
            if split_count > 1:
                await asyncio.sleep(4)  # 대기 시간 (Upbit는 20초였지만 Binance는 더 짧게 가능)

        return results

    async def market_sell(self, order_info: MarketOrder):
        """시장가 매도 (분할 주문 포함)"""
        sell_amount = await self.get_amount(order_info)
        price = await self.get_price(order_info.unified_symbol)
        total_price = price * sell_amount

        # 분할 기준 (예: 50만 달러 단위)
        split_count = max(1, round(total_price / 50000)) + 2
        split_amount = sell_amount / split_count

        results = []
        for i in range(split_count):
            if i < split_count - 1:
                order_info.amount = float(
                    self.client.amount_to_precision(order_info.unified_symbol, split_amount)
                )
            else:
                # 마지막 잔여 수량 처리
                order_info.amount = float(
                    self.client.amount_to_precision(
                        order_info.unified_symbol,
                        await self.get_balance(order_info.base)
                    )
                )

            order_info.price = price
            result = await self.market_order(order_info)
            results.append(result)
            if split_count > 1:
                await asyncio.sleep(4)

        return results

    async def get_order(self, order_id: str):
        return await self.client.fetch_order(order_id)

    async def get_order_amount(self, order_id: str):
        order = await self.get_order(order_id)
        return order["filled"]

    async def close(self):
        """세션 종료"""
        await self.client.close()

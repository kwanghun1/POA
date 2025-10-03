from exchange.pexchange import ccxt, httpx # 동기식 CCXT 클라이언트 사용
from devtools import debug
from exchange.model import MarketOrder
import exchange.error as error
import time # asyncio.sleep 대신 time.sleep을 사용 (분할 주문 시)

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
        self.client.load_markets() # 초기 마켓 로드
        self.order_info: MarketOrder = None

    # 1. init_info: 동기 함수
    def init_info(self, order_info: MarketOrder):
        self.order_info = order_info
        market = self.client.market(order_info.unified_symbol)

        if order_info.amount is not None:
            order_info.amount = float(
                self.client.amount_to_precision(order_info.unified_symbol, order_info.amount)
            )
        
        # 선물/현물에 따른 defaultType 설정 로직 유지
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

    # 2. get_ticker: 동기 함수
    def get_ticker(self, symbol: str):
        return self.client.fetch_ticker(symbol)

    # 3. get_price: 동기 함수
    def get_price(self, symbol: str):
        return (self.get_ticker(symbol))["last"]

    # 4. get_futures_position: 동기 함수 (선물 포지션 조회 로직)
    def get_futures_position(self, symbol=None, all=False):
        if symbol is None and all:
            positions = (self.client.fetch_balance())["info"]["positions"]
            positions = [position for position in positions if float(position["positionAmt"]) != 0]
            return positions

        positions = None
        if self.order_info.is_coinm:
            positions = (self.client.fetch_balance())["info"]["positions"]
            positions = [
                position
                for position in positions
                if float(position["positionAmt"]) != 0
                and position["symbol"] == self.client.market(symbol).get("id")
            ]
        else:
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
                    # 'contracts'가 아닌 'info'에서 계약 수량을 확인해야 할 수 있습니다. 
                    # ccxt의 기본 포맷인 'contracts'를 사용한다고 가정합니다.
                    if float(position["info"].get("positionAmt", 0)) > 0:
                        long_contracts = float(position["info"].get("positionAmt"))
                    elif float(position["info"].get("positionAmt", 0)) < 0:
                        short_contracts = abs(float(position["info"].get("positionAmt")))
                        
            # 청산 주문 수량 반환 로직
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
        # 청산 주문이 아니면 포지션 정보를 리턴하지 않음 (get_amount에서 처리)
        return {"long": long_contracts, "short": short_contracts}

    # 5. get_balance: 동기 함수 (현물/선물 증거금 잔고 조회)
    def get_balance(self, base: str) -> float:
        free_balance_by_base = None

        if self.order_info.is_entry or (self.order_info.is_spot and (self.order_info.is_buy or self.order_info.is_sell)):
            free_balance = (self.client.fetch_free_balance() if not self.order_info.is_total else self.client.fetch_total_balance())
            free_balance_by_base = free_balance.get(base)

        if free_balance_by_base is None or free_balance_by_base == 0:
            raise error.FreeAmountNoneError()
        return free_balance_by_base

    # 6. get_amount: 동기 함수 (주문 수량 계산)
    def get_amount(self, order_info: MarketOrder) -> float:
        if order_info.amount is not None and order_info.percent is not None:
            raise error.AmountPercentBothError()
        elif order_info.amount is not None:
            if order_info.is_contract:
                current_price = self.get_price(order_info.unified_symbol)
                # 계약 수량 계산 시 소수점 이하 버림
                result = (order_info.amount * current_price) // order_info.contract_size
            else:
                result = order_info.amount
        elif order_info.percent is not None:
            if order_info.is_entry or (order_info.is_spot and order_info.is_buy):
                # 매수(진입) 시: 쿼트 코인 잔고 기반 계산
                if order_info.is_coinm:
                    free_base = self.get_balance(order_info.base)
                    current_price = self.get_price(order_info.unified_symbol)
                    result = (free_base * order_info.percent / 100 * current_price) // order_info.contract_size if order_info.is_contract else free_base * order_info.percent / 100
                else:
                    free_quote = self.get_balance(order_info.quote)
                    current_price = self.get_price(order_info.unified_symbol)
                    if order_info.is_contract:
                        # 선물 레버리지 적용 시: 주문 금액(free_quote * percent) * 레버리지 / 현재가 / 계약크기
                        # 다만, 레버리지가 이미 마진으로 반영된다고 가정하고 단순하게 계산
                        result = (free_quote * order_info.percent / 100) / current_price * order_info.leverage // order_info.contract_size 
                    else:
                        result = free_quote * (order_info.percent - 0.5) / 100 / current_price
            elif self.order_info.is_close:
                # 청산 시: 포지션 잔고 기반 계산
                free_amount = self.get_futures_position(order_info.unified_symbol) # 이 함수는 닫을 수량(float)을 반환해야 함
                result = free_amount * order_info.percent / 100
            elif order_info.is_spot and order_info.is_sell:
                # 현물 매도 시: 베이스 코인 잔고 기반 계산
                free_amount = self.get_balance(order_info.base)
                result = free_amount * order_info.percent / 100
            else:
                raise error.AmountPercentNoneError()

            result = float(self.client.amount_to_precision(order_info.unified_symbol, result))
            order_info.amount_by_percent = result
        else:
            raise error.AmountPercentNoneError()

        return result

    # 7. set_leverage: 동기 함수
    def set_leverage(self, leverage, symbol):
        if self.order_info.is_futures:
            self.client.set_leverage(leverage, symbol)

    # 8. market_order: 동기 함수 (주문 실행 공통 로직)
    def market_order(self, order_info: MarketOrder):
        from exchange.pexchange import retry # 동기 retry 함수 사용

        params = {}
        try:
            return retry(
                self.client.create_order,
                order_info.unified_symbol,
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

    # 9. market_buy: 동기 함수 (업비트처럼 분할 매수 로직 반영)
    def market_buy(self, order_info: MarketOrder):
        buy_amount = self.get_amount(order_info)
        price = self.get_price(order_info.unified_symbol)
        
        # 분할 매수 로직 (바이낸스 선물/현물에 맞게 수정 필요)
        # 현물/선물은 업비트 현물과 최소 주문 금액/수량 조건이 다름
        total_price = price * buy_amount
        split_count = max(1, round(total_price / 1000))+1 # 바이낸스에 맞는 임의의 분할 기준 (예: $1000)
        split_amount = buy_amount / split_count

        results = []

        for _ in range(split_count):
            # 수량 정밀도 적용
            order_info.amount = float(self.client.amount_to_precision(order_info.unified_symbol, split_amount))
            
            # price는 시장가 주문 시 None이 될 수 있지만, 업비트처럼 가격정보를 넣는다면 유지
            # 바이낸스 시장가 주문은 price가 None이어야 합니다.
            order_info.price = None 

            # 레버리지 설정 (진입 주문이 아니므로 생략하거나, 필요한 경우 set_leverage 호출)
            
            result = self.market_order(order_info)
            results.append(result)
            
            # 동기식 sleep 사용
            if split_count > 1:
                time.sleep(20) 

        return results

    # 10. market_sell: 동기 함수 (업비트처럼 분할 매도 로직 반영)
    def market_sell(self, order_info: MarketOrder):
        sell_amount = self.get_amount(order_info)
        price = self.get_price(order_info.unified_symbol)
        
        total_price = price * sell_amount
        split_count = max(1, round(total_price / 1000))+2 # 바이낸스에 맞는 임의의 분할 기준
        split_amount = sell_amount / split_count

        results = []

        for i in range(split_count):
            if i < split_count - 1:
                order_info.amount = float(self.client.amount_to_precision(order_info.unified_symbol, split_amount))
            else:
                # 마지막 주문은 남은 전량으로 채우기 위해 잔고를 다시 조회 (안전 장치)
                order_info.amount = float(self.client.amount_to_precision(order_info.unified_symbol, self.get_balance(order_info.base)))

            order_info.price = None 
            result = self.market_order(order_info)
            results.append(result)
            
            # 동기식 sleep 사용
            if split_count > 1:
                time.sleep(10)

        return results
    
    # 11. market_entry: 동기 함수 (선물 진입)
    def market_entry(self, order_info: MarketOrder):
        from exchange.pexchange import retry # 동기 retry 함수 사용

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
                None, # 가격은 None (시장가)
                params,
                order_info=order_info,
                max_attempts=10,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)

    # 12. market_close: 동기 함수 (선물 청산)
    def market_close(self, order_info: MarketOrder):
        from exchange.pexchange import retry # 동기 retry 함수 사용

        symbol = order_info.unified_symbol
        close_amount = self.get_amount(order_info)

        # 단방향 모드(one-way)에서는 reduceOnly: True
        params = {"reduceOnly": True} if self.position_mode == "one-way" else {}
        
        # 양방향 모드(hedge)에서는 positionSide 지정
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
                None, # 가격은 None (시장가)
                params,
                order_info=order_info,
                max_attempts=10,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)
            
    # 13. get_listen_key: 동기 함수 (웹소켓 리스닝 키 조회)
    def get_listen_key(self):
        # httpx 동기 호출 가정
        url = "https://fapi.binance.com/fapi/v1/listenKey"
        # CCXT 동기 API를 사용하는 것이 가장 좋습니다.
        # listenkey = self.client.fapiPrivatePostListenKey()["listenKey"]
        
        # 원본 구조 유지하며 동기 호출 가정
        listenkey = (httpx.post(url, headers={"X-MBX-APIKEY": self.client.apiKey})).json()["listenKey"]
        return listenkey
        
    # 14. get_trades: 동기 함수
    def get_trades(self):
        if self.order_info and self.order_info.is_futures:
            trades = self.client.fetch_my_trades()
            print(trades)
        
    # 15. get_order: 동기 함수
    def get_order(self, order_id: str):
        return self.client.fetch_order(order_id)

    # 16. get_order_amount: 동기 함수
    def get_order_amount(self, order_id: str):
        return self.get_order(order_id)["filled"]

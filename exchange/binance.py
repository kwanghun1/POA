from exchange.pexchange import ccxt, httpx # 동기식 CCXT 클라이언트 사용
from devtools import debug
from exchange.model import MarketOrder
import exchange.error as error
import asyncio # 비동기 함수 구조 유지를 위해 필요

class Binance:
    def __init__(self, key, secret):
        # 동기 CCXT 클라이언트 사용 (현물만 사용하므로 defaultType은 spot)
        self.client = ccxt.binance(
            {
                "apiKey": key,
                "secret": secret,
                "options": {"adjustForTimeDifference": True},
                "defaultType": "spot", # 현물 전용으로 설정
            }
        )
        self.client.load_markets() # 초기 마켓 로드
        self.order_info: MarketOrder = None

    # 1. init_info: 동기 함수 (선물 로직 제거)
    def init_info(self, order_info: MarketOrder):
        self.order_info = order_info
        
        if order_info.amount is not None:
            # 수량 정밀도 적용
            order_info.amount = float(
                self.client.amount_to_precision(order_info.unified_symbol, order_info.amount)
            )
        
        # 현물 전용으로 고정
        self.client.options["defaultType"] = "spot"
        
        # 현물 거래에서는 아래 선물 관련 필드들을 사용하지 않도록 강제 (모델이 허용한다면)
        order_info.is_futures = False
        order_info.is_coinm = False
        order_info.is_contract = False
        order_info.leverage = None

    # 2. get_ticker: 동기 함수
    def get_ticker(self, symbol: str):
        return self.client.fetch_ticker(symbol)

    # 3. get_price: 동기 함수
    def get_price(self, symbol: str):
        return (self.get_ticker(symbol))["last"]

    # 4. get_futures_position: 선물 전용 함수이므로 제거 (주문 함수에서 직접 잔고 조회 사용)
    # 5. get_balance: 동기 함수 (현물 잔고 조회만 남음)
    def get_balance(self, base: str) -> float:
        free_balance_by_base = None

        # 현물 매수/매도 시 잔고 조회
        if self.order_info.is_buy or self.order_info.is_sell:
            # 현물 잔고 조회
            free_balance = (self.client.fetch_free_balance() if not self.order_info.is_total else self.client.fetch_total_balance())
            free_balance_by_base = free_balance.get(base)

        if free_balance_by_base is None or free_balance_by_base == 0:
            raise error.FreeAmountNoneError()
        return free_balance_by_base

    # 6. get_amount: 동기 함수 (주문 수량 계산, 현물 로직만 남김)
    def get_amount(self, order_info: MarketOrder) -> float:
        if order_info.amount is not None and order_info.percent is not None:
            raise error.AmountPercentBothError()
        
        elif order_info.amount is not None:
            # 현물 수량 그대로 사용
            result = order_info.amount
            
        elif order_info.percent is not None:
            if order_info.is_buy:
                # 현물 매수 시: 쿼트 코인(USDT 등) 잔고 기반 계산
                free_quote = self.get_balance(order_info.quote)
                current_price = self.get_price(order_info.unified_symbol)
                # 안전 마진(0.5%)을 적용하지 않는 단순 계산으로 변경
                result = free_quote * order_info.percent / 100 / current_price
            
            elif order_info.is_sell:
                # 현물 매도 시: 베이스 코인(BTC 등) 잔고 기반 계산
                free_amount = self.get_balance(order_info.base)
                result = free_amount * order_info.percent / 100
            
            else:
                raise error.AmountPercentNoneError()

            result = float(self.client.amount_to_precision(order_info.unified_symbol, result))
            order_info.amount_by_percent = result
            
        else:
            raise error.AmountPercentNoneError()

        return result

    # 7. set_leverage: 선물 전용 함수이므로 제거

    # 8. market_order: 동기 함수 (주문 실행 공통 로직)
    def market_order(self, order_info: MarketOrder):
        from exchange.pexchange import retry 

        params = {}
        try:
            return retry(
                self.client.create_order,
                order_info.unified_symbol,
                order_info.type.lower(),
                order_info.side,
                order_info.amount,
                None, # 시장가 주문이므로 price는 None
                params,
                order_info=order_info,
                max_attempts=5,
                delay=0.1,
                instance=self,
            )
        except Exception as e:
            raise error.OrderError(e, self.order_info)

    ## 9. market_buy: 비동기 함수 (현물 매수)
    async def market_buy(self, order_info: MarketOrder):
        # 1. 주문 수량 계산
        order_info.amount = self.get_amount(order_info)
        
        # 2. 시장가 주문
        order_info.price = None
        order_info.side = "buy" # side를 명시적으로 설정

        # 3. 주문 실행
        return self.market_order(order_info)

    ## 10. market_sell: 비동기 함수 (현물 매도)
    async def market_sell(self, order_info: MarketOrder):
        # 1. 주문 수량 계산
        order_info.amount = self.get_amount(order_info)
        
        # 2. 시장가 주문
        order_info.price = None
        order_info.side = "sell" # side를 명시적으로 설정
        
        # 3. 주문 실행
        return self.market_order(order_info)
    
    # 11. market_entry: 선물 전용 함수이므로 제거
    # 12. market_close: 선물 전용 함수이므로 제거
            
    # 13. get_listen_key: 동기 함수 (선물 대신 현물 웹소켓 사용이 필요할 수 있으나, 기존 코드 유지)
    def get_listen_key(self):
        # 현물(Spot) listenKey URL로 변경하는 것이 정확하나, 기존 fapi URL을 유지합니다.
        url = "https://api.binance.com/api/v3/userDataStream" # 현물 listenKey URL
        try:
            listenkey = (httpx.post(url, headers={"X-MBX-APIKEY": self.client.apiKey})).json()["listenKey"]
        except Exception:
             # 현물 API키가 아닌 경우 fapi(선물) API 키를 사용해야 할 수 있으므로 예외 처리
             url = "https://fapi.binance.com/fapi/v1/listenKey"
             listenkey = (httpx.post(url, headers={"X-MBX-APIKEY": self.client.apiKey})).json()["listenKey"]

        return listenkey
        
    # 14. get_trades: 동기 함수 (선물 관련 조건 제거)
    def get_trades(self):
        trades = self.client.fetch_my_trades()
        print(trades)
        
    # 15. get_order: 동기 함수
    def get_order(self, order_id: str):
        return self.client.fetch_order(order_id)

    # 16. get_order_amount: 동기 함수
    def get_order_amount(self, order_id: str):
        return self.get_order(order_id)["filled"]

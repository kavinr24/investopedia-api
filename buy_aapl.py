
from investopedia_api import TradeAPI

with TradeAPI(headless=False) as api:
    result = api.place_order("AAPL", 1, "buy")
    print(result)

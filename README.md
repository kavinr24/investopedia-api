# investopedia-api

```
from investopedia_api import TradeAPI

    api = TradeAPI(headless=True)
    portfolio = api.get_portfolio()
    api.place_order("AAPL", 10, "buy")
    api.close()
```

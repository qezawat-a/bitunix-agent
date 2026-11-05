<!-- Bitunix doc: /futures/trade/get_pending_orders | source: https://www.bitunix.com/api-docs/futures/trade/get_pending_orders.html -->
# Get Pending Orders

# Get Pending Orders [​](#get-pending-orders)

Rate Limit: 10 req/sec/uid

### Description [​](#description)

get Pending orders, sort by create time desc

### HTTP Request [​](#http-request)

- GET /api/v1/futures/trade/get\_pending\_orders

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| symbol | string | false | Trading pair |
| orderId | string | false | order id |
| clientId | string | false | client id |
| status | string | false | Order status **NEW** or **PART\_FILLED** |
| startTime | int64 | false | Start timestamp Unix timestamp in milliseconds format, e.g. 1597026383085 |
| endTime | int64 | false | Start timestamp Unix timestamp in milliseconds format, e.g. 1597026683085 |
| skip | int64 | false | skip order count default: 0 |
| limit | int64 | false | Number of queries: Maximum: 100, default: 10 |

Request Example

bash

```
curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/futures/trade/get_pending_orders?symbol=BTCUSDT' \\
 -H "api-key:*******" \\
 -H "sign:*" \\
 -H "nonce:your-nonce" \\
 -H "timestamp:1659076670000" \\
 -H "language:en-US" \\
 -H "Content-Type: application/json"
```

### Response Parameters [​](#response-parameters)

| Parameter | Type | Description |
| --- | --- | --- |
| orderList | list | order list |
| >orderId | string | order id |
| >symbol | string | Trading pair |
| >qty | string | Amount (base coin) |
| >tradeQty | string | Fill amount (base coin) |
| >positionMode | string | ONE\_WAY or HEDGE |
| >marginMode | string | ISOLATION or CROSS |
| >leverage | int | leverage |
| >price | string | Price of the order. Required if the order type is **LIMIT** |
| >side | string | Order direction buy: **BUY** sell: **SELL** |
| >orderType | string | Order type **LIMIT**: limit orders **MARKET**: market orders |
| >effect | string | Order expiration date. Required if the orderType is limit **IOC**: Immediate or cancel **FOK**: Fill or kill **GTC**: Good till canceled(default value) **POST\_ONLY**: POST only |
| >clientId | string | Customize order ID |
| >reduceOnly | boolean | Whether or not to just reduce the position |
| >status | string | **INIT**:prepare status **NEW**:pending **PART\_FILLED**:partially filled **CANCELED**:canceled **FILLED**All filled |
| >fee | string | fee |
| >realizedPNL | string | realized pnl |
| >tpPrice | string | take profit trigger price |
| >tpStopType | string | take profit trigger type **MARK\_PRICE** **LAST\_PRICE** |
| >tpOrderType | string | take profit trigger place order type **LIMIT** **MARKET** |
| >tpOrderPrice | string | take profit trigger place order price **LIMIT** **MARKET** required if tpOrderType is **LIMIT** |
| >slPrice | string | stop loss trigger price |
| >slStopType | string | stop loss trigger type **MARK\_PRICE** **LAST\_PRICE** |
| >slOrderType | string | stop loss trigger place order type **LIMIT** **MARKET** |
| >slOrderPrice | string | stop loss trigger place order price **LIMIT** **MARKET** required if slOrderType is **LIMIT** |
| >ctime | int64 | create timestamp |
| >mtime | int64 | latest modify timestamp |
| total | int64 | total count |

Response Example

json

```
{"code":0,"data":{"orderList":[{"orderId":"11111","qty":"1","tradeQty":"0.5","price":"60000","symbol":"BTCUSDT","positionMode":"HEDGE","marginMode":"ISOLATION","leverage":15,"status":"NEW","fee":"0.01","realizedPNL":"1.78","type":"LIMIT","effect":"GTC","reduceOnly":false,"clientId":"22222","tpPrice":"61000","tpStopType":"MARK","tpOrderType":"LIMIT","tpOrderPrice":"61000.1","slPrice":"59000","slStopType":"MARK","slOrderType":"LIMIT","slOrderPrice":"59000.1","source":"api","ctime":1597026383085,"mtime":1597026383085}],"total":10},"msg":"Success"}
```

`,14

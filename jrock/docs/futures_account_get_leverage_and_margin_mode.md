<!-- Bitunix doc: /futures/account/get_leverage_and_margin_mode | source: https://www.bitunix.com/api-docs/futures/account/get_leverage_and_margin_mode.html -->
# Get Leverage and Margin Mode

# Get Leverage and Margin Mode [​](#get-leverage-and-margin-mode)

Rate Limit: 10 req/sec/uid

### Description [​](#description)

get Leverage and Margin Mode

### HTTP Request [​](#http-request)

- GET /api/v1/futures/account/get\_leverage\_margin\_mode

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| symbol | string | true | Trading pair |
| marginCoin | string | true | Margin coin |

Request Example

bash

```
curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/futures/account/get_leverage_margin_mode?symbol=BTCUSDT&marginCoin=USDT' \\
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
| symbol | string | Trading pair |
| marginCoin | string | Margin coin |
| leverage | int | leverage |
| marginMode | string | **ISOLATION** or **CROSS** |

Response Example

json

```
{"code":0,"data":{"symbol":"BTCUSDT","marginCoin":"USDT","leverage":10,"marginMode":"ISOLATION"},"msg":"Success"}
```

`,14

<!-- Bitunix doc: /futures/account/change_leverage | source: https://www.bitunix.com/api-docs/futures/account/change_leverage.html -->
# Change Leverage

# Change Leverage [​](#change-leverage)

Rate Limit: 10 req/sec/uid

### Description [​](#description)

Adjust the leverage on the given symbol

### HTTP Request [​](#http-request)

- POST /api/v1/futures/account/change\_leverage

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| marginCoin | string | true | Margin coin |
| symbol | string | true | Trading pair |
| leverage | int | true | Leverage |

Request Example

bash

```
curl -X 'POST' --location 'https://fapi.bitunix.com/api/v1/futures/account/change_leverage' \\
 -H "api-key:*******" \\
 -H "sign:*" \\
 -H "nonce:your-nonce" \\
 -H "timestamp:1659076670000" \\
 -H "language:en-US" \\
 -H "Content-Type: application/json" \\
 --data '{"symbol":"BTCUSDT","leverage":12,"marginCoin":"USDT"}'
```

### Response Parameters [​](#response-parameters)

| Parameter | Type | Description |
| --- | --- | --- |
| marginCoin | string | Margin coin |
| symbol | string | Trading pair |
| leverage | int | Leverage |

Response Example

json

```
{"code":0,"data":[{"marginCoin":"USDT","leverage":12,"symbol":"BTCUSDT"}],"msg":"Success"}
```

`,14

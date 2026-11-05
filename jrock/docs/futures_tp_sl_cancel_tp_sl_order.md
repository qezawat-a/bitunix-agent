<!-- Bitunix doc: /futures/tp_sl/cancel_tp_sl_order | source: https://www.bitunix.com/api-docs/futures/tp_sl/cancel_tp_sl_order.html -->
# Cancel TP/SL Order

# Cancel TP/SL Order [​](#cancel-tp-sl-order)

Rate Limit: 10 req/sec/UID

### Description [​](#description)

Cancel TP/SL Order 
Successful interface response is not necessarily equal to the success of the operation, please use the websocket push message as an accurate judgment of the success of the operation.

### HTTP Request [​](#http-request)

- POST /api/v1/futures/tpsl/cancel\_order

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| symbol | string | true | coin Pair |
| orderId | string | true | TP/SL Order ID |

Request Example

bash

```
curl -X 'POST' --location 'https://fapi.bitunix.com/api/v1/futures/tpsl/cancel_order' \\
 -H "api-key:*******" \\
 -H "sign:*" \\
 -H "nonce:your-nonce" \\
 -H "timestamp:1659076670000" \\
 -H "language:en-US" \\
 -H "Content-Type: application/json" \\
 --data '{"symbol":"BTCUSDT","orderId":"12"}'
```

### Response Parameters [​](#response-parameters)

| Parameter | Type | Description |
| --- | --- | --- |
| orderId | string | TP/SL Order ID |

Response Example

json

```
{"code":0,"data":{"orderId":"11111"},"msg":"Success"}
```

`,14

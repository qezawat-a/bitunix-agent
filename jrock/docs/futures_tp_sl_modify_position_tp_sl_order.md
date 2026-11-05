<!-- Bitunix doc: /futures/tp_sl/modify_position_tp_sl_order | source: https://www.bitunix.com/api-docs/futures/tp_sl/modify_position_tp_sl_order.html -->
# Modify Position TP/SL Order

# Modify Position TP/SL Order [​](#modify-position-tp-sl-order)

Rate Limit: 20 req/sec/UID

### Description [​](#description)

Modify Position TP/SL Order

### HTTP Request [​](#http-request)

- POST /api/v1/futures/tpsl/position/modify\_order

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| symbol | string | true | Trading pair |
| positionId | string | true | Position ID associated with take-profit and stop-loss |
| tpPrice | string | false | Take-profit trigger price At least one of `tpPrice` or `slPrice` is required. |
| tpStopType | string | false | Take-profit trigger type LAST\_PRICE MARK\_PRICE Default is market price. |
| slPrice | string | false | Stop-loss trigger price At least one of `tpPrice` or `slPrice` is required. |
| slStopType | string | false | Stop-loss trigger type LAST\_PRICE MARK\_PRICE Default is market price. |

Request Example

bash

```
curl -X 'POST' --location 'https://fapi.bitunix.com/api/v1/futures/tpsl/position/modify_order' \\
 -H "api-key:*******" \\
 -H "sign:*" \\
 -H "nonce:your-nonce" \\
 -H "timestamp:1659076670000" \\
 -H "language:en-US" \\
 -H "Content-Type: application/json" \\
 --data '{"symbol":"BTCUSDT","positionId":"11","tpPrice":"12","tpStopType":"LAST_PRICE","slPrice":"9","slStopType":"LAST_PRICE"}'
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

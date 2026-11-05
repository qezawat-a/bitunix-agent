<!-- Bitunix doc: /futures/account/adjust_position_margin | source: https://www.bitunix.com/api-docs/futures/account/adjust_position_margin.html -->
# Adjust Position Margin

{"title":"Adjust Position Margin","description":"","frontmatter":{"slug":"adjust\_position\_margin","title":"Adjust Position Margin"},"headers":[],"relativePath":"futures/account/adjust\_position\_margin.md","filePath":"account/adjust\_position\_margin.md"}'),n={name:"futures/account/adjust\_position\_margin.md"};function l(r,t,h,o,d,p){return i(),a("div",null,t[0]||(t[0]=[e(`

# Adjust Position Margin [​](#adjust-position-margin)

Rate Limit: 5 req/sec/uid

### Description [​](#description)

Add or reduce the margin（only for isolated margin mode）

### HTTP Request [​](#http-request)

- POST /api/v1/futures/account/adjust\_position\_margin

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| symbol | string | true | Trading pair |
| marginCoin | string | true | Margin coin |
| amount | string | true | Margin amount, positive means increase, and negative means decrease |
| side | string | false | Position side **LONG** **SHORT** Either side or positionId required |
| positionId | string | false | Position id Either side or positionId required |

Request Example

bash

```
curl -X 'POST' --location 'https://fapi.bitunix.com/api/v1/futures/account/adjust_position_margin' \\
 -H "api-key:*******" \\
 -H "sign:*" \\
 -H "nonce:your-nonce" \\
 -H "timestamp:1659076670000" \\
 -H "language:en-US" \\
 -H "Content-Type: application/json" \\
 --data '{"symbol":"BTCUSDT","amount":"-100","marginCoin":"USDT","side":"LONG"}'
```

### Response Parameters [​](#response-parameters)

| Parameter | Type | Description |
| --- | --- | --- |
| N/A | | |

Response Example

json

```
{"code":0,"data":"","msg":"Success"}
```

`,14

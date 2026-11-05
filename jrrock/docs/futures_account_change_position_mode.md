<!-- Bitunix doc: /futures/account/change_position_mode | source: https://www.bitunix.com/api-docs/futures/account/change_position_mode.html -->
# Change Position Mode

{"title":"Change Position Mode","description":"","frontmatter":{"slug":"change\_position\_mode","title":"Change Position Mode"},"headers":[],"relativePath":"futures/account/change\_position\_mode.md","filePath":"account/change\_position\_mode.md"}'),n={name:"futures/account/change\_position\_mode.md"};function h(o,s,l,p,r,d){return t(),a("div",null,s[0]||(s[0]=[e(`

# Change Position Mode [​](#change-position-mode)

Rate Limit: 10 req/sec/uid

### Description [​](#description)

Adjust the position mode between 'one way mode' and 'hedge mode'

If you want to change the user's position mode on all symbol contracts, you need to specify hedge mode positions or one-way positions. Note: The position mode can't be adjusted when there is an open position order under the product type. Changes the user's position mode for all symbol futures: hedging mode or one-way mode.When users hold positions or orders on any side of any trading pair in the specific product type, the request may fail.

### HTTP Request [​](#http-request)

- POST /api/v1/futures/account/change\_position\_mode

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| positionMode | string | true | Position Mode **ONE\_WAY** **HEDGE** |

Request Example

bash

```
curl -X 'POST' --location 'https://fapi.bitunix.com/api/v1/futures/account/change_position_mode' \\
 -H "api-key:*******" \\
 -H "sign:*" \\
 -H "nonce:your-nonce" \\
 -H "timestamp:1659076670000" \\
 -H "language:en-US" \\
 -H "Content-Type: application/json" \\
 --data '{"positionMode":"HEDGE"}'
```

### Response Parameters [​](#response-parameters)

| Parameter | Type | Description |
| --- | --- | --- |
| positionMode | string | Position Mode **ONE\_WAY** **HEDGE** |

Response Example

json

```
{"code":0,"data":[{"positionMode":"HEDGE"}],"msg":"Success"}
```

json

```
{"code":200014,"data":null,"msg":"Existing positions or orders, unable to switch holding mode."}
```

`,16

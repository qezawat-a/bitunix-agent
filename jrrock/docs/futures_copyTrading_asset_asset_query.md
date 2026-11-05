<!-- Bitunix doc: /futures/copyTrading/asset/asset_query | source: https://www.bitunix.com/api-docs/futures/copyTrading/asset/asset_query.html -->
# Asset Query

{"title":"Asset Query","description":"","frontmatter":{"slug":"asset\_query","title":"Asset Query"},"headers":[],"relativePath":"futures/copyTrading/asset/asset\_query.md","filePath":"copyTrading/asset/asset\_query.md"}'),n={name:"futures/copyTrading/asset/asset\_query.md"};function l(h,s,r,p,k,d){return a(),i("div",null,s[0]||(s[0]=[e(`

# Asset Query [​](#asset-query)

Rate Limit: 10 req/sec/uid

### Description [​](#description)

Interface is used to asset query.

### HTTP Request [​](#http-request)

- GET /api/v1/cp/asset/query

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |

Request Example

bash

```
curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/cp/asset/query' \\
 -H "api-key:*******" \\
 -H "sign:*" \\
 -H "timestamp:1659076670000" \\
 -H "nonce:your-nonce" \\
 -H "language:en-US"
```

### Response Parameters [​](#response-parameters)

| Parameter | Type | Description |
| --- | --- | --- |
| available | string | Futures available |
| maxTransfer | string | Maximum transfer amount |

Response Example

json

```
{"code":0,"msg":"result.success","data":{"available":"54.20916","maxTransfer":"52.20916"},"success":true}
```

`,14

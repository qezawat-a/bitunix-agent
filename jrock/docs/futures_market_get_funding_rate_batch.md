<!-- Bitunix doc: /futures/market/get_funding_rate_batch | source: https://www.bitunix.com/api-docs/futures/market/get_funding_rate_batch.html -->
# Get Funding Rate

# Get Funding Rate [​](#get-funding-rate)

Rate Limit: 10 req/sec/ip

### Description [​](#description)

Get the current funding rate of the contract

### HTTP Request [​](#http-request)

- GET /api/v1/futures/market/funding\_rate/batch

Request Example

bash

```
curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/futures/market/funding_rate/batch'
```

### Response Parameters [​](#response-parameters)

| Parameter | Type | Description |
| --- | --- | --- |
| symbol | string | Coin pair |
| markPrice | decimal | mark price |
| lastPrice | decimal | last price |
| indexPrice | decimal | index price |
| fundingRate | decimal | Current funding rates |
| nextFundingTime | int64 | next Funding settlement time(ms) |
| fundingInterval | int32 | funding settlement interval(hour) |
| maxFundingRate | decimal | Max Current Funding Rate |
| minFundingRate | decimal | Min Current Funding Rate |

Response Example

json

```
{"code":0,"data":[{"symbol":"BTCUSDT","markPrice":"60000","lastPrice":"60001","indexPrice":"60001","fundingRate":"0.0005","fundingInterval":8,"nextFundingTime":"1770710400000","maxFundingRate":"0.3","minFundingRate":"-0.3"}],"msg":"Success"}
```

',12

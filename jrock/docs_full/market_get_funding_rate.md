<!-- Bitunix official doc: /market/get_funding_rate | source: https://www.bitunix.com/api-docs/futures/market/get_funding_rate.html -->

# get_funding_rate

Get Funding Rate ​ 
 Rate Limit: 10 req/sec/ip
 Description ​ 
 Get the current funding rate of the contract
 HTTP Request ​ 
 GET /api/v1/futures/market/funding_rate
 Request Parameters ​ 
 Parameter Type Required Description 
 symbol string true Trading pair, based on the symbolName, i.e. BTCUSDT 
 Request Example
 bash curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/futures/market/funding_rate?symbol=BTCUSDT' 
 Response Parameters ​ 
 Parameter Type Description 
 symbol string Coin pair 
 markPrice decimal mark price 
 lastPrice decimal last price 
 indexPrice decimal index price 
 fundingRate decimal Current funding rates 
 nextFundingTime int64 next Funding settlement time(ms) 
 fundingInterval int32 funding settlement interval(hour) 
 maxFundingRate decimal Max Current Funding Rate 
 minFundingRate decimal Min Current Funding Rate 
 Response Example
 json { "code" : 0 , "data" :[{ "symbol" : "BTCUSDT" , "markPrice" : "60000" , "lastPrice" : "60001" , "indexPrice" : "60001" , "fundingRate" : "0.0005" , "fundingInterval" : 8 , "nextFundingTime" : "1770710400000" , "maxFundingRate" : "0.3" , "minFundingRate" : "-0.3" }], "msg" : "Success" }

<!-- Bitunix official doc: /market/get_funding_rate_history | source: https://www.bitunix.com/api-docs/futures/market/get_funding_rate_history.html -->

# get_funding_rate_history

Get Funding Rate History ​ 
 Rate Limit: 10 req/sec/ip
 Description ​ 
 Get the history funding rate of the contract
 HTTP Request ​ 
 GET /api/v1/futures/market/get_funding_rate_history
 Request Parameters ​ 
 Parameter Type Required Description 
 symbol string true Trading pair, based on the symbolName, i.e. BTCUSDT 
 startTime int64 false Start timestamp(Funding settle time)
Unix timestamp in milliseconds format, e.g. 1597026383085 
 endTime int64 false End timestamp(Funding settle time)
Unix timestamp in milliseconds format, e.g. 1597026383085 
 limit int32 false Default: 100,Maximum: 200 
 Request Example
 bash curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/futures/market/get_funding_rate_history?symbol=BTCUSDT&limit=10' 
 Response Parameters ​ 
 Parameter Type Description 
 markPrice string Mark price 
 fundingRate string Funding rate 
 fundingTime int64 Funding timestamp 
 Response Example
 json { "code" : 0 , "data" :[{ "fundingRate" : "-0.00001191" , "fundingTime" : "1772449200000" , "markPrice" : "66286.6" }], "msg" : "Success" }

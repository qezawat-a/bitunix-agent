<!-- Bitunix official doc: /account/get_leverage_and_margin_mode | source: https://www.bitunix.com/api-docs/futures/account/get_leverage_and_margin_mode.html -->

# get_leverage_and_margin_mode

Get Leverage and Margin Mode ​ 
 Rate Limit: 10 req/sec/uid
 Description ​ 
 get Leverage and Margin Mode
 HTTP Request ​ 
 GET /api/v1/futures/account/get_leverage_margin_mode
 Request Parameters ​ 
 Parameter Type Required Description 
 symbol string true Trading pair 
 marginCoin string true Margin coin 
 Request Example
 bash curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/futures/account/get_leverage_margin_mode?symbol=BTCUSDT&marginCoin=USDT' \ 
 -H "api-key:*******" \ 
 -H "sign:*" \ 
 -H "nonce:your-nonce" \ 
 -H "timestamp:1659076670000" \ 
 -H "language:en-US" \ 
 -H "Content-Type: application/json" 
 Response Parameters ​ 
 Parameter Type Description 
 symbol string Trading pair 
 marginCoin string Margin coin 
 leverage int leverage 
 marginMode string ISOLATION or CROSS 
 Response Example
 json { "code" : 0 , "data" :{ "symbol" : "BTCUSDT" , "marginCoin" : "USDT" , "leverage" : 10 , "marginMode" : "ISOLATION" }, "msg" : "Success" }

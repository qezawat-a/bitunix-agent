<!-- Bitunix official doc: /account/get_trading_settings | source: https://www.bitunix.com/api-docs/futures/account/get_trading_settings.html -->

# get_trading_settings

Get Trading Settings ​ 
 Rate Limit: 20 req/sec/uid
 Description ​ 
 Get the user's futures trading settings (margin mode and leverage) by symbol.
If symbols is omitted, all configured trading settings for the user are returned.
If symbols is provided, settings for the specified symbols are returned (comma-separated, case-insensitive, max 50 symbols).
 HTTP Request ​ 
 GET /api/v1/futures/account/trading_settings
 Request Parameters ​ 
 Parameter Type Required Description 
 symbols string false Trading pair symbols, comma-separated (e.g. BTCUSDT,ETHUSDT ). Case-insensitive. Max 50 symbols. If omitted, returns all user trading settings. 
 Request Example
 bash curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/futures/account/trading_settings?symbols=BTCUSDT,ETHUSDT' \ 
 -H "api-key:*******" \ 
 -H "sign:*" \ 
 -H "nonce:your-nonce" \ 
 -H "timestamp:1659076670000" \ 
 -H "language:en-US" \ 
 -H "Content-Type: application/json" 
 Response Parameters ​ 
 Parameter Type Description 
 code integer Response code. 0 means success 
 msg string Response message 
 data list Trading settings list 
 >symbol string Trading pair symbol 
 >marginCoin string Margin coin (settlement coin) 
 >marginMode string Margin mode 
 CROSS : cross margin 
 ISOLATION : isolated margin 
 >leverage int Leverage (legacy / one-way field) 
 >longLeverage int Long leverage in hedge mode 
 >shortLeverage int Short leverage in hedge mode 
 Response Example
 json { "code" : 0 , "data" :[{ "symbol" : "BTCUSDT" , "marginCoin" : "USDT" , "marginMode" : "CROSS" , "leverage" : 20 , "longLeverage" : 20 , "shortLeverage" : 20 },{ "symbol" : "ETHUSDT" , "marginCoin" : "USDT" , "marginMode" : "ISOLATION" , "leverage" : 10 , "longLeverage" : 10 , "shortLeverage" : 10 }], "msg" : "Success" }

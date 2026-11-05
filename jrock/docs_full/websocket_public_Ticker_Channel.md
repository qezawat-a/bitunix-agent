<!-- Bitunix official doc: /websocket/public/Ticker Channel | source: https://www.bitunix.com/api-docs/futures/websocket/public/Ticker%20Channel.html -->

# Ticker Channel

Description ​ 
 24hr rolling window mini-ticker statistics for all symbols. These are NOT the statistics of the UTC day, but a 24hr rolling window from requestTime to 24hrs before.
 Request Parameters ​ 
 Parameter Type Required Description 
 op String Yes Operation, subscribe unsubscribe 
 args List<Object> Yes 
 > symbol String Yes Product ID e.g: ETHUSDT 
 > ch String Yes Channel, ticker 
 request example:
 json { 
 "op" : "subscribe" , 
 "args" :[ 
 { 
 "symbol" : "BTCUSDT" , 
 "ch" : "ticker" 
 } 
 ] 
 } 
 Push Parameters ​ 
 Parameter Type Description 
 ch String Channel name 
 symbol String Product ID E.g. ETHUSDT 
 ts int64 Time stamp 
 data List<String> Subscription data 
 > s String Symbol, Product ID E.g. ETHUSDT 
 > o String Opening price 
 > h String Highest price 
 > l String Lowest price 
 > la String Last price 
 > b String Trading volume of the coin 
 > q String Trading volume of quote currency 
 > r String 24-hour fluctuations 
 push data:
 json { 
 "ch" : "ticker" , 
 "symbol" : "BNBUSDT" , 
 "ts" : 1732178884994 , 
 "data" :{ 
 "s" : "BTCUSDT" , 
 "la" : "68650.9" , 
 "o" : "69141.6" , 
 "h" : "70319.9" , 
 "l" : "68241.9" , 
 "b" : "26295.3977" , 
 "q" : "1823374525.0193" , 
 "r" : "-0.7097029863" 
 } 
 }

<!-- Bitunix official doc: /websocket/public/Kline channel | source: https://www.bitunix.com/api-docs/futures/websocket/public/kline%20channel.html -->

# Kline channel

Description ​ Retrieve the candlesticks data of a symbol. Data will be pushed every 500 ms. The channel will push a snapshot after successful subscription, followed by subsequent updates. To switch K-line intervals without disconnecting the WebSocket, you must first send an "unsubscribe" command to cancel the previous K-line subscription before subscribing to the new interval. For example, if you are currently subscribed to mark_kline_1min and want to switch to mark_kline_15min , you must first unsubscribe from mark_kline_1min and then subscribe to mark_kline_15min . Request Parameters ​ Parameter Type Required Description op String Yes Operation, subscribe unsubscribe args List<Object> Yes List of channels to request subscription > ch String Yes Channel name, The subscription channel is: Price Type klineTime Interval；The price types include market price and marked price；market _ kline _ 1min,mark _ kline _ 1min,market _ kline _ 3min,mark _ kline _ 3min,market _ kline _ 5min,mark _ kline _ 5min,market _ kline _ 15min,mark _ kline _ 15min,market _ kline _ 30min,mark _ kline _ 30min,market _ kline _ 60min,mark _ kline _ 60min,market _ kline _ 2h,mark _ kline _ 2h,market _ kline _ 4h,mark _ kline _ 4h,market _ kline _ 6h,mark _ kline _ 6h,market _ kline _ 8h,mark _ kline _ 8h,market _ kline _ 12h,mark _ kline _ 12h,market _ kline _ 1day,mark _ kline _ 1day,market _ kline _ 3day,mark _ kline _ 3day,market _ kline _ 1week,mark _ kline _ 1week,market _ kline _ 1month,mark _ kline _ 1month > symbol String Yes Product ID E.g. ETHUSDT request example: json { 
 "op" : "subscribe" , 
 "args" :[ 
 { 
 "symbol" : "BTCUSDT" , 
 "ch" : "market_kline_1min" 
 } 
 ] 
 } Push Parameters ​ Parameter Type Description ch String Channel name symbol String Product ID E.g. ETHUSDT ts int64 Time stamp data List<String> Subscription data > o String Opening price > h String Highest price > l String Lowest price > c String Closing price > b String Trading volume of the coin > q String Trading volume of quote currency push data: json { 
 "ch" : "market_kline_1min" , 
 "symbol" : "BTCUSDT" , 
 "ts" : 1775541412718 , 
 "data" :{ 
 "o" : "68581.4" , 
 "c" : "68583.4" , 
 "h" : "68590" , 
 "l" : "68579.5" , 
 "b" : "5.2395" , 
 "q" : "359348.14078" 
 } 
 }

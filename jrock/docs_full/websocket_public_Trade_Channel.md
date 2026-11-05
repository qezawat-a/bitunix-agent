<!-- Bitunix official doc: /websocket/public/Trade Channel | source: https://www.bitunix.com/api-docs/futures/websocket/public/Trade%20Channel.html -->

# Trade Channel

Description ​ 
 Get the public trade data
 Request Parameters ​ 
 Parameter Type Required Description 
 op String Yes Operation, subscribe unsubscribe 
 args List<Object> Yes op list 
 > symbol String Yes Product ID e.g: ETHUSDT 
 > ch String Yes Channel, trade 
 request example:
 json { 
 "op" : "subscribe" , 
 "args" :[ 
 { 
 "symbol" : "BTCUSDT" , 
 "ch" : "trade" 
 } 
 ] 
 } 
 Push Parameters ​ 
 Parameter Type Description 
 ch String Channel, trade 
 symbol String Symbol: ETHUSDT 
 ts String Time stamp 
 data List<Object> Data 
 > p String Filled price 
 > v String Filled amount 
 > s String Filled side, sell/buy 
 > t String Time stamp 
 push data:
 json { 
 "ch" : "trade" , 
 "symbol" : "BTCUSDT" , 
 "ts" : 1775540872598 , 
 "data" : [ 
 { 
 "t" : "2026-04-07T05:47:52Z" , 
 "p" : "68621.4" , 
 "v" : "0.7142" , 
 "s" : "buy" 
 }, 
 { 
 "t" : "2026-04-07T05:47:52Z" , 
 "p" : "68621.4" , 
 "v" : "0.0018" , 
 "s" : "sell" 
 } 
 ] 
 }

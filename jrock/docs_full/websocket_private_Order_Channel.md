<!-- Bitunix official doc: /websocket/private/Order Channel | source: https://www.bitunix.com/api-docs/futures/websocket/private/Order%20Channel.html -->

# Order Channel

Description ​ 
 Subscribe the order channel
 Data will be pushed when the following events occured:
 Open/Close orders are created
 Open/Close orders are filled
 Orders canceled
 Push Parameters ​ 
 Parameter Type Description 
 ch String Channel name: order 
 ts Int64 Gateway send time in milliseconds. Not business event time; do not confuse with ctime / mtime . 
 data <Object> Subscription data 
 > event String Event，CREATE/UPDATE/CLOSE 
 > orderId String order Id 
 > symbol String Symbol 
 > positionType String Margin mode,ISOLATION/CROSS 
 > positionMode String Position mode,ONE_WAY/HEDGE 
 > side String Sell,BUY/SELL 
 > effect String Order expiration date.Required if the orderType is limit.IOC: Immediate or cancel;FOK: Fill or kill;GTC: Good till canceled(default value);POST_ONLY: POST only; 
 > type String LIMIT/MARKET 
 > qty String Amount (base coin) 
 > price String Price of the order. Required if the order type is LIMIT 
 > ctime String Create time, ISO-8601 nanosecond string, e.g. 2024-05-16T08:13:09.123456789Z 
 > mtime String Last modify time, ISO-8601 nanosecond string. Same format as ctime . REST order APIs use millisecond integers. 
 > leverage String Leverage 
 > orderStatus String INIT:prepare status;NEW:pending;PART_FILLED:partially filled;CANCELED:canceled;FILLEDAll filled;PART_FILLED_CANCELED:part filled canceled; 
 > fee String Deducted transaction fees: transaction fees deducted during the position 
 > averagePrice String Average price 
 > dealAmount String Deal amount 
 > clientId String Client ID 
 > tpStopType String Take profit trigger type.MARK_PRICE/LAST_PRICE 
 > tpPrice String Take profit trigger price 
 > tpOrderType String Take profit trigger place order type.LIMIT/MARKET 
 > tpOrderPrice String Take profit trigger place order price.LIMIT/MARKET 
 > slStopType String Stop loss trigger type.MARK_PRICE/LAST_PRICE 
 > slPrice String Stop loss trigger price 
 > slOrderType String Stop loss trigger place order type.LIMIT/MARKET 
 > slOrderPrice String Stop loss trigger place order price.LIMIT/MARKET

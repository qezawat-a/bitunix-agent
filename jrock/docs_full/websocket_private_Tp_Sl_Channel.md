<!-- Bitunix official doc: /websocket/private/Tp Sl Channel | source: https://www.bitunix.com/api-docs/futures/websocket/private/Tp%20Sl%20Channel.html -->

# Tp Sl Channel

Description ​ 
 TP/SL Order
 After private WebSocket login succeeds, this channel is pushed automatically. Channel name is tpsl . data is always an object, not an array.
 CLOSE alone is not a final state. Always read event together with status . Partial fill / full fill of the child order is pushed on the order channel or queried via REST, not on tpsl .
 FILLED means the TP/SL trigger succeeded and the child order was placed. It does not mean the child order is fully filled.
 Statuses that are not pushed: INIT , PENDING_CANCEL , TRIGGER_WAIT_PLACE . Do not document PART_FILLED on this channel.
 Push Parameters ​ 
 Parameter Type Description 
 ch String Channel name: tpsl 
 ts Int64 Gateway send time, Unix milliseconds. Do not use this field to order business events. 
 data <Object> Always an object (not an array) 
 > event String CREATE / UPDATE / CLOSE 
 > positionId String position Id 
 > orderId String order Id 
 > symbol String Symbol 
 > leverage String Leverage 
 > side String BUY/SELL 
 > positionMode String Position mode, ONE_WAY/HEDGE 
 > status String NEW / CANCELED / SYSTEM_CANCELED / FILLED / FAILED. See event-status table below. 
 > ctime String Create time, ISO-8601 nanosecond string, e.g. 2024-05-16T08:13:09.123456789Z . REST history APIs use millisecond integers instead. 
 > type String LIMIT/MARKET 
 > tpQty String Take-profit order quantity (base coin). Omitted if unused. 
 > slQty String Stop-loss order quantity (base coin). Omitted if unused. Never Boolean. Never JSON null. "0" is sent as "0" . 
 > tpStopType String take profit trigger type. MARK_PRICE/LAST_PRICE 
 > tpPrice String take profit trigger price 
 > tpOrderType String take profit trigger place order type. LIMIT/MARKET 
 > tpOrderPrice String take profit trigger place order price 
 > slStopType String stop loss trigger type. MARK_PRICE/LAST_PRICE 
 > slPrice String stop loss trigger price 
 > slOrderType String stop loss trigger place order type. LIMIT/MARKET 
 > slOrderPrice String stop loss trigger place order price 
 Event and status ​ 
 event status Meaning 
 CREATE NEW TP/SL order became effective 
 UPDATE NEW TP/SL order was modified 
 CLOSE CANCELED Canceled by the user 
 CLOSE SYSTEM_CANCELED Canceled by the system 
 CLOSE FILLED Trigger succeeded and the child order was placed (not child-order full fill) 
 CLOSE FAILED Trigger failed

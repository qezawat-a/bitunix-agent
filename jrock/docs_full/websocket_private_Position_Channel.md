<!-- Bitunix official doc: /websocket/private/Position Channel | source: https://www.bitunix.com/api-docs/futures/websocket/private/Position%20Channel.html -->

# Position Channel

Description ​ 
 Subscribe the position channel
 Data will be pushed when the following events occurred:
 Open/Close orders are created
 Open/Close orders are filled
 Orders are canceled
 Push Parameters ​ 
 Parameter Type Description 
 ch String Channel name: position 
 ts Int64 Time stamp 
 data <Object> Subscription data 
 > event String Event，OPEN/UPDATE/CLOSE 
 > positionId String position Id 
 > marginMode String Margin mode,ISOLATION/CROSS 
 > positionMode String Position mode,ONE_WAY/HEDGE 
 > side String Position direction,SHORT/LONG 
 > leverage String Leverage 
 > margin String margin 
 > ctime String Create time 
 > qty String Open position size 
 > symbol String Symbol 
 > realizedPNL String Realized PnL(exclude funding fee and transaction fee) 
 > unrealizedPNL String Unrealized PnL 
 > funding String total funding fee during the position 
 > fee String Deducted transaction fees: transaction fees deducted during the position

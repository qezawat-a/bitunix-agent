<!-- Bitunix doc: /futures/websocket/private/Balance Channel | source: https://www.bitunix.com/api-docs/futures/websocket/private/Balance%20Channel.html -->
# futures_websocket_private_Balance_Channel

### Description [​](#description)

Balance

### Push Parameters [​](#push-parameters)

| Parameter | Type | Description |
| --- | --- | --- |
| ch | String | Channel name: balance |
| ts | Int64 | Time stamp |
| data | <Object> | |
| > coin | String | coin |
| > available | String | available |
| > frozen | String | frozen = isolationFrozen + crossFrozen |
| > isolationFrozen | String | Freeze on a per warehouse basis |
| > crossFrozen | String | Full warehouse entrusted freeze |
| > margin | String | Margin |
| > isolationMargin | String | Warehouse by warehouse margin. |
| > crossMargin | String | Full warehouse margin. |
| > expMoney | String | Experience Money. |

',4

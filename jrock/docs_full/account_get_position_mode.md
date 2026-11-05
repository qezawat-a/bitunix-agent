<!-- Bitunix official doc: /account/get_position_mode | source: https://www.bitunix.com/api-docs/futures/account/get_position_mode.html -->

# get_position_mode

Get Position Mode ​ 
 Rate Limit: 20 req/sec/uid
 Description ​ 
 Get the user's futures position mode (one-way or hedge).
 HTTP Request ​ 
 GET /api/v1/futures/account/position_mode
 Request Parameters ​ 
 Parameter Type Required Description 
 None - - No query parameters 
 Request Example
 bash curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/futures/account/position_mode' \ 
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
 data object Response data object 
 data.positionMode string Position mode 
 ONE_WAY : one-way position mode 
 HEDGE : hedge (dual-side) position mode 
 Response Example
 json { "code" : 0 , "data" :{ "positionMode" : "HEDGE" }, "msg" : "Success" }

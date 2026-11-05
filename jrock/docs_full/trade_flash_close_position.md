<!-- Bitunix official doc: /trade/flash_close_position | source: https://www.bitunix.com/api-docs/futures/trade/flash_close_position.html -->

# flash_close_position

Flash Close Position ​ 
 Rate Limit: 5 req/sec/uid
 Description ​ 
 Close position by position id
 HTTP Request ​ 
 POST /api/v1/futures/trade/flash_close_position
 Request Parameters ​ 
 Parameter Type Required Description 
 positionId String true position id 
 Request Example
 bash curl -X 'POST' --location 'https://fapi.bitunix.com/api/v1/futures/trade/flash_close_position' \ 
 -H "api-key:*******" \ 
 -H "sign:*" \ 
 -H "nonce:your-nonce" \ 
 -H "timestamp:1659076670000" \ 
 -H "language:en-US" \ 
 -H "Content-Type: application/json" \ 
 --data '{"positionId":"19848247723672"}' 
 Response Parameters ​ 
 Parameter Type Description 
 positionId string Position ID 
 Response Example
 json { "code" : 0 , "data" :{ "positionId" : "19848247723672" }, "msg" : "Success" }

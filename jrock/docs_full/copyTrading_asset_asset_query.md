<!-- Bitunix official doc: /copyTrading/asset/asset_query | source: https://www.bitunix.com/api-docs/futures/copyTrading/asset/asset_query.html -->

# asset_query

Asset Query ​ 
 Rate Limit: 10 req/sec/uid
 Description ​ 
 Interface is used to asset query.
 HTTP Request ​ 
 GET /api/v1/cp/asset/query
 Request Parameters ​ 
 Parameter Type Required Description 
 Request Example
 bash curl -X 'GET' --location 'https://fapi.bitunix.com/api/v1/cp/asset/query' \ 
 -H "api-key:*******" \ 
 -H "sign:*" \ 
 -H "timestamp:1659076670000" \ 
 -H "nonce:your-nonce" \ 
 -H "language:en-US" 
 Response Parameters ​ 
 Parameter Type Description 
 available string Futures available 
 maxTransfer string Maximum transfer amount 
 Response Example
 json { "code" : 0 , "msg" : "result.success" , "data" :{ "available" : "54.20916" , "maxTransfer" : "52.20916" }, "success" : true }

<!-- Bitunix doc: /futures/copyTrading/asset/transfer_asset_from_main_account_to_sub_account | source: https://www.bitunix.com/api-docs/futures/copyTrading/asset/transfer_asset_from_main_account_to_sub_account.html -->
# transfer asset from main account to sub account

{"title":"transfer asset from main account to sub account","description":"","frontmatter":{"slug":"transfer\_asset\_from\_main\_account\_to\_sub\_account","title":"transfer asset from main account to sub account"},"headers":[],"relativePath":"futures/copyTrading/asset/transfer\_asset\_from\_main\_account\_to\_sub\_account.md","filePath":"copyTrading/asset/transfer\_asset\_from\_main\_account\_to\_sub\_account.md"}'),n={name:"futures/copyTrading/asset/transfer\_asset\_from\_main\_account\_to\_sub\_account.md"};function r(l,s,h,p,o,k){return a(),i("div",null,s[0]||(s[0]=[e(`

# transfer asset from main account to sub account [​](#transfer-asset-from-main-account-to-sub-account)

Rate Limit: 10 req/sec/uid

### Description [​](#description)

Interface is used to transfer asset from main Account to subAccount.

### HTTP Request [​](#http-request)

- POST /api/v1/cp/asset/transfer-to-sub-account

### Request Parameters [​](#request-parameters)

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| amount | string | true | transfer amount |
| assetType | string | true | Assets are transferred from the futures account or spot account of the main account. eg: FUTURES/SPOT |

Request Example

bash

```
curl -X 'POST' --location 'https://fapi.bitunix.com/api/v1/cp/asset/transfer-to-sub-account' \\
 -H "api-key:*******" \\
 -H "sign:*" \\
 -H "nonce:your-nonce" \\
 -H "timestamp:1659076670000" \\
 -H "language:en-US" \\
 -H "Content-Type: application/json" \\
 --data '{"amount":"10","assetType":"SPOT"}'
```

### Response Parameters [​](#response-parameters)

| Parameter | Type | Description |
| --- | --- | --- |

Response Example

json

```
{"code":0,"msg":"result.success","data":"","success":true}
```

`,14

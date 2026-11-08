|endpoint|params|action|
| --- | --- | --- |
|`GET /status`||get active accounts, active clients, and stream counts|
|`POST /status`|`kill=<pid>`|kill stream process `pid`|
|`POST /status`|`rescan=1`|reload config, reselect/rescan accounts, refresh lineups|
|`GET /config`|<br>`defaults=0`|get current config<br>(without default values)|
|`POST /config`|`<key>=<value>`<br>`<filter_type>=<filter_text>`<br>`remove=1`|set `key` to `value`<br>add `filter_text` to list `filter_type`<br>remove `key` from config or `filter_text` from `filter_type`|
|`GET /log`||get logs|
|`GET /lineup`||get lineup|
|`GET /sources`||get sources|
|`POST /sources`|`add=<source>&username=<username>&password=<password>`<br>|add account `username`/`password` to source `source`|
|`POST /sources`|`remove=<source>&username=<username>&password=<password>`|remove account matching `username`/`password` from source `source`| 
|`POST /sources`|`update=<source>&disabled=1`<br>`update=<source>&disabled=0`|disable `source`<br>enable `source`| 
|`POST /sources`|`update=<source>&priority=<pri>`|set `source` priority to `pri`| 
|`GET /m3u`||get M3U playlist|
|`GET /xmltv`||get EPG data|
|`GET /stream/<id>`|`direct=0`|proxy stream `id` from best available source and serve MPEGTS|
|`GET /stream/<id>`|`direct=1`|redirect to URL of stream `id` from best available source|
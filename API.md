|endpoint|params|action|
| --- | --- | --- |
|`GET /status.json`||get active `accounts` and active `procs`|
|`POST /status.json`|`kill=<pid>`|kill stream process `pid`|
|`POST /lineup.post`||reload config, reselect/rescan accounts, refresh lineups
|`GET /config.json`|<br>`defaults=0`|get current config<br>(without default values)|
|`POST /config.json`|`<key>=<value>`<br>`<filter_type>=<filter_text>`<br>`remove=1`|set `key` to `value`<br>add `filter_text` to list `filter_type`<br>remove `key` from config or `filter_text` from `filter_type`|
|`POST /config.json`|`config=<json>`|write `json` to config|
|`GET /log.json`||get logs|
|`GET /lineup.json`||get lineup|
|`GET /groups.json`||get groups available from sources|
|`GET /sources.json`||get sources|
|`POST /sources.json`|`add=<source>&username=<username>&password=<password>`<br>|add account `username`/`password` to source `source`|
|`POST /sources.json`|`remove=<source>&username=<username>&password=<password>`|remove account matching `username`/`password` from source `source`| 
|`POST /sources.json`|`update=<source>&disabled=1`<br>`update=<source>&disabled=0`|disable `source`<br>enable `source`| 
|`POST /sources.json`|`update=<source>&priority=<pri>`|set `source` priority to `pri`| 
|`GET /m3u`||get M3U playlist|
|`GET /xmltv`||get EPG data|
|`GET /stream/<id>`|`direct=0`|proxy stream `id` from best available source and serve MPEGTS|
|`GET /stream/<id>`|`direct=1`|redirect to URL of stream `id` from best available source|
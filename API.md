|endpoint|params|action|
| --- | --- | --- |
|`GET /status.json`||get active `accounts` and active `procs`|
|`POST /status.json`|`kill=<pid>`<br>`rescan=1`|kill stream process `pid`<br>same as `POST /lineup.post`|
|`POST /lineup.post`||reload config, reselect/rescan accounts, refresh lineups
|`GET /config.json`|<br>`defaults=0`<br>`sources=1`|get current config<br>without defaults<br>with sources and accounts|
|`POST /config`|`<key>=<value>`<br>`<filter_type>=<filter_text>`<br>`remove=1`|set `key` to `value`<br>add `filter_text` to list `filter_type`<br>remove `key` from config or `filter_text` from `filter_type`|
|`POST /config`|`config=<json>`|write `json` to config|
|`GET /log.json`||get logs|
|`GET /lineup.json`||get lineup|
|`POST /sources`|`add=<text>`<br>|parse text as `source username password`<br>or a URL as `<source>/path?username=<username>&password=<password>`<br> and add to sources|
|`POST /sources`|`remove=<source>&username=<username>&password=<password>`|remove account matching `source`/`username`/`password` from sources| 
|`POST /sources`|`update=<source>&disabled=1`<br>`update=<source>&disabled=0`|disable `source`<br>enable `source`| 
|`POST /sources`|`update=<source>&priority=<pri>`|set `source` priority to `pri`| 
|`GET /stream/<id>`|`direct=0`|proxy stream `id` from best available source and serve MPEGTS|
|`GET /stream/<id>`|`direct=1`|redirect to URL of stream `id` from best available source|
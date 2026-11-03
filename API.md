|endpoint|params|action|
| --- | --- | --- |
|`GET /status.json`|<br>`kill=<pid>`|get active `accounts` and active `procs`<br>kill stream process `pid`|
|`POST /lineup.post`||reload config, reselect/rescan accounts, refresh lineups
|`GET /config.json`|<br>`defaults=0`<br>`sources=1`|get current config<br>without defaults<br>with sources and accounts|
|`POST /config`|`update_config=1&<key>=<value>`<br>`update_config=1&<filter_type>=<filter_text>`<br>`save_config=<json>`|set `key` to `value`<br>add `filter_text` to list `filter_type`<br>write `json` to config
|`GET /log.json`||get logs|
|`GET /lineup.json`|<br>`del=<filter_type>&v=<filter_text>`|get lineup<br>delete filter of `filter_type` if matching `filter_text`|
|`POST /sources`|`add_account=<text>`|parse text as `source username passwprd`<br>or a URL as `<SOURCE>/path?username=<username>&password=<password>`<br> and add to sources|
|`GET /sources`|`remove=<source>&username=<username>&password=<password>`<br>`source=<source>&disabled=<1>`<br>`source=<source>&disabled=<0>`<br>`source=<source>&priority=<pri>`|remove account matching `source` `username` `password` from sources<br>disable `source`<br>enable `source`<br>set `source` priority to `pri`| 
|`GET /stream/<id>`|`direct=0`<br>`direct=1`<br>&nbsp;|proxy stream `id` from best available source and serve MPEGTS<br>redirect to URL of stream `id` from best available source<br>if `direct=` not set, will use `DIRECT=` setting from config
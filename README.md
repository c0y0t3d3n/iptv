# IPTV Tuner proxy for Plex

Plex can't manage IPTV but has a very good EPG. This emulates a HDHR and is designed to proxy IPTV using Plex as the EPG source. It can filter and rename channels to match what the Plex guide data is expecting. When Plex tunes a channel, it will refresh the status of all accounts and choose the one with the most open slots.

If not all accounts have the same URL, the lineups from all providers are merged. This eliminates duplicate channels and chooses the least busy account across all if that channel is available from multiple sources. Make sure you have the filters set so channels have the same name across all providers.

## Getting started
A docker-compose.yaml to spin up Plex and tuner containers is included, modify as needed.

You need a tuner.cfg with filters and xtream codes. A sample with working accounts and some generic filters is included.

To add the tuner to Plex you need to manually enter the SERVER_IP:SERVER_PORT and it should appear. When you rescan channels in Plex it reloads the config and fetches lineups. Once you have the group and channel name filters set properly Plex should be able to auto map most of them to one of your local cable/satellite provider lineups. Any manual mappings you do should stick even if you change IPTV providers, as long as you adjust the filters.

Visit `http://SERVER_IP:SERVER_PORT/` for status, stream links, logs, and to edit the config. The letters before each stream link represent the source(s) providing that channel.

## Config file entries
Keys are case-insensitive. `key+=...` will extend list of values for the key.

`GROUPS=` regex patterns of groups to match, `!pattern` to exclude

`STREAMS=` regex patterns of streams to include and `!pattern` of streams to remove. Overrides groups to allow adding or removing individual channels.

`RENAME=` regex patterns to strip or replace in stream names. `pattern=string` will replace pattern with string.

`REPLACE=` replace any streams with the same name if a stream matching name+`pattern` exists.\
Example: `REPLACE= UHD$` will turn 'ABC UHD' into 'ABC', removing any streams named 'ABC', but only if 'ABC UHD' exists.\
`REPLACE=^FHD: ` will do the same for 'FHD: ' at the start of the name.

All matching allows multiple comma-separated values and is case-insensitive as all patterns and strings are by default uppercased. This generally makes filtering and merging easie but limits what regexes you can use. Set `UPPER=0` in config to disable uppercasing.

## account list
Put xtream codes in config file as:

`URL USER PASS PRI`

PRI is optional and defaults to 0. Lower number is higher priority and will be preferred unless full.\
If you have a large number of accounts for a source, you probablty do not want to hit all of them every time. Set `CHECK=n` to randomly select `n` accounts per source to check.

Paste an iptvlookup.com URL into the field below the config and hit 'add account' to fetch the account info and add it to the config.

# Usage
## tuner.py emulates a HDHomeRun tuner
`./tuner.py config_file`

Server config (can set in config or environment):

`SERVER_IP` and `SERVER_PORT` to set listening IP and port. Defaults to localhost:5004\
`DIRECT=1` will bypass ffmpeg remuxing and redirect clients to the stream URL.\
`CMD=` will override the command used to fetch and remux the stream. Whatever you use should accept a stream URL as `%s` and pipe to STDOUT.

Plex currently has an issue with streams containing AAC audio (HE-AAC in particular) so you may want to try\
`CMD=ffmpeg -hide_banner -loglevel error -i %s -c copy -c:a ac3 -copyts -f mpegts pipe:1`\
to transcode all audio to AC3.

## iptv.py generates m3u playlists from xtream codes
`./iptv.py URL USER PASS`  (to check acct)\
`./iptv.py URL USER PASS m3u_file` (check acct and write m3u)\
`./iptv.py config_file` (to check accts)\
`./iptv.py config_file m3u_file` (to check accts, write m3u for account with most open slots)

æÑ­'fgweñ¦’"6S^— ­

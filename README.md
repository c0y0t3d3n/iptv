# iptv
M3U generator and tuner proxy for Plex

Plex can’t manage IPTV but has a very good EPG. This emulates a HDHR and is designed to proxy IPTV using Plex as the EPG source. It can filter and rename channels to match what the Plex guide data is expecting. When Plex tunes a channel, it will refresh the status of all accounts and choose the one with the most open slots.

If not all accounts have the same URL, the lineups from all providers are merged. This eliminates duplicate channels and chooses the least busy account across all if that channel is available from multiple sources. Make sure you have the filters set so channels have the same name across all providers.

## getting started
A docker compose-file to spin up Plex and tuner containers is included, modify as needed.

You’ll need a tuner.cfg with filters and xtream codes as documented in the README. A sample with working accounts and some generic filters is included.

To add the tuner to Plex you need to manually enter the SERVER_IP:SERVER_PORT and it should appear. When you rescan channels in Plex it reloads the config and fetches lineups. Once you have the group and channel name filters set properly Plex should be able to auto map most of them to one of your local cable/satellite provider lineups. Any manual mappings you do should stick even if you change IPTV providers, as long as you adjust the filters.

Visit `http://SERVER_IP:SERVER_PORT/` for status, stream links, logs, and to edit the config. The number next to each stream link is the count of sources providing that channel.

## config file entries
All string matching is case-insensitive but matches any included whitespace. Use this to match either partial or full words in names.

`groups=` pattern of groups to match, default is exact match, `|pattern` for start match, `pattern|` for end match, `!pattern` to exclude if match anywhere

`streams=` patterns of streams to include and !patterns of streams to remove. Overrides GROUPS to allow adding or removing individual channels vs. entire categories.

`rename=` patterns to strip from stream names. default is anywhere in name, `|pattern` for start, `pattern|` for end. `pattern=string` will replace pattern with string.

`replace=` replace any streams with the same name if a stream matching name+pattern exists. `|pattern` will replace streams if pattern+name exists. Example: `REPLACE= UHD` will turn 'ABC UHD' into 'ABC', removing any streams named 'ABC', but only if 'ABC UHD' exists. `REPLACE=|FHD: ` will do the same for 'FHD: ' at the start of the name.

Put xtream codes in config as\
`URL USER PASS PRI` (PRI is optional and defaults to 0. Lower number = higher priority and will be preferred unless full.)

# usage
## tuner.py emulates a HDHomeRun tuner
`./tuner.py config_file`

server config (can set in config or environment):

`SERVER_IP` and `SERVER_PORT` to set listening IP and port. Defaults to localhost:5004\
`DIRECT=1` will bypass ffmpeg remuxing and redirect clients to the remote stream URL after following any redirects. This avoids having to spawn ffmpeg subprocesses, but if that stream fails Plex will try to reconnect to the same stream vs. making a new request.

## iptv.py generates m3u playlists from xtream codes
`./iptv.py URL USER PASS`  (to check acct)\
`./iptv.py URL USER PASS m3u_file` (check acct and write m3u)\
`./iptv.py config_file` (to check accts)\
`./iptv.py config_file m3u_file` (to check accts, write m3u for account with most open slots)




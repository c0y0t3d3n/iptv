# iptv
M3U generator and tuner proxy for Plex

# usage
## tuner.py emulates a HDHomeRun tuner
./tuner.py config_file

server config (can set in config or environment):

SERVER_IP, SERVER_PORT to set listening IP and port. Defaults to localhost:5004\
DIRECT=1 will bypass ffmpeg remuxing and redirect clients to the remote stream URL after following any redirects.

Info page with stream links is at http://localhost:5004/ or the SERVER_IP:SERVER_PORT you have it running on. Here you can edit the config and hit save to write it.

Lineups from each source will be merged into a single lineup by stream name. Each stream link shows how many sources provide that stream.\
When Plex requests a channel scan or you visit the info page the config will be reloaded and the channel lineup fetched.\
When a stream request is made, it will refresh the status of all accounts and choose the account with the most open slots across all sources providing that stream.

## iptv.py generates m3u playlists from xtream codes
(requires tuner.py)

./iptv.py URL USER PASS  (to check acct)\
./iptv.py URL USER PASS m3u_file (check acct and write m3u)\
./iptv.py config_file (to check accts)\
./iptv.py config_file m3u_file (to check accts, write m3u for account with most open slots)

# config file entries
lineup filtering:

GROUPS=pattern of groups to match, default is exact match, ^pattern for start match, pattern$ for end match, !pattern to exclude if match anywhere

STREAMS=patterns to include and !patterns for streams to remove. overrides GROUPS and matches anywhere.

RENAME=patterns to strip from stream names. default is anywhere in name, ^pattern for start, pattern$ for end. pattern/string will replace pattern with string.

REPLACE=replace any streams with the same name if a stream matching name+pattern exists.\
 example: REPLACE= UHD will turn 'ABC UHD' into 'ABC', removing any streams named 'ABC', but only if 'ABC UHD' exists.

put xtream codes in config as\
URL USER PASS

(see sample tuner.cfg)
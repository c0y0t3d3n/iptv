# iptv
M3U generator and tuner proxy for Plex

# usage
## iptv.py generates m3u playlists from xtream codes
./iptv.py URL USER PASS  (to check acct)\
./iptv.py URL USER PASS m3u_file (check acct and write m3u)\
./iptv.py config_file (to check accts)\
./iptv.py config_file m3u_file (to check accts, write m3u, reload threadfin)

lineup filtering:\
GROUPS=pattern of groups to match, default is exact match, ^pattern for start match, pattern$ for end match, !pattern to exclude\
STRIP=patterns to strip from stream names. default is anywhere in name, ^pattern for start match, pattern$ for end match\
STREAMS=patterns to include and !patterns for streams to remove, ignores GROUPS. matches anywhere in name.\
REPLACE=replace any streams with the same name if a stream matching name+pattern exists.\
 example: REPLACE= UHD will turn 'ABC UHD' into 'ABC', removing any streams named 'ABC', but only if 'ABC UHD' exists.

list xtream codes as\
URL USER PASS\
m3u will be generated using the account with the most open slots.

## tuner.py is like xteve/threadfin/dispatcharr but very lightweight
./tuner.py config_file (same format as iptv.py)

server config (can set in config or environment)\
SERVER_IP, SERVER_PORT to set listening IP and port. Defaults to localhost:5004\
DIRECT=1 will bypass ffmpeg remuxing and redirect clients to the remote stream URL after following any redirects.

Info page with stream links is at http://localhost:5004/ (or the IP:PORT you have it running on)

Will pick the account with the most open slots per stream request.\
When Plex requests a channel scan or you visit the info page the config and accounts will be reloaded, and the lineup regenerated.

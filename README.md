# IPTV Tuner proxy for Plex/Jellyfin

* Emulates an HDHR to proxy IPTV for Plex. 
* Provides M3U/XMLTV for Jellyfin.
* Filters and renames channels to match what the Plex EPG is expecting.
* Automerges lineups from multiple providers.
* When a channel is requested, refreshes the status of all accounts and chooses the best available.

# Getting started
A `docker-compose.yaml` and `Dockerfile` to build/run a tuner container is included. Make sure you set `SERVER_IP` in `docker-compose.yaml`. A default config will be created if it does not exist.

To add the tuner to Plex you need to manually enter the SERVER_IP:SERVER_PORT and it should appear. When you rescan channels in Plex it reloads the config and fetches lineups. Once you have the group and channel name filters set properly Plex should be able to map most of them to one of your local cable/satellite provider lineups. Any manual mappings you do should stick even if you change IPTV providers, as long as you adjust the filters. If you are using Jellyfin or another IPTV player, see <B>M3U and XMLTV</b>.

Visit `http://SERVER_IP:SERVER_PORT/` for status, lineup with stream links, logs, and to edit the config. Below the config editor is a link to IPTVLookup and an import field. Paste the URL of an IPTVLookup account details page and hit 'add account' to import it.

There is a list of groups provided by each source and a basic filter builder at the bottom of the lineup page. Choose a group to add from the dropdown or enter a pattern to match. Checkboxes select only matching at start or end of name; select both for exact match. You can also enter patterns to include or exclude streams by name, rename streams, and replace duplicates.

## Automapping

Channels from each source will be merged into a single lineup by combining channels with the same name or same EPG ID. Matching names take priority over EPG IDs. To disable matching by EPG ID, set `AUTO_MAP=0`\
In the lineup the letters before each stream link represent the source(s) providing that channel. 

## M3U and XMLTV
If you want to use the tuner with Jellyfin or an IPTV player, use the M3U and XMLTV links on the status page. XMLTV will be fetched from all sources and filtered to only channels in the lineup.\
To filter the lineup to only channels with EPG data, set `MIN_SOURCES=<n>` where `<n>` is the minimum number of EPG sources a channel must have to be in the lineup.

## Caching
The tuner can cache accounts, lineups, and xmltv data to reduce how often it has to hit the API endpoints of the sources. Account status will be refreshed when the cache expires, the reload button is pressed, Plex requests a channel scan, or a stream failure occur (as this usually means the account is overused). Lineups will be refreshed when the cache expires, when the reload button is pressed or Plex requests a channel scan. XMLTV will be refreshed when lineups are refreshed, but XMLTV is not fetched from the source until requested by the client. 

Account caching is disabled by default, but should be enabled if you have multiple accounts per source to avoid potentially being banned. To enable caching, set `CACHE=<seconds>` where `<seconds>` is how long to cache account data.

Lineups and XMLTV are by default cached for 24 hours (86400 seconds). To change this set `LINEUP_CACHE=<seconds>`. 0 disables lineup and XMLTV caching.

# Configuration
Keys are case-insensitive. Repeated keys and comma-seperated values will create a list of values for the key. All match patterns and replace strings are by default uppercased. This generally makes filtering and merging easier but limits what regexes you can use. Set `UPPER=0` in config to disable uppercasing.

## Filtering

`GROUP_RENAME=` regex patterns to strip or replace in group names. `pattern=string` will replace pattern with string. This is applied before group selection/exclusion.

`GROUPS=` regex patterns of groups to match, `!pattern` to exclude

`STREAMS=` regex patterns of streams to include and `!pattern` of streams to remove. Overrides groups to allow adding or removing individual channels.

`RENAME=` regex patterns to strip or replace in stream names. `pattern=string` will replace pattern with string.

`REPLACE=` replace any streams with the same name if a stream matching name+`pattern` exists.\
Example: `REPLACE= UHD$` will turn 'ABC UHD' into 'ABC', removing any streams named 'ABC', but only if 'ABC UHD' exists. `REPLACE=^FHD: ` will do the same for 'FHD: ' at the start of the name.

`ORDER=` order of groups in the lineup. Groups matching the first pattern will be followed by groups matching the next, and so on. Groups that do not match any pattern will be placed last.

## Accounts
Put xtream codes in the config as:

`URL USER PASS PRI`

PRI is optional and defaults to 0. Lower number is higher priority and will be preferred unless full.

If you have a large number of accounts for a source, you may not want to probe all of them on every rescan.\
Set `CHECK=<n>` to randomly select at most `<n>` accounts per source.


# Command line and options

`./tuner.py config_file`

Options can set in config or environment

`SERVER_IP` and `SERVER_PORT` to set listening IP and port. Defaults to localhost:5004\
`DIRECT=1` will bypass ffmpeg remuxing and redirect clients to the stream URL.\
`TUNER_COUNT=<n>` set the number of tuners reported to Plex.\
`CACHE=<seconds>` cache account status for the specified number of seconds.\
`LINEUP_CACHE=<seconds>` cache lineup and XMLTV for the specidied number of seconds. Defualt 86400.\
`AUTO_MAP=0` disable automapping of channels across source lineups by EPG ID.\
`MIN_SOURCES=<n>` require a channel have a minimum number of sources to be included in the lineup. 1 will require channels have EPG IDs.\
`TIMEOUT=<seconds>` set a timeout on refreshing each account and lineup.\
`DELAY=<seconds>` add a delay between refreshing each accoumnt.\
`UPPER=0` disable uppercasing of group and channel names.\
`LOGLEVEL=<level>` set the logging level. 10=debug, 20=info, 30=warning, 40=error. Must be set in environment.\
`LOGDEPTH==<n>` retain `n` messages on the log page.\
`FORMAT=<string>` URL format for requesting a stream from the source. %s substitutions will be host, port, username, passsword, stream ID.\
`CMD=` will override the command used to fetch and remux the stream. Whatever you use should accept a stream URL as `%s` and pipe to STDOUT.

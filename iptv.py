#!/usr/bin/python3
import time 
import requests
import json
import sys
import os
from datetime import datetime
from urllib.parse import urlparse
import random

THREADFIN='http://10.0.0.10:34400/api/'

DELAY=1

ENV_VARS=['DELAY','GROUPS','STRIP','REMOVE','REPLACE','UPPER','FORMAT','THREADFIN']

UPPER=1  
#stream format
FORMAT='ts'
# GROUPS to these categories 
GROUPS=''
STRIP='^US: ,^USA: ,^US | ,^USA | '
STREAMS=''
REPLACE=''

#config from env
for e in ENV_VARS:
    globals()[e]=os.getenv(e,globals()[e])

#config from k=v in file
if len(sys.argv)>1 and not sys.argv[1].startswith('http'):
    with open(sys.argv[1]) as f:
        lines=f.readlines()
        for l in lines:
            l=l.split('#')[0]
            if '=' in l:
                k,v=l.strip('\n').split('=',1)
                if k in ENV_VARS:
                    globals()[k]=v

#parse config
GROUPS=GROUPS.split(',')
if '' in GROUPS: GROUPS.remove('')
GROUPS_EXCLUDE=[f[1:] for f in GROUPS if f.startswith('!')]
GROUPS_STARTSWITH=[f[1:] for f in GROUPS if f.startswith('^')]
GROUPS_ENDSWITH=[f[:-1] for f in GROUPS if f.endswith('$')]
GROUPS=[f for f in GROUPS if not f.startswith('!') and not f.startswith('^') and not f.endswith('$')]
if not any ([GROUPS, GROUPS_EXCLUDE, GROUPS_STARTSWITH, GROUPS_ENDSWITH]):
    GROUPS=None
# patterns to strip from channel names. ^startwith, endswith$, or anywhere if no modifier
STRIP=STRIP.split(',')
if '' in STRIP: STRIP.remove('')
STRIP.append(',') #plex does not like commas in channel names
# pattern of channels to select/remove.
STREAMS=STREAMS.split(',')
if '' in STREAMS: STREAMS.remove('')
REMOVE=[c[1:] for c in STREAMS if c.startswith('!')]
STREAMS=[c for c in STREAMS if not c.startswith('!')]
# replace any channels with base name if a channel matching name+pattern exists 
# example: REPLACE=' LHD' will rename 'ABC LHD' to 'ABC', removing any channels named 'ABC', but only if 'ABC LHD' exists.
REPLACE=REPLACE.split(',')
if '' in REPLACE: REPLACE.remove('')

def xtream_request(url,user,pw,action):
    r=requests.get(url+'/player_api.php',params={'username':user,'password':pw,'action':action})
    print (action,r.status_code, file=sys.stderr)
    r.raise_for_status()
    return json.loads(r.text)

# get server and account info
def check_xtream(url,user,pw):
    info=xtream_request(url,user,pw,'server_info')
    try:
        server_info,user_info=info['server_info'],info['user_info']
        print('%s:%s %s %s %s %s/%s %s' % (
            server_info['url'],
            server_info['port'],
            user,pw,
            user_info['status'],
            user_info['active_cons'],
            user_info['max_connections'],
            datetime.fromtimestamp(int(user_info['exp_date'])) if user_info['exp_date'] else None
            ))
        return url,user,pw,int(user_info['active_cons']),int(user_info['max_connections']), user_info['status'], server_info
    except:
        print(info,url,user,pw,file=sys.stderr)
        return url, user, pw, None, None, '', {}

def start_session(url,mac):
    session = requests.Session()
    session.cookies.update({"mac": mac})
    session.headers.update(
        {
            "Referer": f"{url}/c/",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "X-Requested-With": "XMLHttpRequest",
        }
    )
    return session

def check_mag(url, mac):
    parsed_url = urlparse(url)
    scheme = parsed_url.scheme or "http"
    host = parsed_url.hostname
    port = parsed_url.port or 80
    url = f"{scheme}://{host}:{port}"

    session = start_session(url,mac)
    """Gets token using MAC authentication."""
    headers = {"Authorization": f"MAC {mac}"}
    r = session.get(f"{url}/portal.php?action=handshake&type=stb&token=&JsHttpRequest=1-xml", headers=headers)
    r.raise_for_status()
    token = r.json()["js"]["token"]
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = session.get(f"{url}/portal.php?type=account_info&action=get_main_info&JsHttpRequest=1-xml", headers=headers)
        r.raise_for_status()
        data = r.json()
        mac = data["js"]["mac"]
        expiry = data.get("js", {}).get("phone", "N/A")
        print ('%s expires %s'%(mac,expiry))
        return url, user, token, 0, 0, 'active', None
    except Exception as e:
        print(e,url,user,token,file=sys.stderr)
        return url, user, None, None, None, '', {}

def fetch_xtream(url,user,pw):
    cats=dict( (e['category_id'],e['category_name']) for e in xtream_request(url,user,pw,'get_live_categories') \
        if GROUPS is None or e['category_name'] in GROUPS \
        or any(e['category_name'].startswith(f) for f in GROUPS_STARTSWITH) \
        or any(e['category_name'].endswith(f) for f in GROUPS_ENDSWITH) \
        and not any (e['category_name'].startswith(f) for f in GROUPS_EXCLUDE) )
    print('categories',len(cats),sorted(cats.values()))
    streams=[s for s in xtream_request(url,user,pw,'get_live_streams') if s['category_id'] in cats]
    print('streams',len(streams))
    return cats, streams

def fetch_mag(url,mac,token):
    """Gets the full channel list and genre information."""
    session = start_session(url,mac)
    headers = {"Authorization": f"Bearer {token}"}
    url_genre = (
        f"{url}/server/load.php?type=itv&action=get_genres&JsHttpRequest=1-xml"
    )
    r = session.get(url_genre, headers=headers)
    r.raise_for_status()
    genre_data = r.json()["js"]
    cats = dict( (e["id"], e["title"]) for e in genre_data \
            if GROUPS is None or e['title'] in GROUPS \
            or any(e['title'].startswith(f) for f in GROUPS_STARTSWITH) \
            or any(e['title'].endswith(f) for f in GROUPS_ENDSWITH)\
            and not any(e['title'].startswith(f) for f in GROUPS_EXCLUDE) )
    print('categories',len(cats),sorted(cats.values()))
    url_channels = f"{url}/portal.php?type=itv&action=get_all_channels&JsHttpRequest=1-xml"
    r = session.get(url_channels, headers=headers)
    r.raise_for_status()
    channels_data = r.json()["js"]["data"]
    streams = [dict( name=s['name'], category_id=s['tv_genre_id'], epg_channel_id=s['name'], stream_icon=s['logo'], stream_id=s['id']) for s in channels_data if s['tv_genre_id'] in cats or any(c.upper() in s['name'].upper() for c in STREAMS)]
    print('streams',len(streams))
    return cats, streams

def generate_m3u(url,user,pw,server_info,m3u):
    if server_info:
        cats,streams = fetch_xtream(url,user,pw)
    else:
        cats,streams = fetch_mag(url,user,pw)
    
    #remove and rename streams
    out=[]
    for s in streams:
        n=s['name'].upper() if int(UPPER) else s['name']
        if  any(r in n for r in REMOVE):
            continue
        for p in STRIP:
            if p.startswith('^'):
                if n.startswith(p[1:]):
                    n=n[len(p[1:]):]
            elif p.endswith('$'):
                if n.endswith(p[:-1]):
                    n=n[:-len(p[:-1])]
            else: n=n.replace(p,'')
        out.append([n,
            cats[s['category_id']], 
            s['epg_channel_id'],
            s['stream_icon'], 
            s['stream_id']
            ])
    streams=out
    
    #skip channels if channel+pattern exists
    for r in REPLACE:
        replaced=set()
        replaced.update(s[0][:-len(r)] for s in streams if s[0].endswith(r))
        #remove replaced channels
        streams=[s for s in streams if s[0] not in replaced]
        #rename name+pattern to name to replace channels
        for s in streams:
            if s[0].endswith(r):
                s[0]=s[0][:-len(r)]
    
    #write out m3u
    i=0
    with open(m3u,'w') as f:
        print('#EXTM3U',file=f)
        for s in streams:
            print('#EXTINF:-1 group-title="%s" tvg-id="%s" tvg-name="%s" tvg-logo="%s",%s' % (s[1],s[2],s[0],s[3],s[0]), file=f)
            if server_info: #xtream
                    print('http://%s:%s/live/%s/%s/%s.%s' % (
                    server_info['url'].replace('http://',''),
                    server_info['port'],
                    user, pw, s[4], FORMAT 
                    ), file=f)
            else: #mag
                    print("%s/play/live.php?mac=%s&stream=%s&extension=%s" % (url, user, s[4], FORMAT), file=f)
            i+=1
    print(m3u,i)
    if server_info: 
        print('xmltv %s/xmltv.php?username=%s&password=%s' % (url,user,pw))

if len(sys.argv) < 2:
    print('''
usage: 
	./iptv.sh <URL> <user/MAC> [pass if xtream] (to check account)
	./iptv.sh <URL> <user/MAC> [pass if xtream] [m3u_file] (to generate m3u)
	./iptv.sh <account_list> (to check all acccounts)
	./iptv.sh <account_list> [m3u file] (to generate m3u for least used account, tell threadfin to reload)

IPTV account lists should be:

GROUPS=pattern of categories to match, default is exact match, ^pattern for start match, pattern$ for end match, !pattern to exclude
STRIP=patterns to strip from channel names. default is anywhere in name, ^pattern for start match, pattern$ for end match
STREAMS=!patterns for channels to remove. matches anywhere in name.
REPLACE=replace any channels with the same name if a channel matching name+pattern exists.
 example: REPLACE=' UHD' will turn 'ABC UHD' into 'ABC', removing any channels named 'ABC', but only if 'ABC UHD' exists.

followed by a list of:
SERVER USER/MAC PASS (if xtream)

defaults:''')

    for e in ENV_VARS:
        print (e,globals()[e])
    sys.exit(0)

if sys.argv[1].startswith('http'):
    url, user = sys.argv[1:3]
    if ':' in user:
        pw=None
        if len(sys.argv)>3:
            m3u=sys.argv[3]
        else:
            m3u=None
        url,user,token,used,limit,status,server_info=check_mag(url,user)
    else:
        pw=sys.argv[3]
        if len(sys.argv)>4:
            m3u=sys.argv[4]
        else:
            m3u=None        
        url,user,pw,used,limit,status,server_info=check_xtream(url,user,pw)
    if m3u: generate_m3u(url,user,pw,server_info,m3u)
else:
    if len(sys.argv)>2:
        m3u=sys.argv[2]
    else:
        m3u=None
    accts=[]
    with open(sys.argv[1]) as f:
        lines=f.readlines()
        for l in lines:
            if l.startswith('http'):
                try:
                    url,user,pw=l.strip().split()[:3]
                    accts.append(check_xtream(url,user,pw))
                    mag=False
                except:
                    url,user=l.strip().split()[:2]
                    accts.append(check_mag(url,user))
                    mag=True
                time.sleep(int(DELAY))
        if m3u:
            #remove dead accouts
            accts=[a for a in accts if a[-2].lower()=='active']
            #sort by max-active, descending
            accts.sort(key=lambda a: a[4]-a[3])
            for a in accts:
                print (a[4]-a[3], *a[0:3])
            if accts:
                if mag:
                    a=random.choice(accts)
                else:
                    a=accts[-1]
                generate_m3u(a[0],a[1],a[2],a[-1],m3u)
                if THREADFIN:
                    r=requests.post(THREADFIN,json=dict(cmd='update.m3u'))
                    print ('update',r,r.text)
                    r=requests.post(THREADFIN,json=dict(cmd='status'))
                    print ('status',r,r.text)

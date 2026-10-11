#!/usr/bin/python3
import time 
import json
import sys
import os
import re
from datetime import datetime
import requests
import http.server
import subprocess
import logging
import random
from logging.handlers import QueueHandler
from collections import deque
from urllib.parse import quote,unquote
from threading import Lock
from io import StringIO, BytesIO
import xmltv

global XMLTV_CACHE, PROCS, LOCK, LAST_REFRESH, LAST_RESCAN
PROCS={}
LOCK=Lock()
LAST_REFRESH=LAST_RESCAN=0
XMLTV_CACHE=None

def upper(s):
    if int(UPPER): 
        return s.upper() if s else ''
    else: 
        return s if s else ''

def config(config_file=None):
    ENV_VARS=['SERVER_IP','SERVER_PORT','CMD','TIMEOUT','DELAY','DIRECT','GROUPS','STREAMS','RENAME','REPLACE','FORMAT','BUFFER','LOGLEVEL','TUNER_COUNT','UPPER','CHECK','CACHE','LINEUP_CACHE','MIN_SOURCES','ORDER','AUTO_MAP']

    #set defaults 
    global SERVER_IP,SERVER_PORT,CMD,DELAY,DIRECT,GROUPS,STREAMS,RENAME,REPLACE,FORMAT,BUFFER,LOGLEVEL,LOGDEPTH,TUNER_COUNT,UPPER,CHECK,TIMEOUT,CACHE,LINEUP_CACHE,MIN_SOURCES,ORDER,AUTO_MAP

    LOGLEVEL=logging.INFO
    LOGDEPTH=100

    SERVER_IP='localhost'
    SERVER_PORT=5004

    TUNER_COUNT=4
    UPPER=1

    CMD='ffmpeg -hide_banner -loglevel error -i %s -c copy -copyts -f mpegts pipe:1'

    TIMEOUT=None
    DELAY=0
    DIRECT=0
    CHECK=0
    CACHE=0
    LINEUP_CACHE=86400
    MIN_SOURCES=0
    AUTO_MAP=1
    FORMAT='http://%s:%s/%s/%s/%s'
    GROUPS=''
    RENAME=''
    STREAMS=''
    REPLACE=''
    ORDER=''

    BUFFER=1024*1024 #buffer size for streaming

    #config from k=v in file
    if config_file:
        try:
            with open(config_file) as f:
                lines=f.readlines()
                for l in lines:
                    l=l.split('#')[0]
                    if '=' in l:
                        k,v=l.strip('\n').split('=',1)
                        k=k.upper()
                        # if key+=value extend value list else set key to value
                        if k.endswith('+'):
                            k=k[:-1]
                            if k in ENV_VARS:
                                globals()[k]+=','+v
                        elif k in ENV_VARS:
                            globals()[k]=v
        except Exception as e:
            logging.warning(e)
    #config from env
    for e in ENV_VARS:
        globals()[e]=os.getenv(e,globals()[e])

    #parse config
    global GROUPS_EXCLUDE,STREAMS_EXCLUDE
    ENV_VARS+=['GROUPS_EXCLUDE','STREAMS_EXCLUDE']
    # channel group regexes, !pattern to exclude
    GROUPS=upper(GROUPS).split(',')
    GROUPS_EXCLUDE=[f[1:] for f in GROUPS if f.startswith('!')]
    GROUPS=[f for f in GROUPS if f and not f.startswith('!') ]
    # channel name regexs to include or exclude if !pattern regardless of group
    STREAMS=upper(STREAMS).split(',')
    STREAMS_EXCLUDE=[c[1:] for c in STREAMS if c.startswith('!')]
    STREAMS=[c for c in STREAMS if c and not c.startswith('!')]
    # regex patterns to strip or replace in channel names. ^startwith, endswith$, or anywhere if no modifier
    # pattern=string will replace pattern with string
    RENAME=[r for r in upper(RENAME).split(',') if r]
    # replace any channels with base name if a channel matching regex exists 
    # example: REPLACE=' LHD$' will rename 'ABC LHD' to 'ABC', removing any STREAMS named 'ABC', but only if 'ABC LHD' exists.
    REPLACE=[r for r in upper(REPLACE).split(',') if r]
    # ordering of the lineup. Groups matching each pattern will be output in the order of the patterns. Remaining groups will be at end
    ORDER=[o for o in upper(ORDER).split(',') if o]

    # return config for info 
    return dict((k,globals()[k]) for k in ENV_VARS)

def xtream_request(url,user,pw,action=None,path='player_api.php',raw=False):
    # xtream codes API requests
    params={'username':user,'password':pw}
    if action:
        params.update(action=action)
    r=requests.get(url+'/'+path,params=params,timeout=float(TIMEOUT) if TIMEOUT else None)
    r.raise_for_status()
    return r.text if raw else json.loads(r.text)

def check_acct(url,user,pw,pri=0):
    # get server and account info
    info=None
    try:
        info=xtream_request(url,user,pw,'server_info')
        return (
            user, pw, pri, 
            int(info['user_info']['active_cons']), int(info['user_info']['max_connections']), info['user_info']['status'], 
            datetime.fromtimestamp(int(info['user_info']['exp_date'])) if info['user_info']['exp_date'] else None, 
            info
        )
    except Exception as e:
        # if account is not valid info will not parse so return 0/0 slots available and raw info
        logging.warning('%s checking %s %s %s %s',e,url,user,pw,info)
        return user, pw, pri, 0, 0, str(info), None, info

def refresh_accts(accounts):
    global LAST_REFRESH
    # check accounts and return map of source to accounts sorted by free slots
    sources={}
    n=int(CHECK)
    for url,accts in accounts.items():
        if n:
            accts=random.sample(accts,min(n,len(accts)))
        logging.info('refreshing %s',url)
        for a in accts:
            sources.setdefault(url,[]).append(check_acct(url,*a[0:3]))
            logging.debug('refresh: %s',sources[url][-1][-1])
            time.sleep(int(DELAY))
        #sort accounts by used-max to put active most free slots first
        sources[url].sort(key=lambda a:(not a[5]=='Active', a[3]-a[4]))
    LAST_REFRESH=time.time()
    return sources

def select_sources(sources,source_list=None,check_free=True):
    # return list of url, acct by highest priority and most free slots
    # first account per source will be most free slots, so filter to source in list if active and optinally any free slots
    # then sort by priority,used-max to prefer higher priority sources even if less free slots
    selected_sources=sorted(
        ( (source,accts[0]) for source,accts in sources.items() \
            if (not source_list or source in source_list) \
            and accts[0][5] == 'Active' \
            and (not check_free or accts[0][4]-accts[0][3] > 0) ),
        key=lambda s:(int(s[1][2]),s[1][3]-s[1][4])
    )
    logging.debug('select: %s',selected_sources)
    return selected_sources
    
def fetch_lineup(selected_sources):
    # fetch a lineup from each source, filter/modify channel names, and merge linueps
    global GROUPS_CACHE,STREAMS_CACHE
    GROUPS_CACHE={}
    STREAMS_CACHE={}
    for (url,acct) in selected_sources:
        logging.info('fetching %s',url)
        user,pw=acct[:2] 
        try:
            #fetch from selected source account, strip commas as plex does not like them and we can't filter on them
            GROUPS_CACHE[url]=dict( (e['category_id'],upper(e['category_name'].replace(',',''))) for e in xtream_request(url,user,pw,'get_live_categories') )
            streams=STREAMS_CACHE.setdefault(url,[])
            for s in xtream_request(url,user,pw,'get_live_streams'):
                s['name']=upper(s['name'].replace(',',''))
                streams.append(s)
            logging.debug('fetch: %s %s groups %s streams',url,len(GROUPS_CACHE[url]),len(STREAMS_CACHE[url]))
        except Exception as e:
            logging.warning('%s fetching %s %s %s',e,url,user,pw)
            continue

def fetch_xmltv(selected_sources):
    cache={'data':{},'channels':{},'programs':[]}
    for url, acct in selected_sources:
        user,pw=acct[:2]
        logging.info('XMLTV: fetching from %s',url)
        try:
            xmlin=StringIO(xtream_request(url,user,pw,path='xmltv.php',raw=True))
            xmlin.seek(0)
            cache['data'].update((k.replace('-','_'),v) for k,v in xmltv.read_data(xmlin).items())
            n=0
            xmlin.seek(0)
            for c in xmltv.read_channels(xmlin):
                if c['id']:
                    c['id'] = c['id'].lower()
                    cache['channels'][c['id']]=c
                    n+=1
            logging.debug('XMLTV: fetched %s channels from %s'%(n,url))
            n=0
            xmlin.seek(0)
            for p in xmltv.read_programmes(xmlin):
                if p['channel']:
                    p['channel'] = p['channel'].lower()
                    cache['programs'].append(p)
                    n+=1
            logging.debug('XMLTV: fetched %s programs from %s'%(n,url))
        except Exception as e:
            logging.warning('%s fetching XMLTV from %s %s %s',e,url,user,pw)
    return cache

def build_xmltv(cache):
    ids=set(l['GuideID'] for l in LINEUP.values()) #EPG ID of channels in lineup
    chs=[]
    writer=xmltv.Writer(**dict((k.replace('-','_'),v) for k,v in cache['data'].items()))
    #filter to lineup channels
    n=0
    for i in ids:
        c=cache['channels'].get(i)
        if c:
            n+=1
            writer.addChannel(c)
    logging.info('XMLTV: %s channels'%n)
    #add programs for lineup channels
    n=0
    for p in cache['programs']:
        if p['channel'] in ids:
            n+=1
            writer.addProgramme(p)
    logging.info('XMLTV: %s programs'%n)
    #generate XML
    xmlout=BytesIO()
    writer.write(xmlout)
    xmlout.seek(0)
    return xmlout.read().decode()

def build_lineup():
    global SOURCE_GROUPS,GROUP_ORDER
    lineup={}
    ids={}
    SOURCE_GROUPS={}
    GROUP_ORDER=[]
    for url in sorted(GROUPS_CACHE.keys()):
        SOURCE_GROUPS[url]=dict( (n,False) for n in GROUPS_CACHE[url].values() )
        #select groups by filters
        groups=dict( (i,n) for i,n in GROUPS_CACHE[url].items() if any(re.search(p,n) for p in GROUPS) and not any(re.search(p,n) for p in GROUPS_EXCLUDE) )
        SOURCE_GROUPS[url].update( (n,True) for n in groups.values() )
        #select streams by group or filter
        streams_in=[s for s in STREAMS_CACHE[url] if s['category_id'] in groups or any(re.search(p,upper(s['name'])) for p in STREAMS) ]
        #remove and rename streams
        streams=[]
        for s in streams_in:
            n=upper(s['name'])
            if  any(re.search(p,n) for p in STREAMS_EXCLUDE):
                continue
            for p in RENAME:
                if '=' in p:
                    p,r=p.split('=',1)
                else:
                    r=''
                n=re.sub(p,r,n)
            # each stream is name, EPG ID, source stream ID, group name
            streams.append(
                [n,
                s['epg_channel_id'].lower() if s['epg_channel_id'] and ' ' not in s['epg_channel_id'] else None,
                s['stream_id'],
                GROUPS_CACHE[url][s['category_id']]]
            )
        #replace channels without pattern if channel with pattern exists
        for p in REPLACE:
            replaced=set()
            replaced.update(re.sub(p,'',s[0]) for s in streams if re.search(p,s[0]))
            #remove replaced channels
            streams=[s for s in streams if s[0] not in replaced]
            #rename name+pattern to name to replace channel
            for s in streams:
                s[0]=re.sub(p,'',s[0])
        #strip any remaining leading/trailing whitespace
        for s in streams:
            s[0]=s[0].strip()
        logging.info('%s %s streams',url,len(streams))
        # build lineup by merging streams across sources
        for s in streams:
            k=s[0].lower().replace(' ','_') 
            #auto mapping will merge channels with different names if the epg ids match
            if int(AUTO_MAP) and k not in lineup and s[1] in ids:
                    logging.debug('lineup: %s mapped %s to %s',s[1],k,ids[s[1]])
                    k=ids[s[1]]
            #map EPG IDs to key
            if s[1]:
                ids[s[1]]=k
            #create channel and add this source
            lineup.setdefault(k, {
                                'GuideNumber':s[0],
                                'GuideName':s[0], 
                                'GuideID':s[1],
                                'GuideCategory':s[3],
                                'sources':{},
                                'URL':'http://%s:%s/stream/%s'%(SERVER_IP,SERVER_PORT,quote(k))
                            })['sources'][url]=s[2]
            #ensure a guide ID if possible
            if not lineup[k]['GuideID']:
                lineup[k]['GuideID']=s[1]
    #if set, filter to channels with EPG IDs and at least MIN_SOURCES sources
    if int(MIN_SOURCES):
        lineup=dict((k,l) for k,l in lineup.items() if l['GuideID'] and len(l['sources'])>=int(MIN_SOURCES))
    # now order the lineup
    ordered_lineup={}
    ordered_groups=dict(sorted((l['GuideCategory'],False) for l in lineup.values()))
    for o in ORDER+['']: #empty string at end will output all remaining groups
        for g in ordered_groups:
            #if group matches pattern and has not been output
            if not ordered_groups[g] and re.search(o,g):
                #output group ahd mark as output
                ordered_lineup.update((k,l) for k,l in lineup.items() if l['GuideCategory']==g)
                ordered_groups[g]=True
                logging.debug('lineup: ordering group %s',g)
                GROUP_ORDER.append(g)
    logging.info('lineup has %s streams',len(ordered_lineup))
    return ordered_lineup

def rescan(config_file,fetch=None,get_xmltv=False):
    global ACCOUNTS,SOURCES,LINEUP,LAST_RESCAN,XMLTV_CACHE
    try:
        #reload config 
        logging.info('reloading %s',config_file)
        config(config_file)
        #load accounts from config
        ACCOUNTS={}
        with open(config_file) as f:
            lines=f.readlines()
            for l in lines:
                if l.startswith('http'):
                    try:
                        l=l.strip().split()
                        url,user,pw=l[:3]
                        pri=l[3] if len(l)>3 else 0
                        ACCOUNTS.setdefault(url,[]).append((user,pw,pri))
                    except: pass
        #refresh account status and lineup if fetch=True, or cache expired
        if fetch is True or fetch is None and (not LAST_RESCAN or LAST_RESCAN + int(LINEUP_CACHE) < time.time()):
            SOURCES=refresh_accts(ACCOUNTS)
            selected=select_sources(SOURCES,check_free=False)
            fetch_lineup(selected)
            XMLTV_CACHE=None #clear cached XMLTV on lineup rescan 
            LAST_RESCAN=time.time()
        else:
            selected=None
            logging.debug('lineup cached for %ss',int(int(LINEUP_CACHE)+LAST_RESCAN-time.time()))
        #refresh xmltv if requested
        if get_xmltv and not XMLTV_CACHE:
            selected=select_sources(SOURCES,check_free=False)
            XMLTV_CACHE=fetch_xmltv(selected)
        #build lineup
        LINEUP=build_lineup()
        return LINEUP,selected,SOURCES
    except Exception as e:
        logging.exception(e)
        logging.warning('no usable accounts: %s',e)
        LINEUP=None
        return None,None,SOURCES

def generate_m3u(lineup):
    i=0
    m3u=[]
    m3u.append('#EXTM3U')
    for k,l in lineup.items():
        m3u.append('#EXTINF:-1 group-title="%s" tvg-id="%s" tvg-name="%s",%s' % (l['GuideCategory'],l['GuideID'],l['GuideName'],l['GuideNumber']))
        m3u.append('http://%s:%s/stream/%s' % ( SERVER_IP, SERVER_PORT, k))
        i+=1
    logging.info('M3U: %s streams'%i)
    return m3u

class HDHR_handler(http.server.BaseHTTPRequestHandler):
    # emulate a HDHR
    def send(self,html='',redirect=None,code=200):
        self.send_response(302 if redirect and code==200 else code)
        if redirect:
            self.send_header('Location',redirect)
        self.end_headers()
        self.wfile.write(html.encode())

    def add_filter(self,filter_type,filter_text,rename_to=None,start=False,end=False,exclude=False,comment=''):
        global CONFIG_FILE
        #read exiting config, if this type of filter is in there we need to make it type+=value, not type=value
        with open(CONFIG_FILE) as f:
            lines=f.readlines()
        if any(l.startswith(filter_type) for l in lines):
            filter_type+='+'
        if start:
            filter_text='^'+filter_text
        if end:
            filter_text+='$'
        if rename_to:
            filter_text+='='+rename_to
        if exclude:
            filter_text='!'+filter_text
        with open(CONFIG_FILE,'a') as f:
            l=filter_type+'='+filter_text+'#'+comment
            f.write(l+'\n')
            logging.info('added %s to %s',l,CONFIG_FILE)
        return False #we do not need to fetch every time we add a filter

    def do_POST(self):
        global CONFIG_FILE, LOCK
        with LOCK:
            # fetch account status and lineup on account add or reload request. Will be set to False if config edit to speed up response time
            fetch=True 
            # get POST data 
            l=int(self.headers.get('content-length',0))
            body=self.rfile.read(l)
            params=dict( unquote(p.replace('+',' ')).split('=',1) \
                if p else (None,None) \
                for p in body.decode().split('&'))
            for key,val in params.items():
                try:
                    if val:
                        # fitler params were submitted
                        if key == 'group':
                            fetch=self.add_filter('groups',val,start='group_start' in params,end='group_end' in params,comment=params['comment'])
                        if key == 'stream':
                            fetch=self.add_filter('streams',val,start='stream_start' in params,end='stream_end' in params,exclude='stream_exclude' in params,comment=params['comment'])
                        if key == 'rename':
                            fetch=self.add_filter('rename',val,start='rename_start' in params,end='rename_end' in params,rename_to=params['rename_to'],comment=params['comment'])
                        if key == 'replace':
                            fetch=self.add_filter('replace',val,start='replace_start' in params,end='replace_end' in params,comment=params['comment'])
                    # config text was submitted
                    if key == 'config':
                        fetch=False
                        with open(CONFIG_FILE,'w') as f:
                            f.write(val)
                            logging.info('wrote %s',CONFIG_FILE) 
                    # url/hash was submitted, add account and rescan
                    if key == 'add':
                        from lookup import iptvlookup
                        info=iptvlookup(val)
                        if info:
                            with open(CONFIG_FILE,'a') as f:
                                f.write(info+'\n')
                                logging.info('fetched %s, added %s to %s',val,info,CONFIG_FILE)     
                except Exception as e:
                    logging.exception(e)
                    self.send(str(e),code=500)
                    return
            #reload config, scan sources if fetch is True, rebuild lineup
            rescan(CONFIG_FILE,fetch)
        #respond like GET of posting page
        self.do_GET()
        return

    def do_GET(self):
        global CONFIG_FILE,FORMAT,ACCOUNTS,SOURCES,LINEUP,PROCS,LOGQ,LOCK,SOURCE_GROUPS,LAST_REFRESH

        #serve streams
        if self.path.startswith('/stream/'):
            k=unquote(self.path.split('/stream/')[-1])
            if LINEUP and k in LINEUP:
                logging.info('%s stream %s'%(self.client_address,k))
                l=LINEUP[k]
                with LOCK:
                    if LAST_REFRESH and LAST_REFRESH + int(CACHE) > time.time():
                        logging.debug('accounts cached for %ss',int(int(CACHE)+LAST_REFRESH-time.time()))
                    else:
                        SOURCES=refresh_accts(ACCOUNTS)
                try:
                    (source,a)=select_sources(SOURCES,list(l['sources'].keys()))[0]
                except:
                    self.send('no source for %s' % k,code=503)
                    return
                url = FORMAT % (a[-1]['server_info']['url'], 
                                a[-1]['server_info']['port'], a[0], a[1], 
                                l['sources'][source])
                if int(DIRECT):
                    # send the URL to plex
                    logging.info('%s redirect to %s', self.client_address, url)
                    self.send(redirect=url)
                    return
                else:
                    # remux with ffmpeg
                    args = CMD % url
                    logging.info('%s starting %s', self.client_address, args)
                    try:
                        cmd = subprocess.Popen(args.split(), shell=False, stdout=subprocess.PIPE)
                        logging.info('%s pid %s started', self.client_address, cmd.pid)
                        PROCS[cmd.pid]=(self.client_address,k,args)
                    except Exception as e:
                        logging.exception(e)
                        self.send(str(e),code=500)
                        return
                    self.send()
                    try:
                        while cmd.poll() is None: #cmd exited
                            data = cmd.stdout.read(int(BUFFER))
                            if not data: # cmd exited, source dropped the stream
                                break
                            self.wfile.write(data)
                        # cmd exited likely source dropped the stream
                        logging.warning('%s pid %s exited',self.client_address, cmd.pid)
                        LAST_REFRESH=0 #invalidate cache as account may be full
                    except BrokenPipeError: 
                        logging.debug('stream: %s pid %s disconnected',self.client_address, cmd.pid)
                    except Exception as e:
                        logging.exception(e)
                    cmd.stdout.close() # stop cmd if running
                    cmd.wait() # wait for exit
                    logging.info('%s pid %s stop (%d)', self.client_address, cmd.pid, cmd.returncode)
                    del PROCS[cmd.pid]
                    return
            else:
                self.send('%s not in lineup' % k,code=404)
                return

        #serve HDHR data
        elif self.path=='/discover.json':
            self.send(json.dumps({
                "DeviceID": "TUNER",
                "FriendlyName": "Tuner",
                "TunerCount": int(TUNER_COUNT),
                'BaseURL':'http://%s:%s'%(SERVER_IP,SERVER_PORT),
                'LineupURL':'http://%s:%s/lineup.json'%(SERVER_IP,SERVER_PORT),
            }))
            return

        elif self.path=='/lineup_status.json':
            self.send(json.dumps({
                'ScanInProgress':0,
                'ScanPossible':1,
                'Source':'Cable'
            }))
            return

        elif self.path=='/lineup.json':
            with LOCK:
                rescan(CONFIG_FILE) #will refresh if cache expired and rebuild lineup
            self.send(json.dumps(list(LINEUP.values())))
            return

        #serve M#U/XMLTV
        elif self.path=='/m3u':
            with LOCK:
                rescan(CONFIG_FILE) #will refresh if cache expired and rebuild lineup
            self.send('\n'.join(generate_m3u(LINEUP)))
            return

        elif self.path=='/xmltv':
            with LOCK:
                rescan(CONFIG_FILE,get_xmltv=True)
            self.send(build_xmltv(XMLTV_CACHE))
            return 

        #serve HTML pages
        else:
            try:
                code=200
                html=self.html_start()

                #serve lineup
                if self.path.startswith('/lineup'):
                    html+='''<p><table>'''
                    if LINEUP:
                        for g in GROUP_ORDER:
                            html+='<tr/><tr><th colspan=2 id="%s">%s</th></tr>\n'%(quote(g),g)
                            for k,l in [(k,l) for k,l in LINEUP.items() if l['GuideCategory']==g]:
                                html+='<tr><td>%s</td><td><a href="%s">%s</a></td><td>%s</td></tr>\n'%(
                                    ''.join(s.split('//')[1][0] for s in l['sources']),
                                    l['URL'],
                                    l['GuideName'],
                                    l['GuideID'] if l['GuideID'] else ''
                                )
                        html+='''</table></p>'''
                    html+='''<p><table>'''
                    for s,sg in SOURCE_GROUPS.items():
                        html+='<tr><th>%s</th><td>%s</td></tr>'%(
                            s,
                            ','.join('<a href="#%s">%s</a>'%(quote(g),g) if e else g for g,e in sorted(sg.items()))
                        )
                    html+='''</table></p>'''
                    # filter builder
                    html+='''<datalist id=group-select>'''
                    for s,sg in SOURCE_GROUPS.items():
                        #escape | character, this is common in group names and will break regexes
                        html+='\n'.join('<option value="%s">'%g.replace('|','\|') for g in sorted(sg.keys()))
                    html+='''</datalist>'''
                    html+='''<form method=post><p><table>
                    <tr><th>group:</th><td><input type=checkbox title='match at start' name=group_start><input type=text size=30 name=group list=group-select><input type=checkbox title='match at end' name=group_end></td></tr>
                    <tr><th>streams:</th><td><input type=checkbox title='match at start' name=stream_start><input type=text size=30 name=stream><input type=checkbox title='match at end' name=stream_end></td>
                    <td><input type=checkbox title='exclude matching streams' name=stream_exclude>exclude</td></tr>
                    <tr><th>rename:</th><td><input type=checkbox title='match at start' name=rename_start><input type=text size=30 title='remove matching text from name' name=rename><input type=checkbox title='match at end' name=rename_end></td>
                    <td> to <input type=text title='replace matching text with' name=rename_to></td></tr>
                    <tr><th>replace:</th><td><input type=checkbox title='match at start' name=replace_start><input type=text title='if channel matching pattern exists, replace channels matching name without pattern' size=30 name=replace><input type=checkbox title='match at end' name=replace_end></td></tr>
                    <tr><th>comment:</th><td><input type=text size=35 name=comment></td><td><input type=submit value="add filter"></tr>
                    </table></p></form>'''
                #serve logs
                elif self.path.startswith('/log'): 
                    html=self.html_start()
                    html+='''<p>'''
                    for l in LOGQ:
                        html+=l.msg+'<br>'
                    html+='''</p>'''

                # serve status page
                elif self.path == '/': 
                    if PROCS:
                        html+='''<p><table><tr><th>pid</th><th>client</th><th>stream</th><th>command</th></tr>'''
                        for pid,args in PROCS.items():
                            html+='<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n'%(pid,*args)
                        html+='''
                        </table>
                    </p>'''
                    with LOCK:
                        env=config(CONFIG_FILE)
                    if SOURCES:
                        html+='''<p><table><tr><th></th><th>user</th><th>pass</th><th>priority</th><th colspan=2>status</th><th>expires</th>'''
                        if CACHE:
                            t=int(int(CACHE)+LAST_REFRESH-time.time())
                            if t > 0:
                                html+='<td>(cached for %ss)</td>'%t
                        html+='''</tr>'''
                        for url,accts in SOURCES.items():
                            html+='<tr><th colspan=7>%s</th><td>(%s streams)</td></tr>'%(url,
                            len(list(s for s in LINEUP.values() if url in s['sources'])) if LINEUP else '0')
                            for a in accts:
                                html+='<tr><td></td><td>%s</td><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td><td>%s</td></tr>\n'%a[:-1]
                        html+='''<tr><td><form method=post><input type=submit name=reload value=reload></form></td></tr>'''
                        if LINEUP:
                            url='http://%s:%s/m3u' % (SERVER_IP,SERVER_PORT)
                            html+='<tr><th>M3U</th><td colspan=6><a href="'+url+'">'+url+'</a></td>'
                            if LINEUP_CACHE:
                                t=int(int(LINEUP_CACHE)+LAST_RESCAN-time.time())
                                if t > 0:
                                    html+='<td>(cached for %ss)</td>'%t
                            html+='</tr>'
                            url='http://%s:%s/xmltv' % (SERVER_IP,SERVER_PORT)
                            html+='<tr><th>XMLTV</th><td colspan=6><a href="'+url+'">'+url+'</a></td></tr>'
                        html+='''</table></p>'''
                    html+='''<p><table>'''
                    for k,v in sorted(env.items()):
                        html+='<tr><th>%s</th><td>%s</td></tr>\n'%(k,v)
                    html+='''</table></p>'''

                #invalid route
                else:
                    self.send(self.path,code=400)
                    return
            
            #output what we can
            except Exception as e:
                logging.exception(e)
                code=500
                html+='\n'+str(e)

            #alwwys add config editor
            html+=self.html_end()
            self.send(html,code=code)
            return

    def html_start(self):
        global LINEUP
        return '''<html>
    <head>
        <style>
            body{font-family:monospace}
            th{text-align:left}
        </style>
    </head>
    <body>
        <p><table><tr>
            <th><a href='/'>status</a>&nbsp;&nbsp;&nbsp;</th>
            <th><a href='/log'>log</a>&nbsp;&nbsp;&nbsp;</th>
            <th><a href='/lineup'>lineup</a>&nbsp;</th><td>(%s streams)</td>
        </tr></table></p>''' % (len(list(LINEUP)) if LINEUP else '0')

    def html_end(self):
        html=''
        if CONFIG_FILE:
            html+='''
            <p>&nbsp;</p><p><form method=post>
            <textarea style=font-family:monospace name=config cols=100 rows=20>'''
            try:
                with open(CONFIG_FILE) as f:
                    html+=f.read(-1)
            except Exception as e:
                html+=str(e)
            html+='''</textarea><br>
            <input type=submit value="save config">
            </form></p>'''
            html+='''
            <p><form method=post>
                <a href='https://iptvlookup.com/list?filter_type=xtream' target=_new>IPTVlookup</a> URL: <input type=text size=80 name=add>
                <input type=submit value="add account">
            </form></p>'''
        html+='''</body><html>'''
        return html
        

class LogQ(deque):
   '''leaky queue that drops oldest items'''
   def put_nowait(self, item, **kwargs):
        self.append(item)

def main(*args):
    global CONFIG_FILE, LOGQ
    CONFIG_FILE=args[0] if args else None
    #init logging
    env=config(CONFIG_FILE)
    LOGQ=LogQ(maxlen=LOGDEPTH)
    logging.basicConfig(level=int(LOGLEVEL), 
                        format='%(asctime)s %(levelname)s:%(message)s', 
                        handlers=[logging.StreamHandler(),QueueHandler(LOGQ)])
    for k,v in env.items():
        logging.debug('%s=%s',k,v)
    # do initial scan
    rescan(CONFIG_FILE)
    # start server
    httpd = http.server.ThreadingHTTPServer((SERVER_IP, int(SERVER_PORT)), HDHR_handler)
    logging.info('serving at http://%s:%s' % (SERVER_IP, SERVER_PORT))
    httpd.serve_forever()

if __name__ == '__main__':    
    main(*sys.argv[1:])

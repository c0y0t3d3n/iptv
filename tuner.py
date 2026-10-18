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

def upper(s):
    global UPPER
    if UPPER: 
        return s.upper() if s else ''
    else: 
        return s if s else ''


class Config:
    #variables we accept and types to cast to
    env_vars={
        'server_ip':str,
        'server_port':int,
        'cmd':str,
        'timeout':int,
        'delay':int,
        'direct':int,
        'groups':str,
        'streams':str,
        'rename':str,
        'group_rename':str,
        'replace':str,
        'format':str,
        'buffer':int,
        'loglevel':int,
        'tuner_count':int,
        'upper':int,
        'check':int,
        'cache':int,
        'lineup_cache':int,
        'min_sources':int,
        'order':str,
        'auto_map':int,
        'user_agent':str,
        'groups_exclude':str,
        'streams_exclude':str}

    #defaults
    loglevel=logging.INFO
    logdepth=100
    server_ip='localhost'
    server_port=5004
    tuner_count=4
    upper=1
    cmd='ffmpeg -hide_banner -loglevel error -i %s -c copy -copyts -f mpegts pipe:1'
    timeout=0
    delay=0
    direct=0
    check=0
    cache=0
    lineup_cache=86400
    min_sources=0
    auto_map=1
    format='http://%s:%s/%s/%s/%s'
    groups=''
    groups_exclude=''
    rename=''
    group_rename=''
    streams=''
    streams_exclude=''
    replace=''
    order=''
    user_agent=''
    buffer=1024*1024 #buffer size for streaming

    def __init__(self,config_file=None):
        self.config_file=config_file
        self.lock=Lock()

    def reload(self):
        '''read, build, and return config'''
        with self.lock:
            #config from k=v in file
            if self.config_file:
                try:
                    with open(self.config_file) as f:
                        lines=f.readlines()
                        for l in lines:
                            l=l.split('#')[0]
                            if '=' in l:
                                k,v=l.strip('\n').split('=',1)
                                k=k.lower()
                                # if key+=value extend value list else set key to value
                                if k.endswith('+'):
                                    k=k[:-1]
                                    if k in self.env_vars:
                                        setattr(self,k,getattr(self,k)+','+v)
                                elif k in self.env_vars:
                                    setattr(self,k,v)
                except Exception as e:
                    logging.warning(e)
            #config from env_vars
            for k,t in self.env_vars.items():
                try:
                    #get from env overrides
                    v=os.getenv(k.upper(),getattr(self,k))
                    #do not cast if list
                    if type(v) is not list and type(v) is not t:
                        v=t(v)
                    setattr(self,k,v)

                except Exception as e:
                    logging.warning('%s: %s',k,e)

            #set global uppercasing
            global UPPER
            UPPER=self.upper

            #parse filter args
            # channel group regexes, !pattern to exclude
            self.groups=upper(self.groups).split(',')
            self.groups_exclude=[p[1:] for p in self.groups if p.startswith('!')]
            self.groups=[p for p in self.groups if p and not p.startswith('!') ]
            # channel name regexs to include or exclude if !pattern regardless of group
            self.streams=upper(self.streams).split(',')
            self.streams_exclude=[p[1:] for p in self.streams if p.startswith('!')]
            self.streams=[p for p in self.streams if p and not p.startswith('!')]
            # regex patterns to strip or replace in channel names. ^startwith, endswith$, or anywhere if no modifier
            # pattern=string will replace pattern with string
            self.rename=[p for p in upper(self.rename).split(',') if p]
            self.group_rename=[p for p in upper(self.group_rename).split(',') if p]
            # replace any channels with base name if a channel matching regex exists 
            # example: REPLACE=' LHD$' will rename 'ABC LHD' to 'ABC', removing any STREAMS named 'ABC', but only if 'ABC LHD' exists.
            self.replace=[p for p in upper(self.replace).split(',') if p]
            # ordering of the lineup. Groups matching each pattern will be output in the order of the patterns. Remaining groups will be at end
            self.order=[p for p in upper(self.order).split(',') if p]

            # return config for info 
            return dict((e,getattr(self,e)) for e in self.env_vars)

    def get_config(self):
        with self.lock:
            try:
                with open(self.config_file) as f:
                    return f.read(-1)
            except Exception as e:
                return str(e)

    def get_accounts(self):
        with self.lock:
            accounts={}
            with open(self.config_file) as f:
                lines=f.readlines()
                for l in lines:
                    if l.startswith('http'):
                        try:
                            l=l.strip().split()
                            url,user,pw=l[:3]
                            pri=l[3] if len(l)>3 else 0
                            accounts.setdefault(url,[]).append(Account(user,pw,pri))
                        except Exception as e:
                            logging.debug('%s parsing %s',e,l)
        return accounts

    def write(self,text):    
        with self.lock:    
            with open(self.config_file,'w') as f:
                f.write(text)
                logging.info('wrote %s bytes to %s',len(text),self.config_file)

    def append(self,text):
        with self.lock:
            with open(self.config_file,'a') as f:
                f.write(text+'\n')
                logging.info('added %s to %s',text,self.config_file)

    def add_account(self,text):
        from lookup import iptvlookup
        logging.info('looking up %s',text)
        info=iptvlookup(text)
        if info:
            self.append(info)

    def add_filter(self,filter_type,filter_text,rename_to=None,start=False,end=False,exclude=False,comment=''):
        #read exiting config, if this type of filter is in there we need to make it type+=value, not type=value
        with self.lock:
            with open(self.config_file) as f:
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
        self.append(filter_type+'='+filter_text+'#'+comment)


class Account:
    '''struct for xtream codes account and status'''
    def __init__(self,username,password,priority=0,active_conns=0,max_conns=0,status=None,expires=None,info={}):
        self.username=username
        self.password=password
        self.priority=int(priority) if priority else 0
        self.active_conns=int(active_conns) if active_conns else 0
        self.max_conns=int(max_conns) if max_conns else 0
        self.status=status
        self.expires=datetime.fromtimestamp(int(expires)) if expires else None
        self.info=info

    def active(self):
        return str(self.status).lower()=='active'

    def available(self):
        return self.max_conns-self.active_conns

    def data(self):
        return self.username,self.password,self.priority,self.active_conns,self.max_conns,self.status,self.expires

    def __repr__(self):
        return str(self.data())


class Data:
    '''data store for accounts, lineup, and EPG'''
    def __init__(self,config):
        self.config=config
        self.last_refresh=0
        self.last_rescan=0
        self.xmltv_cache=None
        self.accounts_lock=Lock()
        self.rescan_lock=Lock()
        self.accounts={}
        self.lineup=None
        
    def xtream_request(self,url,a,action=None,path='player_api.php',raw=False):
        # xtream codes API requests
        headers={}
        if self.config.user_agent:
            headers['User-Agent']=self.config.user_agent
        params={'username':a.username,'password':a.password}
        if action:
            params.update(action=action)
        r=requests.get(url+'/'+path,params=params,headers=headers,timeout=self.config.timeout if self.config.timeout else None)
        r.raise_for_status()
        return r.text if raw else json.loads(r.text)

    def check_acct(self,url,a):
        # get server and account info
        info=None
        try:
            info=self.xtream_request(url,a,'server_info')
            return Account(
                a.username, a.password, a.priority, 
                info['user_info']['active_cons'], 
                info['user_info']['max_connections'], 
                info['user_info']['status'], 
                info['user_info']['exp_date'], 
                info
            )
        except Exception as e:
            # if account is not valid info will not parse so return 0/0 slots available and raw info
            logging.warning('%s checking %s %s %s %s',e,url,a.username,a.password,a.info)
            return Account(a.username,a.password,a.priority,str(info),None,info)
            
    def refresh_accts(self,accounts=None,force_refresh=False):
        with self.accounts_lock:
            if force_refresh or not self.last_refresh or self.last_refresh + self.config.cache < time.time():
                if accounts is None:
                    accounts=self.accounts.copy()
                self.accounts={}
                # check accounts and return map of source to accounts sorted by free slots
                for url,accts in accounts.items():
                    if self.config.check:
                        accts=random.sample(accts,min(self.config.check,len(accts)))
                    logging.info('refreshing %s',url)
                    for a in accts:
                        self.accounts.setdefault(url,[]).append(self.check_acct(url,a))
                        logging.debug('refresh: %s',self.accounts[url][-1].info)
                        time.sleep(self.config.delay)
                    #sort accounts to put active most free slots first
                    self.accounts[url].sort(key=lambda a:(a.active(), a.available()), reverse=True)
                self.last_refresh=time.time()
            else:
                logging.debug('accounts cached for %ss',int(self.config.cache+self.last_refresh-time.time()))

    def select_sources(self,source_list=None,check_free=True):
        # return list of url, acct by highest priority and most free slots
        # first account per source will be most free slots, so filter to source in list if active and optinally any free slots
        # then sort by priority,-available to prefer highest  priority sources even if less free slots
        selected_sources=sorted(
            ( (url,accts[0]) for url,accts in self.accounts.items() \
                if (not source_list or url in source_list) \
                and accts[0].active() \
                and (not check_free or accts[0].available() > 0) ),
            key=lambda e:(e[1].priority,-e[1].available())
        )
        logging.debug('select: %s',selected_sources)
        return selected_sources

    def fetch_lineup(self,selected_sources):
        # fetch a lineup from each source, filter/modify channel names, and merge linueps
        self.groups_cache={}
        self.streams_cache={}
        for (url,a) in selected_sources:
            logging.info('fetching lineup from %s',url)
            try:
                #fetch from selected source account, strip commas as plex does not like them and we can't filter on them
                self.groups_cache[url]=dict( (e['category_id'],upper(e['category_name'].replace(',',''))) for e in self.xtream_request(url,a,'get_live_categories') )
                streams=self.streams_cache.setdefault(url,[])
                for s in self.xtream_request(url,a,'get_live_streams'):
                    s['name']=upper(s['name'].replace(',',''))
                    streams.append(s)
                logging.debug('fetch: %s %s groups %s streams',url,len(self.groups_cache[url]),len(self.streams_cache[url]))
            except Exception as e:
                logging.warning('%s fetching %s %s %s',e,url,a.username,a.password)
                continue

    def fetch_xmltv(self,selected_sources):
        self.xmltv_cache={'data':{},'channels':{},'programs':[]}
        for url, a in selected_sources:
            logging.info('fetching XMLTV from %s',url)
            try:
                xmlin=StringIO(self.xtream_request(url,a,path='xmltv.php',raw=True))
                xmlin.seek(0)
                self.xmltv_cache['data'].update((k.replace('-','_'),v) for k,v in xmltv.read_data(xmlin).items())
                n=0
                xmlin.seek(0)
                for c in xmltv.read_channels(xmlin):
                    if c['id']:
                        c['id'] = c['id'].lower()
                        self.xmltv_cache['channels'][c['id']]=c
                        n+=1
                logging.debug('XMLTV: fetched %s channels from %s'%(n,url))
                n=0
                xmlin.seek(0)
                for p in xmltv.read_programmes(xmlin):
                    if p['channel']:
                        p['channel'] = p['channel'].lower()
                        self.xmltv_cache['programs'].append(p)
                        n+=1
                logging.debug('XMLTV: fetched %s programs from %s'%(n,url))
            except Exception as e:
                logging.warning('%s fetching XMLTV from %s %s %s',e,url,a.username,a.password)

    def build_xmltv(self):
        ids=set(l['GuideID'] for l in self.lineup.values()) #EPG ID of channels in lineup
        chs=[]
        writer=xmltv.Writer(**dict((k.replace('-','_'),v) for k,v in self.xmltv_cache['data'].items()))
        #filter to lineup channels
        nc=np=0
        for i in ids:
            c=self.xmltv_cache['channels'].get(i)
            if c:
                nc+=1
                writer.addChannel(c)
        #add programs for lineup channels
        np=0
        for p in self.xmltv_cache['programs']:
            if p['channel'] in ids:
                np+=1
                writer.addProgramme(p)
        #generate XML
        xmlout=BytesIO()
        writer.write(xmlout)
        xmlout.seek(0)
        logging.info('XMLTV has %s channels, %s programs',nc,np)
        return xmlout.read().decode()

    def build_lineup(self):
        self.source_groups={}
        self.group_order=[]
        ids={}  
        groups={}
        lineup={}
        for url in sorted(self.groups_cache.keys()):
            for i,g in self.groups_cache[url].items():
                for p in self.config.group_rename:
                    if '=' in p:
                        p,r=p.split('=',1)
                    else:
                        r=''
                    g=re.sub(p,r,g)
                #id:[renamed group, selected]
                self.source_groups.setdefault(url,dict())[i]=[g,False]
                #select groups by filters
                if any(re.search(p,g) for p in self.config.groups) and not any(re.search(p,g) for p in self.config.groups_exclude):
                    self.source_groups[url][i][1]=True
            #select streams by group or filter
            streams_in=[ s for s in self.streams_cache[url] if \
                self.source_groups[url].get(s['category_id'],[None,None])[1] \
                or any(re.search(p,upper(s['name'])) for p in self.config.streams) ]
            #remove and rename streams
            streams=[]
            for s in streams_in:
                n=upper(s['name'])
                if  any(re.search(p,n) for p in self.config.streams_exclude):
                    continue
                for p in self.config.rename:
                    if '=' in p:
                        p,r=p.split('=',1)
                    else:
                        r=''
                    n=re.sub(p,r,n)
                # each stream is name, EPG ID, source stream ID, group name. Filter bogus EPG IDs
                streams.append(
                    [n,
                    s['epg_channel_id'].lower() if s['epg_channel_id'] and ' ' not in s['epg_channel_id'] else None,
                    s['stream_id'],
                    self.source_groups[url].get(s['category_id'],[s['category_id']])[0]]
                )
            #replace channels without pattern if channel with pattern exists
            for p in self.config.replace:
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
            logging.debug('lineup: %s %s groups %s streams',url,len([g for g in self.source_groups[url].values() if g[1]]),len(streams))
            # build lineup by merging streams across sources
            for s in streams:
                k=s[0].lower().replace(' ','_') 
                #auto mapping will merge channels with different names if the epg ids match
                if self.config.auto_map and k not in lineup and s[1] in ids:
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
                                    'URL':'http://%s:%s/stream/%s'%(self.config.server_ip,self.config.server_port,quote(k))
                                })['sources'][url]=s[2]
                #ensure a guide ID if possible
                if not lineup[k]['GuideID']:
                    lineup[k]['GuideID']=s[1]
        #if set, filter to channels with EPG IDs and at least MIN_SOURCES sources
        if self.config.min_sources:
            lineup=dict((k,l) for k,l in lineup.items() if l['GuideID'] and len(l['sources'])>=self.config.min_sources)
        # now order the final lineup
        self.lineup={}
        ordered_groups=dict(sorted((l['GuideCategory'],False) for l in lineup.values()))
        for o in self.config.order+['']: #empty string at end will output all remaining groups
            for g in ordered_groups:
                #if group matches pattern and has not been output
                if not ordered_groups[g] and re.search(o,g):
                    #output group ahd mark as output
                    self.lineup.update((k,l) for k,l in lineup.items() if l['GuideCategory']==g)
                    ordered_groups[g]=True
                    logging.debug('lineup: ordering group %s',g)
                    self.group_order.append(g)
        logging.info('lineup has %s groups, %s streams',len(ordered_groups),len(self.lineup))
        return self.lineup

    def rescan(self,force_rescan=None,get_xmltv=False):
        with self.rescan_lock:
            try:
                self.config.reload()
                #refresh account status and lineup if fetch=True, or cache expired
                if force_rescan or not self.last_rescan or self.last_rescan + self.config.lineup_cache < time.time():
                    #load accounts from config
                    self.refresh_accts(self.config.get_accounts(),force_rescan)
                    self.fetch_lineup(self.select_sources(check_free=False))
                    self.xmltv_cache=None #clear cached XMLTV on lineup rescan 
                    self.last_rescan=time.time()
                else:
                    logging.debug('lineup cached for %ss',int(int(self.config.lineup_cache)+self.last_rescan-time.time()))
                #refresh xmltv if requested
                if get_xmltv and not self.xmltv_cache:
                    self.fetch_xmltv(self.select_sources(check_free=False))
                #build lineup
                self.build_lineup()
            except Exception as e:
                logging.warning('no usable accounts: %s',e)
                raise e

    def generate_m3u(self):
        m3u=[]
        m3u.append('#EXTM3U')
        for k,l in self.lineup.items():
            m3u.append('#EXTINF:-1 group-title="%s" tvg-id="%s" tvg-name="%s",%s' % (l['GuideCategory'],l['GuideID'],l['GuideName'],l['GuideNumber']))
            m3u.append('http://%s:%s/stream/%s' % ( self.config.server_ip, self.config.server_port, k))
        return m3u


class Stream:
    '''streaming subprocess handler'''
    def __init__(self,config,data):
        self.config=config
        self.data=data
        self.procs={}

    def get_stream_url(self,k):
        self.data.refresh_accts()
        l=self.data.lineup[k]
        try:
            #get the best source/account for this channe;l
            (source,a)=self.data.select_sources(list(l['sources'].keys()))[0]
            #format the stream URL
            return self.config.format % (a.info['server_info']['url'], 
                    a.info['server_info']['port'], a.username, a.password, 
                    l['sources'][source])
        except Exception as e: #no source for this stream
            logging.debug(e)

    def serve_stream(self,k,handler):
        url=self.get_stream_url(k)
        if url:
            # remux with ffmpeg
            args = self.config.cmd % url
            logging.info('%s starting %s', handler.client_address, args)
            try:
                cmd = subprocess.Popen(args.split(), shell=False, stdout=subprocess.PIPE)
                logging.info('%s pid %s started', handler.client_address, cmd.pid)
                self.procs[cmd.pid]=(handler.client_address,k,args)
            except Exception as e:
                logging.exception(e)
                handler.send(str(e),code=500)
                return
            handler.send() #HTTP 200 and start sending stream data
            try:
                while cmd.poll() is None: #cmd exited
                    data = cmd.stdout.read(int(self.config.buffer))
                    if not data: # cmd exited, source dropped the stream
                        break
                    handler.wfile.write(data)
                # cmd exited likely source dropped the stream
                logging.warning('%s pid %s exited',handler.client_address, cmd.pid)
                self.data.last_refresh=0 #invalidate cache as account may be full
            except BrokenPipeError: 
                logging.debug('stream: %s pid %s disconnected',handler.client_address, cmd.pid)
            except Exception as e:
                logging.exception(e)
            cmd.stdout.close() # stop cmd if running
            cmd.wait() # wait for exit
            logging.info('%s pid %s stop (%d)', handler.client_address, cmd.pid, cmd.returncode)
            del self.procs[cmd.pid]
        else:
            handler.send(code=503) #no source for stream


class Handler(http.server.BaseHTTPRequestHandler):
    '''HTTP request handler'''

    def send(self,html='',redirect=None,code=200):
        self.send_response(302 if redirect and code==200 else code)
        if redirect:
            self.send_header('Location',redirect)
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        config=self.server.config
        data=self.server.data

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
                        config.add_filter('groups',val,start='group_start' in params,end='group_end' in params,comment=params['comment'])
                        fetch=False
                    if key == 'stream':
                        config.add_filter('streams',val,start='stream_start' in params,end='stream_end' in params,exclude='stream_exclude' in params,comment=params['comment'])
                        fetch=False
                    if key == 'rename':
                        config.add_filter('rename',val,start='rename_start' in params,end='rename_end' in params,rename_to=params['rename_to'],comment=params['comment'])
                        fetch=False
                    if key == 'group_rename':
                        config.add_filter('group_rename',val,start='group_rename_start' in params,end='group_rename_end' in params,rename_to=params['group_rename_to'],comment=params['comment'])
                        fetch=False
                    if key == 'replace':
                        config.add_filter('replace',val,start='replace_start' in params,end='replace_end' in params,comment=params['comment'])
                        fetch=False
                # config text was submitted
                if key == 'config':
                    fetch=False
                    config.write(val)
                # url/hash was submitted, add account and rescan
                if key == 'add':
                    config.add_account(val)
            except Exception as e:
                logging.exception(e)
                self.send(str(e),code=500)
                return
        #reload config, scan sources if fetch is True, rebuild lineup
        data.rescan(fetch)
        #respond like GET of posting page
        self.do_GET()
        return

    def do_GET(self):
        config=self.server.config
        data=self.server.data
        stream=self.server.stream

        #serve stream URL or stream data
        if self.path.startswith('/stream/'):
            k=unquote(self.path.split('/stream/')[-1])
            if k in data.lineup:
                logging.info('%s stream %s'%(self.client_address,k))
                if config.direct:
                    # send the URL to plex
                    url=stream.get_stream_url(k)
                    if url:
                        logging.info('%s redirect to %s', self.client_address, url)
                        self.send(redirect=url)
                    else:
                        self.send('no source for %s' % k,code=503)
                    return
                else: 
                    stream.serve_stream(k,self)
                    return
            else:
                self.send('%s not in lineup' % k,code=404)
                return

        #serve HDHR data
        elif self.path=='/discover.json':
            self.send(json.dumps({
                "DeviceID": "TUNER",
                "FriendlyName": "Tuner",
                "TunerCount": int(config.tuner_count),
                'BaseURL':'http://%s:%s'%(config.server_ip,config.server_port),
                'LineupURL':'http://%s:%s/lineup.json'%(config.server_ip,config.server_port),
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
            data.rescan()
            self.send(json.dumps(list(data.lineup.values())))
            return

        #serve M#U/XMLTV
        elif self.path=='/m3u':
            data.rescan()
            self.send('\n'.join(data.generate_m3u()))
            return

        elif self.path=='/xmltv':
            data.rescan(get_xmltv=True)
            if data.xmltv_cache:
                self.send(data.build_xmltv())
            else:
                self.send(code=404)
            return 

        #serve HTML pages
        else:
            try:
                code=200
                html=self.html_start()

                #serve lineup
                if self.path.startswith('/lineup'):
                    html+='''<p><table>'''
                    if data.lineup:
                        for g in data.group_order:
                            html+='<tr/><tr><th colspan=2 id="%s">%s</th></tr>\n'%(quote(g),g)
                            for k,l in [(k,l) for k,l in data.lineup.items() if l['GuideCategory']==g]:
                                html+='<tr><td>%s</td><td><a href="%s">%s</a></td><td>%s</td></tr>\n'%(
                                    ''.join(s.split('//')[1][0] for s in l['sources']),
                                    l['URL'],
                                    l['GuideName'],
                                    l['GuideID'] if l['GuideID'] else ''
                                )
                        html+='''</table></p>'''
                    html+='''<p><table>'''
                    for s,sg in data.source_groups.items():
                        html+='<tr><th>%s</th><td>%s</td></tr>'%(
                            s,
                            ','.join('<a href="#%s">%s</a>'%(quote(g[0]),g[0]) if g[1] else g[0] for g in sorted(sg.values()))
                        )
                    html+='''</table></p>'''
                    # filter builder
                    html+='''<datalist id=group-select>'''
                    for s,sg in data.source_groups.items():
                        #escape | character, this is common in group names and will break regexes
                        html+='\n'.join('<option value="%s">'%g[0].replace('|','\|') for g in sorted(sg.values()))
                    html+='''</datalist>'''
                    html+='''<form method=post><p><table>
                    <tr><th>group rename:</th><td><input type=checkbox title='match at start' name=group_rename_start><input type=text size=32 title='remove matching text from group name' name=group_rename><input type=checkbox title='match at end' name=group_rename_end></td>
                    <td> to <input type=text title='replace matching text with' name=group_rename_to></td></tr>
                    <tr><th>group:</th><td><input type=checkbox title='match at start' name=group_start><input type=text size=32 name=group list=group-select><input type=checkbox title='match at end' name=group_end></td></tr>
                    <tr><th>streams:</th><td><input type=checkbox title='match at start' name=stream_start><input type=text size=32 name=stream><input type=checkbox title='match at end' name=stream_end></td>
                    <td><input type=checkbox title='exclude matching streams' name=stream_exclude>exclude</td></tr>
                    <tr><th>rename:</th><td><input type=checkbox title='match at start' name=rename_start><input type=text size=32 title='remove matching text from name' name=rename><input type=checkbox title='match at end' name=rename_end></td>
                    <td> to <input type=text title='replace matching text with' name=rename_to></td></tr>
                    <tr><th>replace:</th><td><input type=checkbox title='match at start' name=replace_start><input type=text title='if channel matching pattern exists, replace channels matching name without pattern' size=32 name=replace><input type=checkbox title='match at end' name=replace_end></td></tr>
                    <tr><th>comment:</th><td><input type=text size=32 name=comment></td><td><input type=submit value="add filter"></tr>
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
                    if stream.procs:
                        html+='''<p><table><tr><th>pid</th><th>client</th><th>stream</th><th>command</th></tr>'''
                        for pid,args in stream.procs.items():
                            html+='<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n'%(pid,*args)
                        html+='''
                        </table>
                    </p>'''
                    html+='''<p><table>'''
                    if data.accounts:
                        html+='''<tr><th></th><th>user</th><th>pass</th><th>priority</th><th colspan=2>status</th><th>expires</th>'''
                        t=int(config.cache+data.last_refresh-time.time())
                        if t > 0:
                            html+='<td>(cached for %ss)</td>'%t
                        html+='''</tr>'''
                        for url,accts in data.accounts.items():
                            html+='<tr><th colspan=7>%s</th><td>(%s streams)</td></tr>'%(url,
                            len(list(s for s in data.lineup.values() if url in s['sources'])) if data.lineup else '0')
                            for a in accts:
                                html+='<tr><td></td><td>%s</td><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td><td>%s</td></tr>\n'%a.data()
                    html+='''<tr><td><form method=post><input type=submit name=reload value=reload></form></td></tr>'''
                    if data.lineup:
                        t=int(config.lineup_cache+data.last_rescan-time.time())
                        if t > 0:
                            cached='<td>(cached for %ss)</td>'%t
                        else: 
                            cached=''
                        url='http://%s:%s/m3u' % (config.server_ip,config.server_port)
                        html+='<tr><th>M3U</th><td colspan=6><a href="'+url+'">'+url+'</a></td>'+cached+'</tr>'
                        url='http://%s:%s/xmltv' % (config.server_ip,config.server_port)
                        html+='<tr><th>XMLTV</th><td colspan=6><a href="'+url+'">'+url+'</a>'+(cached if data.xmltv_cache else '')+'</tr>'
                    html+='''</table></p>'''
                    html+='''<p><table>'''
                    for (k,v) in sorted(config.reload().items()):
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

            #always add config editor
            html+=self.html_end()
            self.send(html,code=code)
            return

    def html_start(self):
        data=self.server.data
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
        </tr></table></p>''' % (len(list(data.lineup)) if data.lineup else '0')

    def html_end(self):
        config=self.server.config
        html=''
        if config.config_file:
            html+='''
            <p>&nbsp;</p><p><form method=post>
            <textarea style=font-family:monospace name=config cols=100 rows=20>'''
            html+=config.get_config()
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
    global LOGQ
    #init config
    config=Config(args[0] if args else None)
    env=config.reload()
    #init logging
    LOGQ=LogQ(maxlen=config.logdepth)
    logging.basicConfig(level=int(config.loglevel), 
                        format='%(asctime)s %(levelname)s:%(message)s', 
                        handlers=[logging.StreamHandler(),QueueHandler(LOGQ)])
    for k,v in env.items():
        logging.debug('%s=%s',k,v)
    # init data store
    data=Data(config)
    data.rescan()
    # start server
    httpd = http.server.ThreadingHTTPServer((config.server_ip, int(config.server_port)), Handler)
    httpd.config=config
    httpd.data=data
    httpd.stream=Stream(config,data)
    logging.info('serving at http://%s:%s',config.server_ip, config.server_port)
    httpd.serve_forever()

if __name__ == '__main__':    
    main(*sys.argv[1:])

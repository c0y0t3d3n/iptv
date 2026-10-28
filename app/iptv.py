#!/usr/bin/python3
import sys
import os
import time
from datetime import datetime
import logging
import re
import random
from urllib.parse import quote, urlsplit
from io import StringIO, BytesIO
import xmltv
import asyncio
from aiohttp import ClientSession, ClientTimeout  

def upper(s):
    global UPPER
    if UPPER: 
        return s.upper() if s else ''
    else: 
        return s if s else ''

def parse_url(text):
    #just a url user pass line
    if ' ' in text:
        info=text
    else:
        #probably a http://host:port/get.php?username=...&password=...&... URL
        try:
            parts=urlsplit(text)
            url=parts.scheme+'://'+parts.netloc
            params=dict(p.split('=') for p in parts.query.split('&'))
            info='%s %s %s'%(url,params['username'],params['password'])
        except Exception as e:
            logging.warning('%s parsing %s',e,text)      
            info=None
    return info  

class Config:
    #variables we accept and types to cast to
    args=dict(
        loglevel=(int,logging.INFO),
        logdepth=(int,100),
        upper=(int,1),
        timeout=(int,0),
        delay=(int,0),
        check=(int,0),
        cache=(int,0),
        lineup_cache=(int,86400),
        min_sources=(int,0),
        auto_map=(int,1),
        auto_pri=(int,1),
        groups=(str,''),
        rename=(str,''),
        group_rename=(str,''),
        streams=(str,''),
        streams_exclude=(str,''),
        replace=(str,''),
        order=(str,''),
        map=(str,''),
        user_agent=(str,''),
        format=(str,'http://%s:%s/%s/%s/%s'),
    )

    def __init__(self,config_file=None):
        self.config_file=config_file
        self.lock=asyncio.Lock()

    async def reload(self):
        '''read, build, and return config'''
        async with self.lock:
            config={}
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
                                if k in config:
                                    config[k]=config[k]+','+v
                                elif k in self.args:
                                    config[k]=v
                except Exception as e:
                    logging.error('%s reading %s',e,self.config)
            #build config from file and args
            for k,(t,d) in self.args.items():
                try:
                    #priorirty is env var > config > default
                    setattr(self,k,t(os.getenv(k.upper(),config.get(k,d))))
                except Exception as e:
                    logging.warning('config %s: %s',k,e)

            #set global uppercasing
            global UPPER
            UPPER=self.upper

            #parse filter args
            # channel group regexes, !pattern to exclude
            self.groups=[p for p in upper(self.groups).split(',') if p]
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
            # parse overrides for epg sources
            self.map=dict(p.split('=') for p in upper(self.map).split(',') if p)

            # return config for info 
            return dict((e,getattr(self,e,None)) for e in self.args)

    async def get_config(self):
        async with self.lock:
            try:
                with open(self.config_file) as f:
                    return f.read(-1)
            except Exception as e:
                return str(e)

    async def get_accounts(self):
        async with self.lock:
            accounts={}
            try:
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
            except Exception as e:
                logging.error('%s reading config',e)
        return accounts

    async def write(self,text):    
        async with self.lock:    
            try:
                with open(self.config_file,'w') as f:
                    f.write(text)
                logging.info('wrote %s',self.config_file)
            except Exception as e:
                logging.error('%s writing %s',e,self.config_file)

    async def append(self,text):
        async with self.lock:
            try:
                with open(self.config_file,'a') as f:
                    f.write(text+'\n')
                    logging.info('added %s to %s',text,self.config_file)
            except Exception as e:
                logging.error('%s writing config',e)

    async def add_account(self,text):
        text=text.strip()
        info=parse_url(text)
        if info:
            await self.append(info)

    async def add_filter(self,filter_type,filter_text,start=False,end=False,exclude=False,rename_to=None,comment=''):
        if exclude:
            filter_text='!'+filter_text
        if start:
            filter_text='^'+filter_text
        if end:
            filter_text+='$'
        if rename_to:
            filter_text+='='+rename_to
        await self.append(filter_type+'='+filter_text+'#'+comment)


class Account:
    '''struct for xtream codes account and status'''
    def __init__(self,username,password,priority=0,active_conns=0,max_conns=0,status=None,expires=None,info={},source=None):
        self.username=username
        self.password=password
        self.priority=int(priority) if priority else 0
        self.active_conns=int(active_conns) if active_conns else 0
        self.max_conns=int(max_conns) if max_conns else 0
        self.status=status
        self.expires=datetime.fromtimestamp(int(expires)) if expires else None
        self.info=info
        self.source=source

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
        self.refresh_lock=asyncio.Lock()
        self.rescan_lock=asyncio.Lock()
        self.accounts={}
        self.lineup={}
        
    async def xtream_request(self,url,a,action=None,path='player_api.php',raw=False):
        # xtream codes API requests
        headers={}
        if self.config.user_agent:
            headers['User-Agent']=self.config.user_agent
        params={'username':a.username,'password':a.password}
        if action:
            params.update(action=action)
        async with ClientSession(timeout=ClientTimeout(connect=self.config.timeout if self.config.timeout else None)) as s:
            async with s.get(url+'/'+path,params=params,headers=headers) as r:
                return await r.text() if raw else await r.json()

    async def check_acct(self,url,a):
        # get server and account info
        info=None
        try:
            info=await self.xtream_request(url,a,'server_info')
            return Account(
                a.username, a.password, a.priority, 
                info['user_info']['active_cons'], 
                info['user_info']['max_connections'], 
                info['user_info']['status'], 
                info['user_info']['exp_date'], 
                info, url
            )
        except Exception as e:
            # if account is not valid info will not parse so return 0/0 slots available and raw info
            logging.warning('refresh: %s checking %s %s %s %s',e,url,a.username,a.password,a.info)
            return Account(a.username,a.password,a.priority,None,None,str(info),None,info)
            
    async def refresh_accts(self,accounts=None,force_refresh=False):
        #refresh status of accounts from source if not cached
        async with self.refresh_lock:
            if force_refresh or not self.last_refresh or self.last_refresh + self.config.cache < time.time():
                if accounts is None: #if no list passed use comfig accounts
                    accounts=await self.config.get_accounts()
                self.accounts={}
                # check accounts and return map of source to accounts sorted by free slots
                for url,accts in accounts.items():
                    if self.config.check:
                        accts=random.sample(accts,min(self.config.check,len(accts)))
                    logging.info('refresh: %s',url)
                    for a in accts:
                        ra=await self.check_acct(url,a)
                        logging.debug('refresh: %s',ra.info)
                        self.accounts.setdefault(url,[]).append(ra)
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

    async def fetch_lineup(self,selected_sources):
        # fetch a lineup from each source, filter/modify channel names, and merge linueps
        self.groups_cache={}
        self.streams_cache={}
        for (url,a) in selected_sources:
            logging.info('lineup: fetch from %s',url)
            try:
                #fetch from selected source account, strip commas as plex does not like them and we can't filter on them
                self.groups_cache[url]=dict( (e['category_id'],upper(e['category_name'].replace(',',''))) for e in await self.xtream_request(url,a,'get_live_categories') )
                streams=self.streams_cache.setdefault(url,[])
                for s in await self.xtream_request(url,a,'get_live_streams'):
                    s['name']=upper(s['name'].replace(',',''))
                    streams.append(s)
                logging.debug('fetch: %s %s groups %s streams',url,len(self.groups_cache[url]),len(self.streams_cache[url]))
            except Exception as e:
                logging.warning('fetch: %s for %s %s %s',e,url,a.username,a.password)
                continue

    async def build_lineup(self,acct=None):
        self.source_groups={}
        self.group_order=[]
        ids={}  
        groups={}
        lineup={}
        try:
            for url in self.groups_cache.keys():
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
                    if any(re.search(p,g) for p in self.config.groups):
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
                    # each stream is name, EPG ID, group name, source stream ID,
                    streams.append([
                        n,
                        s['epg_channel_id'].lower() if s['epg_channel_id'] else None,
                        self.source_groups[url].get(s['category_id'],[str(s['category_id'])])[0],
                        s['stream_id']
                    ])
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
                    #key will be name lower/snakecased.
                    k=s[0].lower().replace(' ','_') 
                    #auto mapping will merge channels with different names if the epg ids match
                    if self.config.auto_map and k not in lineup and s[1] in ids:
                            logging.debug('lineup: %s mapped %s to %s',s[1],k,ids[s[1]])
                            k=ids[s[1]]
                    #map EPG IDs to key
                    if s[0] in self.config.map:
                        s[1]=self.config.map[s[0]].lower()
                    if s[1]:
                        ids[s[1]]=k
                    #create channel and add this source
                    if acct: # build limeup for single account
                        stream_url=self.config.format % (
                            acct.info['server_info']['url'], 
                            acct.info['server_info']['port'], 
                            acct.username, 
                            acct.password, 
                            s[3]
                        )
                    else:
                        stream_url='http://%s:%s/stream/%s' % (self.config.server_ip,self.config.server_port,quote(k))
                    lineup.setdefault(k, {
                                        'GuideNumber':s[0],
                                        'GuideName':s[0], 
                                        'GuideID':s[1],
                                        'GuideCategory':s[2],
                                        'sources':{},
                                        'URL':stream_url
                                    })['sources'][url]=s[3]
                    #ensure a guide ID if possible
                    if not lineup[k]['GuideID']:
                        lineup[k]['GuideID']=s[1]
                    #deal with source order changing by always keeping shorter name
                    if len(s[0]) < len(lineup[k]['GuideNumber']):
                        lineup[k].update(GuideNumber=s[0],GuideName=s[0])
            #if set, filter to channels with EPG IDs and at least MIN_SOURCES sources
            if self.config.min_sources:
                lineup=dict((k,l) for k,l in lineup.items() if l['GuideID'] and len(l['sources'])>=self.config.min_sources)
            # now order the final lineup
            ordered_lineup={}
            ordered_groups=dict(sorted((l['GuideCategory'],False) for l in lineup.values()))
            for o in self.config.order+['']: #empty string at end will output all remaining groups
                for g in ordered_groups:
                    #if group matches pattern and has not been output
                    if not ordered_groups[g] and re.search(o,g):
                        #output group ahd mark as output
                        ordered_lineup.update((k,l) for k,l in lineup.items() if l['GuideCategory']==g)
                        ordered_groups[g]=True
                        logging.debug('lineup: ordering group %s',g)
                        self.group_order.append(g)
            logging.info('lineup: %s groups, %s streams',len(ordered_groups),len(ordered_lineup))
            self.lineup=ordered_lineup
        except Exception as e:
            try:
                logging.warning('lineup: %s, last pattern was %s',e,p)
            except:
                logging.warning('lineup: %s',e)
        return self.lineup

    async def fetch_xmltv(self,selected_sources):
        self.xmltv_cache={'data':{},'channels':{},'programs':[]}
        for url, a in selected_sources:
            logging.info('xmltv: fetch from %s',url)
            try:
                xmlin=StringIO(await self.xtream_request(url,a,path='xmltv.php',raw=True))
                xmlin.seek(0)
                self.xmltv_cache['data'].update(xmltv.read_data(xmlin).items())
                n=0
                xmlin.seek(0)
                for c in xmltv.read_channels(xmlin):
                    if c['id']:
                        c['id'] = c['id'].lower()
                        self.xmltv_cache['channels'][c['id']]=c
                        n+=1
                logging.debug('xmltv: fetched %s channels from %s'%(n,url))
                n=0
                xmlin.seek(0)
                for p in xmltv.read_programmes(xmlin):
                    if p['channel']:
                        p['channel'] = p['channel'].lower()
                        self.xmltv_cache['programs'].append(p)
                        n+=1
                logging.debug('xmltv: fetched %s programs from %s'%(n,url))
            except Exception as e:
                logging.warning('xmltv: %s for %s %s %s',e,url,a.username,a.password)

    async def build_xmltv(self):
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
        logging.info('xmltv: %s channels, %s programs',nc,np)
        return xmlout.read().decode()

    async def build_m3u(self):
        m3u=[]
        m3u.append('#EXTM3U')
        for k,l in self.lineup.items():
            m3u.append('#EXTINF:-1 group-title="%s" tvg-id="%s" tvg-name="%s",%s\n%s' % (l['GuideCategory'],l['GuideID'],l['GuideName'],l['GuideNumber'],l['URL']))
        return m3u

    async def rescan(self,force_rescan=None,get_xmltv=False):
        async with self.rescan_lock:
            try:
                await self.config.reload()
                #refresh account status and lineup if fetch=True, or cache expired
                if force_rescan or not self.last_rescan or self.last_rescan + self.config.lineup_cache < time.time():
                    #load accounts from config
                    await self.refresh_accts(force_refresh=force_rescan)
                    await self.fetch_lineup(self.select_sources(check_free=False))
                    self.xmltv_cache=None #clear cached XMLTV on lineup rescan 
                    self.last_rescan=time.time()
                else:
                    logging.debug('lineup cached for %ss',int(int(self.config.lineup_cache)+self.last_rescan-time.time()))
                #refresh xmltv if requested
                if get_xmltv and not self.xmltv_cache:
                    await self.fetch_xmltv(self.select_sources(check_free=False))
                #build lineup
                await self.build_lineup()
            except Exception as e:
                logging.warning('no usable accounts: %s',e)


async def main(url,m3u=None,xml=None):
    import logging
    config=Config(os.getenv('CONFIG'))
    env=await config.reload()
    logging.basicConfig(level=config.loglevel)
    data=Data(config)
    r=parse_url(url)
    if r:
        r=r.split() 
        url,a=r[0],Account(*r[1:3])
        await data.refresh_accts({url:[a]})
        a=data.accounts[url][0]
        print (url,' '.join(str(s) for s in a.data()))
        if m3u:
            if not config.streams:
                config.streams=['']
            await data.fetch_lineup([(url,a)])
            await data.build_lineup(a)
            with open(m3u,'w') as f:
                f.write('\n'.join(await data.build_m3u()))
            if xml:
                await data.fetch_xmltv([(url,a)])
                with open(xml,'w') as f:
                    f.write(await data.build_xmltv())

if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:]))


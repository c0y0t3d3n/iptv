#!/usr/bin/python3
import sys
import os
import time
from datetime import datetime
import logging
import re
import random
import json
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
        info=text.split()[:3]
    else:
        #probably a http://host:port/get.php?username=...&password=...&... URL
        try:
            parts=urlsplit(text.strip())
            url=parts.scheme+'://'+parts.netloc
            params=dict(p.split('=') for p in parts.query.split('&'))
            info=url,params['username'],params['password']
        except Exception as e:
            logging.warning('%s parsing %s',e,text)      
            info=None
    return info  

class Config:
    '''store for config'''
    #variables we accept and types to cast to
    args=dict(
        loglevel=(int,logging.INFO),
        logdepth=(int,100),
        upper=(bool,True),
        timeout=(int,0),
        delay=(int,0),
        check=(int,0),
        cache=(int,0),
        lineup_cache=(int,86400),
        min_sources=(int,0),
        auto_map=(bool,True),
        auto_pri=(bool,True),
        group_rename=(list,[]),
        groups=(list,[]),
        streams=(list,[]),
        streams_exclude=(list,[]),
        map=(list,[]),
        rename=(list,[]),
        replace=(list,[]),
        order=(list,[]),
        user_agent=(str,''),
        format=(str,'http://%s:%s/%s/%s/%s'),
        sources=(dict,{})
    )
    '''
    sources will be { url : 
                    {
                        disabled: bool,
                        priority: int,
                        accounts: 
                            [
                                {   'username': username,
                                    'password': password,
                                    'priority': priority
                                }, ...
                            ]
                    }
                } 
    '''

    def __init__(self,config_file=None):
        self.config_file=config_file
        self.lock=asyncio.Lock()
        self.__config={}

    async def reload(self):
        '''read, build, and return config'''
        async with self.lock:
            self.config={}
            #config from k=v in file
            if self.config_file:
                try:
                    with open(self.config_file) as f:
                        self.__config=json.load(f)
                except Exception as e:
                    logging.error('%s reading %s',e,self.config_file)
            #build config from file and args
            for k,(t,d) in self.args.items():
                try:
                    #priorirty is env var > config > default
                    setattr(self,k,t(os.getenv(k.upper(),self.__config.get(k,d))))
                except Exception as e:
                    logging.warning('config %s: %s',k,e)

            #set global uppercasing and handle uppercasing of list values
            global UPPER
            UPPER=self.upper
            for k,(t,d) in self.args.items():
                if t is list:
                    setattr(self,k,[upper(v) for v in getattr(self,k)])

            #clean up sources with no accounts
            for url in list(self.sources.keys()):
                if not self.sources[url].get('accounts',[]):
                    del self.sources[url]
            
    async def get_config(self,sources=False,defaults=True):
        #get the built config
        async with self.lock:
            return dict(
                (e,getattr(self,e,None)) for e in self.args \
                if (sources or e!='sources') and (defaults or e in self.__config)
            )

    async def get_config_json(self,**kwargs):
        return json.dumps(
            await self.get_config(**kwargs),
            indent=4
        )

    async def save(self,text):
        async with self.lock:
            self.__config=json.loads(text)
            self.__config['sources']=self.sources.copy()
            logging.info('config: saving to %s',self.config_file)
            self.write()

    async def update(self,remove=False,**kwargs):
        async with self.lock:
            for k,v in kwargs.items():
                #update value or add/remove from list in the config, write json
                t=self.args.get(k,[None])[0] #get type from args
                if t is not dict: #don't let this mess with sources dict
                    if t is list:
                        if remove:
                            self.__config.setdefault(k,[]).remove(v)
                            logging.info('config: removed %s from %s',v,k)
                        else:
                            self.__config.setdefault(k,[]).append(v)
                            logging.info('config: added %s to %s',v,k)
                    elif t:
                        if remove or v is None:
                            if k in self.__config:
                                del self.__config[k]
                                logging.info('config: deleted %s',k)
                        else:
                            if t is bool and type(v) is str:
                                v=int(v) #str to int before cast to bool
                            self.__config[k]=t(v)
                            logging.info('config: updated %s=%s',k,v)
            self.write()

    async def get_accounts(self):
        #return {url:account objects} for enabled sources
        async with self.lock:
            accounts={}
            for url,source in self.sources.items():
                if source.get('disabled'):
                    logging.debug('source %s is disabled',url)
                    continue
                for account in source.get('accounts',[]):
                    try:
                        accounts.setdefault(url,[]).append(
                            Account(username=account['username'],
                                    password=account['password'],
                                    priority=source.get('priority',0))
                        )
                    except Exception as e:
                        logging.debug('config: %s parsing %s',e,account)
        return accounts

    async def update_source(self,update=None,disabled=None,priority=None):
        # set the disbled flag or priority of a source
        async with self.lock:
            s=self.sources[update]
            if disabled is not None:
                s['disabled']=bool(int(disabled))
            if priority is not None:
                s['priority']=int(priority)
            logging.info('source %s set to disabled=%s priority=%s',update,s['disabled'],s['priority'])
            self.__config['sources']=self.sources.copy()
            self.write()

    async def add_account(self,text):
        text=text.strip()
        info=parse_url(text)
        if info:
            async with self.lock:
                url,username,password=info
                self.sources.setdefault(url,{'priority':0,'disabled':False,'accounts':[]})['accounts'].append(
                    {
                        'username':username,
                        'password':password
                    }
                )
                logging.info('config: added account for source %s username %s password %s',url,username,password)
                self.__config['sources']=self.sources.copy()
                self.write()

    async def remove_account(self,remove=None,username=None,password=None):
        try:
            async with self.lock:
                self.sources[remove]['accounts'].remove(
                    {
                        'username':username,
                        'password':password
                    }
                )
                logging.info('config: removed account for source %s username %s password %s',remove,username,password)
                self.__config['sources']=self.sources.copy()
                await self.write()
        except:
            logging.warning('config: not found removing account for source %s username %s password %s',remove,username,password)

    def write(self):
        # write the saved config to JSON
        # call reload() after writing to read and apply the changes
        try:
            with open(self.config_file,'w') as f:
                json.dump(self.__config,f,indent=4)
                logging.debug('config: wrote %s',self.config_file)
        except Exception as e:
            logging.error('config: %s writing %s',e,self.config_file)
                    

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
        return dict(
            username=self.username,
            password=self.password,
            priority=self.priority,
            active_conns=self.active_conns,
            max_conns=self.max_conns,
            status=self.status,
            expires=str(self.expires),
            info=self.info,
            source=self.source
        )

    def __repr__(self):
        return str(list(self.data().values()))


class Data:
    '''data store for accounts, lineup, and EPG'''
    def __init__(self,config):
        self.config=config
        self.last_refresh=0
        self.last_rescan=0
        self.xmltv_cache=None
        self.accounts_lock=asyncio.Lock()
        self.lineup_lock=asyncio.Lock()
        self.xmltv_lock=asyncio.Lock()
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
        timeout=ClientTimeout(sock_connect=self.config.timeout,sock_read=self.config.timeout) if self.config.timeout else None
        async with ClientSession(timeout=timeout) as s:
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
            return Account(a.username,a.password,a.priority,None,None,str(info) if info else str(e),None,info)
            
    async def refresh_accts(self,accounts=None,force_refresh=False):
        #refresh status of accounts from source if not cached
        async with self.accounts_lock:
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
                        await asyncio.sleep(self.config.delay)
                    #sort accounts to put active most free slots first
                    self.accounts[url].sort(key=lambda a:(a.active(), a.available()), reverse=True)
                self.last_refresh=time.time()
            else:
                logging.debug('accounts cached for %ss',int(self.config.cache+self.last_refresh-time.time()))

    async def select_sources(self,source_list=None,check_free=True):
        # return list of url, acct by highest priority and most free slots
        # first account per source will be most free slots, so filter to source in list if active and optinally any free slots
        # then sort by priority,-available to prefer highest  priority sources even if less free slots
        async with self.accounts_lock:
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
                    await asyncio.sleep(0)
                logging.debug('fetch: %s %s groups %s streams',url,len(self.groups_cache[url]),len(self.streams_cache[url]))
            except Exception as e:
                logging.warning('fetch: %s for %s %s %s',e,url,a.username,a.password)
                continue

    async def compile_regex(self,k):
        crex=[]
        for p in getattr(self.config,k):
            try:
                p,s=p.split('=',1) if '=' in p else (p,'')
                logging.debug('config: compiling %s=%s%s',k,p,'='+s if s else '')
                crex.append((re.compile(p),s))
            except Exception as e:
                logging.warning('config: %s compiling %s=%s%s',e,k,p,'='+s if s else '')
        await asyncio.sleep(0)
        return crex

    async def build_lineup(self,acct=None):
        self.source_groups={}
        self.group_order=[]
        ids={}  
        groups={}
        lineup={}
        nsources=0
        #get the compiled patterns. Each will be (Pattern,replace) even if replace is unused
        re_group_rename=await self.compile_regex('group_rename')
        re_groups=await self.compile_regex('groups')
        re_streams=await self.compile_regex('streams')
        re_streams_exclude=await self.compile_regex('streams_exclude')
        re_rename=await self.compile_regex('rename')
        re_replace=await self.compile_regex('replace')
        re_order=await self.compile_regex('order')
        #get the manual mappings
        map_name_id=dict(p.split('=',1) for p in self.config.map)
        map_id_name=dict((v.lower(),k) for k,v in map_name_id.items())
        try:
            for url in self.groups_cache.keys():
                self.source_groups[url]={}
                for i,g in self.groups_cache[url].items():
                    for p,r in re_group_rename:
                        g=p.sub(r,g)
                    #id:(renamed group, selected)
                    self.source_groups[url][i]=(g,any(p.search(g) for p,r in re_groups))
                    await asyncio.sleep(0)
                #select streams by group or filter
                streams_in=[ s for s in self.streams_cache[url] if \
                    self.source_groups[url].get(s['category_id'],[None,None])[1] \
                    or any(p.search(upper(s['name'])) for p,r in re_streams) ]
                #remove and rename streams
                streams=[]
                for s in streams_in:
                    n=upper(s['name'])
                    if  any(p.search(n) for p,r in re_streams_exclude):
                        continue
                    for p,r in re_rename:
                        n=p.sub(r,n)
                    # each stream is name, EPG ID, group name, source stream ID,
                    streams.append([
                        n,
                        s['epg_channel_id'].lower() if s['epg_channel_id'] else None,
                        self.source_groups[url].get(s['category_id'],[str(s['category_id'])])[0],
                        s['stream_id']
                    ])
                    await asyncio.sleep(0)
                #replace channels without pattern if channel with pattern exists
                for p,r in re_replace:
                    replaced=set()
                    replaced.update(p.sub('',s[0]) for s in streams if p.search(s[0]))
                    #remove replaced channels
                    streams=[s for s in streams if s[0] not in replaced]
                    #rename name+pattern to name to replace channel
                    for s in streams:
                        s[0]=p.sub('',s[0])
                        await asyncio.sleep(0)
                #strip any remaining leading/trailing whitespace
                for s in streams:
                    s[0]=s[0].strip()
                    await asyncio.sleep(0)
                logging.debug('lineup: %s %s groups %s streams',url,len([g for g in self.source_groups[url].values() if g[1]]),len(streams))
                # build lineup by merging streams across sources
                if streams: #this source is good
                    nsources+=1
                for s in streams:
                    #set names and EPG IDs if mapped
                    if s[0] in map_name_id:
                        logging.debug('lineup: map %s %s -> %s',s[0],s[1],map_name_id[s[0]].lower())
                        s[1]=map_name_id[s[0]].lower()
                    elif s[1] in map_id_name:
                        logging.debug('lineup: map %s -> %s %s',s[0],map_id_name[s[1]],s[1])
                        s[0]=map_id_name[s[1]]
                    #key will be name lower/snakecased.
                    k=s[0].lower().replace(' ','_') 
                    #auto mapping will merge channels with different names if the epg ids match
                    if self.config.auto_map and k not in lineup and s[1] in ids:
                            logging.debug('lineup: automap %s %s -> %s',s[1],k,ids[s[1]])
                            k=ids[s[1]]
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
                        logging.debug('lineup: autorename %s -> %s',lineup[k]['GuideNumber'],s[0])
                        lineup[k].update(GuideNumber=s[0],GuideName=s[0])
                    await asyncio.sleep(0)
            #if set, filter to channels with EPG IDs and at least minimum sources. 
            if self.config.min_sources:
                #ensure the minimum is not > num sources in case a source is down 
                min_sources=min(nsources,self.config.min_sources)
                lineup=dict((k,l) for k,l in lineup.items() if l['GuideID'] and len(l['sources'])>=min_sources)
            # now order the final lineup
            self.lineup={}
            groups=set(l['GuideCategory']for l in lineup.values())
            ng=len(groups)
            for p,r in re_order+[(re.compile(''),'')]: #empty string at end will output all remaining groups
                for g in sorted(groups.copy()):
                    #if group matches pattern and has not been output
                    if g in groups and p.search(g):
                        #output group and mark as output
                        logging.debug('lineup: ordering group %s',g)
                        self.lineup.update((k,l) for k,l in lineup.items() if l['GuideCategory']==g)
                        self.group_order.append(g)
                        groups.remove(g) #group has been output
                        await asyncio.sleep(0)
            logging.info('lineup: %s groups, %s streams',ng,len(self.lineup))
        except Exception as e:
            logging.exception(e)
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
                        await asyncio.sleep(0)
                logging.debug('xmltv: fetched %s channels from %s'%(n,url))
                n=0
                xmlin.seek(0)
                for p in xmltv.read_programmes(xmlin):
                    if p['channel']:
                        p['channel'] = p['channel'].lower()
                        self.xmltv_cache['programs'].append(p)
                        n+=1
                        await asyncio.sleep(0)
                logging.debug('xmltv: fetched %s programs from %s'%(n,url))
            except Exception as e:
                logging.warning('xmltv: %s for %s %s %s',e,url,a.username,a.password)

    async def build_xmltv(self):
        async with self.lineup_lock:
            ids=set(l['GuideID'] for l in self.lineup.values()) #EPG ID of channels in lineup
        writer=xmltv.Writer(**dict((k.replace('-','_'),v) for k,v in self.xmltv_cache['data'].items()))
        #filter to lineup channels
        nc=np=0
        for i in ids:
            c=self.xmltv_cache['channels'].get(i)
            if c:
                nc+=1
                writer.addChannel(c)
                await asyncio.sleep(0)
        #add programs for lineup channels
        np=0
        for p in self.xmltv_cache['programs']:
            if p['channel'] in ids:
                np+=1
                writer.addProgramme(p)
                await asyncio.sleep(0)
        #generate XML
        xmlout=BytesIO()
        writer.write(xmlout)
        await asyncio.sleep(0)
        xmlout.seek(0)
        logging.info('xmltv: %s channels, %s programs',nc,np)
        return xmlout.read().decode()

    async def build_m3u(self,lineup):
        m3u=[]
        m3u.append('#EXTM3U')
        for k,l in lineup.items():
            m3u.append('#EXTINF:-1 group-title="%s" tvg-id="%s" tvg-name="%s",%s\n%s' % (l['GuideCategory'],l['GuideID'],l['GuideName'],l['GuideNumber'],l['URL']))
            await asyncio.sleep(0)
        return m3u

    async def rescan(self,force_rescan=None,get_xmltv=False):
        try:
            await self.config.reload()
            async with self.lineup_lock:
                #refresh account status and lineup if fetch=True, or cache expired
                if force_rescan or not self.last_rescan or self.last_rescan + self.config.lineup_cache < time.time():
                    #load accounts from config
                    await self.refresh_accts(force_refresh=force_rescan)
                    await self.fetch_lineup(await self.select_sources(check_free=False))
                    self.last_rescan=time.time()
                    self.xmltv_cache=None #clear cached XMLTV on lineup rescan 
                else:
                    logging.debug('lineup cached for %ss',int(self.config.lineup_cache+self.last_rescan-time.time()))
                #build lineup
                lineup = await self.build_lineup()
            #get xmltv if requested
            if get_xmltv:
                async with self.xmltv_lock:
                    if not self.xmltv_cache:
                        await self.fetch_xmltv(await self.select_sources(check_free=False))
                    return await self.build_xmltv()
            else:
                return lineup
        except Exception as e:
            if self.config.loglevel<20:
                logging.exception(e)
            logging.warning('rescan: no usable accounts')


async def main(url,m3u=None,xml=None):
    import logging
    config=Config(os.getenv('CONFIG'))
    env=await config.reload()
    logging.basicConfig(level=config.loglevel)
    data=Data(config)
    r=parse_url(url)
    if r:
        url,a=r[0],Account(*r[1:3])
        await data.refresh_accts({url:[a]})
        a=data.accounts[url][0]
        print (url,' '.join(str(s) for s in list(a.data().values())[:-2])) #strip off info and source
        if m3u:
            if not config.streams:
                config.streams=['']
            await data.fetch_lineup([(url,a)])
            lineup=await data.build_lineup(a)
            with open(m3u,'w') as f:
                f.write('\n'.join(await data.build_m3u(lineup)))
            if xml:
                await data.fetch_xmltv([(url,a)])
                with open(xml,'w') as f:
                    f.write(await data.build_xmltv())

if __name__ == '__main__':
    asyncio.run(main(*sys.argv[1:]))


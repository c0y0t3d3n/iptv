#!/usr/bin/python3
import time 
import sys
import os
import signal
import logging
from logging.handlers import QueueHandler
from collections import deque
from urllib.parse import quote, unquote
from html import escape
import asyncio
from aiohttp import web
from iptv import Config, Data, Account

class Tuner:
    '''handler for tuner app'''
    def __init__(self,config,data):
        self.config=config
        self.data=data
        self.procs={}

    async def get_stream(self,request):
        #serve stream URL or stream data
        k=unquote(request.match_info['stream'])
        logging.debug('stream: %s request %s'%(request.remote, k))
        if k in self.data.lineup:
            # send the URL to plex
            try:
                await self.data.refresh_accts()
                l=self.data.lineup[k]
                #get the best source/account for this channel
                sources=await self.data.select_sources(list(l['sources'].keys()))
                (source,acct)=sources[0]
                #format the stream URL
                url=self.config.format % (acct.info['server_info']['url'], 
                        acct.info['server_info']['port'], acct.username, acct.password, 
                        l['sources'][source])
                direct=request.query.get('direct')
                if direct is None:
                    direct=self.config.direct
                if direct:
                    #redirect client to stream
                    logging.info('stream: %s %s redirected to %s', request.remote, k, url)
                    return web.Response(status=302,headers={'Location':url})
                else:  
                    logging.info('stream: %s %s from %s'%(request.remote, k, url))
                    # remux with ffmpeg
                    args = self.config.cmd % url
                    logging.debug('stream: %s %s exec %s', request.remote, k, args)
                    try:
                        proc = await asyncio.create_subprocess_exec(*args.split(), stdout=asyncio.subprocess.PIPE, limit=self.config.buffer)
                        logging.info('stream: %s %s pid %s started', request.remote, k, proc.pid)
                        self.procs[proc.pid]=(request.remote,k,args)
                    except Exception as e:
                        logging.exception(e)
                        return web.Response(status=500,text=str(e))
                    try:
                        response=web.StreamResponse()
                        await response.prepare(request)
                        acct.active_conns+=1 #update cached account
                        while True:
                            data = await proc.stdout.read(self.config.buffer)
                            if not data: # cmd exited, source dropped the stream
                                break
                            #logging.debug('stream: %s %s pid %s proxied %s bytes', request.remote, k, proc.pid, len(data))
                            await response.write(data)
                        # cmd exited likely source dropped the stream
                        rc=await proc.wait()
                        logging.warning('stream: %s %s pid %s exited (%s)',request.remote, k, proc.pid, rc)
                        if self.config.auto_pri:
                            # drop priority for this source on fail, will be reset on refresh
                            for sa in self.data.accounts[acct.source]:
                                sa.priority+=1
                        else:
                            self.data.last_refresh=0 #expire cache if not adjusting priority
                    except OSError:
                        # only decrement on normal exit. will help skip overloaded cached accounts. 
                        acct.active_conns-=1
                        logging.debug('stream: %s %s pid %s disconnected',request.remote, k, proc.pid)
                    except Exception as e:
                        logging.exception(e)
                    try:
                        proc.kill()
                    except:
                        pass #it gone
                    logging.info('stream: %s %s pid %s stopped', request.remote, k, proc.pid)
                    del self.procs[proc.pid]
                    return response
            except Exception as e: #no source available for this stream
                logging.debug('stream: %s %s',k,e)
                self.data.last_refresh=0 #expire cache if nothing usable
                return web.Response(status=503,text=k)
        else:
            return web.Response(status=404,text=k)

    async def get_discover(self,request):
        return web.json_response({
                "DeviceID": "TUNER",
                "FriendlyName": "Tuner",
                "TunerCount": self.config.tuner_count,
                'BaseURL':'http://%s:%s'%(self.config.server_ip,self.config.server_port),
                'LineupURL':'http://%s:%s/lineup.json'%(self.config.server_ip,self.config.server_port),
            })

    async def get_lineup_status(self,request):
        return web.json_response({
            'ScanInProgress':0,
            'ScanPossible':1,
            'Source':'Cable'
        })

    async def get_lineup(self,request,flat=False):
        data = await self.data.rescan()
        source = request.query.get('source')
        if source:
            return web.json_response(dict(groups=self.data.groups_cache[source],streams=self.data.streams_cache[source]))
        else:        
            return web.json_response(list(self.data.lineup.values()) if flat else data)

    async def get_config(self,request):
        return web.json_response(await self.config.get_config(**dict((k,int(v)) for k,v in request.query.items())))

    async def get_sources(self,request):
        config = await self.config.get_config(sources=1)
        return web.json_response(config['sources'])

    async def get_log(self,request):
        return web.json_response([lr.getMessage() for lr in LOGQ])

    async def get_status(self,request):
        return web.json_response({
            'sources':dict((url,[a.data() for a in self.data.accounts[url]]) for url in self.data.accounts),
            'procs':dict((pid,{'client':p[0], 'stream':p[1], 'cmd':p[2]}) for pid,p in self.procs.items()),
            'sources_ngroups':dict((url,len(s)) for url,s in self.data.groups_cache.items()),
            'sources_nstreams':dict((url,len(s)) for url,s in self.data.streams_cache.items()),
            'sources_cached':max(0,int(self.config.cache+self.data.last_refresh-time.time())),
            'lineup_ngroups':len(self.data.group_lineup) if self.data.lineup else 0,
            'lineup_nstreams':len(self.data.lineup) if self.data.lineup else 0,
            'lineup_cached':max(0,int(self.config.lineup_cache+self.data.last_rescan-time.time())),
            'xmltv_cached':max(0,int(self.config.lineup_cache+self.data.last_rescan-time.time()) if self.data.xmltv_cache else 0)
        })

    async def rescan(self,*args):
        await self.data.rescan(force_rescan=True)
        return web.Response()

    async def post_status(self,request):
        params=await request.post()
        if params.get('rescan'):
            return await self.rescan() #force full rescan
        pid=params.get('kill')
        if pid:
            pid=int(pid)
            if pid in self.procs:
                os.kill(pid,signal.SIGKILL)
        return web.Response()

    async def post_config(self,request):
        params=await request.post()
        # key=value
        # remove=1 key=value
        try:
            await self.config.update(
                **dict((k,v if len(v) else None) for k,v in params.items())
                )
            await self.data.rescan() #rebuild lineup only
        except Exception as e:
                logging.exception(e)
                return web.Response(text=str(e),status=400)
        return web.Response()

    async def post_sources(self,request):
        params=await request.post()
        # add=url/hash was submitted, add account and rescan
        # remove=url&username=username&password=password
        # set=url&priority=pri&disabled=0/1
        try:
            if 'add' in params:
                await self.config.add_account(**params)
            elif 'remove' in params:
                await self.config.remove_account(**params)
            elif 'update' in params:
                await self.config.update_source(**params)
        except Exception as e:
                logging.exception(e)
                return web.Response(text=str(e),status=400)
        return web.Response()

    async def get_m3u(self,request):
        lineup = await self.data.rescan()
        return web.Response(text='\n'.join(await self.data.build_m3u(self.data.lineup))) if lineup else web.Response(status=503)

    async def get_xmltv(self,request):
        xmltv = await self.data.rescan(get_xmltv=True)
        return web.Response(text=xmltv, content_type='application/xml') if xmltv else web.Response(status=503)

    #return path routing table
    def routes(self):
        return [
            web.get('/stream/{stream}',self.get_stream),
            web.get('/m3u',self.get_m3u),
            web.get('/xmltv',self.get_xmltv),
            web.get('/discover.json',self.get_discover),
            web.get('/lineup_status.json',self.get_lineup_status),
            web.get('/lineup.json',lambda request: self.get_lineup(request,flat=True)),
            web.get('/lineup',self.get_lineup),
            web.get('/log',self.get_log),
            web.get('/sources',self.get_sources),
            web.get('/config',self.get_config),
            web.get('/status',self.get_status),
            web.get('/',lambda request: web.FileResponse('index.html')),
            web.post('/lineup.post',self.rescan),
            web.post('/status',self.post_status),
            web.post('/config', self.post_config),
            web.post('/sources',self.post_sources),
         ]


class LogQ(deque):
   '''leaky queue that drops oldest items for the logs page'''
   def put_nowait(self, item, **kwargs):
        self.append(item)


#add the tuner args to the config def
Config.args.update( dict(
        server_ip=(str,'localhost'),
        server_listen_ip=(str,''),
        server_port=(int,5004),
        verbose=(bool,0),
        tuner_count=(int,4),
        cmd=(str,'ffmpeg -hide_banner -loglevel error -user_agent tuner -i %s -c copy -c:a ac3 -copyts -f mpegts pipe:1'),
        direct=(bool,0),
        buffer=(int,1024*1024) #buffer size for streaming
))

#this will be written to the config file if it does not exist
DEFAULT_CONFIG=dict(
    #default group name cleanup
    group_rename=['^USA? ?[\\|:] ?','^news.*weather$=news','^family.*kids$=kids','(basic|document).*=entertainment'],
    #default group selection
    groups=['^(ENTERTAINMENT|KIDS|MOVIES|MUSIC|NEWS|SPORTS)$'],
    #group order in lineup, non-matching groups will be at end 
    order=['LOCAL','SPORTS','NEWS','ENTERTAINMENT','MUSIC','KIDS','MOVIES'],
    #exclude non US and west,include callsigns of locals 
    streams=['!(CA|CAN|UK|GB|ES|LBW)[: ][-\\|: ]','! \\(?west\\)?','MYCITY.*(CALLSIGN|CALLSIGN)'],
    #clean up local channel names, strip common tags
    rename=['.*(CALLSIGN|CALLSIGN).*=\\1','^USA? ?[\\|: ]',' \\(?east\\)?',' HD$'],
    #replace duplicate channels with higher def, prefer UHD,FHD,SHD
    replace=['SHD$','FHD$','UHD$']
)

async def setup(config_file=None):
    global LOGQ
    #init config
    config=Config(config_file)
    if config.config_file and not os.path.exists(config.config_file):
        with open(config_file,'w') as f:
            json.dump(DEFAULT_CONFIG,f,indent=4)
    await config.reload()
    env = await config.get_config()
    #init logging
    LOGQ=LogQ(maxlen=config.logdepth)
    logging.basicConfig(level=int(config.loglevel), 
                        format='%(asctime)s %(levelname)s:%(message)s', 
                        handlers=[logging.StreamHandler(),QueueHandler(LOGQ)])
    for k,v in env.items():
        logging.debug('%s=%s',k,v)
    # init datastore
    data=Data(config)
    await data.rescan()
    return config,data

def main(*args):
    # setup app
    config,data=asyncio.run(setup(*args))
    tuner = Tuner(config,data)
    app = web.Application() 
    app.add_routes(tuner.routes())
    # start server
    web.run_app(app,
                host=config.server_listen_ip if config.server_listen_ip else config.server_ip, 
                port=config.server_port, 
                access_log=logging.getLogger('aiohttp.access') if config.verbose else None
                )

if __name__ == '__main__':
    main(*sys.argv[1:])

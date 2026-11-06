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
import json
import asyncio
from aiohttp import web
from iptv import Config, Data, Account

class Tuner:
    '''handler for tuner app'''
    def __init__(self,config,data):
        self.config=config
        self.data=data
        self.procs={}


    #serve stream
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


    #serve API
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

    async def get_lineup_json(self,request):
        lineup = await self.data.rescan()
        return web.json_response(list(lineup.values())) if lineup else web.Response(status=503)

    async def get_groups_json(self,request):
        groups = await self.data.rescan(get_groups=True)
        return web.json_response(groups) if groups else web.Response(status=503)

    async def get_config_json(self,request):
        return web.json_response(await self.config.get_config(**dict((k,int(v)) for k,v in request.query.items())))

    async def get_sources_json(self,request):
        config = await self.config.get_config(sources=1)
        return web.json_response(config['sources'])

    async def get_log_json(self,request):
        return web.json_response([lr.getMessage() for lr in LOGQ])

    async def get_status_json(self,request):
        return web.json_response({
            'accounts':dict((url,[a.data() for a in self.data.accounts[url]]) for url in self.data.accounts),
            'procs':[{'pid':pid, 'client':p[0], 'stream':p[1], 'cmd':p[2]} for pid,p in self.procs.items()]
        })


    #serve M#U/XMLTV
    async def get_m3u(self,request):
        lineup = await self.data.rescan()
        return web.Response(text='\n'.join(await self.data.build_m3u(lineup))) if lineup else web.Response(status=503)

    async def get_xmltv(self,request):
        xmltv = await self.data.rescan(get_xmltv=True)
        return web.Response(text=xmltv, content_type='application/xml') if xmltv else web.Response(status=503)


    #serve pages
    def html_start(self):
        #nav bar and sources status
        html='''<html>
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
            <th><a href='/sources'>sources</a>&nbsp;&nbsp;&nbsp;</th>
            <th><a href='/config'>config</a>&nbsp;&nbsp;&nbsp;</th>
            <th><a href='/lineup'>lineup</a>&nbsp;</th><td>(%s streams)</td>
        </tr></table></p>\n''' % (len(list(self.data.lineup)) if self.data.lineup else '0')
        html+='<p><table>\n'
        if self.data.accounts:
            html+='<tr><th></th><th>user</th><th>pass</th><th>pri</th><th colspan=2>status</th><th>expires</th>\n'
            t=int(self.config.cache+self.data.last_refresh-time.time())
            if t > 0:
                html+='<td>(cached for %ss)</td>'%t
            html+='</tr>'
            for url,accts in self.data.accounts.items():
                html+='<tr><th colspan=7>%s</th><td>(%s streams)</td></tr>\n'%(url,
                len(list(s for s in self.data.lineup.values() if url in s['sources'])) if self.data.lineup else '0')
                for a in accts:
                    html+='<tr><td></td><td>%s</td><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td><td>%s</td></tr>\n'%tuple(escape(str(d)) for d in list(a.data().values())[:-2])
        else:
            html+='<tr><td>No sources configured</td></tr>\n'
        html+='<tr><td><form method=post><input type=submit name=rescan value=Rescan></form></td></tr></table></p>\n'
        return html

    def html_end(self):
        html='''</body><html>'''
        return html

    async def get_status(self,request):
        r=await self.call_handler(self.status_action,request)
        if r: #action taken and we redirect
            return r
        #m3u/xmltv links and active clients
        html='<p><table>\n'
        if self.data.lineup:
            t=int(self.config.lineup_cache+self.data.last_rescan-time.time())
            if t > 0:
                cached='<td>(cached for %ss)</td>'%t
            else: 
                cached=''
            url='http://%s:%s/m3u' % (self.config.server_ip,self.config.server_port)
            html+='<tr><th>M3U</th><td colspan=6><a href="'+url+'">'+url+'</a></td>'+cached+'</tr>\n'
            url='http://%s:%s/xmltv' % (self.config.server_ip,self.config.server_port)
            html+='<tr><th>XMLTV</th><td colspan=6><a href="'+url+'">'+url+'</a>'+(cached if self.data.xmltv_cache else '')+'</tr>\n'
        else:
            html+='<tr><td>No data available</td></tr>\n'
        html+='</table></p>\n<p><table>'
        if self.procs:    
            html+='<tr><td></td><th>pid</th><th>client</th><th>stream</th><th>command</th></tr>\n'
            for pid,args in self.procs.items():
                html+='<tr><td>[<a href="/?kill=%s">x</a>]</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n'%(pid,pid,*args)
        else:
            html+='<tr><td colspan=5>No active clients</td></tr>\n'
        html+='</table></p>'
        return web.Response(text=self.html_start()+html+self.html_end(), content_type='text/html')

    async def get_log(self,request):
        r=await self.call_handler(self.status_action,request)
        if r: #action taken and we redirect
            return r
        #serve logs
        html='<p>'
        for lr in LOGQ:
            html+=escape(lr.getMessage())+'<br>'
        html+='</p>'
        return web.Response(text=self.html_start()+html+self.html_end(), content_type='text/html')

    async def get_config(self,request):
        html=''
        fields={str:('%s',40),int:('%d',10),bool:('%d',1)}
        html+='''<p><form method=post><table>'''
        await self.config.reload()
        env=await self.config.get_config()
        config=await self.config.get_config(defaults=False)
        for (k,ev) in sorted(env.items()):
            if type(ev) not in fields:
                continue
            fmt,size=fields[type(ev)]
            cv=config.get(k)
            html+='<tr><th>%s</th><td>'%escape(k)
            if self.config.config_file:
                html+='<input type=text name=%s size=%s'%(escape(k),size)
                html+=' value="'+(escape(fmt%cv) if cv is not None else '')+'">'
            html+='</td><td>'
            if cv is None:
                html+='<i>'+escape(fmt%ev)+"</i>"
            html+='</td></tr>\n'
        html+='''</table></p>'''
        if self.config.config_file:
            html+='''<input type=submit value='Update config'>'''
        html+='''</form>'''
        if self.config.config_file:
            html+='''<p>JSON editor</p><p><form method=post>
            <textarea style=font-family:monospace name=config cols=80 rows=25>'''
            html+=escape(await self.config.get_config_json(defaults=False,sources=True))
            html+='''</textarea><br>
            <input type=submit value="Save JSON">
            </form>'''
            html+='</p>'
        return web.Response(text=self.html_start()+html+self.html_end(), content_type='text/html')

    async def get_sources(self,request):
        r=await self.call_handler(self.sources_action,request)
        if r: #action taken and we redirect
            return r
        config=await self.config.get_config(sources=True)
        html='<p>URL or SOURCE USERNAME PASSWORD:<br><form method=post><input type=text size=80 name=add><input type=submit value="Add account"></form></p>\n'
        html+='<p><table><tr><td></td><th colspan=2>source/accounts</th><th>priority</th></tr>\n'
        for url,source in config['sources'].items():
            html+='<tr><td></td><th colspan=2>%s</th><td>'%escape(url)
            if source.get('disabled'):
                html+='[<a href="/sources?update=%s&disabled=0">enable</a>]'%escape(quote(url))
            else:
                pri=source.get('priority')
                html+='[<a href="/sources?update=%s&priority=%s">-</a>]'%(escape(quote(url)),max(0,pri-1))
                html+='%s'%pri
                html+='[<a href="/sources?update=%s&priority=%s">+</a>]'%(escape(quote(url)),pri+1)
                html+='&nbsp;[<a href="/sources?update=%s&disabled=1">disable</a>]'%escape(quote(url))
            html+='</td></tr>\n'
            for a in source['accounts']:
                html+='<tr><td>[<a href="/sources?remove=%s&username=%s&password=%s">x</a>]</td>'%(
                    escape(quote(url)),
                    escape(quote(a['username'])),
                    escape(quote(a['password']))
                )
                html+='<td>%s</td><td>%s</td></tr>\n'%(
                    escape(quote(a['username'])),
                    escape(quote(a['password']))
                )
        html+='</table></p>'
        return web.Response(text=self.html_start()+html+self.html_end(), content_type='text/html')

    async def get_lineup(self,request):
        r=await self.call_handler(self.config_action,request)
        if r: #action taken and we redirect
            return r
        #serve html page
        async with self.data.lineup_lock:
            html='''<p><table>'''
            if self.data.lineup:
                for g in self.data.group_order:
                    html+='<tr/><tr><th colspan=2 id="%s">%s</th></tr>\n'%(escape(quote(g)),escape(g))
                    for k,l in [(k,l) for k,l in self.data.lineup.items() if l['GuideCategory']==g]:
                        html+='<tr><td>%s</td><td><a href="%s">%s</a></td><td>%s</td></tr>\n'%(
                            escape(''.join(s.split('//')[1][0] for s in l['sources'])),
                            escape(l['URL']),
                            escape(l['GuideName']),
                            escape(l['GuideID']) if l['GuideID'] else ''
                        )
                html+='</table></p>\n'
            html+='<p><table>\n'
            for s,sg in self.data.source_groups.items():
                html+='<tr><th>%s</th><td>%s</td></tr>\n'%(
                    s,
                    ','.join('<a href="#%s">%s</a>'%(escape(quote(g[0])),escape(g[0])) if g[1] else escape(g[0]) for g in sorted(sg.values()))
                )
            html+='</table></p>\n'
            #get list of source groups for select
            html+='<datalist id=groups-select>\n'
            for s,sg in self.data.source_groups.items():
                #escape | character, this is common in group names and will break regexes
                html+='\n'.join('<option value="%s">'%escape(g[0].replace('|','\\|')) for g in sorted(sg.values()))
            #now list of groups in lineup for order
            order_list=set(l['GuideCategory'] for l in self.data.lineup.values())
            html+='</datalist>\n<datalist id=order-select>\n'
            html+='\n'.join('<option value="%s">'%escape(g.replace('|','\\|')) for g in sorted(order_list))
            html+='</datalist><p><table>\n'
            #get the list args from the config, those will be the filters
            filter_types=[k for k in self.config.args if self.config.args[k][0] is list]
            config=await self.config.get_config()
            for ft in filter_types:
                #build the list and add field for each filter
                html+=self.filter_list(ft,config[ft])
            html+='</table>'
        return web.Response(text=self.html_start()+html+self.html_end(), content_type='text/html')

    def filter_list(self,k,filters):
        # filter builder
        html='<tr><th colspan=2>%s</th></tr><form method=post>' % escape(k)
        for f in filters:
            html+='<tr><td>[<a href="/lineup?remove=1&%s=%s">x</a>]</td><td>%s</td></tr>'%(escape(quote(k)),escape(quote(f)),escape(f))
        html+='<tr><td></td><td><input type=text size=40 name=%s list=%s-select><input type=submit value=Add></td></tr></form>'%(escape(k),escape(k))
        return html


    #action handlers called by POST or a GET with query
    async def status_action(self,rescan=False,kill=None,**params):
        # status takes rescan=1 or kill=pid
        if rescan:
            await self.data.rescan(force_rescan=True)
        if kill is not None:
            pid=int(kill)
            if pid in self.procs:
                os.kill(pid,signal.SIGKILL)

    async def config_action(self,rescan=False,**params):
        # rescan button is on every page 
        if rescan:
            await self.data.rescan(force_rescan=True)
        # config=json
        # key=value
        # remove=1 key=value
        try:
            if 'config' in params:
                await self.config.save(params['config'])
            else:
                await self.config.update(
                    **dict((k,v if len(v) else None) for k,v in params.items())
                )
            await self.data.rescan()
        except Exception as e:
                logging.exception(e)
                return web.Response(text=str(e),status=400)

    async def sources_action(self,rescan=False,**params):
        # rescan button is on every page 
        if rescan:
            await self.data.rescan(force_rescan=True)
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
            await self.data.rescan()
        except Exception as e:
                logging.exception(e)
                return web.Response(text=str(e),status=400)

    #handle POST data or query params and call action handler
    async def call_handler(self,handler,request,redirect=True,**params):
        params.update(await request.post())
        params.update((unquote(k),unquote(v)) for k,v in request.query.items())
        if params or request.method == 'POST':
            r = await handler(**params)
            if r: #handler returned a response 
                return r
            if redirect:
                return web.Response(status=302,headers={'Location':request.path}) #redirect to posting page
            else:
                return web.Response()

    #return path routing table
    def get_routes(self):
        return [
            web.get('/stream/{stream}',self.get_stream),
            web.get('/m3u',self.get_m3u),
            web.get('/xmltv',self.get_xmltv),
            web.get('/discover.json',self.get_discover),
            web.get('/lineup_status.json',self.get_lineup_status),
            web.get('/lineup.json',self.get_lineup_json),
            web.get('/lineup',self.get_lineup),
            web.get('/log.json',self.get_log_json),
            web.get('/log',self.get_log),
            web.get('/groups.json',self.get_groups_json),
            web.get('/sources.json',self.get_sources_json),
            web.get('/sources',self.get_sources),
            web.get('/config.json',self.get_config_json),
            web.get('/config',self.get_config),
            web.get('/status.json',self.get_status_json),
            web.get('/',self.get_status),
            web.post('/lineup.post',lambda request: self.call_handler(self.status_action,request,rescan=True,redirect=False)),
            web.post('/{tail:(config|lineup).*}',lambda request: self.call_handler(self.config_action,request)),
            web.post('/{tail:sources.*}',lambda request: self.call_handler(self.sources_action,request)),
            web.post('/{tail:.*}',lambda request: self.call_handler(self.status_action,request))
         ]


class LogQ(deque):
   '''leaky queue that drops oldest items for the logs page'''
   def put_nowait(self, item, **kwargs):
        self.append(item)


#add the tuner args to the config def
Config.args.update( dict(
        server_ip=(str,'localhost'),
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
    app.add_routes(tuner.get_routes())
    # start server
    web.run_app(app,
                host=config.server_ip, 
                port=config.server_port, 
                access_log=logging.getLogger('aiohttp.access') if config.verbose else None
                )

if __name__ == '__main__':
    main(*sys.argv[1:])

#!/usr/bin/python3
import time 
import sys
import os
import logging
from logging.handlers import QueueHandler
from collections import deque
from urllib.parse import quote
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


    #redirect to or serve stream
    async def get_stream(self,request):
        #serve stream URL or stream data
        k=unquote(request.match_info['stream'])
        if k in self.data.lineup:
            logging.info('%s stream %s'%(request.remote,k))
            # send the URL to plex
            try:
                await self.data.refresh_accts()
                l=self.data.lineup[k]
                #get the best source/account for this channe;l
                (source,acct)=self.data.select_sources(list(l['sources'].keys()))[0]
                #format the stream URL, return URL and account
                url=self.config.format % (acct.info['server_info']['url'], 
                        acct.info['server_info']['port'], acct.username, acct.password, 
                        l['sources'][source])
                if self.config.direct:
                    logging.info('%s redirected to %s', request.remote, url)
                    return web.Response(status=302,headers={'Location':url})
                else:  
                    # remux with ffmpeg
                    args = self.config.cmd % url
                    logging.info('%s starting %s', request.remote, args)
                    try:
                        proc = await asyncio.create_subprocess_exec(*args.split(), stdout=asyncio.subprocess.PIPE)
                        logging.info('%s pid %s started', request.remote, proc.pid)
                        self.procs[proc.pid]=(request.remote,k,args)
                    except Exception as e:
                        logging.exception(e)
                        return web.Response(status=500,text=str(e))
                    try:
                        response=web.StreamResponse()
                        await response.prepare(request)
                        acct.active_conns+=1 #update cached account
                        while True: #cmd exited
                            data = await proc.stdout.read(int(self.config.buffer))
                            if not data: # cmd exited, source dropped the stream
                                break
                            await response.write(data)
                        # cmd exited likely source dropped the stream
                        logging.warning('%s pid %s stream failed',request.remote, proc.pid)
                        if self.config.auto_pri:
                            # drop priority for this source on fail, will be reset on refresh
                            for sa in self.data.accounts[acct.source]:
                                sa.priority+=1
                        else:
                            self.data.last_refresh=0 #expire cache if 'best' account still fails
                    except OSError: 
                        logging.debug('%s pid %s disconnected',request.remote, proc.pid)
                    except Exception as e:
                        logging.exception(e)
                    try:
                        proc.kill() # stop cmd if running
                        await proc.wait() # wait for exit
                    except:
                        pass #it gone
                    acct.active_conns-=1
                    logging.info('%s pid %s stop (%d)', request.remote, proc.pid, proc.returncode)
                    del self.procs[proc.pid]
                    return response
            except Exception as e: #no source available for this stream
                logging.debug('%s %s',k,e)
                self.data.last_refresh=0 #expire cache if nothing usable
                return web.Response(status=503,text=k)
        else:
            return web.Response(status=404,text=k)


    #serve HDHR data
    async def get_discover(self,request):
        return web.json_response({
                "DeviceID": "TUNER",
                "FriendlyName": "Tuner",
                "TunerCount": int(self.config.tuner_count),
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
        await self.data.rescan()
        return web.json_response(list(self.data.lineup.values()))

    #serve M#U/XMLTV
    async def get_m3u(self,request):
        await self.data.rescan()
        return web.Response(text='\n'.join(await self.data.build_m3u()))

    async def get_xmltv(self,request):
        await self.data.rescan(get_xmltv=True)
        if self.data.xmltv_cache:
            return web.Response(text=await self.data.build_xmltv(), content_type='application/xml')
        else:
            return web.Response(status=503)

    #serve HTML pages
    async def get_lineup(self,request):
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
            html+='''</table></p>'''
        html+='''<p><table>'''
        for s,sg in self.data.source_groups.items():
            html+='<tr><th>%s</th><td>%s</td></tr>'%(
                s,
                ','.join('<a href="#%s">%s</a>'%(escape(quote(g[0])),escape(g[0])) if g[1] else escape(g[0]) for g in sorted(sg.values()))
            )
        html+='''</table></p>'''
        # filter builder
        html+='''<datalist id=group-select>'''
        for s,sg in self.data.source_groups.items():
            #escape | character, this is common in group names and will break regexes
            html+='\n'.join('<option value="%s">'%escape(g[0].replace('|','\\|')) for g in sorted(sg.values()))
        html+='''</datalist>'''
        html+='''<form method=post><p><table>
        <tr><th>group rename:</th>
        <td><input type=checkbox title='match at start' name=group_rename_start></td>
        <td><input type=text size=40 title='remove matching text from group name' name=group_rename></td>
        <td><input type=checkbox title='match at end' name=group_rename_end></td>
        <td>to</td><td><input type=text title='replace matching text with' name=group_rename_to></td></tr>
        <tr><th>group select:</th><td><input type=checkbox title='match at start' name=groups_start></td>
        <td><input type=text size=40 name=groups list=group-select></td>
        <td><input type=checkbox title='match at end' name=groups_end></td></tr>
        <tr><th>stream select:</th>
        <td><input type=checkbox title='match at start' name=streams_start></td>
        <td><input type=text size=40 name=streams></td>
        <td><input type=checkbox title='match at end' name=streams_end></td>
        <td><input type=checkbox title='exclude matching streams' name=streams_exclude></td><td>exclude</td></tr>
        <tr><th>stream rename:</th>
        <td><input type=checkbox title='match at start' name=rename_start></td>
        <td><input type=text size=40 title='remove matching text from name' name=rename></td>
        <td><input type=checkbox title='match at end' name=rename_end></td>
        <td>to</td><td><input type=text title='replace matching text with' name=rename_to></td></tr>
        <tr><th>stream replace:</th>
        <td><input type=checkbox title='match at start' name=replace_start></td>
        <td><input type=text title='if channel matching pattern exists, replace channels matching name without pattern' size=40 name=replace></td>
        <td><input type=checkbox title='match at end' name=replace_end></td></tr>
        <tr><th>comment:</th>
        <td></td>
        <td><input type=text size=40 name=comment></td>
        <td colspan=2></td>
        <td><input type=submit name='add_filter' value="Add filter"></td></tr>
        </table></p></form>'''
        return web.Response(text=self.html_start()+html+self.html_end(await self.config.get_config()), content_type='text/html')

    async def get_log(self,request):
        #serve logs
        html='<p>'
        for l in LOGQ:
            html+=escape(l.msg)+'<br>'
        html+='</p>'
        return web.Response(text=self.html_start()+html+self.html_end(await self.config.get_config()), content_type='text/html')

    async def get_status(self,request):
        html=''
        if self.procs:
            html+='''<p><table><tr><th>pid</th><th>client</th><th>stream</th><th>command</th></tr>'''
            for pid,args in self.procs.items():
                html+='<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>\n'%(pid,*args)
            html+='''
            </table>
        </p>'''
        html+='''<p><table>'''
        if self.data.accounts:
            html+='''<tr><th></th><th>user</th><th>pass</th><th>pri</th><th colspan=2>status</th><th>expires</th>'''
            t=int(self.config.cache+self.data.last_refresh-time.time())
            if t > 0:
                html+='<td>(cached for %ss)</td>'%t
            html+='''</tr>'''
            for url,accts in self.data.accounts.items():
                html+='<tr><th colspan=7>%s</th><td>(%s streams)</td></tr>'%(url,
                len(list(s for s in self.data.lineup.values() if url in s['sources'])) if self.data.lineup else '0')
                for a in accts:
                    html+='<tr><td></td><td>%s</td><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td><td>%s</td></tr>\n'%tuple(escape(str(d)) for d in a.data())
        html+='''<tr><td><form method=post><input type=submit name=rescan value=Rescan></form></td></tr>'''
        html+='''
            <tr><td colspan=8><form method=post>
                URL: <input type=text size=80 name=add_account>
                <input type=submit value="Add account">
            </form></td></tr>'''
        if self.data.lineup:
            t=int(self.config.lineup_cache+self.data.last_rescan-time.time())
            if t > 0:
                cached='<td>(cached for %ss)</td>'%t
            else: 
                cached=''
            url='http://%s:%s/m3u' % (self.config.server_ip,self.config.server_port)
            html+='<tr><th>M3U</th><td colspan=6><a href="'+url+'">'+url+'</a></td>'+cached+'</tr>'
            url='http://%s:%s/xmltv' % (self.config.server_ip,self.config.server_port)
            html+='<tr><th>XMLTV</th><td colspan=6><a href="'+url+'">'+url+'</a>'+(cached if self.data.xmltv_cache else '')+'</tr>'
        html+='''</table></p>'''
        html+='''<p><table>'''
        env=await self.config.reload()
        for (k,v) in sorted(env.items()):
            html+='<tr><th>%s</th><td>%s</td></tr>\n'%(escape(k),escape(str(v)))
        html+='''</table></p>'''
        return web.Response(text=self.html_start()+html+self.html_end(await self.config.get_config()), content_type='text/html')

    def html_start(self):
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
        </tr></table></p>''' % (len(list(self.data.lineup)) if self.data.lineup else '0')

    def html_end(self,config_text):
        html=''
        if self.config.config_file:
            html+='''
            <p>&nbsp;</p><p><form method=post>
            <textarea style=font-family:monospace name=config cols=80 rows=25>'''
            html+=escape(config_text)
            html+='''</textarea><br>
            <input type=submit value="save config">
            </form></p>'''
        html+='''</body><html>'''
        return html


    #handle form data POST
    async def form_handler(self,request):
        data = await request.post()
        for k,handler in self.form_routes.items():
            if k in data:
                await handler(self,k,data[k],**data) #call handler with this instance
        return web.Response(status=302,headers={'Location':request.path}) #redirect to posting page


    #handle HDHR channel scan request
    async def lineup_post(self,request):
        await self.rescan()
        return web.Response()


    #form field handlers
    async def rescan(self,*args,**kwargs):
        #trigger a full account/lineup rescan
        await self.data.rescan(force_rescan=True)

    async def add_filter(self,key,val,**params):
        if val:
            self.config.add_filter(key,val,
                start=key+'_start' in params,
                end=key+'_end' in params,
                exclude=key+'_exxclude' in params,
                rename_to=params.get(key+'_to'),
                comment=params['comment']
            )
            await self.data.rescan() #rebuild lineup
    
    async def save_config(self,key,val,**params):
        # config text was submitted
        await self.config.write(val)
        await self.data.rescan()
    
    async def add_account(self,key,val,**params):
        # url/hash was submitted, add account and rescan
        if val:
            await self.config.add_account(val)
            #reload config, scan sources if fetch is True, rebuild lineup
            await self.data.rescan(True)


    #form routing table
    form_routes={
        'rescan':rescan,
        'config':save_config,
        'add_account':add_account,
        'groups':add_filter,
        'group_rename':add_filter,
        'streams':add_filter,
        'rename':add_filter,
        'replace':add_filter
    }


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
            web.get('/log',self.get_log),
            web.get('/',self.get_status),
            web.post('/lineup.post',self.lineup_post),
            web.post('/',self.form_handler),
            web.post('/lineup',self.form_handler),
            web.post('/log',self.form_handler),
         ]


class LogQ(deque):
   '''leaky queue that drops oldest items for the logs page'''
   def put_nowait(self, item, **kwargs):
        self.append(item)


#add the tuner args to the config def
Config.args.update( dict(
        server_ip=(str,'localhost'),
        server_port=(int,5004),
        tuner_count=(int,4),
        cmd=(str,'ffmpeg -hide_banner -loglevel error -user_agent tuner -i %s -c copy -c:a ac3 -copyts -f mpegts pipe:1'),
        direct=(int,0),
        buffer=(int,1024*1024) #buffer size for streaming
))

#this will be written to the config file if it does not exist
DEFAULT_CONFIG='''
#default group name cleanup
group_rename=^^USA? ?[\\|:] ?
group_rename=^news.*weather$=news
group_rename=^family.*kids$=kids

#default group selection
groups=^(ENTERTAINMENT|KIDS|MOVIES|MUSIC|NEWS|SPORTS)$

#group order in lineup, non-matching groups will be at end 
order=LOCAL,SPORTS,NEWS,ENTERTAINMENT,MUSIC,KIDS,MOVIES

#exclude non US and west
streams=!(CA|CAN|UK|GB|ES)[: ][: ]
streams=! \\(?west\\)?

#to include your local stations, insert names of cities or call signs of your stations
streams=mycity,CALLSIGN
#clean up local channel names, substitute your state and city
rename=( - )?(ST).*(city) ?\\|?= #to space if embedded

#strip common prefixes/suffixes
rename=^USA? ?[\\|: ], \\(?east\\)?, HD$

#replace duplicate channels with higher def, prefer UHD,FHD,SHD
replace=SHD$,FHD$,UHD$

#accounts go here as
#URL USERNAME PASSWORD PRIORITY

'''


async def setup(config_file=None):
    global LOGQ
    #init config
    config=Config(config_file)
    if config.config_file and not os.path.exists(config.config_file):
        with open(self.config_file,'w') as f:
            f.write(DEFAULT_CNFIG)
    env=await config.reload()
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
    web.run_app(app,host=config.server_ip, port=config.server_port)

if __name__ == '__main__':
    main(*sys.argv[1:])

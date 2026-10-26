#!/usr/bin/python3
import time 
import json
import sys
import os
import subprocess
import logging
from logging.handlers import QueueHandler
from collections import deque
from urllib.parse import quote,unquote
from html import escape
from iptv import Config, Data, Account
from web import Server, RequestHandler

#add the tuner config
Config.args.update( dict(
        server_ip=(str,'localhost'),
        server_port=(int,5004),
        tuner_count=(int,4),
        cmd=(str,'ffmpeg -hide_banner -loglevel error -user_agent tuner -i %s -c copy -c:a ac3 -copyts -f mpegts pipe:1'),
        direct=(int,0),
        buffer=(int,1024*1024) #buffer size for streaming
))

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

class StreamProxy:
    '''streaming subprocess handler'''
    def __init__(self,config,data):
        self.config=config
        self.data=data
        self.procs={}

    def get_stream(self,k):
        try:
            self.data.refresh_accts()
            l=self.data.lineup[k]
            #get the best source/account for this channe;l
            (source,a)=self.data.select_sources(list(l['sources'].keys()))[0]
            #format the stream URL, return URL and account
            return self.config.format % (a.info['server_info']['url'], 
                    a.info['server_info']['port'], a.username, a.password, 
                    l['sources'][source]),a
        except Exception as e: #no source for this stream
            logging.debug(e)
            return None,None

    def proxy_stream(self,k,url,acct,handler):
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
            acct.active_conns+=1 #update cached account
            while cmd.poll() is None: #cmd exited
                data = cmd.stdout.read(int(self.config.buffer))
                if not data: # cmd exited, source dropped the stream
                    break
                handler.wfile.write(data)
            # cmd exited likely source dropped the stream
            logging.warning('%s pid %s stream failed',handler.client_address, cmd.pid)
            if self.config.auto_pri:
                # drop priority for this source on fail, will be reset on refresh
                for sa in self.data.accounts[acct.source]:
                    sa.priority+=1
            else:
                self.data.last_refresh=0 #expire cache if 'best' account still fails
        except BrokenPipeError: 
            logging.debug('stream: %s pid %s disconnected',handler.client_address, cmd.pid)
        except Exception as e:
            logging.exception(e)
        cmd.stdout.close() # stop cmd if running
        cmd.wait() # wait for exit
        acct.active_conns-=1
        logging.info('%s pid %s stop (%d)', handler.client_address, cmd.pid, cmd.returncode)
        del self.procs[cmd.pid]


class TunerHandler(RequestHandler):
    '''handler for tuner app'''

    # paths the client can GET
    routes={
        'GET':{
            '/stream':'get_stream',
            '/discover.json':'get_discover',
            '/lineup_status.json':'get_lineup_status',
            '/lineup.json':'get_lineup_json',
            '/m3u':'get_m3u',
            '/xmltv':'get_xmltv',
            '/lineup':'get_lineup',
            '/log':'get_log',
            '/':'get_status'
        },
        'POST':{
            '/lineup.post':'rescan'
        }
    }

    # POST routes are same as GET (params are handled independently)
    routes['POST'].update(routes['GET'].copy())

    #params with handler methods
    params={
        'rescan':'rescan',
        'config':'save_config',
        'add_account':'add_account',
        'groups':'add_filter',
        'group_rename':'add_filter',
        'streams':'add_filter',
        'rename':'add_filter',
        'replace':'add_filter'
    }

    def start_request(self):
        #populate context from server
        self.config=self.server.config
        self.data=self.server.data
        self.streamproxy=self.server.streamproxy
   
    def rescan(self,*args,**kwargs):
        #trigger a full account/lineup rescan
        self.data.rescan(force_rescan=True)

    def add_filter(self,key,val,**params):
        if val:
            self.config.add_filter(key,val,
                start=key+'_start' in params,
                end=key+'_end' in params,
                exclude=key+'_exxclude' in params,
                rename_to=params.get(key+'_to'),
                comment=params['comment']
            )
            self.data.rescan() #rebuild lineup
    
    def save_config(self,key,val,**params):
        # config text was submitted
        self.config.write(val)
        self.data.rescan()
    
    def add_account(self,key,val,**params):
        # url/hash was submitted, add account and rescan
        if val:
            self.config.add_account(val)
            #reload config, scan sources if fetch is True, rebuild lineup
            self.data.rescan(True)

    def get_stream(self,path,**params):
        #serve stream URL or stream data
        k=unquote(self.path.split('/stream/')[-1])
        if k in self.data.lineup:
            logging.info('%s stream %s'%(self.client_address,k))
            # send the URL to plex
            url,a=self.streamproxy.get_stream(k)
            if url:
                if self.config.direct:
                    logging.info('%s redirected to %s', self.client_address, url)
                    self.send(redirect=url)
                else: 
                    self.streamproxy.proxy_stream(k,url,a,self)
            else:
                self.data.last_refresh=0 #expire cache if nothing usable
                self.send('no source for %s' % k,code=503)
        else:
            self.send('%s not in lineup' % k,code=404)

    #serve HDHR data
    def get_discover(self,path,**params):
        return json.dumps({
                "DeviceID": "TUNER",
                "FriendlyName": "Tuner",
                "TunerCount": int(self.config.tuner_count),
                'BaseURL':'http://%s:%s'%(self.config.server_ip,self.config.server_port),
                'LineupURL':'http://%s:%s/lineup.json'%(self.config.server_ip,self.config.server_port),
            })

    def get_lineup_status(self,path,**params):
        return json.dumps({
            'ScanInProgress':0,
            'ScanPossible':1,
            'Source':'Cable'
        })

    def get_lineup_json(self,path,**params):
        self.data.rescan()
        return json.dumps(list(self.data.lineup.values()))

    #serve M#U/XMLTV
    def get_m3u(self,path,**params):
        self.data.rescan()
        return '\n'.join(self.data.generate_m3u())

    def get_xmltv(self,path,**params):
        self.data.rescan(get_xmltv=True)
        if self.data.xmltv_cache:
            return self.data.build_xmltv()
        else:
            self.send(code=404)

    #serve HTML pages
    def get_lineup(self,path,**params):
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
        return self.html_start()+html+self.html_end()    

    def get_log(self,path,**params):
        #serve logs
        html='<p>'
        for l in LOGQ:
            html+=escape(l.msg)+'<br>'
        html+='</p>'
        return self.html_start()+html+self.html_end()    

    def get_status(self,path,**params):
        html=''
        if self.streamproxy.procs:
            html+='''<p><table><tr><th>pid</th><th>client</th><th>stream</th><th>command</th></tr>'''
            for pid,args in self.streamproxy.procs.items():
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
        for (k,v) in sorted(self.config.reload().items()):
            html+='<tr><th>%s</th><td>%s</td></tr>\n'%(escape(k),escape(str(v)))
        html+='''</table></p>'''
        return self.html_start()+html+self.html_end()    

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

    def html_end(self):
        html=''
        if self.config.config_file:
            html+='''
            <p>&nbsp;</p><p><form method=post>
            <textarea style=font-family:monospace name=config cols=80 rows=25>'''
            html+=escape(self.config.get_config())
            html+='''</textarea><br>
            <input type=submit value="save config">
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
    if config.config_file and not os.path.exists(config.config_file):
        with open(self.config_file,'w') as f:
            f.write(DEFAULT_CNFIG)
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
    # init server and set context
    httpd = Server((config.server_ip, config.server_port), TunerHandler)
    httpd.server.config=config
    httpd.server.data=data
    httpd.server.streamproxy=StreamProxy(config,data)
    # start server
    logging.info('serving at http://%s:%s',config.server_ip, config.server_port)
    httpd.run()

if __name__ == '__main__':    
    main(*sys.argv[1:])

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

global PROCS, LOGQ
PROCS={}

def upper(s):
    if int(UPPER): 
        return s.upper()
    else: 
        return s

def config(config_file=None):
    ENV_VARS=['SERVER_IP','SERVER_PORT','CMD','DELAY','DIRECT','GROUPS','STREAMS','RENAME','REPLACE','FORMAT','BUFFER','LOGLEVEL','TUNER_COUNT','UPPER','CHECK']

    #set defaults 
    global SERVER_IP,SERVER_PORT,CMD,DELAY,DIRECT,GROUPS,STREAMS,RENAME,REPLACE,FORMAT,BUFFER,LOGLEVEL,LOGDEPTH,TUNER_COUNT,UPPER,CHECK
    LOGLEVEL=logging.INFO
    LOGDEPTH=100

    SERVER_IP='localhost'
    SERVER_PORT=5004

    TUNER_COUNT=4
    UPPER=1

    CMD='ffmpeg -hide_banner -loglevel error -user_agent tuner -i %s -c copy -copyts -f mpegts pipe:1'

    DELAY=0
    DIRECT=0
    CHECK=0
    FORMAT='http://%s:%s/%s/%s/%s'
    GROUPS=''
    RENAME=''
    STREAMS=''
    REPLACE=''

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
    RENAME.append(',') #plex does not like commas in channel names
    # replace any channels with base name if a channel matching regex exists 
    # example: REPLACE=' LHD$' will rename 'ABC LHD' to 'ABC', removing any STREAMS named 'ABC', but only if 'ABC LHD' exists.
    REPLACE=[r for r in upper(REPLACE).split(',') if r]

    # return config for info 
    return dict((k,globals()[k]) for k in ENV_VARS)

def xtream_request(url,user,pw,action):
    r=requests.get(url+'/player_api.php',params={'username':user,'password':pw,'action':action})
    r.raise_for_status()
    return json.loads(r.text)

# get server and account info
def check_acct(url,user,pw,pri=0):
    try:
        info=None
        info=xtream_request(url,user,pw,'server_info')
        return (
            user, pw, pri, 
            int(info['user_info']['active_cons']), int(info['user_info']['max_connections']), info['user_info']['status'], 
            datetime.fromtimestamp(int(info['user_info']['exp_date'])) if info['user_info']['exp_date'] else None, 
            info
        )
    except Exception as e:
        logging.warning('%s %s %s %s %s',url,user,pw,e,info)
        return user, pw, pri, None, None, str(info), None, info

def refresh_accts(accounts):
    refreshed={}
    n=int(CHECK)
    for url,accts in accounts.items():
        if n:
            accts=random.sample(accts,min(n,len(accts)))
        logging.info('refreshing %s accounts for %s',len(accts),url)
        for a in accts:
            refreshed.setdefault(url,[]).append(check_acct(url,*a[0:3]))
            logging.debug(refreshed[url][-1][-1])
            time.sleep(int(DELAY))
    return refreshed

def select_acct(sources):
    selected={}
    for url,accts in sources.items():
        #get accounts that are active
        active=[a for a in accts if a[5].lower()=='active']
        #sort by max-used to get most free slots at end
        if active:
            active.sort(key=lambda a: a[4]-a[3])
            logging.debug('%s: %s active',url,len(active))
            selected[url]=active[-1]
    return selected #account from each source with most available connections

def select_source(selected,source_list):
    #return url, acct data of source with highest priority, most free slots
    #filter to this stream's sources with free slots
    selected_sources=list((s,a) for s,a in selected.items() if s in source_list and a[4]-a[3] > 0) 
    #sort by most free slots, then by priority to always prefer higher prioirty source
    sorted_sources=sorted(
        sorted(selected_sources, key=lambda s: s[1][4]-s[1][3]),  
        key=lambda s: int(s[1][2]), reverse=True)
    return sorted_sources[-1]
    
def fetch_lineup(selected):
    global GROUPS_INCLUDE,GROUPS_STARTSWITH,GROUPS_ENDSWITH,GROUPS_EXCLUDE,STREAMS_INCLUDE,STREAMS_EXCLUDE,SOURCE_GROUPS
    lineup={}
    SOURCE_GROUPS={}
    for url,acct in selected.items():
        logging.debug('selected %s', acct[-1])
        user,pw=acct[:2]
        #fetch from selected source account
        groups_in=dict( (e['category_id'],upper(e['category_name'])) for e in xtream_request(url,user,pw,'get_live_categories') )
        groups=dict( (i,n) for i,n in groups_in.items() \
            if (not GROUPS) or any(re.search(p,n) for p in GROUPS) and not any(re.search(p,n) for p in GROUPS_EXCLUDE) )
        logging.debug('%s groups: %s',url,list(groups.values()))
        SOURCE_GROUPS[url]=groups.values()
        streams_in=[s for s in xtream_request(url,user,pw,'get_live_streams') if s['category_id'] in groups \
            or any(re.search(p,upper(s['name'])) for p in STREAMS) ]
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
            streams.append([n,s['stream_id'],groups_in[s['category_id']]])
        #replace channels if pattern_+channel exists
        for p in REPLACE:
            replaced=set()
            replaced.update(re.sub(p,'',s[0]) for s in streams if re.search(p,s[0]))
            #remove replaced channels
            streams=[s for s in streams if s[0] not in replaced]
            #rename name+pattern to name to replace channel
            for s in streams:
                s[0]=re.sub(p,'',s[0])
        logging.info('%s %s streams',url,len(streams))
        # build lineup
        for s in streams:
            k=quote(s[0])
            lineup.setdefault(k, {
                                'GuideName':s[0], 
                                'GuideNumber':s[0], 
                                'GuideCategory':s[2],
                                'sources':{},
                                'URL':'http://%s:%s/stream/%s'%(SERVER_IP,SERVER_PORT,k)
                            })['sources'][url]=s[1]
    logging.info('lineup has %s streams',len(lineup))
    return lineup

def scan(config_file):
    global ACCOUNTS
    sources={}
    try:
        logging.info('reloading %s',config_file)
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
        #refresh account status
        sources=refresh_accts(ACCOUNTS)
        selected=select_acct(sources)
        return fetch_lineup(selected),selected,sources
    except Exception as e:
        logging.exception(e)
        logging.warning('no usable accounts: %s',e)
        return None,None,sources

class HDHR_handler(http.server.BaseHTTPRequestHandler):
    # emulate a HDHR
    def send(self,html='',redirect=None,code=200):
        self.send_response(302 if redirect and code==200 else code)
        if redirect:
            self.send_header('Location',redirect)
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        global CONFIG_FILE,SOURCES,LINEUP
        # get POST data 
        l=int(self.headers.get('content-length',0))
        key,val=(unquote(self.rfile.read(l).decode().replace('+',' ')).split('=',1)) if l else (None,None)
        try:
            # config text was submitted
            if key == 'config':
                with open(CONFIG_FILE,'w') as f:
                    f.write(val)
                    logging.info('wrote %s',CONFIG_FILE) 
            # url/hash was submitted
            elif key == 'add':
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
        #reload config and lineups
        config(CONFIG_FILE)
        LINEUP,selected,SOURCES=scan(CONFIG_FILE)
        #respond like GET of posting page
        self.do_GET()

    def do_GET(self):
        global CONFIG_FILE,FORMAT,ACCOUNTS,SOURCES,LINEUP,PROCS,LOGQ,SOURCE_GROUPS

        #serve streams
        if self.path.startswith('/stream/'):
            k=self.path.split('/stream/')[-1]
            if LINEUP and k in LINEUP:
                logging.info('%s stream %s'%(self.client_address,k))
                l=LINEUP[k]
                SOURCES=refresh_accts(ACCOUNTS)
                source,a=select_source(select_acct(SOURCES),list(l['sources'].keys()))
                logging.debug('selected %s', a[-1])
                url = FORMAT % (a[-1]['server_info']['url'].split('//')[-1].split('/')[0], 
                                                         a[-1]['server_info']['port'], a[0], a[1], 
                                                         l['sources'][source])
                if int(DIRECT):
                    # send the URL to plex
                    logging.info('%s redirect to %s', self.client_address, url)
                    self.send(redirect=url)
                else:
                    # remux with ffmpeg
                    args = CMD % url
                    logging.info('%s start %s', self.client_address, args)
                    try:
                        cmd = subprocess.Popen(args.split(), shell=False, stdout=subprocess.PIPE)
                        logging.info('%s pid %s', self.client_address, cmd.pid)
                        PROCS[cmd.pid]=(self.client_address,args)
                    except Exception as e:
                        logging.exception(e)
                        self.send(str(e),code=500)
                        return
                    self.send()
                    try:
                        while cmd.poll() is None: #cmd exited
                            data = cmd.stdout.read(int(BUFFER))
                            if not data: break # cmd exited
                            self.wfile.write(data)
                    except BrokenPipeError: pass # plex disconnected 
                    except Exception as e:
                        logging.exception(e)
                    cmd.stdout.close() # will stop cmd
                    cmd.wait()
                    logging.info('%s pid %s stop (%d)', self.client_address, cmd.pid, cmd.returncode)
                    del PROCS[cmd.pid]
            else:
                self.send(stream,404)

        #serve HDHR data
        elif self.path=='/discover.json':
            self.send(json.dumps({
                "DeviceID": "TUNER",
                "FriendlyName": "Tuner",
                "TunerCount": TUNER_COUNT,
                'BaseURL':'http://%s:%s'%(SERVER_IP,SERVER_PORT),
                'LineupURL':'http://%s:%s/lineup.json'%(SERVER_IP,SERVER_PORT),
            }))

        elif self.path=='/lineup_status.json':
            self.send(json.dumps({
                'ScanInProgress':0,
                'ScanPossible':1,
                'Source':'Cable'
            }))

        elif self.path=='/lineup.json':
            self.send(json.dumps(list(LINEUP.values())))

        #serve HTML pages
        else:
            try:
                code=200
                html=self.html_start()

                #serve lineup
                if self.path.startswith('/lineup'):
                    html+='''<p><table>'''
                    if LINEUP:
                        cats=set(l['GuideCategory'] for l in LINEUP.values())
                        for g in sorted(cats):
                            html+='<tr/><tr><th colspan=2 id="%s">%s</th></tr>\n'%(quote(g),g)
                            for k,l in [(k,l) for k,l in LINEUP.items() if l['GuideCategory']==g]:
                                html+='<tr><td>%s</td><td><a href="%s">%s</a></td></tr>\n'%(
                                    ''.join(s.split('//')[1][0] for s in l['sources']),
                                    l['URL'],
                                    l['GuideName']
                                )
                        html+='''</table></p>\n <p><table>'''
                        for s,sg in SOURCE_GROUPS.items():
                            html+='<tr><th>%s</th><td>%s</td></tr>'%(
                                s,
                                ','.join('<a href="#%s">%s</a>'%(quote(g),g) for g in sorted(sg))
                            )
                    html+='''</table></p>'''

                #serve logs
                elif self.path.startswith('/log'): 
                    html=self.html_start()
                    html+='''<p>'''
                    for l in LOGQ:
                        html+=l.msg+'<br>'
                    html+='''</p>'''

                # catch-all to serve status page
                else: 
                    if PROCS:
                        html+='''<p><table><tr><th>pid</th><th>client</th><th>command</th></tr>'''
                        for pid,args in PROCS.items():
                            html+='<tr><td>%s</td><td>%s</td><td>%s</td></tr>\n'%(pid,*args)
                        html+='''
                        </table>
                    </p>'''
                    env=config(CONFIG_FILE)
                    if SOURCES:
                        html+='''<p><table><tr><th></th><th>user</th><th>pass</th><th>priority</th><th colspan=2>status</th><th>expires</th></tr>'''
                        for url,accts in SOURCES.items():
                            html+='<tr><th colspan=7>%s</th><td>(%s streams)</td></tr>'%(url,
                            len(list(s for s in LINEUP.values() if url in s['sources'])) if LINEUP else '0')
                            for a in sorted(accts, key=lambda a: a[4]-a[3], reverse=True):
                                html+='<tr><td></td><td>%s</td><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td><td>%s</td></tr>\n'%a[:-1]
                        html+='''<tr><td><form method=post><input type=submit name=reload value=reload></form></td></tr></table></p>'''
                    html+='''<p><table>'''
                    for k,v in sorted(env.items()):
                        html+='<tr><th>%s</th><td>%s</td></tr>\n'%(k,v)
                    html+='''</table></p>'''

            except Exception as e:
                logging.exception(e)
                code=500
                html+='\n'+str(e)

            html+=self.html_end()
            self.send(html,code=code)

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
    global CONFIG_FILE, LOGQ, SOURCES
    CONFIG_FILE=args[0] if args else None
    env=config(CONFIG_FILE)
    LOGQ=LogQ(maxlen=LOGDEPTH)
    logging.basicConfig(level=int(LOGLEVEL), 
                        format='%(asctime)s %(levelname)s:%(message)s', 
                        handlers=[logging.StreamHandler(),QueueHandler(LOGQ)])
    for k,v in env.items():
        logging.debug('%s=%s',k,v)
    global LINEUP
    LINEUP,selected,SOURCES = scan(CONFIG_FILE)
    httpd = http.server.ThreadingHTTPServer((SERVER_IP, int(SERVER_PORT)), HDHR_handler)
    logging.info('serving at http://%s:%s' % (SERVER_IP, SERVER_PORT))
    httpd.serve_forever()

if __name__ == '__main__':    
    main(*sys.argv[1:])

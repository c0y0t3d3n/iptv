#!/usr/bin/python3
import time 
import json
import sys
import os
from datetime import datetime
import requests
import http.server
import subprocess
import logging
from logging.handlers import QueueHandler
from collections import deque
from urllib.parse import quote,unquote

global PROCS, LOGQ
PROCS={}

def config(config_file=None):
    ENV_VARS=['SERVER_IP','SERVER_PORT','CMD','DELAY','DIRECT','GROUPS','STREAMS','RENAME','REPLACE','FORMAT','BUFFER','LOGLEVEL','TUNER_COUNT']

    #set defaults 
    global SERVER_IP,SERVER_PORT,CMD,DELAY,DIRECT,GROUPS,STREAMS,RENAME,REPLACE,FORMAT,BUFFER,LOGLEVEL,LOGDEPTH,TUNER_COUNT
    LOGLEVEL=logging.INFO
    LOGDEPTH=50

    SERVER_IP='localhost'
    SERVER_PORT=5004

    TUNER_COUNT=4

    CMD='ffmpeg -hide_banner -loglevel error -user_agent tuner -i %s -c copy -copyts -f mpegts pipe:1'

    DELAY=0
    DIRECT=0
    FORMAT='ts'
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
                        if k.upper() in ENV_VARS:
                            globals()[k.upper()]=v
        except Exception as e:
            logging.warning(e)
    #config from env
    for e in ENV_VARS:
        globals()[e]=os.getenv(e,globals()[e])

    #parse config
    global GROUPS_INCLUDE, GROUPS_EXCLUDE, GROUPS_STARTSWITH, GROUPS_ENDSWITH, STREAMS_EXCLUDE, STREAMS_INCLUDE, REPLACE_STARTSWITH, REPLACE_ENDSWITH
    # channel groups
    GROUPS=GROUPS.upper().split(',')
    GROUPS_EXCLUDE=[f[1:] for f in GROUPS if f.startswith('!')]
    GROUPS=[f for f in GROUPS if f and not f.startswith('!') ]
    GROUPS_INCLUDE=[f for f in GROUPS if not f.startswith('^') and not f.endswith('$')]
    GROUPS_STARTSWITH=[f[1:] for f in GROUPS if f.startswith('|')]
    GROUPS_ENDSWITH=[f[:-1] for f in GROUPS if f.endswith('|')]
    # channel names
    STREAMS=STREAMS.upper().split(',')
    STREAMS_EXCLUDE=[c[1:] for c in STREAMS if c.startswith('!')]
    STREAMS_INCLUDE=[c for c in STREAMS if c and not c.startswith('!')]
    # patterns to strip from channel names. ^startwith, endswith$, or anywhere if no modifier
    # pattern/string will replace pattern with string
    RENAME=[r for r in RENAME.upper().split(',') if r]
    RENAME.append(',') #plex does not like commas in channel names
    # replace any channels with base name if a channel matching name+pattern exists 
    # example: REPLACE=' LHD' will rename 'ABC LHD' to 'ABC', removing any STREAMS named 'ABC', but only if 'ABC LHD' exists.
    REPLACE=REPLACE.upper().split(',')
    REPLACE_STARTSWITH=[r[1:] for r in REPLACE if r.startswith('|')]
    REPLACE_ENDSWITH=[r for r in REPLACE if r and not r.startswith('|')]

    # return config for info 
    return dict((k,globals()[k]) for k in ENV_VARS)

def xtream_request(url,user,pw,action):
    r=requests.get(url+'/player_api.php',params={'username':user,'password':pw,'action':action})
    r.raise_for_status()
    return json.loads(r.text)

# get server and account info
def check_acct(url,user,pw):
    try:
        info=None
        info=xtream_request(url,user,pw,'server_info')
        server_info,user_info=info['server_info'],info['user_info']
        return user, pw,int(user_info['active_cons']), int(user_info['max_connections']), user_info['status'], datetime.fromtimestamp(int(user_info['exp_date'])) if user_info['exp_date'] else None, server_info
    except Exception as e:
        logging.warning('%s %s %s %s %s',url,user,pw,e,info)
        return user, pw, None, None, str(info), None, {}

def refresh_accts(sources):
    refreshed={}
    for url,accts in sources.items():
        for a in accts:
            refreshed.setdefault(url,[]).append(check_acct(url,a[0],a[1]))
            time.sleep(int(DELAY))
    return refreshed

def select_acct(sources):
    selected={}
    for url,accts in sources.items():
        active=[a for a in accts if a[-3].lower()=='active']
        #sort by max-active to get most free slots at end
        active.sort(key=lambda a: a[3]-a[2])
        selected[url]=active[-1]
        logging.debug('selected %s %s %s %s/%s', url, *selected[url][:-3])
    return selected #account from each source with most available connections

def select_source(selected,source_list):
    #return url, acct data of source with most free slots
    selected_sources=list((k,v) for k,v in selected.items() if k in source_list) #filter to stream sources
    return sorted(selected_sources, key=lambda s: s[1][3]-s[1][2])[-1]
    
def fetch_lineup(selected):
    global GROUPS_INCLUDE, GROUPS_STARTSWITH, GROUPS_ENDSWITH, GROUPS_EXCLUDE, STREAMS_INCLUDE, STREAMS_EXCLUDE
    lineup={}
    for url,acct in selected.items():
        user,pw=acct[:2]
        #fetch from selected source account
        groups_in=dict( (e['category_id'],e['category_name'].upper()) for e in xtream_request(url,user,pw,'get_live_categories') )
        groups=dict( (i,n) for i,n in groups_in.items() \
            if ( not any ([GROUPS_INCLUDE, GROUPS_STARTSWITH, GROUPS_ENDSWITH]) \
                or n in GROUPS_INCLUDE \
                or any(n.startswith(f) for f in GROUPS_STARTSWITH) \
                or any(n.endswith(f) for f in GROUPS_ENDSWITH)\
            ) and not any (f in n for f in GROUPS_EXCLUDE) )
        logging.debug('%s groups: %s',url,list(groups.values()))
        streams_in=[s for s in xtream_request(url,user,pw,'get_live_streams') \
            if s['category_id'] in groups \
            or any(f in s['name'].upper() for f in STREAMS_INCLUDE)]
        #remove and rename streams
        streams=[]
        for s in streams_in:
            n=s['name'].upper()
            if  any(f in n for f in STREAMS_EXCLUDE):
                continue
            for p in RENAME:
                r=''
                if '=' in p:
                    p,r=p.split('=',1)
                if p.startswith('|'):
                    if n.startswith(p[1:]):
                        n=r+n[len(p[1:]):]
                elif p.endswith('|'):
                    if n.endswith(p[:-1]):
                        n=n[:-len(p[:-1])]+r
                else: n=n.replace(p,r)
            streams.append([n,s['stream_id'],groups_in[s['category_id']]])
        #replace channels if pattern_+channel exists
        for r in REPLACE_STARTSWITH:
            replaced=set()
            replaced.update(s[0][len(r):] for s in streams if s[0].startswith(r))
            #remove replaced channels
            streams=[s for s in streams if s[0] not in replaced]
            #rename name+pattern to name to replace channel
            for s in streams:
                if s[0].startswith(r):
                    s[0]=s[0][len(r):]
        #replace channels if channel+pattern exists
        for r in REPLACE_ENDSWITH:
            replaced=set()
            replaced.update(s[0][:-len(r)] for s in streams if s[0].endswith(r))
            #remove replaced channels
            streams=[s for s in streams if s[0] not in replaced]
            #rename name+pattern to name to replace channel
            for s in streams:
                if s[0].endswith(r):
                    s[0]=s[0][:-len(r)]
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
    global SOURCES
    SOURCES={}
    try:
        logging.info('reloading %s',config_file)
        #load accounts from config
        accts={}
        with open(config_file) as f:
            lines=f.readlines()
            for l in lines:
                if l.startswith('http'):
                    try:
                        url,user,pw=l.strip().split()[:3]
                        accts.setdefault(url,[]).append((user,pw))
                    except: pass
        #refresh account status
        SOURCES=refresh_accts(accts)
        selected=select_acct(SOURCES)
        return fetch_lineup(selected),selected,SOURCES
    except Exception as e:
        logging.exception(e)
        logging.warning('no usable accounts: %s',e)
        return None,None,SOURCES

class HDHR_handler(http.server.BaseHTTPRequestHandler):
    # emualte a HDHomeRun
    def do_POST(self):
        global CONFIG_FILE,SOURCES,LINEUP
        if self.path.startswith('/lineup.post'):
            # reload config and scan
            try:
                config(CONFIG_FILE)
                LINEUP = scan(CONFIG_FILE)[0]
                self.send_response(200)
                self.end_headers()
            except Exception as e:
                logging.exception(e)
                self.send_response(500)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        global CONFIG_FILE,SOURCES,LINEUP,PROCS,LOGQ
        if self.path.startswith('/stream/'):
            k=self.path.split('/stream/')[-1]
            if LINEUP and k in LINEUP:
                logging.info('%s stream %s'%(self.client_address,k))
                l=LINEUP[k]
                SOURCES=refresh_accts(SOURCES)
                source,a=select_source(select_acct(SOURCES),list(l['sources'].keys()))
                url = 'http://%s:%s/live/%s/%s/%s.%s' % (a[-1]['url'].split('//')[-1].split('/')[0], 
                                                         a[-1]['port'], a[0], a[1], 
                                                         l['sources'][source], FORMAT)
                if int(DIRECT):
                    # send the URL to plex
                    logging.info('%s request %s', self.client_address, url)
                    res = requests.get(url, allow_redirects=False, stream=True)
                    res.close()
                    if res.status_code==200:
                        loc = res.url
                    elif res.status_code in (301,302,303,307,308):
                        loc = res.headers['Location']
                    else:
                        logging.error('%s status %d', self.client_address, res.status_code)
                        self.send_response(res.status_code)
                        self.end_headers()
                        return
                    logging.info('%s redirect to %s', self.client_address, loc)
                    self.send_response(302)
                    self.send_header('Location', loc)
                    self.end_headers()
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
                        self.send_response(500)
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.end_headers()
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
                return
        elif self.path=='/discover.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                "DeviceID": "TUNER",
                "FriendlyName": "Tuner",
                "TunerCount": TUNER_COUNT,
                'BaseURL':'http://%s:%s'%(SERVER_IP,SERVER_PORT),
                'LineupURL':'http://%s:%s/lineup.json'%(SERVER_IP,SERVER_PORT),
            }).encode())
            return
        elif self.path=='/lineup_status.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'ScanInProgress':0,
                'ScanPossible':1,
                'Source':'Cable'
            }).encode())
            return
        elif self.path=='/lineup.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(list(LINEUP.values())).encode())
            return
        elif self.path.startswith('/?config='):
            param=self.path.split('=',1)[1]
            text=unquote(param.replace('+',' '))
            with open(CONFIG_FILE,'w') as f:
                f.write(text)
                logging.info('wrote %s',CONFIG_FILE)
            self.send_response(302)
            self.send_header('Location','/')
            self.end_headers()
            return
        elif self.path=='/':
            html='''
<html>
    <head>
        <style>
            body{font-family:monospace}
            th{text-align:left}
        </style>
    </head>
    <body>
'''
            try:
                html+='''
            <p>
                <table>
                    <tr><th>pid</th><th>client</th><th>command</th></tr>
'''
                for pid,args in PROCS.items():
                    html+='<tr><td>%s</td><td>%s</td><td>%s</td></tr>\n'%(pid,*args)
                html+='''
                </table>
            </p>
            <p>
                <table>
                    <tr><th>&nbsp;&nbsp;&nbsp;&nbsp;</th><th>user</th><th>pass</th><th colspan=2>status</th><th>expires</th></tr>
'''
                env=config(CONFIG_FILE)
                LINEUP = scan(CONFIG_FILE)[0]
                if SOURCES:
                    for url,accts in SOURCES.items():
                        html+='<tr><th colspan=6>%s</th></tr>'%url
                        for a in accts:
                            html+='<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;</td><td>%s</td><td>%s</td><td>%s/%s</td><td>%s</td><td>%s</td></tr>\n'%a[:-1]
                html+='''
                </table>
            </p>
            <p>
                <table>
'''
                if LINEUP:
                    cats=set(l['GuideCategory'] for l in LINEUP.values())
                    for g in sorted(cats):
                        html+='<tr/><tr><th colspan=2>'+g+'</th></tr>\n'
                        for k,l in [(k,l) for k,l in LINEUP.items() if l['GuideCategory']==g]:
                            html+='<tr><td>%s</td><td><a href="%s">%s</a></td></tr>\n'%(len(l['sources']),l['URL'],l['GuideName'])
                html+='''
                </table>
            </p>
            <p>
'''
                for l in LOGQ:
                    html+=l.msg+'<br>'
                html+='''
            </p>
            <p>
                <table>
'''
                for k,v in sorted(env.items()):
                    html+='<tr><th>%s</th><td>%s</td></tr>\n'%(k,v)
                html+='''
                </table>
            </p>
'''
                if CONFIG_FILE:
                    html+='''
            <p>
                <form method=get>
                    <textarea style=font-family:monospace name=config cols=100 rows=20>'''
                    try:
                        with open(CONFIG_FILE) as f:
                            html+=f.read(-1)
                    except Exception as e:
                        html+=str(e)
                    html+='''</textarea><br>
                    <input type=submit value=save>
                </form>
                </p>'''
                self.send_response(200)
                self.end_headers()
            except Exception as e:
                logging.exception(e)
                self.send_response(500)
                self.end_headers()
                html+='\n\n'+str(e)
            html+='''
        </body>
    <html>
'''
            self.wfile.write(html.encode())
            return
        # bad request
        self.send_response(404)
        self.end_headers()     

class LogQ(deque):
   '''leaky queue that drops oldest items'''
   def put_nowait(self, item, **kwargs):
        self.append(item)

def main(*args):
    global CONFIG_FILE, LOGQ
    CONFIG_FILE=args[0] if args else None
    env=config(CONFIG_FILE)
    LOGQ=LogQ(maxlen=LOGDEPTH)
    logging.basicConfig(level=int(LOGLEVEL), 
                        format='%(asctime)s %(levelname)s:%(message)s', 
                        handlers=[logging.StreamHandler(),QueueHandler(LOGQ)])
    for k,v in env.items():
        logging.debug('%s=%s',k,v)
    global LINEUP
    LINEUP = scan(CONFIG_FILE)[0]
    httpd = http.server.ThreadingHTTPServer((SERVER_IP, int(SERVER_PORT)), HDHR_handler)
    logging.info('serving at http://%s:%s' % (SERVER_IP, SERVER_PORT))
    httpd.serve_forever()

if __name__ == '__main__':    
    main(*sys.argv[1:])

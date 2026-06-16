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

global PROCS
PROCS={}

def config(config_file):
    ENV_VARS=['SERVER_IP','SERVER_PORT','CMD','DELAY','DIRECT','GROUPS','STREAMS','STRIP','REPLACE','FORMAT','BUFFER','LOGLEVEL','TUNER_COUNT']

    #set defaults 
    global SERVER_IP,SERVER_PORT,CMD,DELAY,DIRECT,GROUPS,STREAMS,STRIP,REPLACE,FORMAT,BUFFER,LOGLEVEL,TUNER_COUNT
    LOGLEVEL=logging.INFO

    SERVER_IP='localhost'
    SERVER_PORT=5004

    TUNER_COUNT=4

    CMD='ffmpeg -hide_banner -loglevel error -user_agent tuner -i %s -c copy -copyts -f mpegts pipe:1'

    DELAY=0
    DIRECT=0
    FORMAT='ts'
    # GROUPS to these categories 
    GROUPS=''
    STRIP='^US: ,^USA: ,^US | ,^USA | '
    STREAMS=''
    REPLACE=''

    BUFFER=1024*1024 #buffer size for streaming

    #config from k=v in file
    try:
        with open(config_file) as f:
            lines=f.readlines()
            for l in lines:
                l=l.split('#')[0]
                if '=' in l:
                    k,v=l.strip('\n').split('=',1)
                    if k in ENV_VARS:
                        globals()[k]=v
    except Exception as e:
        logging.warning(e)
    #config from env
    for e in ENV_VARS:
        globals()[e]=os.getenv(e,globals()[e])

    #parse config
    PARSED_VARS=['GROUPS_INCLUDE', 'GROUPS_EXCLUDE', 'GROUPS_STARTSWITH', 'GROUPS_ENDSWITH', 'STREAMS_EXCLUDE', 'STREAMS_INCLUDE']
    # channel groups
    GROUPS=GROUPS.split(',')
    if '' in GROUPS: GROUPS.remove('')
    GROUPS_INCLUDE=[f for f in GROUPS if not f.startswith('!') and not f.startswith('^') and not f.endswith('$')]
    GROUPS_EXCLUDE=[f[1:] for f in GROUPS if f.startswith('!')]
    GROUPS_STARTSWITH=[f[1:] for f in GROUPS if f.startswith('^')]
    GROUPS_ENDSWITH=[f[:-1] for f in GROUPS if f.endswith('$')]
    if not any ([GROUPS_INCLUDE, GROUPS_EXCLUDE, GROUPS_STARTSWITH, GROUPS_ENDSWITH]):
        GROUPS=None
    # channel names
    STREAMS=STREAMS.split(',')
    if '' in STREAMS: STREAMS.remove('')
    STREAMS_EXCLUDE=[c[1:] for c in STREAMS if c.startswith('!')]
    STREAMS_INCLUDE=[c for c in STREAMS if not c.startswith('!')]
    # patterns to strip from channel names. ^startwith, endswith$, or anywhere if no modifier
    STRIP=STRIP.split(',')
    if '' in STRIP: STRIP.remove('')
    STRIP.append(',') #plex does not like commas in channel names
    # replace any channels with base name if a channel matching name+pattern exists 
    # example: REPLACE=' LHD' will rename 'ABC LHD' to 'ABC', removing any STREAMS named 'ABC', but only if 'ABC LHD' exists.
    REPLACE=REPLACE.split(',')
    if '' in REPLACE: REPLACE.remove('')

    # add to glboals
    for e in PARSED_VARS:
        globals()[e]=locals()[e]

    # return full config for info 
    return dict((k,globals()[k]) for k in ENV_VARS+PARSED_VARS)

def xtream_request(url,user,pw,action):
    r=requests.get(url+'/player_api.php',params={'username':user,'password':pw,'action':action})
    r.raise_for_status()
    return json.loads(r.text)

# get server and account info
def check_acct(url,user,pw):
    try:
        info=''
        info=xtream_request(url,user,pw,'server_info')
        server_info,user_info=info['server_info'],info['user_info']
        return url, user, pw,int(user_info['active_cons']), int(user_info['max_connections']), user_info['status'], datetime.fromtimestamp(int(user_info['exp_date'])) if user_info['exp_date'] else None, server_info
    except Exception as e:
        return url, user, pw, None, None,str(e), None, {}

def fetch_lineup(url,user,pw):
    cats=dict( (e['category_id'],e['category_name']) for e in xtream_request(url,user,pw,'get_live_categories') )
    filtered_cats=dict( (i,n) for i,n in cats.items() \
        if GROUPS is None or n in GROUPS_INCLUDE \
        or any(n.startswith(f) for f in GROUPS_STARTSWITH) \
        or any(n.endswith(f) for f in GROUPS_ENDSWITH) \
        and not any (n.startswith(f) for f in GROUPS_EXCLUDE) )
    logging.info('groups: %s',list(filtered_cats.values()))
    streams=[s for s in xtream_request(url,user,pw,'get_live_streams') \
        if s['category_id'] in filtered_cats \
        or any(c.upper() in s['name'].upper() for c in STREAMS_INCLUDE)]
    #remove and rename streams
    out=[]
    for s in streams:
        n=s['name'].upper()
        if  any(r in n for r in STREAMS_EXCLUDE):
            continue
        for p in STRIP:
            if p.startswith('^'):
                if n.startswith(p[1:]):
                    n=n[len(p[1:]):]
            elif p.endswith('$'):
                if n.endswith(p[:-1]):
                    n=n[:-len(p[:-1])]
            else: n=n.replace(p,'')
        out.append([n,s['stream_id'],cats[s['category_id']]])
    streams=out
    #replace channels if channel+pattern exists
    for r in REPLACE:
        replaced=set()
        replaced.update(s[0][:-len(r)] for s in streams if s[0].endswith(r))
        #remove replaced channels
        streams=[s for s in streams if s[0] not in replaced]
        #rename name+pattern to name to replace channel
        for s in streams:
            if s[0].endswith(r):
                s[0]=s[0][:-len(r)]
    # return lineup
    logging.info('streams: %d', len(streams))
    return dict ((int(s[1]), {
        'GuideName':s[0], 
        'GuideNumber':s[0], 
        'GuideCategory':s[2],
        'URL':'http://%s:%s/stream/%s'%(SERVER_IP,SERVER_PORT,s[1])
        } )for s in streams)

def refresh_accts(accts):
    acct_info=[]
    for a in accts:
        acct_info.append(check_acct(*a[:3]))
        time.sleep(int(DELAY))
    return acct_info

def select_acct(accts):
    active=[a for a in accts if a[-3].lower()=='active']
    #sort by max-active to get most free slots at end
    active.sort(key=lambda a: a[4]-a[3])
    selected=active[-1]
    logging.info('selected %s %s %s %s/%s', *selected[:-3])
    return selected #account with most available connections

def scan(acct_file):
    global ACCTS
    try:
        logging.info('reloading %s',acct_file)
        #load accounts from config
        accts=[]
        with open(acct_file) as f:
            lines=f.readlines()
            for l in lines:
                if l.startswith('http'):
                    try:
                        url,user,pw=l.strip().split()[:3]
                        accts.append((url,user,pw))
                    except: pass
        #refresh account status
        ACCTS=refresh_accts(accts)
        return fetch_lineup(*select_acct(ACCTS)[:3])
    except Exception as e:
        logging.warning('no usable accounts: %s',e)
        return None

class HDHR_handler(http.server.BaseHTTPRequestHandler):
    # emualte a HDHomeRun
    def do_POST(self):
        global CONFIG_FILE,ACCTS,LINEUP
        if self.path.startswith('/lineup.post'):
            # reload config and scan
            try:
                config(CONFIG_FILE)
                LINEUP = scan(CONFIG_FILE)
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
        global CONFIG_FILE,ACCTS,LINEUP,PROCS
        if self.path.startswith('/stream/'):
            stream_id=int(self.path.split('/stream/')[-1])
            if stream_id in LINEUP:
                ACCTS=refresh_accts(ACCTS)
                a=select_acct(ACCTS)
                url = 'http://%s:%s/live/%s/%s/%s.%s' % (a[-1]['url'].split('//')[-1].split('/')[0], a[-1]['port'], a[1], a[2], stream_id, FORMAT)
                if int(DIRECT):
                    # send the URL to plex
                    logging.info('direct from %s', url)
                    res = requests.get(url, allow_redirects=False, stream=True)
                    res.close()
                    if res.status_code==200:
                        loc = res.url
                    elif res.status_code in (301,302,303,307,308):
                        loc = res.headers['Location']
                    else:
                        logging.error('status %d', res.status_code)
                        self.send_response(res.status_code)
                        self.end_headers()
                        return
                    logging.info('location: %s', loc)
                    self.send_response(302)
                    self.send_header('Location', loc)
                    self.end_headers()
                else:
                    # remux with ffmpeg
                    args = CMD % url
                    logging.info('starting %s', args)
                    try:
                        cmd = subprocess.Popen(args.split(), shell=False, stdout=subprocess.PIPE)
                        logging.info('pid %s running', cmd.pid)
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
                    logging.info('pid %s stopped (%d)', cmd.pid, cmd.returncode)
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
        elif self.path=='/':
            html='<head></head><body><pre>'
            for pid,args in PROCS.items():
                html+='client %s %s %s\n'%(pid,*args)
            html+='\n'
            try:
                env=config(CONFIG_FILE)
                LINEUP = scan(CONFIG_FILE)
                for k,v in sorted(env.items()):
                    html+='%s %s\n'%(k,v)
                html+='\n'
                if ACCTS:
                    for a in ACCTS:
                        html+='%s %s %s %s/%s %s %s\n'%a[:-1]
                html+='\n'
                if LINEUP:
                    cats=set(l['GuideCategory'] for l in LINEUP.values())
                    for g in cats:
                        html+=g+'\n'
                        for c in [l for l in LINEUP.values() if l['GuideCategory']==g]:
                            html+='   <a href="%(URL)s">%(GuideName)s</a>\n'%c
                self.send_response(200)
                self.end_headers()
            except Exception as e:
                logging.exception(e)
                self.send_response(500)
                self.end_headers()
                html+='\n\n'+str(e)
            html+='</pre></body>'
            self.wfile.write(html.encode())
            return
        # bad request
        self.send_response(404)
        self.end_headers()     

def main(*args):
    global CONFIG_FILE
    CONFIG_FILE=args[0] if args else None
    env=config(CONFIG_FILE)
    logging.basicConfig(level=int(LOGLEVEL), format='%(asctime)s %(levelname)s:%(message)s')
    for k,v in env.items(): logging.info('%s %s',k,v)
    global LINEUP
    LINEUP = scan(CONFIG_FILE)
    httpd = http.server.ThreadingHTTPServer((SERVER_IP, int(SERVER_PORT)), HDHR_handler)
    logging.info('serving at http://%s:%s' % (SERVER_IP, SERVER_PORT))
    httpd.serve_forever()

if __name__ == '__main__':    
    main(*sys.argv[1:])

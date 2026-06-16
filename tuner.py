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

def config():

    ENV_VARS=['SERVER_IP','SERVER_PORT','CMD','DELAY','DIRECT','GROUPS','STREAMS','STRIP','REPLACE','FORMAT','BUFFER','LOGLEVEL','TUNER_COUNT']
    global SERVER_IP, SERVER_PORT, CMD, DELAY, DIRECT, GROUPS, STREAMS, STRIP, REPLACE, FORMAT, BUFFER, LOGLEVEL, TUNER_COUNT

    LOGLEVEL=logging.INFO

    SERVER_IP='localhost'
    SERVER_PORT=5004

    #unique id for this device, used by plex to identify tuners and prevent duplicates.
    TUNER_COUNT=4

    CMD='ffmpeg -hide_banner -loglevel error -user_agent tuner -i %s -c copy -copyts -f mpegts pipe:1'

    #DELAY between account checks, in seconds, default 0. increase if you have many accounts to avoid hitting xtream limits.
    DELAY=0
    DIRECT=0
    #if 1, stream directly from xtream url, otherwise stream through ffmpeg which can fix some issues with plex and xtream urls, but uses more resources.
    FORMAT='ts'
    # GROUPS to these categories 
    GROUPS=''
    STRIP='^US: ,^USA: ,^US | ,^USA | '
    STREAMS=''
    REPLACE=''

    BUFFER=1024*1024 #buffer size for streaming

    #config from k=v in file
    if len(sys.argv)>1:
        with open(sys.argv[1]) as f:
            lines=f.readlines()
            for l in lines:
                l=l.split('#')[0]
                if '=' in l:
                    k,v=l.strip('\n').split('=',1)
                    if k in ENV_VARS:
                        globals()[k]=v
    #config from env
    for e in ENV_VARS:
        globals()[e]=os.getenv(e,globals()[e])

    #parse config
    global GROUPS_EXCLUDE, GROUPS_STARTSWITH, GROUPS_ENDSWITH, REMOVE
    # category GROUPSs
    GROUPS=GROUPS.split(',')
    if '' in GROUPS: GROUPS.remove('')
    GROUPS_EXCLUDE=[f[1:] for f in GROUPS if f.startswith('!')]
    GROUPS_STARTSWITH=[f[1:] for f in GROUPS if f.startswith('^')]
    GROUPS_ENDSWITH=[f[:-1] for f in GROUPS if f.endswith('$')]
    GROUPS=[f for f in GROUPS if not f.startswith('!') and not f.startswith('^') and not f.endswith('$')]
    if not any ([GROUPS, GROUPS_EXCLUDE, GROUPS_STARTSWITH, GROUPS_ENDSWITH]):
        GROUPS=None
    # channel name GROUPSs
    STREAMS=STREAMS.split(',')
    if '' in STREAMS: STREAMS.remove('')
    REMOVE=[c[1:] for c in STREAMS if c.startswith('!')]
    STREAMS=[c for c in STREAMS if not c.startswith('!')]
    # patterns to strip from channel names. ^startwith, endswith$, or anywhere if no modifier
    STRIP=STRIP.split(',')
    if '' in STRIP: STRIP.remove('')
    STRIP.append(',') #plex does not like commas in channel names
    # replace any STREAMS with base name if a channel matching name+pattern exists 
    # example: REPLACE=' LHD' will rename 'ABC LHD' to 'ABC', removing any STREAMS named 'ABC', but only if 'ABC LHD' exists.
    REPLACE=REPLACE.split(',')
    if '' in REPLACE: REPLACE.remove('')

def xtream_request(url,user,pw,action):
    r=requests.get(url+'/player_api.php',params={'username':user,'password':pw,'action':action})
    if r.status_code != 200:
        logging.error("%s status %s", action, r.status_code)
    r.raise_for_status()
    return json.loads(r.text)

# get server and account info
def check_acct(url,user,pw,print_info=False):
    info=xtream_request(url,user,pw,'server_info')
    try:
        server_info,user_info=info['server_info'],info['user_info']
        if print_info:
            logging.info('%s:%s %s %s %s %s/%s %s',
                server_info['url'],
                server_info['port'],
                user,pw,
                user_info['status'],
                user_info['active_cons'],
                user_info['max_connections'],
                datetime.fromtimestamp(int(user_info['exp_date'])) if user_info['exp_date'] else None
            )
        return url,user,pw,int(user_info['active_cons']),int(user_info['max_connections']), user_info['status'], server_info
    except:
        logging.warning("%s %s %s %s", url, user, pw, info)
        return url, user, pw, None, None, '', {}

def fetch_lineup(url,user,pw):
    cats=dict( (e['category_id'],e['category_name']) for e in xtream_request(url,user,pw,'get_live_categories') \
        if GROUPS is None or e['category_name'] in GROUPS \
        or any(e['category_name'].startswith(f) for f in GROUPS_STARTSWITH) \
        or any(e['category_name'].endswith(f) for f in GROUPS_ENDSWITH) \
        and not any (e['category_name'].startswith(f) for f in GROUPS_EXCLUDE) )
    logging.info('groups: %s',list(cats.values()))
    streams=[s for s in xtream_request(url,user,pw,'get_live_streams') if s['category_id'] in cats or any(c.upper() in s['name'].upper() for c in STREAMS)]
    #remove and rename streams
    out=[]
    for s in streams:
        n=s['name'].upper()
        if  any(r in n for r in REMOVE):
            continue
        for p in STRIP:
            if p.startswith('^'):
                if n.startswith(p[1:]):
                    n=n[len(p[1:]):]
            elif p.endswith('$'):
                if n.endswith(p[:-1]):
                    n=n[:-len(p[:-1])]
            else: n=n.replace(p,'')
        out.append([n,s['stream_id']])
    streams=out
    #skip STREAMS if channel+pattern exists
    for r in REPLACE:
        replaced=set()
        replaced.update(s[0][:-len(r)] for s in streams if s[0].endswith(r))
        #remove replaced STREAMS
        streams=[s for s in streams if s[0] not in replaced]
        #rename name+pattern to name to replace STREAMS
        for s in streams:
            if s[0].endswith(r):
                s[0]=s[0][:-len(r)]
    # return lineup
    logging.info('streams: %d', len(streams))
    return dict ((int(s[1]), {
        'GuideName':s[0], 
        'GuideNumber':s[0], 
        'URL':'http://%s:%s/stream/%s'%(SERVER_IP,SERVER_PORT,s[1])
        } )for s in streams)

def select_acct(accts,print_info=False):
    info=[]
    for a in accts:
        info.append(check_acct(*a,print_info=print_info))
        time.sleep(int(DELAY))
    info=[a for a in info if a[-2].lower()=='active']
    #sort by max-active, descending
    info.sort(key=lambda a: a[4]-a[3])
    logging.info('selected %s %s %s %s/%s', *info[-1][:-2])
    return info[-1] #account with most available connections

def scan(print_info=True):
    #load accounts from config
    accts=[]
    with open(sys.argv[1]) as f:
        lines=f.readlines()
        for l in lines:
            if l.startswith('http'):
                try:
                    url,user,pw=l.strip().split()[:3]
                    accts.append((url,user,pw))
                except: pass
    #fetch lineup
    url,user,pw,active,max_conns,status,service_info=select_acct(accts,print_info=print_info)
    return accts, fetch_lineup(url,user,pw)

class HDHR_handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.startswith('/lineup.post'):
            try:
                config()
                global accts, lineup
                accts, lineup = scan()
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
        if self.path.startswith('/stream/'):
            stream_id=int(self.path.split('/stream/')[-1])
            if stream_id in lineup:
                url,user,pw,active,max_conns,status,server_info=select_acct(accts)
                url = 'http://%s:%s/live/%s/%s/%s.%s' % (server_info['url'].split('//')[-1].split('/')[0], server_info['port'], user, pw, stream_id, FORMAT)
                if int(DIRECT):
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
                    cmd = CMD % url
                    logging.info('starting %s', cmd)
                    try:
                        cmd = subprocess.Popen(cmd.split(), shell=False, stdout=subprocess.PIPE)
                        logging.info('pid %s running', cmd.pid)
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
        elif self.path=='/lineup_status.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({
                'ScanInProgress':0,
                'ScanPossible':1,
                'Source':'Cable', 
                'SourceList':['Cable']
            }).encode())
        elif self.path=='/lineup.json':
            self.send_response(200)
            self.send_header('Content-type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps(list(lineup.values())).encode())
        else:
            self.send_response(404)
            self.end_headers()     

def main():     
    config()
    if len(sys.argv) < 2:
        for e in ENV_VARS:
            print (e,globals()[e])
        sys.exit(0)
    logging.basicConfig(level=int(LOGLEVEL), format='%(asctime)s %(levelname)s:%(message)s')
    global accts, lineup
    accts,lineup = scan()
    httpd = http.server.ThreadingHTTPServer((SERVER_IP, int(SERVER_PORT)), HDHR_handler)
    logging.info('serving at http://%s:%s' % (SERVER_IP, SERVER_PORT))
    httpd.serve_forever()

main()

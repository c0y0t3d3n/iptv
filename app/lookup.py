#!/usr/bin/python3
import sys
import requests
import json
import re
from html import unescape

IPTVLOOKUP_URL='https://iptvlookup.com/'
def iptvlookup(url,expr=None):
    try:
        h = url.split('=')[-1] #might be full URL, get the hash
        r=requests.get(IPTVLOOKUP_URL,params={'data':h})
        r.raise_for_status()
        payload=None
        for l in r.text.split('\n'):
            if 'raw-payload-data' in l:
                payload='{'
            elif '</pre>' in l:
                payload+='}'
                break
            elif payload is not None:
                payload+=unescape(l)
        if 'email-protection' in payload: # clever girl
            ep=re.search('<.*>',payload).group(0).split('"')[5]
            x=int(ep[0:2],16)
            username=''.join([ chr(int(ep[i:i+2],16)^x) for i in range(2,len(ep),2) ])
            payload=re.sub('<.*>',username,payload)
        j=json.loads(payload)
        if not expr or eval(expr,{},j['user_info']):
            return 'http://%s:%s %s %s' % (
                j['server_info']['url'],
                j['server_info']['port'],
                j['user_info']['username'],
                j['user_info']['password']
            )
    except Exception as e:
        print (e,r,file=sys.stderr)

def parse_url(url):
    try:
        url,params=url.split('?',1)
        url=url.replace('/get.php','')
        params=dict(p.split('=') for p in params.split('&'))
        return '%s %s %s'%(url,params['username'],params['password'])
    except Exception as e:
        print (e,file=sys.stderr)

def lookup(url,*args):
    if 'iptvlookup' in url:
        r=iptvlookup(url,*args)
    elif ' ' in url:
        r=url
    else:
        r=parse_url(url)
    return r

if __name__ == '__main__':
    from tuner import Config, Account, Data
    import logging
    config=Config()
    config.reload()
    logging.basicConfig(level=config.loglevel)
    data=Data(config)
    r=lookup(*sys.argv[1:])
    if r:
        r=r.split() 
        url,a=r[0],Account(*r[1:3])
        data.refresh_accts({url:[a]})
        a=data.accounts[url][0]
        print (url,' '.join(str(s) for s in a.data()))
        if sys.argv[-1].endswith('m3u'):
            config.streams=['.']
            data.fetch_lineup([(url,a)])
            data.build_lineup()
            with open(sys.argv[-1],'w') as f:
                f.write('\n'.join(data.generate_m3u()))
        
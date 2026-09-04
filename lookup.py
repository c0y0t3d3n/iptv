#!/usr/bin/python3
import sys
import requests
import json
import re
from html import unescape

FETCH_URL='https://iptvlookup.com/'

def iptvlookup(url):
    try:
        h = url.split('=')[-1] #might be full URL, get the hash
        r=requests.get(FETCH_URL,params={'data':h})
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
        return 'http://%s:%s %s %s' % (
            j['server_info']['url'],
            j['server_info']['port'],
            j['user_info']['username'],
            j['user_info']['password']
        )
    except Exception as e:
        print (e,r,file=sys.stderr)

if __name__ == '__main__':
    print (iptvlookup(sys.argv[1]))
        

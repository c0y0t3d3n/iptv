#!/usr/bin/python3
import sys
import requests
import json

REGISTER_URL='https://iptv.tutoje.cz/api/import-key/register'
FETCH_URL='https://iptv.tutoje.cz/api/import'
APIKEY='.amz_api_key'

h = sys.argv[1].split('=')[-1] #might be full URL, get the hash
try:
    with open(APIKEY) as f:
        api_key=f.read()
except:
    api_key=None

if not api_key:
    try:
        r=requests.post(REGISTER_URL)
        r.raise_for_status()
        api_key=json.loads(r.text)['api_key']
        with open(APIKEY,'w') as f:
            f.write(api_key)
    except Exeption as e:
        print(e,r,file=sys.stderr)
        api_key=None

if api_key:
    try:
        r=requests.get(FETCH_URL+'/'+h,headers={'X-Api-Key':api_key})
        r.raise_for_status()
        j=json.loads(r.text)
        print ('%(server)s %(username)s %(password)s'%j)
    except Exception as e:
        print (e,r,file=sys.stderr)

        

#!/usr/bin/python3
import sys
import os
import logging
from tuner import config, check_acct, fetch_lineup, scan

def generate_m3u(selected,lineup,env):
    i=0
    with open(m3u,'w') as f:
        print('#EXTM3U',file=f)
        for l in lineup.values():
            print('#EXTINF:-1 group-title="%s" tvg-id="%s" tvg-name="%s",%s' % (l['GuideCategory'],l['GuideNumber'],l['GuideName'],l['GuideName']), file=f)
            url = list(l['sources'].keys())[0]
            sid = l['sources'][url]
            acct = selected[url]
            user,pw,server_info=acct[0],acct[1],acct[-1]['server_info']
            print(env['FORMAT'] % (
                server_info['url'].split('//')[-1].split('/')[0],
                server_info['port'],
                user, pw, sid
            ), file=f)
            i+=1
    print(m3u,i)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('''
usage: 
    ./iptv.sh <URL> <user> <pas> (to check account)
    ./iptv.sh <URL> <user> <pass> [m3u_file] (to generate m3u)
    ./iptv.sh <account_list> (to check all accounts)
    ./iptv.sh <account_list> [m3u file] (to generate m3u for least busy account)
''')
        sys.exit(0)

    if sys.argv[1].startswith('http'):
        env=config()
        logging.basicConfig(level=int(env['LOGLEVEL']))
        url, user, pw = sys.argv[1:4]
        if len(sys.argv)>4:
            m3u=sys.argv[4]
        else:
            m3u=None    
        acct=check_acct(url,user,pw)
        print('%s %s %s %s %s/%s %s %s'%(url,*acct[:-1]))
        if m3u: 
            selected={url:acct}
            lineup=fetch_lineup(selected)
            generate_m3u(selected,lineup,env)
    else:
        if len(sys.argv)>2:
            m3u=sys.argv[2]
        else:
            m3u=None
        env=config(sys.argv[1])
        logging.basicConfig(level=int(env['LOGLEVEL']))
        lineup,selected,sources=scan(sys.argv[1])
        for url,accts in sources.items():
            for acct in accts:
                print('%s %s %s %s %s/%s %s %s'%(url,*acct[:-1]))
        if m3u and lineup:
            generate_m3u(selected,lineup,env)


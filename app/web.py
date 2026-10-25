#!/usr/bin/python3
import http.server
from urllib.parse import quote, unquote, urlsplit

class RequestHandler(http.server.BaseHTTPRequestHandler):
    '''HTTP request handler'''

    routes={}
    params={}

    def handle(self, *args, **kwargs):
        self.response_sent=False
        self.start_request()
        super().handle(*args, **kwargs)
        self.end_request()

    def start_request(self):
        #define in subclass to do something before every request
        pass

    def end_request(self):
        #define in subclass to do something after every request
        pass

    def send(self,html='',redirect=None,code=200):
        self.send_response(302 if redirect and code==200 else code)
        if redirect:
            self.send_header('Location',redirect)
        self.end_headers()
        if html: 
            self.wfile.write(html.encode())
        self.response_sent=True

    def do_POST(self):
        # get POST data 
        l=int(self.headers.get('content-length',0))
        body=self.rfile.read(l)
        params={}
        for p in body.decode().split('&'):
            if p:
                k,v=unquote(p.replace('+',' ')).split('=',1)
                params[k]=v
        self.__handle_request(self.path,params)

    def do_GET(self):
        #get params from URL
        parts=self.path.split('?',1)
        params={}
        if len(parts)>1:
            for p in path[1].split('&'):
                if p:
                    k,v=unquote(p.replace('+',' ')).split('=',1)
                    params[k]=v
        self.__handle_request(parts[0],params)

    def __handle_request(self,path=None,params={}):
        self.__handle_params(params)
        if not self.response_sent:
            self.send(self.__handle_path(path,**params))

    def __handle_params(self,params):
        #call param handlers if defined
        # params handler will get (key,value,**params)
        r=''
        for key,val in params.items():
            try:
                if key in self.params:
                    r=getattr(self,self.params[key])(key,val,**params)
                if self.response_sent:
                    #stop if we sent a response
                    break
            except Exception as e:
                logging.exception(e)
                self.send(str(e),code=500)
        return r
  
    def __handle_path(self,path,**params):
        # call the first matching route handler, or the handler for an undefined route, or return a 404
        # route handler will get (path,**params)
        routes=self.routes.get(self.command,{})
        route=[routes[r] for r in routes if r and path.startswith(r)]
        route=route[0] if len(route) else routes.get(None)
        r=''
        try:
            if route:
                r=getattr(self,route)(path,**params)
            else:
                self.send(code=404)
        except Exception as e:
            logging.exception(e)
            self.send(str(e),code=500)
        return r
        

class Server():
    def __init__(self,addr,handler,**ssl_args):
        if ssl_args:
            self.server=http.server.ThreadingHTTPSServer(addr,handler,**ssl_args)
        else:
            self.server=http.server.ThreadingHTTPServer(addr,handler)

    def run(self):
        self.server.serve_forever()
        
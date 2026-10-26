#!/usr/bin/python3
import logging
import http.server
from urllib.parse import unquote, urlsplit

class RequestHandler(http.server.BaseHTTPRequestHandler):
    '''HTTP request handler'''

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

    def send(self,response=None,redirect=None,code=200):
        self.send_response(302 if redirect and code==200 else code)
        if redirect:
            self.send_header('Location',redirect)
        self.end_headers()
        if response: 
            self.wfile.write(response.encode())
        self.response_sent=True #flag to indicate response sent by early handler

    def parse_params(self,query='',params=None):
        if params is None:
            params={}
        for p in query.split('&'):
            if p:
                k,v=unquote(p.replace('+',' ')).split('=',1)
                params[k]=v
        return params

    def do_GET(self):
        #handle params/path
        self.__handle_request()

    def do_POST(self):
        # get POST data from body then handle params/path
        l=int(self.headers.get('content-length',0))
        body=self.rfile.read(l)
        self.__handle_request(self.parse_params(body.decode()))

    def __handle_request(self,params=None):
        parts=urlsplit(self.path)
        #update params
        params=self.parse_params(parts.query,params)
        #do global parameter handlers first
        self.__handle_params(params)
        #unless param handler responded handle routing of path
        if not self.response_sent:
            self.send(self.__handle_path(parts.path,**params))

    def __handle_params(self,params):
        #call param handlers if defined
        # params handler will get (key,value,**params)
        for key,val in params.items():
            try:
                if key in self.params:
                    self.params[key](self,key,val,**params)
                if self.response_sent:
                    #stop if we sent a response
                    break
            except Exception as e:
                logging.exception(e)
                self.send(str(e),code=500)
  
    def __handle_path(self,path,**params):
        # call the first matching route handler, or the None handler for an undefined route, or return a 404
        # route handler will get (path,**params)
        routes=self.routes.get(self.command,{})
        routes=[routes[r] for r in routes if r and path.startswith(r)]+[routes.get(None)] 
        response=''
        try:
            if routes[0]:
                response=routes[0](self,path,**params)
            else:
                self.send(code=404)
        except Exception as e:
            logging.exception(e)
            self.send(str(e),code=500)
        return response


    '''param handlers defined in subclass are called before routing.
    params={ 
        'key': param_handler 
    }
    and should be defined as
    def param_handler(self,key,value,**params):
        #can terminate request with
        self.send(response,code=...)
    '''  
    params={}

    '''routes are defined in subclass as:
    routes={ 
        'HTTP_METHOD':{
            '/path': path_handler,
            None: default_path_handler (returns 404 if not defined)
        }
    }
    and should be defined as
    def path_handler(self,path,**params):
        #can respond with
        self.send(response,code=...)
        #or
        return response #code will be 200
    '''        
    routes={}


class Server():
    ''' http.serer wrapper, creates HTTPS if kwargs for SSL are provided else HTTP '''
    def __init__(self,addr,handler,**ssl_args):
        if ssl_args:
            self.server=http.server.ThreadingHTTPSServer(addr,handler,**ssl_args)
        else:
            self.server=http.server.ThreadingHTTPServer(addr,handler)

    def run(self):
        logging.info('serving at http://%s:%s',self.server.server_name, self.server.server_port)
        self.server.serve_forever()
        

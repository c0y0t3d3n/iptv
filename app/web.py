#!/usr/bin/python3
import logging
import http.server
from urllib.parse import unquote, urlsplit
from re import match

class RequestHandler(http.server.BaseHTTPRequestHandler):
    '''HTTP request handler'''

    def do_GET(self):
        #handle params/path
        self.__handle_request()

    def do_POST(self):
        # get POST data from body then handle params/path
        l=int(self.headers.get('content-length',0))
        body=self.rfile.read(l)
        self.__handle_request(self.__parse_params(body.decode()))

    def __parse_params(self,query='',params=None):
        params=params if params else {}
        for p in query.split('&'):
            if p:
                k,v=unquote(p.replace('+',' ')).split('=',1)
                params[k]=v
        return params

    def __handle_request(self,params=None):
        self.response_sent=False   
        #get request parts
        parts=urlsplit(self.path)
        #update params
        params=self.__parse_params(parts.query,params)
        #do global parameter handlers first
        self.__handle_params(params)
        #unless param handler responded handle routing of path
        if not self.response_sent:
            response=self.__handle_path(parts.path,**params)
            if not self.response_sent:
                self.respond(response)

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
                self.respond(str(e),code=500)
  
    def __handle_path(self,path,**params):
        # call the route handler, or the None handler for an undefined route, or return a 404
        # route handler will get (path,**params)
        try:
            routes=self.routes.get(self.command,{}) #get routes for this method
            matches=[r for r in routes if match(r,path)]
            route=routes[matches[0]] if matches else routes.get(None)
            if route:
                return route(self,path,**params)
            else:
                self.respond(code=404)
        except Exception as e:
            logging.exception(e)
            self.respond(str(e),code=500)

    def respond(self,response=None,redirect=None,code=200):
        self.send_response(302 if redirect and code==200 else code)
        if redirect:
            self.send_header('Location',redirect)
        self.end_headers()
        if response: 
            self.wfile.write(response.encode())
        self.response_sent=True #flag to indicate response sent early


    '''param handlers defined in subclass are called before routing.
    params={ 
        'key': param_handler 
    }
    and should be defined as
    def param_handler(self,key,value,**params):
        #can terminate request with
        self.respond(response,code=...)
    '''  
    params={}

    '''routes are defined in subclass as:
    routes={ 
        'HTTP_METHOD':{
            'regex': path_handler,
            None: default_path_handler (returns 404 if not defined)
        }
    }
    and should be defined as
    def path_handler(self,path,**params):
        #can respond with
        self.respond(response,code=...)
        #or
        return response #code will be 200
    '''        
    routes={}


class App():
    ''' http.server wrapper, creates HTTPS if kwargs for SSL are provided else HTTP '''
    def __init__(self,addr,handler,**ssl_args):
        if ssl_args:
            self.server=http.server.ThreadingHTTPSServer(addr,handler,**ssl_args)
        else:
            self.server=http.server.ThreadingHTTPServer(addr,handler)

    def run(self):
        logging.info('running on http://%s:%s',self.server.server_name, self.server.server_port)
        self.server.serve_forever()
        

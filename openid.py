# -*- coding: utf-8 -*-
# OpenID relying party library
# Copyright Martin v. Löwis, 2009
# Licensed under the Academic Free License, version 3

# This library implements OpenID Authentication 2.0,
# in the role of a relying party
# It has the following assumptions and limitations:
# - service discovery requires YADIS (HTML discovery not implemented)
# - only provider-directed mode (identifier_select) is supported
# - direct requests require https
# - as a signature algorithm, HMAC-SHA1 is requested

import urlparse, urllib, httplib, BeautifulSoup, xml.etree.ElementTree
import cStringIO, base64, hmac, sha, datetime

# Don't use urllib2, since it breaks in 2.5
# for https://login.launchpad.net//+xrds

# Don't use urllib, since it sometimes selects HTTP/1.1 (e.g. in PyPI)
# and then fails to parse chunked responses.

def parse_response(s):
    '''Parse a key-value form (OpenID section 4.1.1) into a dictionary'''
    res = {}
    for line in s.splitlines():
        k,v = line.split(':', 1)
        res[k] = v
    return res

def discover(url):
    '''Perform service discovery on the OP URL.
    Return list of service types, and the auth/2.0 URL,
    or None if discovery fails.'''
    scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
    assert not fragment
    if scheme == 'https':
        conn = httplib.HTTPSConnection(netloc)
    elif scheme == 'http':
        conn = httplib.HTTPConnection(netloc)
    else:
        raise ValueError, "Unsupported scheme "+scheme
    if query:
        path += '?'+query
    conn.putrequest("GET", path)
    conn.putheader('Accept', "text/html; q=0.3, "+
                   "application/xhtml+xml; q=0.5, "+
                   "application/xrds+xml")
    conn.endheaders()

    res = conn.getresponse()
    data = res.read()
    conn.close()

    content_type = res.msg.gettype()

    # Yadis 6.2.5 option 2 and 3: header includes x-xrds-location
    xrds_loc = res.msg.get('x-xrds-location')
    if xrds_loc and content_type != 'application/xrds+xml':
        return discover(xrds_loc)

    if content_type == 'text/html':
        soup = BeautifulSoup.BeautifulSoup(data)
        # Yadis 6.2.5 option 1: meta tag
        meta = soup.find('meta', {'http-equiv':lambda v:v.lower()=='x-xrds-location'})
        if meta:
            xrds_loc = meta['content']
            return discover(xrds_loc)
        # OpenID 7.3.3: attempt html based discovery
        claimed_id = url
        op_endpoint = soup.find('link', {'rel':lambda v:v.lower()=='openid2.provider'})
        if not op_endpoint:
            # discovery failed
            return None
        op_endpoint = op_endpoint['href']
        op_local = soup.find('link', {'rel':lambda v:v.lower()=='openid2.local_id'})
        if op_local:
            op_local = op_local['href']
        else:
            op_local = claimed_id
        return ['http://specs.openid.net/auth/2.0/signon'], op_endpoint, claimed_id, op_local
            
    if content_type == 'application/xrds+xml':
        # Yadis 6.2.5 option 4
        doc = xml.etree.ElementTree.fromstring(data)
        for svc in doc.findall(".//{xri://$xrd*($v*2.0)}Service"):
            services = [x.text for x in svc.findall("{xri://$xrd*($v*2.0)}Type")]
            if 'http://specs.openid.net/auth/2.0/server' in services:
                # 7.3.2.1.1 OP Identifier Element
                uri = svc.find("{xri://$xrd*($v*2.0)}URI")
                if uri is not None:
                    claimed_id = op_local = None
                    op_endpoint = uri.text
                    break
            elif 'http://specs.openid.net/auth/2.0/signon' in services:
                # 7.3.2.1.2.  Claimed Identifier Element
                claimed_id = url
                op_local = svc.find("{xri://$xrd*($v*2.0)}URI")
                if op_local is not None:
                    op_local = op_local.text
                else:
                    op_local = claimed_id
                uri = svc.find("{xri://$xrd*($v*2.0)}URI")
                if uri is not None:
                    op_endpoint = uri.text
                    break
        else:
            return None # No OpenID 2.0 service found
    return services, op_endpoint, claimed_id, op_local

def associate(url):
    '''Create an association (OpenID section 8) between RP and OP.
    Return response as a dictionary.'''
    if url.startswith('http:'):
        pieces = urlparse.urlparse(url)
        if pieces[1] == 'www.myopenid.com':
            pieces = ('https',) + pieces[1:]
            url = urlparse.urlunparse(pieces) 
    assert url.startswith('https') # we only support no-encryption sessions
    data = {
        'openid.ns':"http://specs.openid.net/auth/2.0",
        'openid.mode':"associate",
        'openid.assoc_type':"HMAC-SHA1",
        'openid.session_type':"no-encryption",
        }
    res = urllib.urlopen(url, urllib.urlencode(data))
    data = parse_response(res.read())
    return data

def request_authentication(services, url, assoc_handle, return_to,
                           claimed = None, op_local = None):
    '''Request authentication (OpenID section 9).
    services is the list of discovered service types,
    url the OP service URL, assoc_handle the established session
    dictionary, and return_to the return URL.

    The return_to URL will also be passed as realm, and the
    OP may perform RP discovery on it; always request these
    data through SREG 1.0 as well.

    If AX or SREG 1.1 are supported, request email address,
    first/last name, or nickname.

    Return the URL that the browser should be redirected to.'''

    if claimed is None:
        claimed = "http://specs.openid.net/auth/2.0/identifier_select"
    if op_local is None:
        op_local = "http://specs.openid.net/auth/2.0/identifier_select"
    data = {
        'openid.ns':"http://specs.openid.net/auth/2.0",
        'openid.mode':"checkid_setup",
        'openid.assoc_handle':assoc_handle,
        'openid.return_to':return_to,
        'openid.claimed_id':claimed,
        'openid.identity':op_local,
        'openid.realm':return_to,
        'openid.ns.sreg':"http://openid.net/sreg/1.0",
        'openid.sreg.required':'nickname,email',
        }
    if "http://openid.net/srv/ax/1.0" in services:
        data.update({
            'openid.ns.ax':"http://openid.net/srv/ax/1.0",
            'openid.ax.mode':'fetch_request',
            'openid.ax.required':'email,first,last',
            'openid.ax.type.email':'http://axschema.org/contact/email',
            'openid.ax.type.first':"http://axschema.org/namePerson/first",
            'openid.ax.type.last':"http://axschema.org/namePerson/last",
            })
    if "http://openid.net/extensions/sreg/1.1" in services:
        data.update({
            'openid.ns.sreg11':"http://openid.net/extensions/sreg/1.1",
            'openid.sreg11.required':'nickname,email'
            })
    if '?' in url:
        return url+'&'+urllib.urlencode(data)
    else:
        return url+"?"+urllib.urlencode(data)

class NotAuthenticated(Exception):
    pass

def authenticate(session, response):
    '''Process an authentication response.
    session must be the established session (minimally including
    assoc_handle and mac_key), response is the query string as parse
    by cgi.parse_qs.
    If authentication succeeds, return None.
    If the user was not authenticated, NotAuthenticated is raised.
    If the HTTP request is invalid (missing parameters, failure to
    validate signature), different exceptions will be raised, typically
    ValueError.

    Callers must check openid.response_nonce for replay attacks.
    '''

    if response['openid.ns'][0] != 'http://specs.openid.net/auth/2.0':
        raise ValueError('missing openid.ns')
    if session['assoc_handle'] != response['openid.assoc_handle'][0]:
        raise ValueError('incorrect session')
    if response['openid.mode'][0] == 'cancel':
        raise NotAuthenticated('provider did not authenticate user (cancelled)')
    if response['openid.mode'][0] != 'id_res':
        raise ValueError('invalid openid.mode')
    if  'openid.claimed_id' not in response:
        raise ValueError('missing openid.claimed_id')

    # Won't check nonce value - caller must verify this is not a replay

    signed = response['openid.signed'][0].split(',')
    if not (set(['op_endpoint', 'return_to', 'response_nonce',
                 'assoc_handle', 'claimed_id']) < set(signed)):
        raise ValueError("signature failed to sign important fields")
    query = []
    for name in signed:
        value = response['openid.'+name][0]
        query.append('%s:%s\n' % (name, value))
    query = ''.join(query)

    mac_key = base64.decodestring(session['mac_key'])
    transmitted_sig = base64.decodestring(response['openid.sig'][0])
    computed_sig = hmac.new(mac_key, query, sha).digest()

    if transmitted_sig != computed_sig:
        raise ValueError('Invalid signature')

def parse_nonce(nonce):
    '''Split a nonce into a (timestamp, ID) pair'''
    stamp = nonce.split('Z', 1)[0]
    stamp = datetime.datetime.strptime(stamp,"%Y-%m-%dT%H:%M:%S")
    return stamp

def get_namespaces(resp):
    res = {}
    for k, v in resp.items():
        if k.startswith('openid.ns.'):
            k = k.rsplit('.', 1)[1]
            res[v[0]] = k
    return res

def get_ax(resp, ns, validated):
    if "http://openid.net/srv/ax/1.0" not in ns:
        return {}
    ax = ns["http://openid.net/srv/ax/1.0"]+"."
    oax = "openid."+ax
    res = {}
    for k, v in resp.items():
        if k.startswith(oax+"type."):
            k = k.rsplit('.',1)[1]
            value_name = oax+"value."+k
            if ax+"value."+k not in validated:
                continue
            res[v[0]] = resp[value_name][0]
    return res
    

def get_email(resp):
    "Return the email address embedded response, or None."

    validated = resp['openid.signed'][0]

    # SREG 1.0; doesn't require namespace, as the protocol doesn't
    # specify one
    if 'openid.sreg.email' in resp and \
       'sreg.email' in validated:
        return resp['openid.sreg.email'][0]

    ns = get_namespaces(resp)

    ax = get_ax(resp, ns, validated)
    if "http://axschema.org/contact/email" in ax:
        return ax["http://axschema.org/contact/email"]

    # TODO: SREG 1.1
    return None

def get_username(resp):
    "Return either nickname or (first, last) or None."

    validated = resp['openid.signed'][0]
    if 'openid.sreg.nickname' in resp and \
       'sreg.nickname' in validated:
        return resp['openid.sreg.nickname'][0]

    ns = get_namespaces(resp)

    ax = get_ax(resp, ns, validated)
    if "http://axschema.org/namePerson/first" in ax and \
       "http://axschema.org/namePerson/last" in ax:
        return (ax["http://axschema.org/namePerson/first"],
                ax["http://axschema.org/namePerson/last"])

    # TODO: SREG 1.1
    return


################ Test Server #################################

import BaseHTTPServer, cgi

# supported providers
providers = (
    ('Google', 'http://www.google.com/favicon.ico', 'https://www.google.com/accounts/o8/id'),
    ('Yahoo', 'http://www.yahoo.com/favicon.ico', 'http://yahoo.com/'),
    # Verisigns service URL is not https
    #('Verisign', 'https://pip.verisignlabs.com/favicon.ico', 'https://pip.verisignlabs.com')
    ('myOpenID', 'https://www.myopenid.com/favicon.ico', 'https://www.myopenid.com/'),
    ('Launchpad', 'https://login.launchpad.net/favicon.ico', 'https://login.launchpad.net/')
    )
             
sessions = []
class Handler(BaseHTTPServer.BaseHTTPRequestHandler):

    def write(self, payload, type):
        self.send_response(200)
        self.send_header("Content-type", type)
        self.send_header("Content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == '/':
            return self.root()
        path = self.path
        i = path.rfind('?')
        if i >= 0:
            query = cgi.parse_qs(path[i+1:])
            path = path[:i]
        else:
            query = {}
        if path == '/':
            if 'provider' in query:
                prov = [p for p in providers if p[0]  == query['provider'][0]]
                if len(prov) != 1:
                    return self.not_found()
                prov = prov[0]
                services, url, claimed, op_local = discover(prov[2])
                session = associate(url)
                sessions.append(session)
                self.send_response(307) # temporary redirect - do not cache
                self.send_header("Location", request_authentication
                                 (services, url, session['assoc_handle'],
                                  self.base_url+"?returned=1"))
                self.end_headers()
                return
            if 'claimed' in query:
                res = discover(query['claimed'][0])
                if res is None:
                    return self.error('Discovery failed')
                services, url, claimed, op_local = res
                session = associate(url)
                sessions.append(session)
                self.send_response(307)
                self.send_header("Location", request_authentication
                                 (services, url, session['assoc_handle'],
                                  self.base_url+"?returned=1"))
                self.end_headers()
                return                
            if 'returned' in query:
                if 'openid.ns' not in query:
                    return self.rp_discovery()
                handle = query['openid.assoc_handle'][0]
                for session in sessions:
                    if session['assoc_handle'] == handle:
                        break
                else:
                    session = None
                if not session:
                    return self.error('Not authenticated (no session)')
                print query
                try:
                    authenticate(session, query)
                except Exception, e:
                    self.error("Authentication failed"+repr(e))
                if claimed in query:
                    claimed = query['openid.claimed_id'][0]
                else:
                    # OpenID 1, claimed ID not reported - should set cookie
                    claimed = query['openid.identity'][0]
                payload = "Hello "+claimed+"\n"
                email = get_email(query)
                if email:
                    payload += 'Your email is '+email+"\n"
                else:
                    payload += 'No email address is known\n'
                username = get_username(query)
                if isinstance(username, tuple):
                    username = " ".join(username)
                if username:
                    payload += 'Your nickname is '+username+'\n'
                else:
                    payload += 'No nickname is known\n'
                return self.write(payload, "text/plain")
                
        return self.not_found()

    

    def debug(self, value):
        payload = repr(value)
        self.write(payload, "text/plain")

    def error(self, text):
        self.write(text, "text/plain")

    def root(self):
        payload = "<html><head><title>OpenID login</title></head><body>\n"
        
        for name, icon, provider in providers:
            payload += "<p><a href='%s?provider=%s'><img src='%s' alt='%s'></a></p>\n" % (
                self.base_url, name, icon, name)
        payload += "<form>Type your OpenID:<input name='claimed'/><input type='submit'/></form>\n"
        payload += "</body></html>"
        self.write(payload, "text/html")

    def rp_discovery(self):
        payload = '''<xrds:XRDS  
                xmlns:xrds="xri://$xrds"  
                xmlns="xri://$xrd*($v*2.0)">  
                <XRD>  
                     <Service priority="1">  
                              <Type>http://specs.openid.net/auth/2.0/return_to</Type>  
                              <URI>%s</URI>  
                     </Service>  
                </XRD>  
                </xrds:XRDS>
        ''' % (self.base_url+"/?returned=1")
        self.write(payload, 'application/xrds+xml')

    def not_found(self):
        self.send_response(404)
        self.end_headers()
        
# OpenID providers often attempt relying-party discovery
# This requires the test server to use a globally valid URL
# If Python cannot correctly determine the base URL, you
# can pass it as command line argument
def test_server():
    import socket, sys
    if len(sys.argv) > 1:
        base_url = sys.argv[1]
    else:
        base_url = "http://" + socket.getfqdn() + ":8000/"
    Handler.base_url = base_url
    BaseHTTPServer.HTTPServer.address_family = socket.AF_INET6
    httpd = BaseHTTPServer.HTTPServer(('', 8000), Handler)
    httpd.serve_forever()

if __name__ == '__main__':
    test_server()

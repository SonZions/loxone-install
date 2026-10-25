"""Single-owner OAuth authorization code + S256 PKCE, static registered client.
Opaque access tokens; one-use authorization codes; rotating refresh tokens with family revocation on reuse; no password grant.
"""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import re
import secrets
import sqlite3
import time
from urllib.parse import urlencode
from starlette.responses import JSONResponse, HTMLResponse, RedirectResponse

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

class OAuth:
    def __init__(self, config, db):
        self.config = config
        self.issuer = config['public_url'].rstrip('/')
        self.resource = self.issuer + '/mcp'
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS grants (id TEXT PRIMARY KEY, kind TEXT, data TEXT, expires REAL)')
        self.db.commit()

    def put(self, kind, data, seconds):
        token = secrets.token_urlsafe(32)
        self.db.execute('DELETE FROM grants WHERE expires < ?', (time.time(),))
        self.db.execute('INSERT INTO grants VALUES(?,?,?,?)',(digest(token),kind,json.dumps(data),time.time()+seconds))
        self.db.commit()
        return token

    def get(self, token, kind, consume=False):
        row = self.db.execute('SELECT data FROM grants WHERE id=? AND kind=? AND expires>?', (digest(token),kind,time.time())).fetchone()
        if consume:
            self.db.execute('DELETE FROM grants WHERE id=?',(digest(token),)); self.db.commit()
        return json.loads(row[0]) if row else None

    async def resource_metadata(self, request):
        return JSONResponse({'resource':self.resource,'authorization_servers':[self.issuer],
                             'scopes_supported':['loxone:read','loxone:write'],'bearer_methods_supported':['header']})

    async def metadata(self, request):
        return JSONResponse({'issuer':self.issuer,'authorization_endpoint':self.issuer+'/authorize',
            'token_endpoint':self.issuer+'/token','response_types_supported':['code'],
            'authorization_response_iss_parameter_supported':True,
            'grant_types_supported':['authorization_code','refresh_token'],'code_challenge_methods_supported':['S256'],
            'token_endpoint_auth_methods_supported':['client_secret_post','client_secret_basic'],
            'scopes_supported':['loxone:read','loxone:write']})

    def valid_request(self, q):
        c=self.config
        scopes=q.get('scope','loxone:read').split()
        return (self.issuer.startswith('https://') and q.get('client_id')==c['client_id'] and
                q.get('redirect_uri') in c['redirect_uris'] and q.get('response_type')=='code' and
                q.get('code_challenge_method')=='S256' and bool(re.fullmatch(r'[A-Za-z0-9_-]{43}',q.get('code_challenge',''))) and
                q.get('resource')==self.resource and bool(scopes) and set(scopes)<= {'loxone:read','loxone:write'})

    async def authorize(self, request):
        if request.method=='GET':
            q=dict(request.query_params)
            if not self.valid_request(q):
                return JSONResponse({'error':'invalid_request'},status_code=400)
            csrf=self.put('login',q,300)
            scope=html.escape(q.get('scope','loxone:read'))
            return HTMLResponse(f'''<!doctype html><html lang="de"><meta charset="utf-8"><title>Loxone Zugriff</title>
<h1>ChatGPT mit Loxone verbinden</h1><p>Angeforderte Rechte: {scope}</p>
<p>Schreibzugriffe bleiben zusätzlich durch die Geräte-Allowlist beschränkt.</p>
<form method="post"><input type="hidden" name="csrf" value="{csrf}">
<label>Lokales MCP-Administrationspasswort <input type="password" name="password" required autocomplete="current-password"></label>
<button type="submit">Zugriff erlauben</button></form></html>''',headers={
                'Set-Cookie':f'csrf={csrf}; Secure; HttpOnly; SameSite=Lax; Max-Age=300; Path=/authorize',
                'Content-Security-Policy':"default-src 'none'; form-action 'self'; frame-ancestors 'none'",'Cache-Control':'no-store'})
        form=await request.form()
        csrf=str(form.get('csrf',''))
        if not csrf or not hmac.compare_digest(csrf,request.cookies.get('csrf','')):
            return JSONResponse({'error':'invalid_request'},status_code=400)
        q=self.get(csrf,'login',consume=True)
        password=str(form.get('password',''))
        hashed=hashlib.scrypt(password.encode(),salt=bytes.fromhex(self.config['password_salt']),n=16384,r=8,p=1).hex()
        if not q or not hmac.compare_digest(hashed,self.config['password_hash']):
            return JSONResponse({'error':'access_denied'},status_code=403)
        code=self.put('code',q,60)
        uri=q['redirect_uri']+ ('&' if '?' in q['redirect_uri'] else '?')+urlencode({'code':code,'state':q.get('state',''),'iss':self.issuer})
        return RedirectResponse(uri,status_code=303,headers={'Cache-Control':'no-store','Set-Cookie':'csrf=; Secure; HttpOnly; Max-Age=0; Path=/authorize'})

    async def token(self, request):
        f=dict(await request.form())
        client=f.get('client_id',''); secret=f.get('client_secret','')
        header=request.headers.get('authorization','')
        if header.startswith('Basic '):
            try:
                client,secret=base64.b64decode(header[6:],validate=True).decode().split(':',1)
            except Exception:
                return JSONResponse({'error':'invalid_client'},status_code=401)
        if client!=self.config['client_id'] or not hmac.compare_digest(digest(secret),self.config['client_secret_hash']):
            return JSONResponse({'error':'invalid_client'},status_code=401)
        if f.get('grant_type')=='refresh_token':
            raw=str(f.get('refresh_token',''))
            reused=self.get(raw,'refresh_used')
            if reused:
                self.db.execute("DELETE FROM grants WHERE json_extract(data, '$.family')=?",(reused['family'],))
                self.db.commit()
                return JSONResponse({'error':'invalid_grant'},status_code=400)
            data=self.get(raw,'refresh')
            if not data or data.get('client_id')!=client or f.get('resource')!=self.resource:
                return JSONResponse({'error':'invalid_grant'},status_code=400)
            self.db.execute('UPDATE grants SET kind=? WHERE id=?',('refresh_used',digest(raw)))
            self.db.commit()
            return self.issue(data)
        q=self.get(str(f.get('code','')),'code',consume=True)
        verifier=str(f.get('code_verifier',''))
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        if (not q or f.get('grant_type')!='authorization_code' or
            q['client_id']!=client or f.get('redirect_uri')!=q['redirect_uri'] or
            f.get('resource')!=self.resource or not re.fullmatch(r'[A-Za-z0-9._~-]{43,128}',verifier) or
            not hmac.compare_digest(challenge,q['code_challenge'])):
            return JSONResponse({'error':'invalid_grant'},status_code=400)
        scopes=q.get('scope','loxone:read').split()
        return self.issue({'sub':'owner','scopes':scopes,'resource':self.resource,
                           'client_id':client,'family':secrets.token_urlsafe(24),'until':time.time()+2592000})

    def issue(self,data):
        token=self.put('access',data,900)
        refresh=self.put('refresh',data,max(0,data['until']-time.time()))
        return JSONResponse({'access_token':token,'refresh_token':refresh,'token_type':'Bearer',
                             'expires_in':900,'scope':' '.join(data['scopes'])},
                            headers={'Cache-Control':'no-store','Pragma':'no-cache'})

class Guard:
    def __init__(self, app, oauth):
        self.app=app; self.oauth=oauth; self.buckets={}

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        path=scope['path']; headers=dict(scope['headers'])
        # A single owner server: a global rate limit also bounds memory usage.
        bucket='auth' if path in ('/authorize','/token') else 'api'
        stamp=time.monotonic(); window,count=self.buckets.get(bucket,(stamp,0))
        if stamp-window>=60: window,count=stamp,0
        count+=1;self.buckets[bucket]=(window,count)
        if count > (20 if bucket=='auth' else 120):
            return await JSONResponse({'error':'rate_limited'},429,headers={'Retry-After':'60'})(scope,receive,send)
        try: size=int(headers.get(b'content-length',b'0'))
        except ValueError: size=999999
        if size>16384:
            return await JSONResponse({'error':'request_too_large'},413)(scope,receive,send)
        # Bound chunked requests too, before invoking form/JSON parsing.
        chunks=[]; total=0
        while True:
            msg=await asyncio.wait_for(receive(),timeout=10)
            if msg['type']=='http.disconnect': return
            total+=len(msg.get('body',b''))
            if total>16384:
                return await JSONResponse({'error':'request_too_large'},413)(scope,receive,send)
            chunks.append(msg)
            if not msg.get('more_body'): break
        async def bounded_receive():
            if chunks:return chunks.pop(0)
            return await receive()
        if path.startswith('/mcp'):
            token=headers.get(b'authorization',b'').decode()
            identity=self.oauth.get(token[7:],'access') if token.startswith('Bearer ') else None
            if not identity or identity.get('resource')!=self.oauth.resource:
                return await JSONResponse({'error':'unauthorized'},401,headers={
                    'WWW-Authenticate':f'Bearer resource_metadata="{self.oauth.issuer}/.well-known/oauth-protected-resource"'})(scope,bounded_receive,send)
            origin=headers.get(b'origin')
            if origin and origin.decode()!=self.oauth.issuer:
                return await JSONResponse({'error':'invalid_origin'},403)(scope,bounded_receive,send)
            scope.setdefault('state',{})['identity']=identity
        return await self.app(scope,bounded_receive,send)

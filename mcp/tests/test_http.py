import base64
import hashlib
import json
import sys
from pathlib import Path
import pytest
from starlette.testclient import TestClient
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import create_app
from auth import digest
from test_service import MockMiniserver, U

@pytest.fixture
def app(tmp_path):
    salt=b'0123456789abcdef'
    policy=tmp_path/'policy.json';policy.write_text('{"writes_enabled":false,"allowlist":{}}')
    return create_app({'audit_db':str(tmp_path/'audit.sqlite'),'auth_db':str(tmp_path/'auth.sqlite'),
        'policy_path':str(policy),'oauth':{'public_url':'https://mcp.test','client_id':'chatgpt',
        'client_secret_hash':digest('secret'),'password_salt':salt.hex(),
        'password_hash':hashlib.scrypt(b'password',salt=salt,n=16384,r=8,p=1).hex(),
        'redirect_uris':['https://chatgpt.com/connector/oauth/callback']}},MockMiniserver())

def rpc(client,name,args=None,token=''):
    return client.post('/mcp',headers={'Authorization':'Bearer '+token,'Accept':'application/json, text/event-stream'},
                       json={'jsonrpc':'2.0','id':1,'method':name,'params':args or {}})

def test_health_auth_metadata(app):
    with TestClient(app,base_url='https://mcp.test') as c:
        assert c.get('/healthz').status_code==200
        r=rpc(c,'tools/list');assert r.status_code==401
        assert 'resource_metadata' in r.headers['www-authenticate']
        assert c.get('/.well-known/oauth-protected-resource').json()['resource']=='https://mcp.test/mcp'
        assert c.get('/.well-known/oauth-authorization-server').json()['code_challenge_methods_supported']==['S256']
        token=app.oauth.put('access',{'sub':'owner','scopes':['loxone:read'],'resource':'wrong'},60)
        assert rpc(c,'tools/list',token=token).status_code==401

def test_discovery_and_live_mock_reads(app):
    with TestClient(app,base_url='https://mcp.test') as c:
        token=app.oauth.put('access',{'sub':'owner','scopes':['loxone:read'],'resource':app.oauth.resource},60)
        r=rpc(c,'tools/list',token=token)
        assert r.status_code==200
        tools=r.json()['result']['tools'];assert len(tools)==12
        assert all(t['annotations']['readOnlyHint']==(t['name'] not in ['loxone_set_parameter','loxone_execute_command','loxone_execute_group']) for t in tools)
        for name,args in [('loxone_get_system_info',{}),('loxone_get_device',{'uuid':U}),('loxone_get_parameters',{'uuid':U}),('loxone_verify_parameter',{'uuid':U,'parameter':'presenceMin','expected_value':11})]:
            r=rpc(c,'tools/call',{'name':name,'arguments':args},token)
            assert not r.json()['result'].get('isError'),r.text
            assert 'SECRET' not in r.text
        r=rpc(c,'tools/call',{'name':'loxone_set_parameter','arguments':{'uuid':U,'parameter':'presenceMin','value':15,'expected_value':11,'request_id':'test-request-1234'}},token)
        assert r.json()['result']['isError']
        assert not app.service.backend.calls

def test_oauth_flow_and_replay(app):
    with TestClient(app,base_url='https://mcp.test') as c:
        verifier='a'*43
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        params={'client_id':'chatgpt','redirect_uri':'https://chatgpt.com/connector/oauth/callback',
            'response_type':'code','resource':app.oauth.resource,'code_challenge_method':'S256','code_challenge':challenge,'scope':'loxone:read','state':'abc'}
        assert c.get('/authorize',params={**params,'redirect_uri':'https://evil.test'}).status_code==400
        assert c.get('/authorize',params=params).status_code==200
        csrf=c.cookies.get('csrf')
        r=c.post('/authorize',data={'csrf':csrf,'password':'password'},follow_redirects=False)
        assert r.status_code==303
        from urllib.parse import parse_qs,urlsplit
        q=parse_qs(urlsplit(r.headers['location']).query);assert q['state']==['abc']
        data={'client_id':'chatgpt','client_secret':'secret','grant_type':'authorization_code','code':q['code'][0],
              'redirect_uri':params['redirect_uri'],'resource':app.oauth.resource,'code_verifier':verifier}
        r=c.post('/token',data=data);assert r.status_code==200,r.text
        assert rpc(c,'tools/list',token=r.json()['access_token']).status_code==200
        assert c.post('/token',data=data).status_code==400
        refresh={'client_id':'chatgpt','client_secret':'secret','grant_type':'refresh_token',
                 'refresh_token':r.json()['refresh_token'],'resource':app.oauth.resource}
        rotated=c.post('/token',data=refresh);assert rotated.status_code==200
        assert c.post('/token',data=refresh).status_code==400
        assert rpc(c,'tools/list',token=rotated.json()['access_token']).status_code==401


def test_bad_pkce_and_csrf(app):
    with TestClient(app,base_url='https://mcp.test') as c:
        assert c.post('/authorize',data={'csrf':'invalid','password':'password'}).status_code==400
        q={'client_id':'chatgpt','redirect_uri':'https://chatgpt.com/connector/oauth/callback','code_challenge':'a'*43}
        code=app.oauth.put('code',q,60)
        r=c.post('/token',data={'client_id':'chatgpt','client_secret':'secret','code':code,'code_verifier':'b'*43,'grant_type':'authorization_code','redirect_uri':q['redirect_uri'],'resource':app.oauth.resource})
        assert r.status_code==400

def test_limits_and_origin(app):
    with TestClient(app,base_url='https://mcp.test') as c:
        assert c.post('/token',content='x'*17000).status_code==413
        token=app.oauth.put('access',{'sub':'owner','scopes':['loxone:read'],'resource':app.oauth.resource},60)
        assert c.post('/mcp',headers={'Origin':'https://evil.test','Authorization':'Bearer '+token},json={}).status_code==403
        for _ in range(125): r=c.get('/healthz')
        assert r.status_code==429

def test_private_tunnel_credential(app):
    import time
    app.oauth.config['tunnel']={'enabled':True,'token_hash':digest('private-token'),'expires_at':time.time()+60}
    with TestClient(app,base_url='https://mcp.test',client=('127.0.0.1',10000)) as c:
        assert rpc(c,'tools/list',token='private-token').status_code==200
        r=rpc(c,'tools/call',{'name':'loxone_set_parameter','arguments':{'uuid':U,'parameter':'presenceMin','value':15,'expected_value':11,'request_id':'tunnel-denied-0001'}},'private-token')
        assert r.json()['result']['isError']
        assert not app.service.backend.calls
        app.oauth.config['tunnel']['expires_at']=0
        assert rpc(c,'tools/list',token='private-token').status_code==401

def test_tunnel_rejects_nonlocal_peer(app):
    import time
    app.oauth.config['tunnel']={'enabled':True,'token_hash':digest('private-token'),'expires_at':time.time()+60}
    with TestClient(app,base_url='https://mcp.test',client=('192.0.2.1',10000)) as c:
        assert rpc(c,'tools/list',token='private-token').status_code==401

def test_tunnel_disabled_by_default(app):
    with TestClient(app,base_url='https://mcp.test',client=('127.0.0.1',10000)) as c:
        assert rpc(c,'tools/list',token='private-token').status_code==401

def test_tunnel_write_scope_does_not_bypass_policy(app):
    import time
    app.oauth.config['tunnel']={'enabled':True,'allow_writes':True,'token_hash':digest('private-token'),'expires_at':time.time()+60}
    with TestClient(app,base_url='https://mcp.test',client=('127.0.0.1',10000)) as c:
        r=rpc(c,'tools/call',{'name':'loxone_set_parameter','arguments':{'uuid':U,'parameter':'presenceMin','value':15,'expected_value':11,'request_id':'tunnel-policy-denied-01'}},'private-token')
        assert 'write_not_allowlisted' in r.text
        assert not app.service.backend.calls

def test_backend_shutdown_only_at_application_exit(app):
    calls=[]
    async def close():calls.append('closed')
    app.service.backend.close=close
    with TestClient(app,base_url='https://mcp.test') as c:
        token=app.oauth.put('access',{'sub':'owner','scopes':['loxone:read'],'resource':app.oauth.resource},60)
        for _ in range(3):
            assert rpc(c,'tools/list',token=token).status_code==200
            assert not calls
    assert calls==['closed']

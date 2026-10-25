import asyncio
import json
import pytest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Service, Fault
U='11111111-2222-3333-4444555566667777'; S='11111111-2222-3333-4444555566668888'
class MockMiniserver:
    def __init__(self): self.value=11.; self.calls=[]; self.fail=False; self.ignore=False
    async def structure(self):
        return {'msInfo':{'serialNr':'TEST'},'rooms':{'room':{'name':'Test room'}},
            'controls':{U:{'uuidAction':U,'name':'Mock Leaf','type':'Ventilation','room':'room','states':{'presenceMin':S},'details':{}}}}
    async def snapshot(self, ids):
        if self.fail: raise TimeoutError('SECRET must never escape')
        return {S:{'value':self.value,'timestamp':'mock'}}
    async def write(self,u,cmd,v):
        self.calls.append((u,cmd,v))
        if not self.ignore:self.value=v

@pytest.fixture
def svc(tmp_path):
    p=tmp_path/'policy.json';p.write_text(json.dumps({'writes_enabled':True,'allowlist':{U:['presenceMin']}}))
    return Service(MockMiniserver(),str(tmp_path/'audit.sqlite'),p)

@pytest.mark.asyncio
async def test_read_and_parameters(svc):
    assert (await svc.parameters(U))['parameters']['presenceMin']['value']==11
    assert (await svc.states(U,'presenceMin'))['room']=='Test room'

@pytest.mark.asyncio
async def test_write_verify_audit_replay(svc):
    rid='test-request-00001'
    r=await svc.write(U,'presenceMin',15,11,rid,'owner')
    assert r['result']=='verified'
    assert svc.backend.calls==[(U,'setPresenceMin',15)]
    assert svc.history()[0]['old_value']=='11.0'
    with pytest.raises(Fault,match='already_used'): await svc.write(U,'presenceMin',15,11,rid,'owner')
    assert len(svc.backend.calls)==1

@pytest.mark.asyncio
async def test_allowlist(svc):
    svc.policy_path.write_text('{"writes_enabled":false}')
    with pytest.raises(Fault,match='allowlisted'): await svc.write(U,'presenceMin',15,11,'test-request-00002','owner')
    assert not svc.backend.calls
    assert svc.history()[0]['result']=='write_not_allowlisted'

@pytest.mark.asyncio
async def test_precondition(svc):
    with pytest.raises(Fault,match='precondition'): await svc.write(U,'presenceMin',15,12,'test-request-00003','owner')
    assert not svc.backend.calls

@pytest.mark.asyncio
async def test_timeout_redaction(svc):
    svc.backend.fail=True
    with pytest.raises(Fault,match='read_failed'): await svc.write(U,'presenceMin',15,11,'test-request-00004','owner')
    assert 'SECRET' not in str(svc.history())
    assert not svc.backend.calls

@pytest.mark.asyncio
async def test_verification_failure(svc):
    svc.backend.ignore=True
    r=await svc.write(U,'presenceMin',15,11,'test-request-00005','owner')
    assert r['result']=='verification_failed'
    assert len(svc.backend.calls)==1

@pytest.mark.asyncio
@pytest.mark.parametrize('value',[-1,101,float('nan'),float('inf')])
async def test_range(svc,value):
    with pytest.raises(Fault,match='range'): await svc.write(U,'presenceMin',value,11,'test-request-00006','owner')
    assert not svc.backend.calls

@pytest.mark.asyncio
async def test_path_injection(svc):
    with pytest.raises(Fault): await svc.states('../../foo','presenceMin')
    with pytest.raises(Fault): await svc.write(U,'presenceMin/On',15,11,'test-request-00007','owner')
    assert not svc.backend.calls

@pytest.mark.asyncio
async def test_one_concurrent_write(svc):
    results=await asyncio.gather(*[svc.write(U,'presenceMin',15,11,'test-request-00008','owner') for _ in range(2)],return_exceptions=True)
    assert sum(isinstance(r,Fault) for r in results)==1
    assert len(svc.backend.calls)==1

@pytest.mark.asyncio
async def test_pilot_grant(svc):
    p=svc.policy();p['pilot_grant']={'uuid':U,'parameter':'presenceMin','value':15,'expected_value':11,'request_id':'approved-pilot-001'}
    svc.policy_path.write_text(json.dumps(p))
    with pytest.raises(Fault,match='pilot_grant'): await svc.write(U,'presenceMin',16,11,'wrong-pilot-00001','owner')
    assert not svc.backend.calls
    assert (await svc.write(U,'presenceMin',15,11,'approved-pilot-001','owner'))['result']=='verified'

@pytest.mark.asyncio
async def test_durable_history(svc):
    await svc.write(U,'presenceMin',15,11,'durable-request-001','owner')
    import sqlite3
    path=svc.db.execute('PRAGMA database_list').fetchone()[2]
    assert sqlite3.connect(path).execute('SELECT result FROM audit').fetchone()[0]=='verified'

@pytest.mark.asyncio
async def test_logic_connected(svc):
    original=svc.backend.structure
    async def structure():
        d=await original();d['controls'][U]['details']['hasPresenceMin']=True;return d
    svc.backend.structure=structure
    with pytest.raises(Fault,match='logic'):await svc.write(U,'presenceMin',15,11,'logic-request-001','owner')
    assert not svc.backend.calls

@pytest.mark.asyncio
async def test_miniserver_http_contract(monkeypatch):
    import httpx
    from core import Miniserver
    cls=httpx.AsyncClient
    requests=[]
    def handler(req):
        requests.append(req)
        if req.url.path=='/data/LoxAPP3.json':return httpx.Response(200,json={'msInfo':{'serialNr':'TEST'},'controls':{}})
        return httpx.Response(200,json={'LL':{'Code':'200'}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    b=Miniserver({'url':'http://mock.test','username':'service','password':'SECRET','serial':'TEST'})
    assert (await b.structure())['msInfo']['serialNr']=='TEST'
    await b.write(U,'setPresenceMin',15)
    assert requests[1].url.path==f'/jdev/sps/io/{U}/setPresenceMin/15'
    assert requests[0].headers['authorization'].startswith('Basic ')
    b.config['serial']='WRONG'
    with pytest.raises(Fault,match='identity'):await b.structure()

@pytest.mark.asyncio
async def test_http_timeout(monkeypatch):
    import httpx
    from core import Miniserver
    cls=httpx.AsyncClient
    def handler(req):raise httpx.ReadTimeout('sensitive upstream message')
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:cls(transport=httpx.MockTransport(handler),**kw))
    b=Miniserver({'url':'http://mock.test','username':'service','password':'SECRET','serial':'TEST'})
    with pytest.raises(httpx.ReadTimeout):await b.structure()

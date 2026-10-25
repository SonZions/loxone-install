import json
import pytest
from core import Service, Fault, Miniserver
from controls import controls, mappings, wire_command, parameter_value
from test_service import U, S

class Backend:
    def __init__(self, typ, state, value):
        self.child = U+'/AI1'; self.state=state; self.value=value; self.calls=[]
        self.device={'uuidAction':self.child,'type':typ,'name':'light','states':{state:S}}
        self.parent={'uuidAction':U,'type':'LightControllerV2','name':'room lights','room':'r','subControls':{self.child:self.device}}
        self.structure_data={'controls':{U:self.parent},'rooms':{'r':{'name':'room'}}}
    async def structure(self):return self.structure_data
    async def snapshot(self,ids):return {x:{'value':self.value,'timestamp':'mock'} for x in ids}
    async def write(self,u,cmd,value):
        self.calls.append((u,cmd,value))
        if self.state=='active':self.value=int(cmd=='on')
        elif self.state=='position':self.value=float(cmd)
        elif self.state=='color':self.value=cmd
        else:self.value=value

def service(tmp_path, b, uuid, parameter):
    p=tmp_path/'policy.json';p.write_text(json.dumps({'writes_enabled':True,'allowlist':{uuid:[parameter]}}))
    return Service(b,str(tmp_path/'audit.sqlite'),p)

@pytest.mark.asyncio
@pytest.mark.parametrize('typ,state,initial,param,target,expected,command',[
    ('Switch','active',0,'active',1,0,'on'),
    ('Dimmer','position',25,'position',40,25,'40'),
    ('ColorPickerV2','color','hsv(120,80,25)','brightness',40,25,'hsv(120,80,40)'),
    ('ColorPickerV2','color','temp(25,3000)','brightness',40,25,'temp(40,3000)')])
async def test_lights(tmp_path,typ,state,initial,param,target,expected,command):
    b=Backend(typ,state,initial);s=service(tmp_path,b,b.child,param)
    assert (await s.parameters(b.child))['parameters'][param]['value']==expected
    r=await s.write(b.child,param,target,expected,'light-test-request-01','owner')
    assert r['result']=='verified';assert b.calls==[(b.child,command,None)]
    assert json.loads(s.history()[0]['old_value'])==expected

@pytest.mark.asyncio
async def test_parent_lock_restriction(tmp_path):
    b=Backend('Switch','active',0);b.parent['isSecured']=True;s=service(tmp_path,b,b.child,'active')
    with pytest.raises(Fault,match='restricted'):await s.write(b.child,'active',1,0,'restricted-test-001','owner')
    assert not b.calls

@pytest.mark.asyncio
async def test_unlisted_child_and_injection(tmp_path):
    b=Backend('Switch','active',0);s=service(tmp_path,b,b.child,'active')
    for path in (U+'/AI2',U+'/AI1/off',U+'/../x'):
        with pytest.raises(Fault):await s.device(path)
    assert not b.calls

@pytest.mark.asyncio
async def test_nonlight_switch_unsupported(tmp_path):
    b=Backend('Switch','active',0);b.device['uuidAction']=U;b.structure_data['controls']={U:b.device}
    s=service(tmp_path,b,U,'active')
    with pytest.raises(Fault,match='unsupported'):await s.write(U,'active',1,0,'nonlight-test-001','owner')
    assert not b.calls

@pytest.mark.asyncio
@pytest.mark.parametrize('param,command',[('comfortTemperature','setComfortTemperature'),('comfortTemperatureCool','setComfortTemperatureCool')])
async def test_room_temperature(tmp_path,param,command):
    b=Backend('IRoomControllerV2',param,21);b.device['uuidAction']=U;b.structure_data['controls']={U:b.device}
    s=service(tmp_path,b,U,param)
    r=await s.write(U,param,22,21,'room-test-request-01','owner')
    assert r['result']=='verified';assert b.calls==[(U,command,22)]

@pytest.mark.asyncio
async def test_room_logic_and_range(tmp_path):
    b=Backend('IRoomControllerV2','comfortTemperature',21);b.device['uuidAction']=U;b.structure_data['controls']={U:b.device}
    s=service(tmp_path,b,U,'comfortTemperature')
    with pytest.raises(Fault,match='range'):await s.write(U,'comfortTemperature',35,21,'room-range-test-001','owner')
    b.device['details']={'connectedInputs':2}
    with pytest.raises(Fault,match='logic'):await s.write(U,'comfortTemperature',22,21,'room-logic-test-001','owner')
    assert not b.calls

@pytest.mark.asyncio
async def test_wire_paths(monkeypatch):
    import httpx
    original=httpx.AsyncClient;paths=[]
    def handler(req):paths.append(req.url.path);return httpx.Response(200,json={'LL':{'Code':'200'}})
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handler),**kw))
    b=Miniserver({'url':'http://mock','username':'u','password':'s'})
    for cmd in ['on','40','hsv(120,80,40)']:await b.write(U+'/AI1',cmd,None)
    assert paths==[f'/jdev/sps/io/{U}/AI1/{cmd}' for cmd in ['on','40','hsv(120,80,40)']]


def test_allowlist_exact_scope():
    from allowlist import build
    b=Backend('Switch','active',0)
    b.structure_data['controls']['unrelated']={'type':'Switch','states':{'active':S}}
    policy,counts,excluded=build(b.structure_data)
    assert policy['allowlist']=={b.child:['active']}
    assert counts=={'Switch':1}
    b.parent['restrictions']=1
    assert build(b.structure_data)[0]['allowlist']=={}

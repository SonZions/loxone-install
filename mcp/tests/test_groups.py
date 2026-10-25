import json
import pytest
from core import Service,Fault
from groups import read_group,execute_group
from allowlist import build
from test_service import U,S
G='22222222-2222-3333-4444555566667777'
class Backend:
 def __init__(self):
  self.calls=[];self.v={'p':0.4,'t':0.4,'up':0,'down':0,'safe':0,'lock':0,'auto':1}
  self.s={'controls':{U:{'uuidAction':U,'name':'blind','type':'Jalousie','states':{'position':'p','targetPosition':'t','up':'up','down':'down','safetyActive':'safe','locked':'lock','autoAllowed':'auto'}},G:{'uuidAction':G,'name':'group','type':'CentralJalousie','details':{'controls':[{'id':3,'uuid':U}]},'states':{}}}}
 async def structure(self):return self.s
 async def snapshot(self,ids):return {x:{'value':self.v[x],'timestamp':'mock'} for x in ids}
 async def write(self,u,c,v):
  self.calls.append((u,c,v))
  if c.endswith('FullDown'):self.v['t']=1;self.v['down']=1
  elif c.endswith('stop'):self.v['down']=0;self.v['up']=0
  elif c=='manualPosition':self.v['t']=v/100

def service(tmp_path,b):
 p=tmp_path/'policy.json';p.write_text(json.dumps(build(b.s)[0]));return Service(b,str(tmp_path/'audit.sqlite'),p)

@pytest.mark.asyncio
async def test_group_target_ack_and_replay(tmp_path):
 b=Backend();s=service(tmp_path,b);r=await read_group(s,G)
 result=await execute_group(s,G,'FullDown',r['state_token'],'group-request-0001','owner')
 assert result['result']=='verified';assert result['verification_scope']=='target_acknowledged_not_physical_completion'
 assert b.v['p']==0.4
 assert b.calls==[(G,'selectedcontrols/3/FullDown',None)]
 with pytest.raises(Fault,match='already_used'):await execute_group(s,G,'FullDown',r['state_token'],'group-request-0001','owner')

@pytest.mark.asyncio
async def test_group_membership_change(tmp_path):
 b=Backend();s=service(tmp_path,b);r=await read_group(s,G)
 b.s['controls'][G]['details']['controls'][0]['id']=4
 with pytest.raises(Fault,match='membership_changed'):await execute_group(s,G,'FullDown',r['state_token'],'group-members-0001','owner')
 assert not b.calls

@pytest.mark.asyncio
@pytest.mark.parametrize('key',['safe','lock'])
async def test_blind_safety(tmp_path,key):
 b=Backend();s=service(tmp_path,b);b.v[key]=1;r=await read_group(s,G)
 with pytest.raises(Fault,match='safety'):await execute_group(s,G,'FullDown',r['state_token'],'group-safety-0001','owner')
 with pytest.raises(Fault,match='safety'):await s.write(U,'targetPosition',60,40,'single-safety-001','owner')
 assert not b.calls

@pytest.mark.asyncio
async def test_precondition_and_command_injection(tmp_path):
 b=Backend();s=service(tmp_path,b);r=await read_group(s,G);b.v['t']=0.5
 with pytest.raises(Fault,match='precondition'):await execute_group(s,G,'FullDown',r['state_token'],'group-stale-00001','owner')
 with pytest.raises(Fault,match='allowlisted'):await execute_group(s,G,'FullDown/../on',r['state_token'],'group-path-00001','owner')
 assert not b.calls

@pytest.mark.asyncio
async def test_single_target_percent_and_stop(tmp_path):
 b=Backend();s=service(tmp_path,b)
 assert (await s.parameters(U))['parameters']['targetPosition']['value']==40
 r=await s.write(U,'targetPosition',60,40,'single-move-00001','owner')
 assert r['result']=='verified';assert b.calls==[(U,'manualPosition',60)]
 r=await read_group(s,U);b.v['down']=1
 assert (await execute_group(s,U,'stop',r['state_token'],'single-stop-00001','owner'))['result']=='verified'

@pytest.mark.asyncio
async def test_light_group_native_off_and_on(tmp_path):
 b=Backend();child=U+'/AI1'
 b.s['controls'][U]={'uuidAction':U,'type':'LightControllerV2','subControls':{child:{'uuidAction':child,'type':'Switch','states':{'active':'light'}}}}
 b.s['controls'][G]['type']='CentralLightController';b.v['light']=1
 async def write(u,c,v):b.calls.append((u,c,v));b.v['light']=0 if c.endswith('reset') else 1
 b.write=write;s=service(tmp_path,b)
 for i,command in enumerate(['reset','on']):
  r=await read_group(s,G);result=await execute_group(s,G,command,r['state_token'],f'light-group-0000{i}','owner');assert result['result']=='verified'
 assert b.calls==[(G,'selectedcontrols/3/reset',None),(G,'selectedcontrols/3/on',None)]
 assert s.history()[0]['result']=='verified'

@pytest.mark.asyncio
async def test_group_verification_failure_and_unknown(tmp_path):
 b=Backend();s=service(tmp_path,b)
 async def ignore(u,c,v):b.calls.append((u,c,v))
 b.write=ignore;r=await read_group(s,G)
 assert (await execute_group(s,G,'FullDown',r['state_token'],'group-ignore-0001','owner'))['result']=='verification_failed'
 async def fail(u,c,v):raise TimeoutError('secret')
 b.write=fail;r=await read_group(s,G)
 result=await execute_group(s,G,'FullDown',r['state_token'],'group-timeout-001','owner')
 assert result['result']=='outcome_unknown';assert 'secret' not in str(result)

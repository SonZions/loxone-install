"""Native central commands with pinned membership, preconditions and per-member verification."""
import hashlib
import json
import re
from core import Fault, now
from controls import controls, parameter_value

COMMANDS={'CentralJalousie':('FullUp','FullDown','stop','auto','NoAuto'),
          'CentralLightController':('on','reset'),'Jalousie':('stop',)}

def members(uuid, device):
    if device.get('type')=='Jalousie':return [{'id':0,'uuid':uuid}]
    result=device.get('details',{}).get('controls',[])
    if not result or any(type(x.get('id')) is not int or x['id']<0 or not isinstance(x.get('uuid'),str) for x in result):
        raise Fault('invalid_group_members')
    if len({x['id'] for x in result})!=len(result) or len({x['uuid'] for x in result})!=len(result):
        raise Fault('invalid_group_members')
    return sorted([{'id':x['id'],'uuid':x['uuid']} for x in result],key=lambda x:x['id'])

def circuit_members(structure, group_members):
    parents={m['uuid'] for m in group_members}
    return sorted(u for u,d in controls(structure).items() if d.get('_parent') in parents and '/master' not in u)

async def read_group(service, uuid):
    structure,device=await service.device(uuid)
    typ=device.get('type')
    if typ not in COMMANDS:raise Fault('unsupported_group')
    group_members=members(uuid,device);all_controls=controls(structure);states={};details=[];restricted=[]
    def collect(u,d,names):
        if d.get('isSecured') or d.get('restrictions',0):restricted.append(u)
        for name in names:
            if name in d.get('states',{}):states[(u,name)]=d['states'][name]
    collect(uuid,device,('jLocked','safetyActive'))
    for member in group_members:
        u=member['uuid'];d=all_controls.get(u)
        required='LightControllerV2' if typ=='CentralLightController' else 'Jalousie'
        if not d or d.get('type')!=required:raise Fault('unexpected_group_member_type')
        details.append({'uuid':u,'id':member['id'],'name':d.get('name')})
        collect(u,d,('jLocked','safetyActive','locked','adjustingEndPos','deviceState','targetPosition','autoAllowed','up','down','position'))
        if required=='LightControllerV2':
            children=[(cu,cd) for cu,cd in all_controls.items() if cd.get('_parent')==u and '/master' not in cu]
            if not children:raise Fault('group_member_has_no_light_circuits')
            for cu,cd in children:
                collect(cu,cd,('active','position','color','lockedOn'))
                details[-1].setdefault('circuits',[]).append(cu)
    values=await service.backend.snapshot(states.values())
    current={}
    for (u,name),state in states.items():
        value=values[state]['value']
        if name=='color':value=parameter_value('brightness',value)
        current.setdefault(u,{})[name]=value
    # Moving positions are informational; target/mode and light values form the precondition.
    stable={u:{k:v for k,v in d.items() if k not in ('up','down','position') or typ=='CentralLightController'} for u,d in current.items()}
    token=hashlib.sha256(json.dumps({'members':group_members,'state':stable},sort_keys=True).encode()).hexdigest()
    policy=service.policy();grant=policy.get('groups',{}).get(uuid,{})
    return {**service.describe(structure,uuid,device),'members':details,'membership':group_members,
            'states':current,'restricted_members':restricted,'state_token':token,
            'allowed_commands':grant.get('commands',[]) if policy.get('writes_enabled') and grant.get('members')==group_members and (typ!='CentralLightController' or grant.get('circuits')==circuit_members(structure,group_members)) else [],
            'type':typ}

async def execute_group(service, uuid, command, state_token, request_id, actor):
    if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}',request_id):raise Fault('invalid_request_id')
    async with service.lock:
        if service.db.execute('SELECT 1 FROM audit WHERE request_id=?',(request_id,)).fetchone():raise Fault('request_already_used')
        before=None
        try:
            before=await read_group(service,uuid)
            if command not in COMMANDS[before['type']] or command not in before['allowed_commands']:raise Fault('group_not_allowlisted_or_membership_changed')
            if before['state_token']!=state_token:raise Fault('precondition_failed')
            if before['restricted_members']:raise Fault('restricted_device')
            for state in before['states'].values():
                if any(state.get(k) for k in ('jLocked','safetyActive','locked','adjustingEndPos')) or state.get('deviceState')==1:
                    raise Fault('group_safety_or_lock_active')
                if command=='reset' and state.get('lockedOn'):raise Fault('control_locked')
        except Exception as exc:
            reason=str(exc) if isinstance(exc,Fault) else 'read_failed'
            service.record(actor,request_id,uuid,'group:'+command,before['states'] if before else None,command,reason)
            raise Fault(reason) from None
        service.record(actor,request_id,uuid,'group:'+command,before['states'],command,'pending')
        wire=command if before['type']=='Jalousie' else 'selectedcontrols/'+','.join(str(m['id']) for m in before['membership'])+'/'+command
        after=None;verification=[]
        try:
            await service.backend.write(uuid,wire,None)
            after=await read_group(service,uuid)
            for member in after['members']:
                state=after['states'].get(member['uuid'],{})
                if command in ('FullUp','FullDown'):
                    ok=state.get('targetPosition')==(0 if command=='FullUp' else 1)
                elif command=='stop':ok=state.get('up')==0 and state.get('down')==0
                elif command in ('auto','NoAuto'):ok=state.get('autoAllowed')==(1 if command=='auto' else 0)
                else:
                    light_values=[v for cu in member.get('circuits',[]) for k,v in after['states'].get(cu,{}).items() if k in ('active','position','color')]
                    ok=bool(light_values) and (any(v>0 for v in light_values) if command=='on' else all(v==0 for v in light_values))
                verification.append({'uuid':member['uuid'],'verified':ok})
            result='verified' if all(x['verified'] for x in verification) else 'verification_failed'
        except Exception:result='outcome_unknown'
        service.db.execute('UPDATE audit SET result=?,verified_value=? WHERE request_id=?',(result,json.dumps(after['states'] if after else None),request_id));service.db.commit()
        return {'uuid':uuid,'command':command,'result':result,'error':None if result=='verified' else result,
                'timestamp':now(),'verification':verification,'states':after['states'] if after else None,
                'verification_scope':'target_acknowledged_not_physical_completion' if command in ('FullUp','FullDown') else 'member_states',
                'request_id':request_id}

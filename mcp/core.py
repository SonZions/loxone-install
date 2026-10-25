"""Explicit device/parameter mappings; no caller-supplied API paths."""
import asyncio
import json
import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import httpx
from controls import controls, mappings, logic_connected, parameter_value, wire_command
from loxwebsocket import LoxWs

logging.getLogger('loxwebsocket').disabled = True
for name in ('loxwebsocket.lox_ws_api', 'loxwebsocket.encryption', 'loxwebsocket.lxtoken'):
    logging.getLogger(name).disabled = True
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{16}$')
PARAMETERS = {'presenceMin': ('setPresenceMin', 'hasPresenceMin'),
              'presenceMax': ('setPresenceMax', 'hasPresenceMax'),
              'absenceMin': ('setAbsenceMin', 'hasAbsenceMin'),
              'absenceMax': ('setAbsenceMax', 'hasAbsenceMax')}

def now():
    return datetime.now(timezone.utc).isoformat()

class Fault(Exception):
    pass

class Miniserver:
    def __init__(self, config):
        self.config = config
        self.lock = asyncio.Lock()

    async def structure(self):
        c = self.config
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            r = await client.get(c['url'].rstrip('/') + '/data/LoxAPP3.json',
                                 auth=(c['username'], c['password']))
            r.raise_for_status()
            d = r.json()
        if d.get('msInfo', {}).get('serialNr') != c['serial']:
            raise Fault('miniserver_identity_mismatch')
        if not isinstance(d.get('controls'), dict):
            raise Fault('invalid_structure')
        return d

    async def snapshot(self, state_ids):
        # A new authenticated subscription delivers an initial snapshot. This avoids
        # returning a stale cached event as a fresh verification after a write.
        async with self.lock:
            ws = LoxWs(version=17.0)
            values = {}
            ready = asyncio.Event()
            wanted = set(state_ids)
            async def receive(data, kind):
                if kind in (2, 3):
                    for k, v in data.items():
                        key = k.decode() if isinstance(k, bytes) else str(k)
                        if key in wanted:
                            values[key] = {'value': v.decode() if isinstance(v, bytes) else v, 'timestamp': now()}
                    if wanted.issubset(values):
                        ready.set()
            ws.add_message_callback(receive, message_types=[2, 3])
            try:
                async with asyncio.timeout(20):
                    await ws.connect(user=self.config['username'], password=self.config['password'],
                                     loxone_url=self.config['url'], receive_updates=True, max_reconnect_attempts=1)
                    await ready.wait()
                return values
            finally:
                await ws.stop()

    async def write(self, uuid, command, value):
        # Only the fixed command mapping in Service.write can reach this method.
        async with httpx.AsyncClient(timeout=12, follow_redirects=False) as client:
            suffix = command if value is None else f'{command}/{value:g}'
            r = await client.get(f"{self.config['url'].rstrip('/')}/jdev/sps/io/{uuid}/{suffix}",
                                 auth=(self.config['username'], self.config['password']))
            r.raise_for_status()
            if str(r.json().get('LL', {}).get('Code')) != '200':
                raise Fault('miniserver_rejected_command')

class Service:
    def __init__(self, backend, db, policy_path):
        self.backend = backend
        self.policy_path = Path(policy_path)
        self.lock = asyncio.Lock()
        self.db = sqlite3.connect(db, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, timestamp TEXT, actor TEXT, request_id TEXT UNIQUE, uuid TEXT, parameter TEXT, old_value TEXT, new_value TEXT, result TEXT, verified_value TEXT)')
        self.db.commit()

    def policy(self):
        return json.loads(self.policy_path.read_text())

    async def device(self, uuid):
        if not UUID.fullmatch(uuid.split('/')[0]) or (len(uuid.split('/')) > 1 and not re.fullmatch(r'(masterValue|masterColor|AI[0-9]+)', uuid.split('/',1)[1])):
            raise Fault('invalid_device_uuid')
        s = await self.backend.structure()
        d = controls(s).get(uuid)
        if not d or d.get('uuidAction') != uuid:
            raise Fault('unknown_device')
        return s, d

    def describe(self, s, uuid, d):
        return {'uuid': uuid, 'name': d.get('name'), 'room': s.get('rooms', {}).get(d.get('room'), {}).get('name'),
                'room_uuid': d.get('room'), 'type': d.get('type'), 'timestamp': now(), 'error': None}

    async def states(self, uuid, parameter=None):
        s, d = await self.device(uuid)
        ids = d.get('states', {})
        spec = mappings(d).get(parameter, {})
        if parameter is not None:
            state = spec.get('state', parameter)
            if state in ids:
                ids = {parameter: ids[state]}
            if parameter not in ids:
                raise Fault('unknown_parameter')
            ids = {parameter: ids[parameter]}
        values = await self.backend.snapshot(ids.values()) if ids else {}
        return {**self.describe(s, uuid, d), 'states': {k: {'uuid': v, **values[v], 'value':parameter_value(k,values[v]['value']), 'raw_value':values[v]['value']} for k, v in ids.items()}}

    async def parameters(self, uuid):
        s, d = await self.device(uuid)
        specs = mappings(d)
        ids = {k:spec['state'] for k,spec in specs.items()}
        values = await self.backend.snapshot([d['states'][v] for v in ids.values()]) if ids else {}
        policy = self.policy()
        return {**self.describe(s, uuid, d), 'parameters': {
            k: {**values[d['states'][v]], 'value':parameter_value(k,values[d['states'][v]]['value']),
                'state_uuid':d['states'][v], 'unit':specs[k]['unit'], 'min':specs[k]['min'], 'max':specs[k]['max'],
                'command':specs[k]['command'], 'logic_connected':logic_connected(d,specs[k]),
                'write_allowed':bool(policy.get('writes_enabled') and k in policy.get('allowlist', {}).get(uuid, []) and not logic_connected(d,specs[k]))}
            for k,v in ids.items()}, 'limitation':'Runtime ventilation, light brightness/switches and room comfort temperatures only; no configuration upload.'}

    def history(self, limit=20):
        return [dict(r) for r in self.db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (min(max(limit,1),100),))]

    async def write(self, uuid, parameter, value, expected_value, request_id, actor):
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,100}', request_id):
            raise Fault('invalid_request_id')
        if not all(math.isfinite(v) and 0 <= v <= 100 for v in (value, expected_value)):
            raise Fault('value_out_of_range')
        async with self.lock:
            if self.db.execute('SELECT 1 FROM audit WHERE request_id=?', (request_id,)).fetchone():
                raise Fault('request_already_used')
            old = None
            try:
                p = self.policy()
                if not p.get('writes_enabled') or parameter not in p.get('allowlist', {}).get(uuid, []):
                    raise Fault('write_not_allowlisted')
                s, d = await self.device(uuid)
                spec = mappings(d).get(parameter)
                if not spec:
                    raise Fault('unsupported_parameter')
                if d.get('isSecured') or d.get('restrictions', 0):
                    raise Fault('restricted_device')
                if not spec['min'] <= value <= spec['max'] or (spec.get('integer') and value != int(value)):
                    raise Fault('value_out_of_range')
                if logic_connected(d, spec):
                    raise Fault('parameter_controlled_by_logic')
                if d.get('type') == 'Jalousie':
                    checks={k:v for k,v in d.get('states', {}).items() if k in ('safetyActive','locked','adjustingEndPos','deviceState')}
                    checks_values=await self.backend.snapshot(checks.values()) if checks else {}
                    for k,v in checks.items():
                        if (k=='deviceState' and checks_values[v]['value']==1) or (k!='deviceState' and checks_values[v]['value']):
                            raise Fault('blind_safety_or_lock_active')
                if d.get('type') == 'IRoomControllerV2':
                    if 'F' in d.get('details', {}).get('format', ''):
                        raise Fault('unsupported_temperature_unit')
                    other = 'comfortTemperatureCool' if parameter == 'comfortTemperature' else 'comfortTemperature'
                    if not d.get('details', {}).get('singleComfortTemperature') and other in d.get('states', {}):
                        other_value = (await self.states(uuid, other))['states'][other]['value']
                        if (parameter == 'comfortTemperature' and value > other_value) or (parameter == 'comfortTemperatureCool' and value < other_value):
                            raise Fault('inconsistent_heating_cooling')
                if d.get('type') == 'ColorPickerV2' and 'sequenceColorIdx' in d.get('states', {}):
                    sequence = await self.states(uuid, 'sequenceColorIdx')
                    if sequence['states']['sequenceColorIdx']['value'] != -1:
                        raise Fault('active_color_sequence')
                current = await self.states(uuid, parameter)
                old = current['states'][parameter]['value']
                cmd, wire_value = wire_command(spec, value, current['states'][parameter].get('raw_value'))
                parent = d.get('_parent_control', {})
                if parent.get('isSecured') or parent.get('restrictions', 0):
                    raise Fault('restricted_device')
                if 'jLocked' in parent.get('states', {}):
                    parent_lock = await self.backend.snapshot([parent['states']['jLocked']])
                    if parent_lock[parent['states']['jLocked']]['value']:
                        raise Fault('control_locked')
                if 'lockedOn' in d.get('states', {}) and value == 0:
                    locked_on = await self.states(uuid, 'lockedOn')
                    if locked_on['states']['lockedOn']['value']:
                        raise Fault('control_locked')
                if d.get('type') == 'Dimmer':
                    bound_ids = {k:v for k,v in d.get('states', {}).items() if k in ('min','max')}
                    limits = await self.backend.snapshot(bound_ids.values()) if bound_ids else {}
                    if value != 0 and any(value < limits[v]['value'] if k=='min' else value > limits[v]['value'] for k,v in bound_ids.items()):
                        raise Fault('value_out_of_range')
                counterpart = parameter[:-3] + ('Max' if parameter.endswith('Min') else 'Min')
                if counterpart in d.get('states', {}):
                    bounds = await self.states(uuid, counterpart)
                    bound = bounds['states'][counterpart]['value']
                    if (parameter.endswith('Min') and value > bound) or (parameter.endswith('Max') and value < bound):
                        raise Fault('inconsistent_min_max')
                if 'jLocked' in d.get('states', {}):
                    locked = await self.states(uuid, 'jLocked')
                    if locked['states']['jLocked']['value']:
                        raise Fault('control_locked')
                if old != expected_value:
                    raise Fault('precondition_failed')
                grant = p.get('pilot_grant')
                if grant and (grant.get('uuid') != uuid or grant.get('parameter') != parameter or
                              grant.get('value') != value or grant.get('expected_value') != old or
                              grant.get('request_id') != request_id):
                    raise Fault('pilot_grant_mismatch')
            except Exception as exc:
                reason = str(exc) if isinstance(exc, Fault) else 'read_failed'
                self.record(actor,request_id,uuid,parameter,old,value,reason)
                raise Fault(reason) from None
            # Durable intent BEFORE an actuator request. A crash cannot replay it.
            self.record(actor,request_id,uuid,parameter,old,value,'pending')
            verified = None
            try:
                await self.backend.write(uuid, cmd, wire_value)
                current = await self.states(uuid, parameter)
                verified = current['states'][parameter]['value']
                result = 'verified' if math.isclose(verified,value,rel_tol=0,abs_tol=1e-6) else 'verification_failed'
            except Exception:
                result = 'outcome_unknown'
            self.db.execute('UPDATE audit SET result=?, verified_value=? WHERE request_id=?',
                            (result,json.dumps(verified),request_id)); self.db.commit()
            # Never blindly roll back an unknown outcome: a concurrent automation
            # may have changed the value. Operator must inspect fresh state first.
            return {'uuid':uuid,'parameter':parameter,'old_value':old,'new_value':value,
                    'verified_value':verified,'timestamp':now(),'result':result,
                    'error':None if result == 'verified' else result,'request_id':request_id,
                    'verification_scope':'target_acknowledged_not_physical_completion' if d.get('type')=='Jalousie' and parameter.startswith('target') else 'parameter_state'}

    def record(self, actor,rid,uuid,parameter,old,new,result):
        self.db.execute('INSERT INTO audit(timestamp,actor,request_id,uuid,parameter,old_value,new_value,result) VALUES(?,?,?,?,?,?,?,?)',
                        (now(),actor,rid,uuid,parameter,json.dumps(old),json.dumps(new),result)); self.db.commit()

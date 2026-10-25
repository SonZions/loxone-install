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
            r = await client.get(f"{self.config['url'].rstrip('/')}/jdev/sps/io/{uuid}/{command}/{value:g}",
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
        if not UUID.fullmatch(uuid):
            raise Fault('invalid_device_uuid')
        s = await self.backend.structure()
        d = s['controls'].get(uuid)
        if not d or d.get('uuidAction') != uuid:
            raise Fault('unknown_device')
        return s, d

    def describe(self, s, uuid, d):
        return {'uuid': uuid, 'name': d.get('name'), 'room': s.get('rooms', {}).get(d.get('room'), {}).get('name'),
                'room_uuid': d.get('room'), 'type': d.get('type'), 'timestamp': now(), 'error': None}

    async def states(self, uuid, parameter=None):
        s, d = await self.device(uuid)
        ids = d.get('states', {})
        if parameter is not None:
            if parameter not in ids:
                raise Fault('unknown_parameter')
            ids = {parameter: ids[parameter]}
        values = await self.backend.snapshot(ids.values()) if ids else {}
        return {**self.describe(s, uuid, d), 'states': {k: {'uuid': v, **values[v]} for k, v in ids.items()}}

    async def parameters(self, uuid):
        s, d = await self.device(uuid)
        names = {k:v for k,v in d.get('states', {}).items() if k in PARAMETERS}
        values = await self.backend.snapshot(names.values()) if names else {}
        policy = self.policy()
        return {**self.describe(s, uuid, d), 'parameters': {
            k: {**values[v], 'state_uuid':v, 'unit':'%', 'min':0, 'max':100,
                'logic_connected':bool(d.get('details', {}).get(PARAMETERS[k][1])),
                'write_allowed':bool(policy.get('writes_enabled') and k in policy.get('allowlist', {}).get(uuid, []))}
            for k,v in names.items()}, 'limitation':'Only documented runtime ventilation parameters; no configuration-program upload.'}

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
                if d.get('type') != 'Ventilation' or parameter not in PARAMETERS:
                    raise Fault('unsupported_parameter')
                if d.get('isSecured') or d.get('restrictions', 0):
                    raise Fault('restricted_device')
                cmd, flag = PARAMETERS[parameter]
                if d.get('details', {}).get(flag):
                    raise Fault('parameter_controlled_by_logic')
                current = await self.states(uuid, parameter)
                old = current['states'][parameter]['value']
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
                await self.backend.write(uuid, cmd, value)
                current = await self.states(uuid, parameter)
                verified = current['states'][parameter]['value']
                result = 'verified' if verified == value else 'verification_failed'
            except Exception:
                result = 'outcome_unknown'
            self.db.execute('UPDATE audit SET result=?, verified_value=? WHERE request_id=?',
                            (result,json.dumps(verified),request_id)); self.db.commit()
            # Never blindly roll back an unknown outcome: a concurrent automation
            # may have changed the value. Operator must inspect fresh state first.
            return {'uuid':uuid,'parameter':parameter,'old_value':old,'new_value':value,
                    'verified_value':verified,'timestamp':now(),'result':result,
                    'error':None if result == 'verified' else result,'request_id':request_id}

    def record(self, actor,rid,uuid,parameter,old,new,result):
        self.db.execute('INSERT INTO audit(timestamp,actor,request_id,uuid,parameter,old_value,new_value,result) VALUES(?,?,?,?,?,?,?,?)',
                        (now(),actor,rid,uuid,parameter,json.dumps(old),json.dumps(new),result)); self.db.commit()

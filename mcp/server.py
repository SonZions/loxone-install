import asyncio
import json
import logging
import os
from pathlib import Path
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import ToolAnnotations
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from auth import OAuth, Guard
from core import Service, Miniserver, Fault, now
from controls import controls, mappings

READ=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
WRITE=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False)

def create_app(config, backend=None):
    service=Service(backend or Miniserver(config['loxone']),config['audit_db'],config['policy_path'])
    oauth=OAuth(config['oauth'],config['auth_db'])
    from urllib.parse import urlsplit
    host=urlsplit(config['oauth']['public_url']).netloc
    mcp=FastMCP('Loxone controlled access',stateless_http=True,json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[host,'127.0.0.1:*','localhost:*'],allowed_origins=[config['oauth']['public_url']]))

    def identity(ctx,write=False):
        ident=ctx.request_context.request.state.identity
        if ('loxone:write' if write else 'loxone:read') not in ident['scopes']:
            raise ValueError('insufficient_scope')
        return ident['sub']

    async def run(fn):
        try:
            async with asyncio.timeout(35):
                return await fn()
        except Fault as e:
            return {'error':str(e),'timestamp':now()}
        except Exception:
            return {'error':'upstream_unavailable','timestamp':now()}

    @mcp.tool(annotations=READ)
    async def loxone_list_rooms(ctx:Context)->dict:
        """List rooms visible to the server-side Loxone service account."""
        identity(ctx)
        async def action():
            s=await service.backend.structure()
            return {'rooms':[{'uuid':u,'name':r.get('name')} for u,r in s.get('rooms',{}).items()], 'timestamp':now(),'error':None}
        return await run(action)

    @mcp.tool(annotations=READ)
    async def loxone_list_devices(ctx:Context,room_uuid:str|None=None,offset:int=0,limit:int=100)->dict:
        """List visible controls with pagination; optionally filter by room UUID."""
        identity(ctx)
        async def action():
            s=await service.backend.structure()
            ds=[service.describe(s,u,d) for u,d in controls(s).items() if room_uuid is None or d.get('room')==room_uuid]
            return {'devices':ds[max(0,offset):max(0,offset)+max(1,min(limit,100))], 'total':len(ds),'error':None}
        return await run(action)

    @mcp.tool(annotations=READ)
    async def loxone_get_device(uuid:str,ctx:Context)->dict:
        """Get control metadata and named state UUIDs, without raw configuration URLs or secrets."""
        identity(ctx)
        async def action():
            s,d=await service.device(uuid)
            return {**service.describe(s,uuid,d),'states':d.get('states',{}),'secured':d.get('isSecured',False)}
        return await run(action)

    @mcp.tool(annotations=READ)
    async def loxone_get_state(uuid:str,ctx:Context,state:str|None=None)->dict:
        """Read a fresh WebSocket snapshot for the selected control and optional state name."""
        identity(ctx)
        return await run(lambda:service.states(uuid,state))

    @mcp.tool(annotations=READ)
    async def loxone_get_parameters(uuid:str,ctx:Context)->dict:
        """Read mapped runtime parameters for ventilation, light subcontrols and room comfort temperatures. List devices to find exact subcontrol UUIDs; no arbitrary Config parameters."""
        identity(ctx)
        return await run(lambda:service.parameters(uuid))

    @mcp.tool(annotations=READ)
    async def loxone_get_system_info(ctx:Context)->dict:
        """Verify Miniserver identity and report visible room/control counts."""
        identity(ctx)
        async def action():
            s=await service.backend.structure()
            return {'serial':s['msInfo']['serialNr'],'rooms':len(s.get('rooms',{})),
                    'devices':len(s['controls']),'timestamp':now(),'error':None,
                    'writes_enabled':bool(service.policy().get('writes_enabled'))}
        return await run(action)

    @mcp.tool(annotations=WRITE)
    async def loxone_set_parameter(uuid:str,parameter:str,value:float,expected_value:float,request_id:str,ctx:Context)->dict:
        """Set an explicitly allowlisted runtime parameter, then verify it live. Light child UUIDs include /AIx or /masterValue; discover with list_devices/get_parameters.
        Requires write scope, expected current value, and unique request ID (16-100 safe characters).
        ALWAYS ask the user to confirm this exact device, parameter, old value and target value
        immediately before this call. General permission to write is not confirmation of a change.
        Never retry with a new ID after an unknown outcome; inspect state and audit history first.
        """
        actor=identity(ctx,True)
        return await run(lambda:service.write(uuid,parameter,value,expected_value,request_id,actor))

    @mcp.tool(annotations=WRITE)
    async def loxone_execute_command(uuid:str,parameter:str,command:str,value:float,expected_value:float,request_id:str,ctx:Context)->dict:
        """Execute only a mapped runtime command from get_parameters, with explicit device and parameter. No API paths.
        ALWAYS ask the user to confirm this exact device, parameter, old value and target value
        immediately before calling. General permission to write is not confirmation of a change."""
        actor=identity(ctx,True)
        async def action():
            _, device = await service.device(uuid)
            if command != mappings(device).get(parameter, {}).get('command'):
                return {'error':'unsupported_command','timestamp':now()}
            return await service.write(uuid,parameter,value,expected_value,request_id,actor)
        return await run(action)

    @mcp.tool(annotations=READ)
    async def loxone_verify_parameter(uuid:str,parameter:str,expected_value:float,ctx:Context)->dict:
        """Independently read a fresh parameter state and compare against the expected value."""
        identity(ctx)
        async def action():
            r=await service.states(uuid,parameter)
            r['verified']=r['states'][parameter]['value']==expected_value
            r['expected_value']=expected_value
            return r
        return await run(action)

    @mcp.tool(annotations=READ)
    async def loxone_get_change_history(ctx:Context,limit:int=20)->dict:
        """Read durable audit records including rejected, pending, and uncertain writes."""
        identity(ctx)
        return {'changes':service.history(limit),'timestamp':now(),'error':None}

    @mcp.tool(annotations=READ)
    async def loxone_get_group_state(uuid:str,ctx:Context)->dict:
        """Read exact central-group members, current states, allowed commands and a state_token.
        Supports CentralJalousie, CentralLightController, and individual Jalousie (stop).
        Read immediately before asking for confirmation; name all affected members."""
        identity(ctx)
        from groups import read_group
        return await run(lambda:read_group(service,uuid))

    @mcp.tool(annotations=WRITE)
    async def loxone_execute_group(uuid:str,command:str,state_token:str,request_id:str,ctx:Context)->dict:
        """Execute a pinned central-group command or individual blind stop. No arbitrary paths.
        ALWAYS ask the user to confirm the exact group, ALL affected members, command and
        current states immediately before calling. General write permission is not approval.
        Get a fresh state_token from loxone_get_group_state. Blind target verification does
        NOT mean physical travel completed; subsequently read position/up/down. Never auto-retry."""
        actor=identity(ctx,True)
        from groups import execute_group
        return await run(lambda:execute_group(service,uuid,command,state_token,request_id,actor))

    @mcp.custom_route('/healthz',methods=['GET'])
    async def health(request):
        return JSONResponse({'status':'ok','writes_enabled':bool(service.policy().get('writes_enabled'))})
    for path,methods,fn in [('/.well-known/oauth-protected-resource',['GET'],oauth.resource_metadata),
                            ('/.well-known/oauth-protected-resource/mcp',['GET'],oauth.resource_metadata),
                            ('/.well-known/oauth-authorization-server',['GET'],oauth.metadata),
                            ('/authorize',['GET','POST'],oauth.authorize),('/token',['POST'],oauth.token)]:
        mcp.custom_route(path,methods=methods)(fn)
    app=Guard(mcp.streamable_http_app(),oauth)
    app.service=service
    return app

if __name__=='__main__':
    import uvicorn
    config=json.loads(Path(os.environ.get('CONFIG_FILE','/run/secrets/config.json')).read_text())
    # No upstream request or exception logs: URLs and auth responses can contain secrets.
    logging.getLogger('httpx').setLevel(logging.CRITICAL)
    uvicorn.run(create_app(config),host='0.0.0.0',port=8080,access_log=False,log_level='warning')

import asyncio
import json
import pytest
from connection import SharedConnection, OwnedSocket, ConnectionFault

CONFIG={'username':'test','password':'test','url':'mock'}
class MockSocket:
    def __init__(self):
        self.failure=None;self.state='CLOSED';self.stopped=False;self.on_response=None;self.value=11
    def add_message_callback(self, callback, message_types):self.callback=callback
    async def connect(self, **kwargs):self.state='CONNECTED'
    async def send_command(self, cmd):
        assert cmd=='jdev/sps/enablebinstatusupdate'
        await self.callback({'state':-999},2)
        self.on_response()
        await self.callback({'state':self.value},2)
        return json.dumps({'LL':{'Code':'200'}})
    async def confirm_alive(self):pass
    async def stop(self):self.stopped=True;self.state='CLOSED'

@pytest.mark.asyncio
async def test_100_reads_one_connection_fresh_values():
    c=SharedConnection(CONFIG,MockSocket)
    for i in range(100):
        if c.ws:await c.receive({'state':i},2)
        r=await c.snapshot(['state'])
        assert r['state']['value']==(11 if i==0 else i)
    assert c.connections==1
    ws=c.ws;await c.close()
    assert ws.stopped and c.ws is None
    with pytest.raises(ConnectionFault,match='connector_stopped'):await c.snapshot(['state'])

@pytest.mark.asyncio
async def test_failure_cleanup_backoff():
    c=SharedConnection(CONFIG,MockSocket);await c.snapshot(['state'])
    ws=c.ws;ws.failure='event_slots_exhausted'
    with pytest.raises(ConnectionFault,match='event_slots_exhausted'):await c.snapshot(['state'])
    assert ws.stopped and c.ws is None
    with pytest.raises(ConnectionFault,match='upstream_backoff'):await c.snapshot(['state'])
    assert c.connections==1 and c.status()['retry_after_seconds']>=299

@pytest.mark.asyncio
async def test_cancel_handshake():
    entered=asyncio.Event()
    class Slow(MockSocket):
        async def connect(self,**kwargs):entered.set();await asyncio.Event().wait()
    c=SharedConnection(CONFIG,Slow);t=asyncio.create_task(c.snapshot(['state']))
    await entered.wait();ws=c.ws;t.cancel()
    with pytest.raises(asyncio.CancelledError):await t
    assert ws.stopped and c.ws is None

@pytest.mark.asyncio
async def test_real_library_stop_listener_refresh():
    class Socket:
        close_code=1000
        def __init__(self):self.end=asyncio.Event()
        def __aiter__(self):return self
        async def __anext__(self):await self.end.wait();raise StopAsyncIteration
        async def close(self):self.end.set()
    c=OwnedSocket();c._ws=Socket();c.state='CONNECTED'
    tasks=[asyncio.create_task(c.ws_listen()),asyncio.create_task(c.refresh_token())]
    c.background_tasks.update(tasks);await asyncio.sleep(0);await c.stop();await c.reconnect()
    assert all(t.done() for t in tasks) and c.state=='CLOSED' and c._ws is None
    assert not c.background_tasks

@pytest.mark.asyncio
async def test_disconnect_no_reconnect():
    c=OwnedSocket();c.state='CONNECTED';await c.handle_connection_interrupt()
    assert c.state=='CLOSED' and c.failure=='websocket_disconnected' and not c.background_tasks

@pytest.mark.asyncio
async def test_keepalive_header():
    c=OwnedSocket();await c._async_process_message(bytes([3,6,0,0,0,0,0,0]))
    assert c.ack.is_set() and c.last_ack is not None

@pytest.mark.asyncio
async def test_listener_owns_receive_before_start_returns():
    c=OwnedSocket()
    await c.start()
    assert c._listener_running
    await c.stop()
    assert not c.background_tasks

@pytest.mark.asyncio
async def test_concurrent_reads_share_connection():
    c=SharedConnection(CONFIG,MockSocket)
    results=await asyncio.gather(*(c.snapshot(['state']) for _ in range(20)))
    assert len(results)==20 and c.connections==1
    await c.close()

@pytest.mark.asyncio
async def test_invalidate_cannot_reuse_old_values():
    c=SharedConnection(CONFIG,MockSocket)
    await c.snapshot(['state']);old=c.ws
    await c.invalidate()
    assert old.stopped and c.values=={}
    await c.snapshot(['state'])
    assert c.connections==2 and c.ws is not old
    await c.close()

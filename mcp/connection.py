"""One owned WebSocket; no dependency-controlled reconnects or orphan callbacks."""
import asyncio
import json
import random
import time
from datetime import datetime, timezone
from loxwebsocket import LoxWs


class ConnectionFault(Exception):
    pass


class OwnedSocket(LoxWs):
    def __init__(self):
        super().__init__(version=17.0)
        self.stopping = False
        self.failure = None
        self.ack = asyncio.Event()
        self.last_ack = None
        self.on_response = None
        self.ping_lock = asyncio.Lock()

    async def start(self):
        await super().start()
        # Route commands to the scheduled listener even before it first runs.
        self._listener_running = True

    async def reconnect(self):
        # The owner alone controls connection attempts and their cooldown.
        await self.handle_connection_interrupt()

    async def handle_connection_interrupt(self, exception=None):
        if not self.stopping:
            code = self._ws.close_code if self._ws else None
            self.failure = {4008: 'event_slots_exhausted', 4003: 'authentication_blocked',
                            4004: 'authentication_changed', 4005: 'authentication_changed',
                            4006: 'user_disabled'}.get(code, 'websocket_disconnected')
            self.state = 'CLOSED'
            await self.stop()

    async def _async_process_message(self, message):
        # Dispatch synchronously: callbacks cannot outlive the owned listener.
        if await self.parse_loxone_message_header_message(message):
            if self._current_message_type == 6:
                self.ack.set()
                self.last_ack = time.monotonic()
                self._current_message_type = None
            return
        kind = self._current_message_type
        self._current_message_type = None
        if kind == 0 and self._response_future is not None and not self._response_future.done():
            if self.on_response:
                self.on_response()
            self._response_future.set_result(message)
            return
        if kind not in self._message_handler:
            return
        data = await self._message_handler[kind](message, {})
        if data:
            for callback in self._message_callbacks[kind]:
                await callback(data, kind)

    async def confirm_alive(self):
        async with self.ping_lock:
            self.ack.clear()
            await self._ws.send_str('keepalive')
            await asyncio.wait_for(self.ack.wait(), 15)

    async def keep_alive(self, second):
        try:
            while not self.stopping:
                await asyncio.sleep(second)
                await self.confirm_alive()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = 'keepalive_timeout'
            await self.stop()

    async def refresh_token(self):
        try:
            while not self.stopping:
                await asyncio.sleep(max(15, self._token.get_seconds_to_expire() - 86400))
                async with asyncio.timeout(15):
                    await self._refresh_token()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failure = 'token_refresh_failed'
            await self.stop()

    async def stop(self):
        self.stopping = True
        current = asyncio.current_task()
        tasks = [t for t in list(self.background_tasks) if t is not current]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.difference_update(tasks)
        return await super().stop()


class SharedConnection:
    def __init__(self, config, socket_factory=OwnedSocket):
        self.config = config
        self.socket_factory = socket_factory
        self.lock = asyncio.Lock()
        self.ws = None
        self.closed = False
        self.failures = 0
        self.retry_at = 0
        self.last_error = None
        self.connections = 0
        self.values = {}
        self.wanted = set()
        self.ready = asyncio.Event()
        self.collecting = False

    async def receive(self, data, kind):
        if not self.collecting or kind not in (2, 3):
            return
        for key, value in data.items():
            key = key.decode() if isinstance(key, bytes) else str(key)
            self.values[key] = {'value': value.decode() if isinstance(value, bytes) else value,
                                'timestamp': datetime.now(timezone.utc).isoformat()}
        if self.wanted.issubset(self.values):
            self.ready.set()

    def begin_snapshot(self):
        self.values = {}
        self.ready.clear()
        self.collecting = True

    async def invalidate(self):
        async with self.lock:
            await self.drop()

    async def drop(self):
        self.values = {}
        self.collecting = False
        ws, self.ws = self.ws, None
        if ws:
            await ws.stop()

    async def snapshot(self, ids):
        async with self.lock:
            if self.closed:
                raise ConnectionFault('connector_stopped')
            if time.monotonic() < self.retry_at:
                raise ConnectionFault('upstream_backoff')
            try:
                async with asyncio.timeout(20):
                    if self.ws and (self.ws.failure or self.ws.state != 'CONNECTED'):
                        reason = self.ws.failure or 'websocket_disconnected'
                        raise ConnectionFault(reason)
                    self.wanted = set(ids)
                    self.ready.clear()
                    if self.ws is None:
                        self.ws = self.socket_factory()
                        self.ws.add_message_callback(self.receive, message_types=[2, 3])
                        await self.ws.connect(user=self.config['username'], password=self.config['password'],
                            loxone_url=self.config['url'], receive_updates=False, max_reconnect_attempts=1)
                        self.connections += 1
                        self.ws.on_response = self.begin_snapshot
                        response = json.loads(await self.ws.send_command('jdev/sps/enablebinstatusupdate'))
                        self.ws.on_response = None
                        if str(response.get('LL', {}).get('Code', response.get('LL', {}).get('code'))) != '200':
                            raise ConnectionFault('subscription_rejected')
                    if not self.wanted.issubset(self.values):
                        await self.ready.wait()
                    # Ordered keepalive response confirms the stream is still serviced.
                    # Preserve event timestamps; never relabel unchanged values as new events.
                    await self.ws.confirm_alive()
                    if self.ws.failure:
                        raise ConnectionFault(self.ws.failure)
                    self.failures = 0
                    self.last_error = None
                    checked = datetime.now(timezone.utc).isoformat()
                    return {key: {**self.values[key], 'connection_checked_at': checked}
                            for key in self.wanted}
            except BaseException as exc:
                self.last_error = self.ws.failure if self.ws and self.ws.failure else (
                    str(exc) if isinstance(exc, ConnectionFault) else
                    'snapshot_timeout' if isinstance(exc, TimeoutError) else 'upstream_unavailable')
                self.failures += 1
                delay = min(300, 5 * 2 ** min(self.failures - 1, 6))
                if self.last_error in ('event_slots_exhausted', 'authentication_blocked', 'user_disabled'):
                    delay = 300
                self.retry_at = time.monotonic() + delay * random.uniform(1, 1.2)
                await self.drop()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise ConnectionFault(self.last_error) from None

    async def close(self):
        self.closed = True
        async with self.lock:
            await self.drop()

    def status(self):
        return {'websocket_connected': bool(self.ws and self.ws.state == 'CONNECTED' and not self.ws.failure),
                'connections_created': self.connections, 'consecutive_failures': self.failures,
                'last_error': self.last_error, 'retry_after_seconds': max(0, round(self.retry_at-time.monotonic()))}

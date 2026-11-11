"""MqttConnection: asyncio wrapper around paho-mqtt (thread loop -> asyncio queue).

Used by BambuPrinter today; reusable by future MQTT printers (e.g. Centauri Carbon 2).
Maps failures to driver errors: bad credentials -> AuthFailed, network -> PrinterUnreachable.
"""
from __future__ import annotations

import asyncio
import os
import socket
import ssl
from typing import AsyncIterator, Optional

import paho.mqtt.client as mqtt

from ..errors import AuthFailed, DriverError, PrinterUnreachable
from .tls import insecure_context

_CLOSED = object()


class MqttConnection:
    def __init__(self, host: str, port: int, username: str, password: str, *,
                 tls: bool = True, keepalive: int = 30, client_id: Optional[str] = None,
                 ssl_context: Optional[ssl.SSLContext] = None):
        self.host, self.port = host, port
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._connack: Optional[asyncio.Future] = None
        self._keepalive = keepalive
        self._client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                   client_id=client_id or f"printer-dash-{os.getpid()}-{id(self):x}",
                                   protocol=mqtt.MQTTv311)
        self._client.username_pw_set(username, password)
        if tls:
            self._client.tls_set_context(ssl_context or insecure_context())   # printers: self-signed
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect
        self._closed = False

    # ---- paho callbacks (run on paho's thread) ----
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if self._loop and self._connack and not self._connack.done():
            self._loop.call_soon_threadsafe(self._connack.set_result, reason_code)

    def _on_message(self, client, userdata, msg):
        if self._loop:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (msg.topic, msg.payload))

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        if self._loop:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, _CLOSED)

    # ---- API ----
    async def connect(self, timeout: float = 15) -> None:
        self._loop = asyncio.get_running_loop()
        self._connack = self._loop.create_future()
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._client.connect, self.host, self.port, self._keepalive), timeout)
        except (asyncio.TimeoutError, socket.timeout, TimeoutError):
            raise PrinterUnreachable(f"{self.host}:{self.port} timed out") from None
        except ConnectionRefusedError:
            raise PrinterUnreachable(f"{self.host}:{self.port} refused the connection") from None
        except ssl.SSLError as e:
            raise DriverError(f"TLS handshake failed: {e}") from None
        except OSError as e:
            raise PrinterUnreachable(f"{self.host}:{self.port} unreachable: {e}") from None
        self._client.loop_start()
        try:
            rc = await asyncio.wait_for(self._connack, timeout)
        except asyncio.TimeoutError:
            await self.close()
            raise PrinterUnreachable("no MQTT CONNACK") from None
        if rc.is_failure:
            await self.close()
            text = str(rc)
            if "authori" in text.lower() or "user name or password" in text.lower():
                raise AuthFailed("wrong access code")
            raise DriverError(f"MQTT connect refused: {text}")

    def subscribe(self, topic: str) -> None:
        self._client.subscribe(topic, qos=0)

    def publish(self, topic: str, payload: str) -> None:
        self._client.publish(topic, payload, qos=0)

    async def messages(self, idle_timeout: float) -> AsyncIterator[tuple[str, bytes]]:
        """Yield (topic, payload). Raises PrinterUnreachable on disconnect or `idle_timeout` silence."""
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), idle_timeout)
            except asyncio.TimeoutError:
                raise PrinterUnreachable(f"no data for {idle_timeout:.0f} s") from None
            if item is _CLOSED:
                if self._closed:
                    return
                raise PrinterUnreachable("connection lost")
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.to_thread(self._client.loop_stop)

"""Offline regression test for the WebSocket client lifecycle.

Spins up a real local WebSocket server, then drives the threaded
LoafWebSocketClient through connect -> subscribe -> receive -> stop. Guards the
bug where stop() raised "Event loop is closed" after a successful connection.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time

import websockets

from loaf import LoafWebSocketClient


class _EchoServer:
    """Minimal Loaf-like WS server: greets, confirms subscriptions, pushes one update."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.port: int | None = None
        self._thread: threading.Thread | None = None
        self._stop: asyncio.Event | None = None

    def start(self) -> "_EchoServer":
        ready = threading.Event()

        def run() -> None:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)

            async def handler(ws):
                await ws.send(json.dumps({"type": "connection", "timestamp": 0}))
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "subscribe":
                        await ws.send(json.dumps(
                            {"type": "subscription_confirmed",
                             "channels": msg.get("channels", []), "timestamp": 0}))
                        await ws.send(json.dumps(
                            {"type": "orderbook_update", "propertyId": 1,
                             "bids": [{"price": 100.0, "quantity": 1.0}], "asks": [],
                             "timestamp": 0}))

            async def main() -> None:
                self._stop = asyncio.Event()
                async with websockets.serve(handler, "127.0.0.1", 0) as server:
                    self.port = server.sockets[0].getsockname()[1]
                    ready.set()
                    await self._stop.wait()

            self.loop.run_until_complete(main())
            self.loop.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        assert ready.wait(5), "server failed to start"
        return self

    def stop(self) -> None:
        if self.loop and self._stop:
            self.loop.call_soon_threadsafe(self._stop.set)
        if self._thread:
            self._thread.join(timeout=5)


def test_ws_connect_subscribe_stop_is_clean():
    server = _EchoServer().start()
    try:
        ws = LoafWebSocketClient(ws_url=f"ws://127.0.0.1:{server.port}", auto_reconnect=False)
        received: list[str] = []
        ws.on_message(lambda m: received.append(m.get("type")))

        ws.subscribe_orderbook("OPRA")
        ws.start()
        assert ws.wait_until_connected(timeout=5)

        deadline = time.time() + 3
        while "orderbook_update" not in received and time.time() < deadline:
            time.sleep(0.05)

        ws.stop()  # must not raise (the regression)

        assert "connection" in received
        assert "orderbook_update" in received
        assert ws._thread is None
    finally:
        server.stop()


def test_ws_stop_without_start_is_safe():
    ws = LoafWebSocketClient(ws_url="ws://127.0.0.1:1/ws")
    ws.stop()  # no-op, must not raise

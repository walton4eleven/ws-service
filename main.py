"""WebSocket TCP tunnel for Render free tier.

Client connects via WebSocket, sends target as first message:
  ws.send("host:port")
Server opens TCP to target, pipes WS ↔ TCP bidirectionally.

All HTTP headers, TLS, binary protocols pass through untouched.
Deploy: Render Web Service, pip install aiohttp, python main.py
"""
import asyncio
import os
from aiohttp import web

PORT = int(os.environ.get("PORT", 10000))
KEY = os.environ.get("TK", "")

async def ws_tunnel(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    # Auth check:
    if KEY:
        auth = request.headers.get("X-TK", "")
        if auth != KEY:
            await ws.close(code=4003, message=b"forbidden")
            return ws

    # First message = "host:port"
    target_msg = await ws.receive_str()
    if ":" not in target_msg:
        await ws.close(code=4000, message=b"bad target")
        return ws

    host, _, port_s = target_msg.rpartition(":")
    try:
        port = int(port_s)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )
    except Exception as e:
        await ws.close(code=4002, message=str(e).encode()[:120])
        return ws

    await ws.send_str("OK")

    # Pipe: TCP→WS
    async def tcp_to_ws():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception:
            pass

    # Pipe: WS→TCP
    async def ws_to_tcp():
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type == web.WSMsgType.TEXT:
                    writer.write(msg.data.encode())
                    await writer.drain()
                elif msg.type in (web.WSMsgType.CLOSE, web.WSMsgType.ERROR):
                    break
        except Exception:
            pass

    await asyncio.gather(tcp_to_ws(), ws_to_tcp())
    writer.close()
    return ws

# Also keep cors-anywhere style for backward compat:
async def fwd(request):
    from aiohttp import ClientSession, TCPConnector, ClientTimeout
    target = request.path_qs.lstrip("/")
    if not target.startswith("http"):
        return web.Response(status=400, text="url")
    h = {k: v for k, v in request.headers.items()
         if k.lower() not in ("host", "transfer-encoding", "connection", "x-tk")}
    body = await request.read() if request.can_read_body else None
    async with ClientSession(connector=TCPConnector(ssl=False)) as s:
        async with s.request(request.method, target, headers=h, data=body,
                           timeout=ClientTimeout(total=30), allow_redirects=True) as r:
            rb = await r.read()
            ct = r.headers.get("Content-Type", "application/octet-stream")
            return web.Response(body=rb, status=r.status,
                headers={"Access-Control-Allow-Origin": "*", "Content-Type": ct})

async def health(request):
    return web.json_response({"s": "ok", "ws": True})

app = web.Application()
app.router.add_get("/health", health)
app.router.add_get("/ws", ws_tunnel)  # WebSocket tunnel endpoint
app.router.add_route("*", "/{p:.*}", fwd)  # HTTP relay fallback

if __name__ == "__main__":
    web.run_app(app, port=PORT)

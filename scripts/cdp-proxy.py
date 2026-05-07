#!/usr/bin/env python3
"""CDP proxy for Quark/Wine compatibility.

Listens on 0.0.0.0:PROXY_PORT, forwards to 127.0.0.1:QUARK_PORT.
Handles both HTTP (/json/*) and WebSocket on the same port.
Rewrites WebSocket URLs in HTTP responses so clients connect through the proxy.
Intercepts CDP commands unsupported by Wine/Electron and returns fake success.

Usage: cdp-proxy.py [PROXY_PORT [QUARK_PORT]]
"""
import asyncio
import base64
import hashlib
import json
import os
import struct
import sys
import urllib.request

QUARK_HOST = "127.0.0.1"
QUARK_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 9222
PROXY_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9223

# Browser-level CDP commands not supported by Wine/Electron – return empty success.
INTERCEPT = {
    "Browser.setDownloadBehavior",
    "Browser.grantPermissions",
    "Browser.resetPermissions",
    "Browser.setPermission",
}

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


# ── WebSocket helpers ──────────────────────────────────────────────────────────

def _ws_accept(key: str) -> str:
    return base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode()).digest()
    ).decode()


async def _read_frame(reader: asyncio.StreamReader):
    """Read one WebSocket frame; return (opcode, payload) or raise EOFError."""
    try:
        hdr = await reader.readexactly(2)
    except asyncio.IncompleteReadError:
        raise EOFError
    opcode = hdr[0] & 0x0F
    masked = bool(hdr[1] & 0x80)
    length = hdr[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    mask = await reader.readexactly(4) if masked else b""
    try:
        payload = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        raise EOFError
    if masked:
        payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    return opcode, payload


def _frame_server(payload, opcode=1):
    """Unmasked frame (server → client)."""
    if isinstance(payload, str):
        payload = payload.encode()
    n = len(payload)
    hdr = bytes([0x80 | opcode])
    if n < 126:
        hdr += bytes([n])
    elif n < 65536:
        hdr += bytes([126]) + struct.pack("!H", n)
    else:
        hdr += bytes([127]) + struct.pack("!Q", n)
    return hdr + payload


def _frame_client(payload, opcode=1):
    """Masked frame (client → server, required by spec)."""
    if isinstance(payload, str):
        payload = payload.encode()
    mask = os.urandom(4)
    masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    n = len(payload)
    hdr = bytes([0x80 | opcode])
    if n < 126:
        hdr += bytes([0x80 | n])
    elif n < 65536:
        hdr += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        hdr += bytes([0x80 | 127]) + struct.pack("!Q", n)
    return hdr + mask + masked


# ── HTTP helpers ───────────────────────────────────────────────────────────────

async def _read_http_headers(reader: asyncio.StreamReader):
    """Parse request line + headers. Returns (path, headers_dict)."""
    lines = []
    while True:
        line = await reader.readline()
        decoded = line.decode("latin1").rstrip("\r\n")
        if not decoded:
            break
        lines.append(decoded)
    if not lines:
        return None, {}
    path = lines[0].split()[1] if len(lines[0].split()) >= 2 else "/"
    headers = {}
    for line in lines[1:]:
        if ": " in line:
            k, v = line.split(": ", 1)
            headers[k.lower()] = v
    return path, headers


def _fetch_json(path: str, rewrite_host: str) -> tuple:
    """Fetch /json/* from Quark, rewrite ws:// URLs to the client-facing host.
    Returns (body_bytes, content_type_str)."""
    url = f"http://{QUARK_HOST}:{QUARK_PORT}{path}"
    req = urllib.request.Request(url, headers={"Host": f"{QUARK_HOST}:{QUARK_PORT}"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        content_type = resp.headers.get("Content-Type", "application/json")
        body = resp.read()
    # Rewrite the ws:// address to whatever host:port the client used so the
    # WebSocket URL stays routable from the client's perspective.
    body = body.replace(
        f"ws://{QUARK_HOST}:{QUARK_PORT}".encode(),
        f"ws://{rewrite_host}".encode(),
    )
    return body, content_type


# ── WebSocket proxy ────────────────────────────────────────────────────────────

async def _proxy_ws(
    client_r: asyncio.StreamReader,
    client_w: asyncio.StreamWriter,
    ws_path: str,
):
    """Bidirectional WS proxy between the Playwright client and Quark CDP."""
    try:
        quark_r, quark_w = await asyncio.open_connection(QUARK_HOST, QUARK_PORT)
    except OSError:
        client_w.close()
        return

    # Upgrade to WebSocket with Quark (proxy acts as WS client).
    proxy_key = base64.b64encode(b"cdpproxy-key-1234").decode()
    quark_w.write((
        f"GET {ws_path} HTTP/1.1\r\n"
        f"Host: {QUARK_HOST}:{QUARK_PORT}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {proxy_key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    ).encode())
    await quark_w.drain()

    # Consume Quark's 101 Switching Protocols response.
    while True:
        line = await quark_r.readline()
        if line in (b"\r\n", b""):
            break

    async def client_to_quark():
        try:
            while True:
                opcode, payload = await _read_frame(client_r)
                if opcode == 8:  # close
                    quark_w.write(_frame_client(payload, opcode=8))
                    await quark_w.drain()
                    break
                if opcode == 1:  # text – check for intercept
                    try:
                        msg = json.loads(payload)
                        if msg.get("method") in INTERCEPT:
                            fake = json.dumps({"id": msg["id"], "result": {}})
                            client_w.write(_frame_server(fake))
                            await client_w.drain()
                            continue
                    except (json.JSONDecodeError, KeyError):
                        pass
                quark_w.write(_frame_client(payload, opcode=opcode))
                await quark_w.drain()
        except EOFError:
            pass
        finally:
            try:
                quark_w.close()
            except Exception:
                pass

    async def quark_to_client():
        try:
            while True:
                opcode, payload = await _read_frame(quark_r)
                client_w.write(_frame_server(payload, opcode=opcode))
                await client_w.drain()
                if opcode == 8:
                    break
        except EOFError:
            pass
        finally:
            try:
                client_w.close()
            except Exception:
                pass

    tasks = [
        asyncio.create_task(client_to_quark()),
        asyncio.create_task(quark_to_client()),
    ]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        client_w.close()
    except Exception:
        pass
    try:
        quark_w.close()
    except Exception:
        pass


# ── Connection handler ─────────────────────────────────────────────────────────

async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        path, headers = await _read_http_headers(reader)
        if path is None:
            return

        if headers.get("upgrade", "").lower() == "websocket":
            # WebSocket handshake → proxy
            ws_key = headers.get("sec-websocket-key", "")
            writer.write((
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {_ws_accept(ws_key)}\r\n\r\n"
            ).encode())
            await writer.drain()
            await _proxy_ws(reader, writer, path)
        else:
            # HTTP – proxy /json/* and rewrite URLs using the client's Host header
            rewrite_host = headers.get("host", f"127.0.0.1:{PROXY_PORT}")
            try:
                body, content_type = await asyncio.get_event_loop().run_in_executor(
                    None, _fetch_json, path, rewrite_host
                )
                writer.write((
                    "HTTP/1.1 200 OK\r\n"
                    f"Content-Type: {content_type}\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "\r\n"
                ).encode() + body)
            except Exception as exc:
                err = str(exc).encode()
                writer.write((
                    "HTTP/1.1 502 Bad Gateway\r\n"
                    f"Content-Length: {len(err)}\r\n\r\n"
                ).encode() + err)
            await writer.drain()
            writer.close()
    except Exception:
        try:
            writer.close()
        except Exception:
            pass


async def main():
    server = await asyncio.start_server(_handle, "0.0.0.0", PROXY_PORT)
    print(
        f"[cdp-proxy] 0.0.0.0:{PROXY_PORT} → {QUARK_HOST}:{QUARK_PORT}",
        flush=True,
    )
    async with server:
        await server.serve_forever()


asyncio.run(main())

#!/usr/bin/env python3
"""The sandbox's only way out: an HTTPS CONNECT proxy that lets a client container reach its model API and nothing
else (README.md, "A real agent client").

    python3 egress_proxy.py --listen 172.30.99.2 --port 3128 --allow api.anthropic.com[,host2,...]

It runs in its own container on two networks: the sandbox's internal one (where the client finds it at --listen)
and Docker's default bridge (its way out). It accepts only `CONNECT <allowed host>:443`; anything else (plain HTTP,
another host or port) gets a 403. The proxy resolves the name itself, so the client needs no DNS. TLS stays end to
end: the proxy sees host names and byte counts, never the traffic. One line per request on stdout
(`docker logs bg3ai-egress`).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time

HEAD_LIMIT = 8192
CONNECT_TIMEOUT_S = 30


def decide(head: bytes, allow: set[str]) -> tuple[str, int] | str:
    """(host, port) to open for a CONNECT request line to an allowed host on 443, else why not (a log text)."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split(" ")
    if len(parts) != 3 or parts[0] != "CONNECT":
        return f"not a CONNECT: {line[:120]!r}"
    host, _, port = parts[1].rpartition(":")
    host = host.strip("[]").lower().rstrip(".")
    if port != "443":
        return f"port {port!r} refused ({host})"
    if host not in allow:
        return f"host {host} refused"
    return host, 443


async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count: list[int]) -> None:
    try:
        while data := await reader.read(65536):
            count[0] += len(data)
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except (ConnectionError, OSError):
            pass


def say(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, allow: set[str]) -> None:
    peer = writer.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), CONNECT_TIMEOUT_S)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
        writer.close()
        return
    got = decide(head, allow)
    if isinstance(got, str):
        say(f"deny {peer and peer[0]}: {got}")
        writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    host, port = got
    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT_S)
    except (OSError, asyncio.TimeoutError) as e:
        say(f"fail {host}:{port}: {e}")
        writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    say(f"allow {peer and peer[0]} -> {host}:{port}")
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    up, down = [0], [0]
    await asyncio.gather(pipe(reader, up_w, up), pipe(up_r, writer, down))
    say(f"done {host}:{port} up={up[0]} down={down[0]}")


async def serve(listen: str, port: int, allow: set[str]) -> None:
    server = await asyncio.start_server(lambda r, w: handle(r, w, allow), listen, port, limit=HEAD_LIMIT)
    say(f"listening on {listen}:{port}; allowed: {', '.join(sorted(allow)) or '(none)'}")
    async with server:
        await server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--listen", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=3128)
    ap.add_argument("--allow", default="", help="comma-separated host names reachable on 443")
    a = ap.parse_args(argv)
    allow = {h.strip().lower().rstrip(".") for h in a.allow.split(",") if h.strip()}
    try:
        asyncio.run(serve(a.listen, a.port, allow))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())

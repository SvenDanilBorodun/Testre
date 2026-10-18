"""An HTTPS CONNECT proxy that can make Hugging Face unreachable on demand (spec §9 F16, FR-20). E2E only.

    python block_proxy.py --listen 8899 --flag /tmp/block-hf
    HTTPS_PROXY=http://127.0.0.1:8899 NO_PROXY=127.0.0.1,localhost uvicorn app.main:app ...
    touch /tmp/block-hf ; sleep 180 ; rm /tmp/block-hf

While the flag file exists, a CONNECT to a host matching (^|\\.)(huggingface\\.co|hf\\.co)$
answers 403; every other CONNECT is tunnelled. Anything that is not a CONNECT gets 405.
"""

import argparse
import asyncio
import os
import re

BLOCKED = re.compile(r"(^|\.)(huggingface\.co|hf\.co)$", re.IGNORECASE)


def is_blocked(host: str, flag: str) -> bool:
    return bool(BLOCKED.search(host.rstrip("."))) and os.path.exists(flag)


async def _pipe(reader, writer):
    try:
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def make_handler(flag: str):
    async def handle(reader, writer):
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            writer.close()
            return
        first = request.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(first) != 3 or first[0].upper() != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        host, _, port = first[1].rpartition(":")
        host = host.strip("[]")
        if is_blocked(host, flag):
            print(f"block_proxy: 403 {host}", flush=True)
            writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 30)
        except (OSError, ValueError, asyncio.TimeoutError):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))

    return handle


async def serve(host: str, port: int, flag: str):
    server = await asyncio.start_server(make_handler(flag), host, port)
    async with server:
        await server.serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--flag", required=True)
    a = ap.parse_args()
    asyncio.run(serve(a.host, a.listen, a.flag))


if __name__ == "__main__":
    main()

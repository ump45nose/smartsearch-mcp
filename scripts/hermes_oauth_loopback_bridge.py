#!/usr/bin/env python3
"""Temporarily bridge the Hermes OAuth callback from LAN to loopback.

Hermes intentionally binds its native OAuth callback server to 127.0.0.1.
Nginx Proxy Manager reaches this one-shot bridge on the host LAN address.
The bridge does not inspect or log callback bytes.
"""

from __future__ import annotations

import asyncio
import contextlib


LISTEN_HOST = "192.168.31.201"
PORT = 5556
TARGET_HOST = "127.0.0.1"


async def copy_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while data := await reader.read(64 * 1024):
            writer.write(data)
            await writer.drain()
    finally:
        with contextlib.suppress(Exception):
            writer.write_eof()


async def handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    try:
        target_reader, target_writer = await asyncio.open_connection(
            TARGET_HOST,
            PORT,
        )
    except OSError:
        client_writer.close()
        await client_writer.wait_closed()
        return

    try:
        await asyncio.gather(
            copy_stream(client_reader, target_writer),
            copy_stream(target_reader, client_writer),
        )
    finally:
        target_writer.close()
        client_writer.close()
        with contextlib.suppress(Exception):
            await target_writer.wait_closed()
        with contextlib.suppress(Exception):
            await client_writer.wait_closed()


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

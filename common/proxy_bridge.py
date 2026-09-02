# -*- coding: utf-8 -*-
"""Loopback HTTP CONNECT bridge for browsers with flaky proxy auth support."""

from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
from urllib.parse import unquote, urlparse


class AuthenticatedProxyBridge:
    """Expose an unauthenticated loopback proxy backed by one authenticated proxy."""

    def __init__(self, host: str, port: int, username: str, password: str):
        self.upstream_host = host
        self.upstream_port = port
        token = base64.b64encode(
            f"{username}:{password}".encode("utf-8")
        ).decode("ascii")
        self._auth_header = f"Proxy-Authorization: Basic {token}"
        self._server: asyncio.AbstractServer | None = None

    @classmethod
    async def start_for(cls, proxy_url: str | None) -> "AuthenticatedProxyBridge | None":
        parsed = urlparse(str(proxy_url or "").strip())
        if parsed.scheme.lower() != "http" or not parsed.hostname or not parsed.port:
            return None
        if parsed.username is None:
            return None
        bridge = cls(
            parsed.hostname,
            int(parsed.port),
            unquote(parsed.username),
            unquote(parsed.password or ""),
        )
        bridge._server = await asyncio.start_server(
            bridge._handle_client,
            host="127.0.0.1",
            port=0,
            limit=128 * 1024,
        )
        return bridge

    @property
    def url(self) -> str:
        if self._server is None or not self._server.sockets:
            return ""
        port = int(self._server.sockets[0].getsockname()[1])
        return f"http://127.0.0.1:{port}"

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _open_upstream(self):
        return await asyncio.wait_for(
            asyncio.open_connection(self.upstream_host, self.upstream_port),
            timeout=12,
        )

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            request_head = await asyncio.wait_for(
                client_reader.readuntil(b"\r\n\r\n"), timeout=15
            )
            if len(request_head) > 128 * 1024:
                return
            lines = request_head.decode("latin-1").split("\r\n")
            if not lines or not lines[0].strip():
                return
            method = lines[0].split(" ", 1)[0].upper()
            forwarded = [lines[0]]
            for line in lines[1:]:
                if not line:
                    continue
                name = line.split(":", 1)[0].strip().lower()
                if name in {"proxy-authorization", "proxy-connection"}:
                    continue
                forwarded.append(line)
            forwarded.extend([self._auth_header, "Proxy-Connection: keep-alive", "", ""])

            upstream_reader, upstream_writer = await self._open_upstream()
            upstream_writer.write("\r\n".join(forwarded).encode("latin-1"))
            await upstream_writer.drain()

            if method == "CONNECT":
                response_head = await asyncio.wait_for(
                    upstream_reader.readuntil(b"\r\n\r\n"), timeout=20
                )
                client_writer.write(response_head)
                await client_writer.drain()
                status_line = response_head.split(b"\r\n", 1)[0]
                if b" 200 " not in status_line:
                    return

            await self._relay_bidirectional(
                client_reader,
                client_writer,
                upstream_reader,
                upstream_writer,
            )
        except (asyncio.IncompleteReadError, ConnectionError, OSError, asyncio.TimeoutError):
            pass
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with suppress(Exception):
                    await upstream_writer.wait_closed()
            client_writer.close()
            with suppress(Exception):
                await client_writer.wait_closed()

    @staticmethod
    async def _relay_bidirectional(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            while True:
                data = await reader.read(64 * 1024)
                if not data:
                    return
                writer.write(data)
                await writer.drain()

        tasks = {
            asyncio.create_task(pump(client_reader, upstream_writer)),
            asyncio.create_task(pump(upstream_reader, client_writer)),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done | pending:
            with suppress(asyncio.CancelledError, ConnectionError, OSError):
                await task

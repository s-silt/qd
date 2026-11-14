"""Chromium's workbench network boundary. Each connection is pinned to a public IP."""

import asyncio
import base64
import hmac
import ipaddress
import secrets
import socket
from contextlib import suppress
from urllib.parse import urlsplit

from libs.security import _ip_is_blocked


async def public_addresses(host, port):
    from libs.playwright_capture import PLAYWRIGHT_ALLOW_HOSTS

    host = (host or "").lower().rstrip(".")
    allowed = [value.lower().rstrip(".") for value in PLAYWRIGHT_ALLOW_HOSTS]
    if (
        not host
        or port not in (80, 443)
        or (allowed and not any(host == value or host.endswith("." + value) for value in allowed))
    ):
        raise ValueError("目标不在工作台允许的站点范围")
    infos = await asyncio.wait_for(
        asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 5
    )
    addresses = list(dict.fromkeys((info[0], info[4][0]) for info in infos))
    if not addresses or any(
        _ip_is_blocked(ipaddress.ip_address(ip), public_only=True) for _, ip in addresses
    ):
        raise ValueError("工作台仅连接公开的网站地址")
    return addresses


class BrowserEgress:
    def __init__(self):
        self.password = secrets.token_urlsafe(24)
        self.authorization = b"Basic " + base64.b64encode(f"qd:{self.password}".encode())
        self.server = None
        self.clients = set()

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=32768)
        port = self.server.sockets[0].getsockname()[1]
        return dict(
            server=f"http://127.0.0.1:{port}", username="qd", password=self.password, bypass="<-loopback>"
        )

    async def close(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        clients = list(self.clients)
        for client in clients:
            client.cancel()
        await asyncio.gather(*clients, return_exceptions=True)

    @staticmethod
    async def _pipe(reader, writer):
        while chunk := await asyncio.wait_for(reader.read(65536), 180):
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()

    async def _handle(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        upstream = None
        pipes = []
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            request_line, *lines = head[:-4].split(b"\r\n")
            method, target, _ = request_line.decode("ascii").split(" ")
            headers = [line.split(b":", 1) for line in lines]
            auth = next((v.strip() for k, v in headers if k.lower() == b"proxy-authorization"), b"")
            if not hmac.compare_digest(auth, self.authorization):
                writer.write(
                    b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="QD"\r\n'
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n"
                )
                await writer.drain()
                return

            connect = method == "CONNECT"
            url = urlsplit("https://" + target if connect else target)
            if not url.hostname or url.username is not None or url.scheme != ("https" if connect else "http"):
                raise ValueError("invalid proxy target")
            port = 443 if connect else 80
            if url.port not in (None, port):
                raise ValueError("invalid proxy port")
            for family, address in await public_addresses(url.hostname, port):
                try:
                    remote, upstream = await asyncio.wait_for(
                        asyncio.open_connection(address, port, family=family, flags=socket.AI_NUMERICHOST), 4
                    )
                    break
                except (OSError, asyncio.TimeoutError):
                    continue
            if upstream is None:
                raise OSError("unreachable")

            if connect:
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
            else:
                path = url.path or "/"
                if url.query:
                    path += "?" + url.query
                upstream.write(
                    f"{method} {path} HTTP/1.1\r\nHost: {url.netloc}\r\nConnection: close\r\n".encode("ascii")
                )
                for key, value in headers:
                    if key.lower() not in (
                        b"host",
                        b"connection",
                        b"proxy-connection",
                        b"proxy-authorization",
                        b"expect",
                    ):
                        upstream.write(key + b":" + value + b"\r\n")
                upstream.write(b"\r\n")
                await upstream.drain()
            pipes = [
                asyncio.create_task(self._pipe(reader, upstream)),
                asyncio.create_task(self._pipe(remote, writer)),
            ]
            done, _ = await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
            if pipes[0] in done and not pipes[0].exception():
                await pipes[1]
        except (
            OSError,
            ValueError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
        ):
            if upstream is None:
                with suppress(OSError):
                    writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
        finally:
            for pipe in pipes:
                pipe.cancel()
            if upstream is not None:
                upstream.close()
            writer.close()
            self.clients.discard(task)
            await asyncio.gather(*pipes, return_exceptions=True)

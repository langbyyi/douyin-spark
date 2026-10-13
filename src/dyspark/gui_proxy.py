"""Restore the external port only from an authenticated proxy connection."""

from __future__ import annotations

from urllib.parse import urlsplit

from starlette.types import ASGIApp, Receive, Scope, Send
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from dyspark.gui_auth import AuthError
from dyspark.gui_origin import parse_origin


class PanelProxyHeadersMiddleware(ProxyHeadersMiddleware):
    def __init__(self, app: ASGIApp, trusted_hosts: str = "127.0.0.1") -> None:
        if "*" in {host.strip() for host in trusted_hosts.split(",")}:
            raise AuthError("面板可信代理必须为实际来源地址，FORWARDED_ALLOW_IPS 不能设为 *")
        super().__init__(app, trusted_hosts=trusted_hosts)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        peer = scope.get("client")
        if scope["type"] == "http" and peer and peer[0] in self.trusted_hosts:
            hosts = [value for key, value in scope["headers"] if key == b"host"]
            ports = [value for key, value in scope["headers"] if key == b"x-forwarded-port"]
            if len(hosts) == len(ports) == 1:
                host = hosts[0].decode("latin1")
                port = ports[0].strip()
                if (
                    parse_origin(f"http://{host}") is not None
                    and urlsplit(f"http://{host}").port is None
                    and 0 < len(port) <= 5
                    and port.isdigit()
                    and 0 < int(port) < 65536
                ):
                    scope = dict(scope)
                    scope["headers"] = [
                        (key, value + b":" + port) if key == b"host" else (key, value)
                        for key, value in scope["headers"]
                    ]
        # Do this after checking the socket peer: X-Forwarded-For changes scope.client.
        await super().__call__(scope, receive, send)

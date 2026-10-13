"""Tiny helpers shared by the short-lived HTTP endpoints used during login."""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Server6(_Server):
    address_family = socket.AF_INET6


def bind_server(
    bind: str, port: int, handler: type[http.server.BaseHTTPRequestHandler]
) -> http.server.ThreadingHTTPServer:
    """Bind an HTTP server on an IPv4 or IPv6 address; raises ``OSError`` when taken."""
    server_class = _Server6 if ":" in bind else _Server
    return server_class((bind, port), handler)


@contextmanager
def serving(server: http.server.ThreadingHTTPServer) -> Iterator[None]:
    """Serve in a daemon thread until the block ends."""
    thread = threading.Thread(target=server.serve_forever, name="dyspark-httpd", daemon=True)
    thread.start()
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def lan_address() -> str:
    """Best guess of this machine's address on the local network (no packet is sent).

    Skips Clash/TUN ranges such as 198.18.0.0/15 that UDP-connect probes often return.
    """
    import ipaddress

    def _usable(host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False
        if ip.version != 4 or ip.is_loopback or ip.is_link_local or ip.is_multicast:
            return False
        # RFC 2544 / Clash fake-IP / CGNAT-ish probe answers
        return ip not in ipaddress.ip_network("198.18.0.0/15")

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("223.5.5.5", 53))
        host = probe.getsockname()[0]
        if _usable(host):
            return host
    except OSError:
        pass
    finally:
        probe.close()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            host = info[4][0]
            if _usable(host):
                return host
    except OSError:
        pass
    return "127.0.0.1"

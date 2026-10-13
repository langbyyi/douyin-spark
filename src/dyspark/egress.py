"""Public egress IP discovery and comparison.

The lookup runs inside the browser context so the answer reflects exactly the
route (direct or proxy) the Douyin pages will use.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

log = logging.getLogger(__name__)

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b[0-9a-fA-F]{0,4}(?::[0-9a-fA-F]{0,4}){2,7}\b")


class EgressError(RuntimeError):
    """No echo service returned a usable address."""


def parse_ip(text: str) -> str | None:
    """Extract the first public IP address from an echo service response.

    Handles JSON (``{"ip": ...}``), plain text (``1.2.3.4``) and prose such as
    ``当前 IP：1.2.3.4  来自于：中国``. IPv4 is preferred when both appear.
    """
    text = (text or "").strip()
    try:
        payload = json.loads(text)
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("ip", "query", "origin", "address"):
            if isinstance(payload.get(key), str):
                text = payload[key]
                break
    for pattern in (_IPV4, _IPV6):
        for candidate in pattern.findall(text):
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.is_global:
                return str(address)
    return None


def ip_matches(recorded: str, current: str, policy: str = "exact") -> bool:
    """Compare two addresses. ``subnet`` accepts the same /24 (IPv4) or /48 (IPv6)."""
    if recorded == current:
        return True
    if policy != "subnet":
        return False
    try:
        a, b = ipaddress.ip_address(recorded), ipaddress.ip_address(current)
    except ValueError:
        return False
    if a.version != b.version:
        return False
    prefix = 24 if a.version == 4 else 48
    return b in ipaddress.ip_network(f"{a}/{prefix}", strict=False)


def lookup(context: BrowserContext, urls: Iterable[str], timeout_ms: int = 15_000) -> str:
    """Ask the echo services in order, through the browser, until one yields an IPv4.

    An IPv6 answer is kept as a fallback only: dual-stack services may answer over
    IPv6 one day and IPv4 the next, which would look like an environment change.
    """
    page = context.new_page()
    ipv6: str | None = None
    tried: list[str] = []
    try:
        for url in urls:
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if response is not None and not response.ok:
                    raise RuntimeError(f"http {response.status}")
                body = response.text() if response else page.inner_text("body")
            except Exception as exc:  # network errors vary by platform
                log.debug("egress echo %s failed: %s", url, exc)
                tried.append(f"{url} ({str(exc).splitlines()[0][:60] if str(exc) else exc})")
                continue
            address = parse_ip(body)
            if address and ipaddress.ip_address(address).version == 4:
                log.info("egress IP via %s: %s", url, address)
                return address
            if address:
                ipv6 = ipv6 or address
            tried.append(f"{url} (no IPv4 in answer)")
    finally:
        try:
            page.close()
        except Exception as exc:
            log.debug("closing the echo tab failed: %s", exc)
    if ipv6:
        log.info("egress IP (IPv6 only): %s", ipv6)
        return ipv6
    raise EgressError("无法确定出口 IP，所有查询服务均失败：" + "；".join(tried))

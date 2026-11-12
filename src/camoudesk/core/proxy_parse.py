"""Parse the proxy formats providers hand out into canonical ``ProxyConfig`` objects.

Accepted, one per line (blank lines and ``#`` comments are skipped):

* ``scheme://user:pass@host:port`` / ``scheme://host:port``
* ``user:pass@host:port`` and ``host:port@user:pass``
* ``host:port:user:pass`` (the password may itself contain ``:``)
* ``host:port``
* IPv6 hosts in brackets, e.g. ``[2001:db8::1]:8080``

Lines without a scheme use ``default_scheme``.
"""

from dataclasses import dataclass
import re
from typing import List, Tuple
from urllib.parse import quote

from camoudesk.models.profile import ProxyConfig

_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*)://(.*)$")
_HOSTPORT_RE = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[^\s:@/\[\]]+):(\d{1,5})$")


@dataclass(frozen=True)
class ParseError:
    line_no: int
    line: str
    error: str


def _build(scheme: str, host: str, port: str, user: str = "", password: str = "") -> ProxyConfig:
    auth = ""
    if user or password:
        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    return ProxyConfig.from_url(f"{scheme}://{auth}{host}:{port}")


def parse_proxy_line(line: str, default_scheme: str = "http") -> ProxyConfig:
    """Parse one proxy line; raises ValueError with a readable message."""
    text = line.strip()
    if not text:
        raise ValueError("Empty line")

    scheme = default_scheme.lower()
    m = _SCHEME_RE.match(text)
    if m:
        scheme, text = m.group(1).lower(), m.group(2)

    if "@" in text:
        left, right = text.rsplit("@", 1)
        if _HOSTPORT_RE.match(right):  # user:pass@host:port
            user, _, password = left.partition(":")
            host, port = _HOSTPORT_RE.match(right).groups()  # type: ignore[union-attr]
            return _build(scheme, host, port, user, password)
        if _HOSTPORT_RE.match(left):  # host:port@user:pass
            host, port = _HOSTPORT_RE.match(left).groups()  # type: ignore[union-attr]
            user, _, password = right.partition(":")
            return _build(scheme, host, port, user, password)
        raise ValueError("Could not find host:port around '@'")

    if text.startswith("["):  # IPv6 host, optionally followed by :user:pass
        end = text.find("]")
        if end < 0:
            raise ValueError("Unclosed '[' in IPv6 address")
        host = text[: end + 1]
        rest = text[end + 1 :].lstrip(":").split(":", 2)
        if not rest or not rest[0].isdigit():
            raise ValueError("Missing port")
        port = rest[0]
        user = rest[1] if len(rest) > 1 else ""
        password = rest[2] if len(rest) > 2 else ""
        return _build(scheme, host, port, user, password)

    parts = text.split(":", 3)
    if len(parts) == 2:
        host, port = parts
        if not port.isdigit():
            raise ValueError("Port must be a number")
        return _build(scheme, host, port)
    if len(parts) == 4:
        host, port, user, password = parts
        if not port.isdigit():
            raise ValueError("Expected host:port:user:pass with a numeric port")
        return _build(scheme, host, port, user, password)
    raise ValueError("Unrecognised format; use host:port, host:port:user:pass or user:pass@host:port")


def parse_proxy_list(text: str, default_scheme: str = "http") -> Tuple[List[ProxyConfig], List[ParseError]]:
    proxies: List[ProxyConfig] = []
    errors: List[ParseError] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            proxies.append(parse_proxy_line(line, default_scheme))
        except ValueError as exc:
            errors.append(ParseError(i, line, str(exc)))
    return proxies, errors

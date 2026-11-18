"""Proxy strings: parsing, normalisation and masking."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urlsplit

SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")

_HOST_PORT = re.compile(r"^(\[[0-9a-fA-F:.]+\]|[^\s:@/\[\]]+):(\d{1,5})$")


class ProxyParseError(ValueError):
    """A proxy string could not be parsed."""


@dataclass(frozen=True)
class Proxy:
    """One proxy endpoint.

    ``url`` is the normalised form ``scheme://[user:pass@]host:port`` with the
    credentials percent-encoded, which is what requests, httpx, aiohttp and
    Scrapy expect.
    """

    scheme: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    label: str | None = field(default=None, compare=False)

    @property
    def url(self) -> str:
        return self._format(mask=False)

    @property
    def masked(self) -> str:
        """The URL with the password replaced by ``*****``, safe for logs."""
        return self._format(mask=True)

    @property
    def server(self) -> str:
        """``scheme://host:port`` without credentials."""
        return f"{self.scheme}://{self.host}:{self.port}"

    def _format(self, *, mask: bool) -> str:
        auth = ""
        if self.username is not None:
            auth = quote(self.username, safe="")
            if self.password is not None:
                auth += ":" + ("*****" if mask else quote(self.password, safe=""))
            auth += "@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def __str__(self) -> str:
        return self.masked

    def __repr__(self) -> str:
        return f"Proxy({self.masked!r})"


def parse_proxy(text: str, default_scheme: str = "http") -> Proxy:
    """Parse one proxy string.

    Accepted forms::

        host:port
        user:pass@host:port
        host:port:user:pass
        scheme://[user:pass@]host:port

    ``scheme`` is one of http, https, socks4, socks4a, socks5, socks5h. Strings
    without a scheme use ``default_scheme``. Credentials may be percent-encoded.
    """
    raw = text.strip()
    if not raw:
        raise ProxyParseError("empty proxy string")
    if default_scheme not in SCHEMES:
        raise ProxyParseError(f"unknown default scheme {default_scheme!r}")

    if "://" in raw:
        parts = urlsplit(raw)
        scheme = parts.scheme.lower()
        if scheme not in SCHEMES:
            raise ProxyParseError(f"unsupported scheme {scheme!r} in {_safe(raw)}")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ProxyParseError(f"a proxy URL has no path or query: {_safe(raw)}")
        try:
            port = parts.port
        except ValueError as exc:
            raise ProxyParseError(f"invalid port in {_safe(raw)}") from exc
        host = parts.hostname
        if not host or port is None:
            raise ProxyParseError(f"host and port are required: {_safe(raw)}")
        if ":" in host:
            host = f"[{host}]"
        user = unquote(parts.username) if parts.username is not None else None
        password = unquote(parts.password) if parts.password is not None else None
        return _build(scheme, host, port, user, password, raw)

    scheme = default_scheme
    pieces = raw.split(":")
    if len(pieces) >= 4 and pieces[1].isdigit() and "@" not in pieces[0]:
        host, port_text, user = pieces[0], pieces[1], pieces[2]
        password = ":".join(pieces[3:])
        return _build(scheme, host, int(port_text), unquote(user), unquote(password), raw)

    if "@" in raw:
        creds, _, hostport = raw.rpartition("@")
        user, sep, password = creds.partition(":")
        match = _HOST_PORT.match(hostport)
        if not match:
            raise ProxyParseError(f"expected user:pass@host:port, got {_safe(raw)}")
        return _build(
            scheme,
            match.group(1),
            int(match.group(2)),
            unquote(user),
            unquote(password) if sep else None,
            raw,
        )

    match = _HOST_PORT.match(raw)
    if match:
        return _build(scheme, match.group(1), int(match.group(2)), None, None, raw)

    raise ProxyParseError(f"unrecognised proxy format: {_safe(raw)}")


def _build(  # noqa: PLR0917 - private helper, every caller passes all six
    scheme: str, host: str, port: int, user: str | None, password: str | None, raw: str
) -> Proxy:
    if not 0 < port < 65536:
        raise ProxyParseError(f"port out of range in {_safe(raw)}")
    if user == "":
        user = None
        password = None
    return Proxy(scheme=scheme, host=host.lower(), port=port, username=user, password=password)


def _safe(raw: str) -> str:
    """Hide anything that looks like a password before putting input in an error."""
    if "@" in raw:
        head, _, tail = raw.rpartition("@")
        if ":" in head:
            head = head.rsplit(":", 1)[0] + ":*****"
        return f"{head}@{tail}"
    pieces = raw.split(":")
    if len(pieces) >= 4:
        return ":".join([*pieces[:3], "*****"])
    return raw


def parse_lines(lines: Iterable[str], default_scheme: str = "http") -> list[Proxy]:
    """Parse a proxy list, one per line. Blank lines and ``#`` comments are skipped.

    Raises :class:`ProxyParseError` naming the first bad line (1-based).
    Duplicates are kept once, in first-seen order.
    """
    seen: set[Proxy] = set()
    out: list[Proxy] = []
    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        try:
            proxy = parse_proxy(text, default_scheme)
        except ProxyParseError as exc:
            raise ProxyParseError(f"line {number}: {exc}") from None
        if proxy not in seen:
            seen.add(proxy)
            out.append(proxy)
    return out


def load_file(path: str, default_scheme: str = "http") -> list[Proxy]:
    """Read a proxy list file (UTF-8, one proxy per line)."""
    with open(path, encoding="utf-8") as handle:
        return parse_lines(handle, default_scheme)

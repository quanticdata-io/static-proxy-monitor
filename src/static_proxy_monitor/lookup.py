"""Where an exit IP is, according to several sources.

- the **IP echo** is called *through the proxy* and answers with the exit IP
  and the location its database gives (Cloudflare for the default echo);
- each **geolocation source** is called *directly* (not through the proxy)
  with the exit IP and answers with country, city, ASN and operator.

Lookups are cached per IP for the whole run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

DEFAULT_ECHO_URL = "https://api.quanticdata.io/v1/public/ip-echo"
QUANTICDATA_API = "https://api.quanticdata.io/v1"


@dataclass(frozen=True)
class Exit:
    """What the IP echo saw."""

    ip: str
    country: str | None = None
    region: str | None = None
    city: str | None = None


@dataclass(frozen=True)
class Network:
    """What the network lookup knows about an IP."""

    asn: str | None = None  # "AS21928"
    operator: str | None = None  # "T-Mobile USA, Inc."
    country: str | None = None
    city: str | None = None

    @property
    def label(self) -> str:
        parts = [p for p in (self.asn, self.operator) if p]
        return " ".join(parts) if parts else "-"


def _first(data: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def parse_echo(body: bytes | str, content_type: str = "") -> Exit:
    """Read an IP echo answer.

    Understands the QuanticData envelope (``{"type": "response", "payload":
    {...}}``), flat JSON with the common field names (``ip``, ``origin``,
    ``query``; ``countryCode``, ``country_code``, ``country``; ``region``,
    ``regionName``; ``city``) and plain text holding one IP.
    """
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    stripped = text.strip()
    if stripped.startswith("{"):
        import json

        data = json.loads(stripped)
        if isinstance(data.get("payload"), dict):
            data = data["payload"]
        ip = _first(data, "ip", "origin", "query", "address")
        if not ip:
            raise ValueError("the echo answer has no IP field")
        ip = ip.split(",")[0].strip()
        country = _first(data, "countryCode", "country_code", "country")
        return Exit(
            ip=ip, country=country, region=_first(data, "region", "regionName"), city=_first(data, "city")
        )
    match = re.search(r"[0-9a-fA-F:.]{3,45}", stripped)
    if not match or not (("." in match.group(0)) or (":" in match.group(0))):
        raise ValueError(f"cannot find an IP in the echo answer: {stripped[:80]!r}")
    return Exit(ip=match.group(0))


_ASN = re.compile(r"^(AS\d+)\s*(.*)$", re.I)


def parse_network(data: dict[str, Any]) -> Network:
    """Read a network lookup answer.

    Understands the QuanticData ``ip-info`` payload (``as``: ``"AS15169
    Google LLC"``, ``isp``, ``org``), ipinfo.io (``org``: ``"AS15169 Google
    LLC"``) and flat ``asn`` / ``as_name`` / ``isp`` fields.
    """
    if isinstance(data.get("payload"), dict):
        data = data["payload"]
    asn: str | None = None
    operator: str | None = None
    for key in ("as", "org", "asn"):
        value = data.get(key)
        if isinstance(value, dict):  # some services nest {"asn": "AS1", "name": "..."}
            asn = asn or _first(value, "asn")
            operator = operator or _first(value, "name", "domain")
            continue
        if isinstance(value, int):
            asn = asn or f"AS{value}"
            continue
        if isinstance(value, str) and value:
            match = _ASN.match(value.strip())
            if match:
                asn = asn or match.group(1).upper()
                operator = operator or (match.group(2).strip() or None)
            elif key == "org":
                operator = operator or value.strip()
    operator = _first(data, "isp", "carrier", "as_name", "asname") or operator
    country = _first(data, "countryCode", "country_code", "country")
    return Network(asn=asn, operator=operator, country=country, city=_first(data, "city"))


class NetworkLookup(Protocol):
    def lookup(self, ip: str) -> Network: ...


class NoLookup:
    """Skip network lookups: only the echo's IP and location are reported."""

    def lookup(self, ip: str) -> Network:
        return Network()


class HTTPLookup:
    """A JSON lookup service called directly, with the IP in the URL.

    ``url_template`` contains ``{ip}``, for example
    ``https://ipinfo.io/{ip}/json``. ``headers`` are sent with every call.
    """

    def __init__(
        self,
        url_template: str,
        headers: dict[str, str] | None = None,
        timeout: float = 15.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if "{ip}" not in url_template:
            raise ValueError("the lookup URL needs an {ip} placeholder")
        self.url_template = url_template
        self._client = httpx.Client(headers=headers or {}, timeout=timeout, transport=transport)
        self._cache: dict[str, Network] = {}

    def lookup(self, ip: str) -> Network:
        cached = self._cache.get(ip)
        if cached is not None:
            return cached
        try:
            response = self._client.get(self.url_template.replace("{ip}", ip))
            network = parse_network(response.json()) if response.status_code == 200 else Network()
        except (httpx.HTTPError, ValueError):
            network = Network()
        self._cache[ip] = network
        return network

    def close(self) -> None:
        self._client.close()


def quanticdata_lookup(
    api_key: str | None = None, base_url: str | None = None, transport: httpx.BaseTransport | None = None
) -> HTTPLookup:
    """``GET /public/proxies/ip-info?ip=`` of the QuanticData API (free, needs an API key)."""
    key = api_key or os.environ.get("QUANTICDATA_API_KEY", "")
    if not key:
        raise ValueError("QUANTICDATA_API_KEY is not set (needed by --lookup quanticdata)")
    base = (base_url or os.environ.get("QUANTICDATA_API_BASE") or QUANTICDATA_API).rstrip("/")
    return HTTPLookup(
        base + "/public/proxies/ip-info?ip={ip}",
        headers={"Authorization": f"Bearer {key}"},
        transport=transport,
    )


def ipinfo_lookup(token: str | None = None, transport: httpx.BaseTransport | None = None) -> HTTPLookup:
    """ipinfo.io; ``IPINFO_TOKEN`` is optional and raises its rate limit."""
    token = token or os.environ.get("IPINFO_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return HTTPLookup("https://ipinfo.io/{ip}/json", headers=headers, transport=transport)

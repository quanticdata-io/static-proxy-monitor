"""Check a fleet of static proxies: identity, location agreement, target sites,
and what changed since the last run."""

from __future__ import annotations

import ipaddress
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from .lookup import DEFAULT_ECHO_URL, NetworkLookup, parse_echo
from .proxy import Proxy, ProxyParseError, parse_proxy


@dataclass(frozen=True)
class Entry:
    """One static proxy and, when known, the exit IP it must have."""

    proxy: Proxy
    expected_ip: str | None = None

    @property
    def label(self) -> str:
        return self.proxy.masked


def parse_entries(lines: Iterable[str]) -> list[Entry]:
    """``PROXY`` or ``PROXY EXPECTED_IP`` (space, tab or comma separated) per line."""
    out: list[Entry] = []
    for number, line in enumerate(lines, start=1):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.replace(",", " ").split()
        try:
            proxy = parse_proxy(parts[0])
        except ProxyParseError as exc:
            raise ProxyParseError(f"line {number}: {exc}") from None
        expected = None
        if len(parts) > 1:
            try:
                expected = str(ipaddress.ip_address(parts[1]))
            except ValueError:
                raise ProxyParseError(f"line {number}: {parts[1]!r} is not an IP address") from None
        out.append(Entry(proxy, expected))
    return out


@dataclass
class GeoView:
    source: str
    country: str | None = None
    city: str | None = None
    asn: str | None = None
    operator: str | None = None


@dataclass
class TargetCheck:
    url: str
    ok: bool
    status: int | None = None
    latency_ms: float | None = None
    error: str | None = None


@dataclass
class Result:
    proxy: str
    reachable: bool
    # Identifies the proxy across runs: its expected IP when known (connection
    # hosts of some static plans can change), else the masked proxy string.
    key: str = ""
    exit_ip: str | None = None
    expected_ip: str | None = None
    latency_ms: float | None = None
    geo: list[GeoView] = field(default_factory=list)
    targets: list[TargetCheck] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def countries(self) -> dict[str, str]:
        return {g.source: g.country.upper() for g in self.geo if g.country}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


ClientFactory = Callable[[Proxy, float], httpx.Client]


def default_client(proxy: Proxy, timeout: float) -> httpx.Client:
    # A fresh connection per check, no redirects followed silently.
    return httpx.Client(proxy=proxy.url, timeout=timeout, limits=httpx.Limits(max_keepalive_connections=0))


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def check_entry(
    entry: Entry,
    *,
    sources: Mapping[str, NetworkLookup],
    echo_url: str = DEFAULT_ECHO_URL,
    targets: Sequence[str] = (),
    expect_country: str | None = None,
    timeout: float = 20.0,
    client_factory: ClientFactory = default_client,
) -> Result:
    result = Result(
        proxy=entry.label,
        reachable=False,
        key=entry.expected_ip or entry.label,
        expected_ip=entry.expected_ip,
    )
    with client_factory(entry.proxy, timeout) as client:
        started = time.perf_counter()
        try:
            response = client.get(echo_url, headers={"Cache-Control": "no-cache"})
            if response.status_code != 200:
                raise ValueError(f"IP echo answered HTTP {response.status_code}")
            exit_ = parse_echo(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            result.error = _describe(exc)
            result.problems.append(f"unreachable: {result.error}")
            return result
        result.reachable = True
        result.latency_ms = round((time.perf_counter() - started) * 1000, 1)
        result.exit_ip = exit_.ip
        result.geo.append(GeoView("echo", exit_.country, exit_.city))
        for name, lookup in sources.items():
            net = lookup.lookup(exit_.ip)
            result.geo.append(GeoView(name, net.country, net.city, net.asn, net.operator))
        for url in targets:
            t0 = time.perf_counter()
            try:
                r = client.get(url)
                ok = 200 <= r.status_code < 400
                result.targets.append(
                    TargetCheck(url, ok, r.status_code, round((time.perf_counter() - t0) * 1000, 1))
                )
            except httpx.HTTPError as exc:
                result.targets.append(TargetCheck(url, False, error=_describe(exc)))
    _judge(result, expect_country)
    return result


def _judge(result: Result, expect_country: str | None) -> None:
    if result.expected_ip and result.exit_ip and result.expected_ip != result.exit_ip:
        result.problems.append(f"exit IP {result.exit_ip}, expected {result.expected_ip}")
    countries = result.countries
    if len(set(countries.values())) > 1:
        seen = ", ".join(f"{k} {v}" for k, v in countries.items())
        result.problems.append(f"sources disagree on the country: {seen}")
    if expect_country:
        want = expect_country.upper()
        wrong = {k: v for k, v in countries.items() if v != want}
        if wrong:
            result.problems.append(
                f"country {', '.join(f'{v} ({k})' for k, v in wrong.items())}, expected {want}"
            )
    for t in result.targets:
        if not t.ok:
            result.problems.append(f"{t.url}: {t.error or f'HTTP {t.status}'}")


def check_all(entries: Sequence[Entry], *, concurrency: int = 10, **kwargs: Any) -> list[Result]:
    """Check entries in parallel; results keep the input order."""
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        return list(pool.map(lambda e: check_entry(e, **kwargs), entries))


def changes(previous: Mapping[str, Any] | None, results: Sequence[Result]) -> list[str]:
    """What changed since the run saved in ``previous`` (the JSON this tool writes with --state)."""
    if not previous:
        return []
    before = {r.get("key") or r["proxy"]: r for r in previous.get("results", [])}
    out: list[str] = []
    for r in results:
        old = before.get(r.key)
        if old is None:
            out.append(f"{r.proxy}: new in this run")
            continue
        if old.get("exit_ip") != r.exit_ip:
            out.append(f"{r.proxy}: exit IP {old.get('exit_ip') or 'none'} -> {r.exit_ip or 'none'}")
        if bool(old.get("reachable")) != r.reachable:
            out.append(f"{r.proxy}: {'reachable again' if r.reachable else 'no longer reachable'}")
        old_countries = {
            g["source"]: (g.get("country") or "").upper() for g in old.get("geo", []) if g.get("country")
        }
        for source, country in r.countries.items():
            if source in old_countries and old_countries[source] != country:
                out.append(f"{r.proxy}: {source} now places it in {country} (was {old_countries[source]})")
        old_targets = {t["url"]: t for t in old.get("targets", [])}
        for t in r.targets:
            prev = old_targets.get(t.url)
            if prev is not None and bool(prev.get("ok")) != t.ok:
                state = "now passes" if t.ok else f"now fails ({t.error or f'HTTP {t.status}'})"
                out.append(f"{r.proxy}: {t.url} {state}")
    current = {r.key for r in results}
    out.extend(f"{name}: gone from the list" for name in before if name not in current)
    return out

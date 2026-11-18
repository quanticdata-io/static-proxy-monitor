from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from conftest import ECHO_URL, FakeStatic
from static_proxy_monitor import Entry, changes, check_all, check_entry, parse_entries
from static_proxy_monitor.cli import main
from static_proxy_monitor.lookup import HTTPLookup
from static_proxy_monitor.proxy import ProxyParseError, parse_proxy

MakeStatic = Callable[..., FakeStatic]


def sources(base: str, *names: str) -> dict[str, HTTPLookup]:
    return {name: HTTPLookup(f"{base}/{name}/{{ip}}") for name in names}


def test_parse_entries() -> None:
    entries = parse_entries(
        [
            "# fleet",
            "",
            "u:p@gw.example.net:8001 203.0.113.10",
            "gw.example.net:8002:u:p,203.0.113.11",
            "gw.example.net:8003",
        ]
    )
    assert [e.expected_ip for e in entries] == ["203.0.113.10", "203.0.113.11", None]
    assert entries[1].proxy.username == "u"
    with pytest.raises(ProxyParseError, match="line 1"):
        parse_entries(["u:p@h:1 not-an-ip"])


def test_healthy_proxy(make_static: MakeStatic, geo_base: str) -> None:
    fake = make_static("203.0.113.10")
    r = check_entry(
        Entry(parse_proxy(fake.url), "203.0.113.10"),
        sources=sources(geo_base, "db1", "db2"),
        echo_url=ECHO_URL,
        targets=["http://shop.test/ok"],
        expect_country="us",
        timeout=5,
    )
    assert r.reachable
    assert r.problems == []
    assert r.countries == {"echo": "US", "db1": "US", "db2": "US"}
    assert r.geo[1].asn == "AS64500"
    assert r.targets[0].status == 200
    assert "secret" not in json.dumps(r.to_dict())


def test_problems_are_named(make_static: MakeStatic, geo_base: str) -> None:
    fake = make_static("203.0.113.11")
    r = check_entry(
        Entry(parse_proxy(fake.url), "203.0.113.99"),
        sources=sources(geo_base, "db1", "db2"),
        echo_url=ECHO_URL,
        targets=["http://shop.test/blocked"],
        expect_country="US",
        timeout=5,
    )
    assert r.problems == [
        "exit IP 203.0.113.11, expected 203.0.113.99",
        "sources disagree on the country: echo US, db1 US, db2 NL",
        "country NL (db2), expected US",
        "http://shop.test/blocked: HTTP 403",
    ]


def test_unreachable_proxy() -> None:
    def refuse(proxy: object, timeout: float) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        return httpx.Client(transport=httpx.MockTransport(handler))

    r = check_entry(Entry(parse_proxy("gw.example.net:9")), sources={}, client_factory=refuse)
    assert not r.reachable
    assert r.problems == ["unreachable: ConnectError: connection refused"]


def test_check_all_keeps_order(make_static: MakeStatic) -> None:
    fakes = [make_static(ip) for ip in ("203.0.113.10", "198.51.100.5", "203.0.113.11")]
    results = check_all([Entry(parse_proxy(f.url)) for f in fakes], sources={}, echo_url=ECHO_URL, timeout=5)
    assert [r.exit_ip for r in results] == ["203.0.113.10", "198.51.100.5", "203.0.113.11"]


def test_changes_between_runs(make_static: MakeStatic, geo_base: str) -> None:
    fake = make_static("203.0.113.10")
    entry = Entry(parse_proxy(fake.url))
    kwargs = {
        "sources": sources(geo_base, "db2"),
        "echo_url": ECHO_URL,
        "targets": ["http://shop.test/ok"],
        "timeout": 5,
    }
    first = [check_entry(entry, **kwargs)]  # type: ignore[arg-type]
    saved = {"results": [r.to_dict() for r in first]}
    fake.exit_ip = "203.0.113.11"  # the dedicated IP was swapped, and db2 places the new one elsewhere
    second = [check_entry(entry, **kwargs)]  # type: ignore[arg-type]
    label = entry.label
    assert changes(saved, second) == [
        f"{label}: exit IP 203.0.113.10 -> 203.0.113.11",
        f"{label}: db2 now places it in NL (was US)",
    ]
    assert changes(None, second) == []
    assert changes(saved, []) == [f"{label}: gone from the list"]


def run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    return main(argv, out=out, err=err), out.getvalue(), err.getvalue()


def test_cli_table_state_and_fail(make_static: MakeStatic, geo_base: str, tmp_path: Path) -> None:
    good, drifted = make_static("203.0.113.10"), make_static("203.0.113.11")
    listing = tmp_path / "fleet.txt"
    listing.write_text(f"{good.url} 203.0.113.10\n{drifted.url} 203.0.113.11\n", encoding="utf-8")
    state = tmp_path / "state.json"
    base = [
        "-f",
        str(listing),
        "--echo-url",
        ECHO_URL,
        "--geo",
        f"{geo_base}/db1/{{ip}}",
        "--geo",
        f"{geo_base}/db2/{{ip}}",
        "--state",
        str(state),
        "--timeout",
        "5",
    ]
    code, out, err = run([*base, "--fail-on-problems"])
    assert code == 1, err
    assert "2 proxies, 2 reachable, 1 with problems" in out
    assert "sources disagree on the country" in out
    assert "secret" not in out
    assert state.exists()

    drifted.exit_ip = "198.51.100.5"
    code, out, _ = run(base)
    assert code == 0
    assert "changes since the last run:" in out
    assert "exit IP 203.0.113.11 -> 198.51.100.5" in out


def test_cli_json_and_csv(make_static: MakeStatic) -> None:
    fake = make_static("203.0.113.10")
    code, out, _ = run(
        [fake.url, "--echo-url", ECHO_URL, "--no-geo", "--json", "--target", "http://shop.test/ok"]
    )
    doc = json.loads(out)
    assert code == 0
    assert doc["summary"] == {"proxies": 1, "reachable": 1, "with_problems": 0}
    assert doc["results"][0]["targets"][0]["status"] == 200
    code, out, _ = run([fake.url, "--echo-url", ECHO_URL, "--no-geo", "--csv"])
    assert out.splitlines()[0].startswith("proxy,reachable,exit_ip")
    assert ",True,203.0.113.10," in out.splitlines()[1]


def test_cli_quanticdata_uses_ip_addresses(monkeypatch: pytest.MonkeyPatch, make_static: MakeStatic) -> None:
    fake = make_static("203.0.113.10")
    seen: dict[str, object] = {}
    real_client = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        payload = {"proxies": [fake.url], "ipAddresses": ["203.0.113.10"]}
        return httpx.Response(200, json={"type": "response", "payload": payload})

    def fake_client(*args: object, **kwargs: object) -> httpx.Client:
        if "proxy" in kwargs:
            return real_client(*args, **kwargs)  # type: ignore[arg-type]
        return real_client(transport=httpx.MockTransport(handler))

    monkeypatch.setenv("QUANTICDATA_API_KEY", "qd_test_key")
    monkeypatch.setattr(httpx, "Client", fake_client)
    code, out, err = run(
        ["--qd-order", "ord_isp", "--qd-quantity", "5", "--echo-url", ECHO_URL, "--no-geo", "--json"]
    )
    assert code == 0, err
    assert seen["orderId"] == "ord_isp"
    assert seen["quantity"] == 5
    assert json.loads(out)["results"][0]["expected_ip"] == "203.0.113.10"


def test_cli_usage() -> None:
    assert run([])[0] == 2
    assert run(["not a proxy"])[0] == 2

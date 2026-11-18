"""Command line: ``static-proxy-monitor [PROXY ...] [-f FILE] [checks]``."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import IO, Any

import httpx

from ._version import __version__
from .lookup import DEFAULT_ECHO_URL, HTTPLookup, NetworkLookup, ipinfo_lookup, quanticdata_lookup
from .monitor import Entry, Result, changes, check_all, parse_entries
from .proxy import ProxyParseError, parse_proxy

EXIT_OK, EXIT_PROBLEMS, EXIT_USAGE, EXIT_RUNTIME = 0, 1, 2, 3


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="static-proxy-monitor",
        description="Check static (ISP, dedicated datacenter) proxies: exit IP identity, location agreement "
        "between databases, target sites, and changes since the last run.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("proxies", nargs="*", help="PROXY or PROXY,EXPECTED_IP")
    p.add_argument("-f", "--file", help="list file: one 'PROXY [EXPECTED_IP]' per line")
    p.add_argument(
        "--geo",
        action="append",
        metavar="SOURCE",
        help="geolocation source, repeatable: quanticdata, ipinfo, or a URL with {ip} "
        "(default: quanticdata when QUANTICDATA_API_KEY is set, else ipinfo)",
    )
    p.add_argument("--no-geo", action="store_true", help="only the echo's location, no other source")
    p.add_argument(
        "--target", action="append", default=[], metavar="URL", help="also fetch this URL through each proxy"
    )
    p.add_argument("--expect-country", metavar="CC", help="every source must place each exit in this country")
    p.add_argument("--echo-url", default=DEFAULT_ECHO_URL, help="IP echo requested through each proxy")
    p.add_argument(
        "--state", metavar="FILE", help="compare with the previous run saved here, then save this run"
    )
    p.add_argument("--timeout", type=float, default=20.0, help="seconds per request (20)")
    p.add_argument("-c", "--concurrency", type=int, default=10, help="proxies checked in parallel (10)")
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--csv", action="store_true", help="CSV output, one row per proxy")
    p.add_argument("--fail-on-problems", action="store_true", help="exit 1 when any proxy has a problem")
    qd = p.add_argument_group("QuanticData (needs QUANTICDATA_API_KEY)")
    qd.add_argument("--qd-order", metavar="ORDER_ID", help="check the proxies of this ISP or datacenter plan")
    qd.add_argument(
        "--qd-quantity", type=int, default=100, help="lines to generate (Premium ISP: one per IP)"
    )
    qd.add_argument("--qd-protocol", choices=["http", "socks5"], default="http")
    return p


def _sources(args: argparse.Namespace) -> dict[str, NetworkLookup]:
    if args.no_geo:
        return {}
    chosen = args.geo or ["quanticdata" if os.environ.get("QUANTICDATA_API_KEY") else "ipinfo"]
    out: dict[str, NetworkLookup] = {}
    for name in chosen:
        if name == "quanticdata":
            out["quanticdata"] = quanticdata_lookup()
        elif name == "ipinfo":
            out["ipinfo"] = ipinfo_lookup()
        else:
            host = httpx.URL(name.replace("{ip}", "0")).host or name
            out[host] = HTTPLookup(name)
    return out


def _qd_entries(args: argparse.Namespace) -> list[Entry]:
    key = os.environ.get("QUANTICDATA_API_KEY", "").strip()
    if not key:
        raise ValueError("QUANTICDATA_API_KEY is not set")
    base = (os.environ.get("QUANTICDATA_API_BASE") or "https://api.quanticdata.io/v1").rstrip("/")
    body = {
        "orderId": args.qd_order,
        "quantity": args.qd_quantity,
        "protocol": args.qd_protocol,
        "format": f"{args.qd_protocol}://user:pass@host:port",
    }
    with httpx.Client(timeout=60) as client:
        r = client.post(
            f"{base}/public/proxies/generate", json=body, headers={"Authorization": f"Bearer {key}"}
        )
    try:
        envelope = r.json()
    except ValueError as exc:
        raise RuntimeError(f"QuanticData API: the answer is not JSON (HTTP {r.status_code})") from exc
    if r.status_code == 202 or (envelope.get("payload") or {}).get("pending"):
        raise RuntimeError(
            "QuanticData API: the order is still being provisioned, try again in a few minutes"
        )
    if r.status_code >= 300 or envelope.get("type") != "response":
        raise RuntimeError(f"QuanticData API: {envelope.get('message', 'error')} (HTTP {r.status_code})")
    payload = envelope.get("payload") or {}
    lines = payload.get("proxies") or []
    ips = payload.get("ipAddresses") or []
    expected = ips if len(ips) == len(lines) else [None] * len(lines)
    return [Entry(parse_proxy(line, args.qd_protocol), ip) for line, ip in zip(lines, expected, strict=True)]


def _print_table(out: IO[str], results: Sequence[Result], sources: Sequence[str]) -> None:
    cols = ["echo", *sources]
    out.write(f"{'PROXY':<46} {'EXIT IP':<39} " + " ".join(f"{c[:12]:<12}" for c in cols) + "  PROBLEMS\n")
    for r in results:
        countries = r.countries
        geo = " ".join(f"{(countries.get(c) or '-')[:12]:<12}" for c in cols)
        problems = "; ".join(r.problems) if r.problems else "ok"
        out.write(f"{r.proxy[:46]:<46} {r.exit_ip or '-':<39} {geo}  {problems}\n")


CSV_COLUMNS = [
    "proxy",
    "reachable",
    "exit_ip",
    "expected_ip",
    "latency_ms",
    "countries",
    "operator",
    "targets",
    "problems",
]


def _csv(out: IO[str], results: Sequence[Result]) -> None:
    w = csv.writer(out)
    w.writerow(CSV_COLUMNS)
    for r in results:
        operator = next((g.operator for g in r.geo if g.operator), "") or ""
        w.writerow(
            [
                r.proxy,
                r.reachable,
                r.exit_ip or "",
                r.expected_ip or "",
                r.latency_ms or "",
                " ".join(f"{k}={v}" for k, v in r.countries.items()),
                operator,
                " ".join(f"{t.url}={t.status or 'error'}" for t in r.targets),
                " | ".join(r.problems),
            ]
        )


def main(
    argv: Sequence[str] | None = None,
    *,
    out: IO[str] | None = None,
    err: IO[str] | None = None,
    client_factory: Any = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0) and EXIT_USAGE
    try:
        entries = parse_entries(args.proxies)
        if args.file:
            with open(args.file, encoding="utf-8") as handle:
                entries.extend(parse_entries(handle))
        if args.qd_order:
            entries.extend(_qd_entries(args))
        if not entries:
            err.write("error: give proxies as arguments, with -f FILE or with --qd-order\n")
            return EXIT_USAGE
        sources = _sources(args)
        previous = None
        if args.state and os.path.exists(args.state):
            with open(args.state, encoding="utf-8") as handle:
                previous = json.load(handle)
        kwargs: dict[str, Any] = {
            "sources": sources,
            "echo_url": args.echo_url,
            "targets": args.target,
            "expect_country": args.expect_country,
            "timeout": args.timeout,
        }
        if client_factory is not None:
            kwargs["client_factory"] = client_factory
        results = check_all(entries, concurrency=args.concurrency, **kwargs)
    except (ProxyParseError, ValueError) as exc:
        err.write(f"error: {exc}\n")
        return EXIT_USAGE
    except (RuntimeError, OSError, httpx.HTTPError) as exc:
        err.write(f"error: {exc}\n")
        return EXIT_RUNTIME
    diff = changes(previous, results)
    s = {
        "proxies": len(results),
        "reachable": sum(r.reachable for r in results),
        "with_problems": sum(bool(r.problems) for r in results),
    }
    document: dict[str, Any] = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "summary": s,
        "changes": diff,
        "results": [r.to_dict() for r in results],
    }
    if args.json:
        json.dump(document, out, indent=2)
        out.write("\n")
    elif args.csv:
        _csv(out, results)
    else:
        _print_table(out, results, list(sources))
        out.write(
            f"\n{s['proxies']} proxies, {s['reachable']} reachable, {s['with_problems']} with problems\n"
        )
        if previous is not None:
            out.write("changes since the last run:\n" if diff else "no changes since the last run\n")
            for line in diff:
                out.write(f"  {line}\n")
    if args.state:
        with open(args.state, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
    return EXIT_PROBLEMS if args.fail_on_problems and s["with_problems"] else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

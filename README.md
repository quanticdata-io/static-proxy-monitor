# static-proxy-monitor

[![CI](https://github.com/quanticdata-io/static-proxy-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/quanticdata-io/static-proxy-monitor/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Check a fleet of **static proxies** (ISP / static residential, dedicated datacenter IPs) the way you depend on them:

- **identity**: each proxy still exits from the IP you were given;
- **location agreement**: the echo and the geolocation databases you choose all place the IP in the same country (and the one you expect); a dedicated "US" IP that one database puts in the Netherlands is the classic reason a site treats it as foreign;
- **target sites**: the URLs you care about still answer through each IP;
- **changes since the last run**: with `--state`, a scheduled run lists only what moved: a swapped IP, a database that relocated it, a site that started refusing it.

It works with any HTTP or SOCKS5 proxy from any provider. QuanticData ISP plans are supported directly.

```text
$ static-proxy-monitor -f fleet.txt --geo ipinfo --target https://www.example.com/ --expect-country US --state state.json
PROXY                                          EXIT IP                                 echo         ipinfo.io     PROBLEMS
http://user:*****@gw.example.net:8001          203.0.113.10                            US           US            ok
http://user:*****@gw.example.net:8002          203.0.113.11                            US           NL            sources disagree on the country: echo US, ipinfo NL; country NL (ipinfo), expected US

2 proxies, 2 reachable, 1 with problems
changes since the last run:
  http://user:*****@gw.example.net:8002: ipinfo now places it in NL (was US)
```

(Illustrative output with documentation addresses.)

## Install

Not on PyPI yet: install from source (Python 3.10 or later).

```bash
pip install "static-proxy-monitor[socks] @ git+https://github.com/quanticdata-io/static-proxy-monitor"
static-proxy-monitor --help        # or: python -m static_proxy_monitor --help
```

The `socks` extra is needed only for `socks5://` proxies.

## The list

One proxy per line, optionally followed by the exit IP it must have (space, tab or comma); `#` comments and blank lines are skipped. Proxies can also be passed as arguments.

```text
# fleet.txt
user:pass@gw.example.net:8001 203.0.113.10
gw.example.net:8002:user:pass,203.0.113.11
socks5://user:pass@198.51.100.4:1080
```

Proxy formats: `host:port`, `user:pass@host:port`, `host:port:user:pass`, `scheme://[user:pass@]host:port` (`http`, `https`, `socks5`, `socks5h`, `socks4`).

## What each check does

For each proxy, on a fresh connection:

1. **Echo**: one request to an IP echo through the proxy gives the exit IP and the echo's location. The default is `https://api.quanticdata.io/v1/public/ip-echo` (free, no account, never cached), which locates the IP the way Cloudflare does, and with it most sites behind Cloudflare. Change it with `--echo-url`.
2. **Geolocation sources**: the exit IP is looked up **directly** (not through the proxy) in each `--geo` source; results are cached per IP for the run.
3. **Targets**: each `--target URL` is fetched through the proxy; 2xx and 3xx pass.
4. **Verdict**: a problem is reported when the proxy is unreachable, the exit IP differs from the expected one, the sources disagree on the country, a source places it outside `--expect-country`, or a target fails.

| `--geo` value | Source |
|---|---|
| `quanticdata` | QuanticData [`GET /public/proxies/ip-info`](https://quanticdata.io/docs/): ipinfo.io data, free with an API key (`QUANTICDATA_API_KEY`), shares a daily cap of new lookups |
| `ipinfo` | ipinfo.io directly (`IPINFO_TOKEN` optional) |
| a URL with `{ip}` | any JSON service answering `country`/`countryCode`, `city`, and `org`/`as`/`asn` |

`--geo` can be repeated; the default is `quanticdata` when an API key is set, else `ipinfo`. `--no-geo` keeps only the echo.

## Options

| Option | Meaning |
|---|---|
| `-f FILE` | the list |
| `--target URL` | fetch this URL through every proxy (repeatable) |
| `--expect-country CC` | every source must agree on this country |
| `--state FILE` | read the previous run from FILE, report changes, then save this run there |
| `--json`, `--csv` | machine-readable output (CSV: `proxy, reachable, exit_ip, expected_ip, latency_ms, countries, operator, targets, problems`) |
| `--fail-on-problems` | exit 1 when any proxy has a problem (for CI and cron) |
| `-c N`, `--timeout S` | parallel proxies (10), seconds per request (20) |

Across runs a proxy is matched by its expected IP when you give one, otherwise by its masked proxy string. Passwords are never printed or saved.

Exit codes: 0 done, 1 problems with `--fail-on-problems`, 2 bad usage, 3 runtime or API error.

## Run it on a schedule

[`examples/scheduled-monitor.yml`](examples/scheduled-monitor.yml) is a GitHub Actions workflow that runs the monitor every morning, keeps `state.json` in the repository so each run reports changes since the previous one, and fails when there is a problem.

## QuanticData ISP plans

With a QuanticData ISP or Premium ISP plan, the monitor fetches the plan's lines itself through the documented, free [`POST /public/proxies/generate`](https://quanticdata.io/docs/) endpoint. For Premium ISP plans the answer also carries the exit IP of each line (`ipAddresses`), which becomes the expected IP, so a swapped or misrouted IP shows up as a problem.

```bash
export QUANTICDATA_API_KEY=qd_live_...
static-proxy-monitor --qd-order ORDER_ID --qd-quantity 50 --geo quanticdata --geo ipinfo \
  --target https://www.example.com/ --expect-country US --state state.json
```

`--qd-quantity` caps the lines generated (Premium ISP returns one per dedicated IP: pass the plan's number of IPs) and `--qd-protocol` picks `http` or `socks5`. Premium ISP IPs reach only ports 80 and 443, so keep targets on those ports. `QUANTICDATA_API_BASE` overrides the API host. Plans: [quanticdata.io/pricing](https://quanticdata.io/pricing/).

## Use it as a library

```python
from static_proxy_monitor import check_all, parse_entries
from static_proxy_monitor.lookup import ipinfo_lookup

entries = parse_entries(open("fleet.txt"))
for r in check_all(entries, sources={"ipinfo": ipinfo_lookup()}, expect_country="US"):
    print(r.proxy, r.exit_ip, r.countries, r.problems)
```

## Limits

- Databases disagree for honest reasons (recent reassignment, different update cycles). A disagreement is a signal to check, not proof the IP is in the wrong place; the sites you target may use yet another database.
- One echo request per proxy per run: the monitor does not measure latency distributions or rotation (see [proxy-checker](https://github.com/quanticdata-io/proxy-checker) for a full audit of a list).
- Each run sends a few requests through every proxy and uses a little of each plan's traffic.
- It does not check IP reputation or blocklists.

## Development

```bash
uv venv && uv pip install -e ".[socks]" --group dev
ruff check . && ruff format --check . && mypy && pytest
```

Tests use in-process fake proxies and fake geolocation sources on 127.0.0.1; they never use the network. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)

# Contributing

Thanks for helping improve static-proxy-monitor.

## Setup

```bash
git clone https://github.com/quanticdata-io/static-proxy-monitor
cd static-proxy-monitor
uv venv && uv pip install -e ".[socks]" --group dev
```

Plain pip works too: `pip install -e ".[socks]" pytest pytest-cov mypy ruff`.

## Checks

CI runs these on Python 3.10 and 3.13, on Ubuntu and Windows:

```bash
ruff check .
ruff format --check .
mypy
pytest
```

## Ground rules

- Tests never use the network. `tests/conftest.py` has fake static proxies (which also answer the target URLs) and fake geolocation sources, all on 127.0.0.1. Use documentation addresses (203.0.113.0/24, 198.51.100.0/24) in fixtures.
- Report what was observed. A problem message names the source and the value it gave.
- The JSON written with `--state` is read back by the next run: add fields, do not rename them.
- Never print or save a proxy password: results use the masked proxy string.
- User-facing changes need a CHANGELOG entry under "Unreleased".

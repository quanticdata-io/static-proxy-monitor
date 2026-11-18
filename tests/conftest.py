"""In-process fakes on 127.0.0.1: static proxies and geolocation sources.

A fake proxy answers absolute-form requests itself: ``http://echo.test/...``
with its exit IP, ``http://shop.test/ok`` with 200 and ``http://shop.test/blocked``
with 403. A fake geolocation source answers ``GET /<source>/<ip>`` like ipinfo.io.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

ECHO_URL = "http://echo.test/ip"

# What each fake database says about each exit IP.
GEO: dict[str, dict[str, tuple[str, str]]] = {
    "db1": {
        "203.0.113.10": ("US", "Ashburn"),
        "203.0.113.11": ("US", "Dallas"),
        "198.51.100.5": ("DE", "Berlin"),
    },
    "db2": {
        "203.0.113.10": ("US", "Ashburn"),
        "203.0.113.11": ("NL", "Amsterdam"),
        "198.51.100.5": ("DE", "Berlin"),
    },
}


@dataclass
class FakeStatic:
    exit_ip: str
    echo_country: str = "US"
    port: int = 0

    @property
    def url(self) -> str:
        return f"http://user:secret@127.0.0.1:{self.port}"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    fake: FakeStatic | None = None

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def _send(self, status: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        fake = self.fake
        if fake is not None and self.path.startswith("http://echo.test"):
            self._send(
                200, {"type": "response", "payload": {"ip": fake.exit_ip, "countryCode": fake.echo_country}}
            )
        elif self.path == "http://shop.test/ok":
            self._send(200, {"ok": True})
        elif self.path == "http://shop.test/blocked":
            self._send(403, {"ok": False})
        elif fake is None and self.path.count("/") == 2:
            _, source, ip = self.path.split("/")
            country, city = GEO.get(source, {}).get(ip, ("", ""))
            self._send(200, {"ip": ip, "country": country, "city": city, "org": "AS64500 Example Hosting"})
        else:
            self._send(404, {})


def _serve(fake: FakeStatic | None) -> ThreadingHTTPServer:
    handler = type("Handler", (_Handler,), {"fake": fake})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture
def make_static() -> Iterator[Callable[..., FakeStatic]]:
    servers: list[ThreadingHTTPServer] = []

    def make(exit_ip: str, echo_country: str = "US") -> FakeStatic:
        fake = FakeStatic(exit_ip, echo_country)
        server = _serve(fake)
        fake.port = int(server.server_address[1])
        servers.append(server)
        return fake

    yield make
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def geo_base() -> Iterator[str]:
    server = _serve(None)
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()

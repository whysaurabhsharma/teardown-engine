"""A threaded HTTP server on 127.0.0.1 for Guard tests. Nothing here reaches the internet."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

Reply = tuple[int, dict[str, str], bytes]


@dataclass
class Hit:
    path: str
    at: float  # time.monotonic()
    user_agent: str | None


@dataclass
class LocalServer:
    routes: dict[str, list[Reply] | Callable[[], Reply]] = field(default_factory=dict)
    hits: list[Hit] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    base_url: str = ""

    def route(self, path: str, status: int = 200, body: bytes | str = b"",
              headers: dict[str, str] | None = None) -> None:
        """Static reply for path."""
        self.sequence(path, [(status, headers or {}, body)])

    def sequence(self, path: str, replies: list[tuple[int, dict[str, str], bytes | str]]) -> None:
        """The nth request to path gets the nth reply; the last one repeats."""
        self.routes[path] = [
            (s, h, b.encode("utf-8") if isinstance(b, str) else b) for s, h, b in replies
        ]

    def handler(self, path: str, fn: Callable[[], Reply]) -> None:
        self.routes[path] = fn

    def hits_for(self, path: str) -> list[Hit]:
        with self.lock:
            return [h for h in self.hits if h.path == path]

    def url(self, path: str) -> str:
        return self.base_url + path

    def _reply(self, path: str) -> Reply:
        with self.lock:
            n = sum(1 for h in self.hits if h.path == path) - 1
        r = self.routes.get(path)
        if r is None:
            return 404, {}, b"no such page"
        if callable(r):
            return r()
        return r[min(n, len(r) - 1)]


def make_handler(server: LocalServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            with server.lock:
                server.hits.append(Hit(self.path, time.monotonic(), self.headers.get("User-Agent")))
            status, headers, body = server._reply(self.path)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (ConnectionError, OSError):
                pass  # client stopped reading (e.g. the 2 MB cap)

        def log_message(self, *args: object) -> None:
            pass

    return Handler


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        pass

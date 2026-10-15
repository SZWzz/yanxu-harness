"""A loopback wire fixture: a real HTTP server that emits a scripted SSE stream.

The provider tests must not touch a vendor, and they must also not stub out the
transport — a stubbed transport cannot prove that the *wire* carries what the
contract says it carries (headers, body shape, one request per call). So this
fixture speaks real HTTP over loopback and records what it received.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Mapping, Sequence

__all__ = ["WireFixture", "delta_frame", "tool_delta_frame", "usage_frame", "error_body"]


def delta_frame(text: str, *, finish: str = "") -> Mapping[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": {"content": text}}
    if finish:
        choice["finish_reason"] = finish
    return {"choices": [choice]}


def tool_delta_frame(index: int, *, call_id: str = "", name: str = "", arguments: str = "") -> Mapping[str, Any]:
    function: dict[str, Any] = {}
    if name:
        function["name"] = name
    if arguments:
        function["arguments"] = arguments
    call: dict[str, Any] = {"index": index, "function": function}
    if call_id:
        call["id"] = call_id
    return {"choices": [{"index": 0, "delta": {"tool_calls": [call]}}]}


def usage_frame(**usage: int) -> Mapping[str, Any]:
    return {"choices": [], "usage": usage}


def error_body(message: str, code: int = 400) -> bytes:
    return json.dumps({"error": {"message": message, "code": code}}).encode("utf-8")


def _sse(payloads: Iterable[Mapping[str, Any] | str], *, include_pings: bool = False) -> bytes:
    lines: list[str] = []
    if include_pings:
        lines.append(": keep-alive\n\n")
    for payload in payloads:
        body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        lines.append(f"data: {body}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


class WireFixture:
    """A one-shot-per-path loopback server that records every request it served."""

    def __init__(
        self,
        frames: Sequence[Mapping[str, Any] | str],
        *,
        status: int = 200,
        body: bytes | None = None,
        content_type: str = "text/event-stream",
        include_pings: bool = False,
        delay_seconds: float = 0.0,
    ) -> None:
        self.frames = list(frames)
        self.status = status
        self.body = body
        self.content_type = content_type
        self.include_pings = include_pings
        self.delay_seconds = delay_seconds
        self.requests: list[dict[str, Any]] = []
        self._fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # keep the test output clean
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                fixture = self.server.fixture  # type: ignore[attr-defined]
                fixture.requests.append(
                    {
                        "path": self.path,
                        "headers": {key.lower(): value for key, value in self.headers.items()},
                        "body": raw.decode("utf-8"),
                    }
                )
                if fixture.delay_seconds:
                    import time as _time

                    _time.sleep(fixture.delay_seconds)
                payload = (
                    fixture.body
                    if fixture.body is not None
                    else _sse(fixture.frames, include_pings=fixture.include_pings)
                )
                self.send_response(fixture.status)
                self.send_header("content-type", fixture.content_type)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.fixture = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def __enter__(self) -> "WireFixture":
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # -- convenience ----------------------------------------------------

    def first_request(self) -> Mapping[str, Any]:
        assert self.requests, "the provider never sent a request"
        return self.requests[0]

    def body_of(self, index: int = 0) -> Mapping[str, Any]:
        return json.loads(self.requests[index]["body"])

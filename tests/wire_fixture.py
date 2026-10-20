"""A loopback wire fixture: a real HTTP server that emits a scripted SSE stream.

The provider tests must not touch a vendor, and they must also not stub out the
transport — a stubbed transport cannot prove that the *wire* carries what the
contract says it carries (headers, body shape, one request per call). So this
fixture speaks real HTTP over loopback and records what it received.

Two knobs exist for tests that need more than "one fast response":

``scripts``
    A list of frame lists, one per request (the last one repeats). A tool round
    trip is two requests, and serving the *same* frames twice would make a
    single-tool-call response loop forever.
``chunk_delay_seconds``
    Write the body one SSE frame at a time, with a real pause between frames.
    That is what makes "a slow provider must not block the event loop" a measured
    statement instead of a claim.
"""

from __future__ import annotations

import json
import threading
import time
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
    """A loopback server that records every request and serves scripted frames."""

    def __init__(
        self,
        frames: Sequence[Mapping[str, Any] | str] = (),
        *,
        scripts: Sequence[Sequence[Mapping[str, Any] | str]] | None = None,
        status: int = 200,
        body: bytes | None = None,
        content_type: str = "text/event-stream",
        include_pings: bool = False,
        delay_seconds: float = 0.0,
        chunk_delay_seconds: float = 0.0,
        chunked: bool = False,
        chunk_split: int = 0,
        raw_body: bool = False,
        content_length_override: int | None = None,
        close_after_body: bool = False,
        tls_context: Any | None = None,
    ) -> None:
        if scripts is not None and frames:
            raise ValueError("pass either `frames` (one response) or `scripts` (one per request)")
        self.frames = list(frames)
        self.scripts = [list(script) for script in (scripts or [])]
        self.status = status
        self.body = body
        self.content_type = content_type
        self.include_pings = include_pings
        self.delay_seconds = delay_seconds
        self.chunk_delay_seconds = chunk_delay_seconds
        self.chunked = chunked
        #: Fixed-size chunk pieces, so a boundary can land in the middle of an SSE line
        #: (the split-on-blank-line mode above never does).
        self.chunk_split = chunk_split
        #: Write ``body`` exactly, framing and all: for malformed/truncated framing tests.
        self.raw_body = raw_body
        #: Declare a different ``content-length`` than the bytes written: the client then
        #: meets a body that ends early, which is what a truncated response looks like.
        self.content_length_override = content_length_override
        #: Hang up after the body, so a declared-but-missing tail arrives as EOF.
        self.close_after_body = close_after_body
        self.tls_context = tls_context
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
                    time.sleep(fixture.delay_seconds)
                payload = fixture.payload_for(len(fixture.requests) - 1)
                self.send_response(fixture.status)
                self.send_header("content-type", fixture.content_type)
                if fixture.chunked:
                    self.send_header("transfer-encoding", "chunked")
                else:
                    declared = fixture.content_length_override
                    self.send_header(
                        "content-length", str(len(payload) if declared is None else declared)
                    )
                if fixture.close_after_body:
                    self.send_header("connection", "close")
                    self.close_connection = True
                self.end_headers()
                if fixture.chunked and fixture.raw_body:
                    self.wfile.write(payload)
                    self.wfile.flush()
                elif fixture.chunked and fixture.chunk_split:
                    pieces = [
                        payload[index : index + fixture.chunk_split]
                        for index in range(0, len(payload), fixture.chunk_split)
                    ]
                    for piece in pieces:
                        self.wfile.write(f"{len(piece):X}\r\n".encode("ascii"))
                        self.wfile.write(piece + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                elif fixture.chunked:
                    pieces = [piece + b"\n\n" for piece in payload.split(b"\n\n") if piece]
                    for piece in pieces:
                        self.wfile.write(f"{len(piece):X}\r\n".encode("ascii"))
                        self.wfile.write(piece + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                elif fixture.chunk_delay_seconds and fixture.body is None:
                    fixture.drip(self.wfile, payload)
                else:
                    self.wfile.write(payload)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.fixture = self  # type: ignore[attr-defined]
        if tls_context is not None:
            self._server.socket = tls_context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        scheme = "https" if self.tls_context is not None else "http"
        return f"{scheme}://{host}:{port}/v1"

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

    def payload_for(self, index: int) -> bytes:
        """The response body for the Nth request (0-based)."""
        if self.body is not None:
            return self.body
        frames = self.frames
        if self.scripts:
            frames = self.scripts[min(index, len(self.scripts) - 1)]
        return _sse(frames, include_pings=self.include_pings)

    def drip(self, stream: Any, payload: bytes) -> None:
        """Write the payload one SSE frame at a time, pausing between frames."""
        pieces = [piece + b"\n\n" for piece in payload.split(b"\n\n") if piece]
        for index, piece in enumerate(pieces):
            stream.write(piece)
            stream.flush()
            if index < len(pieces) - 1:
                time.sleep(self.chunk_delay_seconds)

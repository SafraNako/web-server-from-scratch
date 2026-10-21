"""Reading requests from a connection, strictly: RFC 9112 framing with the checks that keep request smuggling out.

A request is a head (request line and header fields, ended by an empty line, every line ended by CRLF) and a body whose length is given by ``Content-Length`` or by
``Transfer-Encoding: chunked``. Whatever is ambiguous - both of those at once, two different lengths, a space before a colon, a folded header line, a bare LF - is
refused with 400, because two servers reading such a message differently is how smuggled requests happen.
"""
from __future__ import annotations

import re
import socket
import time

from .errors import HTTPError
from .headers import TOKEN, Headers
from .request import Request

SUPPORTED_METHODS = {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"}
_VERSION = re.compile(r"^HTTP/(\d)\.(\d)$")
_TARGET_BAD = re.compile(r"[\x00-\x20\x7f#]")
_HEX = re.compile(r"^[0-9a-fA-F]{1,16}$")
_BARE_LF = re.compile(rb"(?<!\r)\n")
# RFC 9110 7.2 / RFC 3986 3.2.2: Host = uri-host [ ":" port ]. The reg-name here is the host-name subset (unreserved characters and percent-escapes, no sub-delims like ; , ' that
# no real client sends) -- the same line Apache draws, which keeps odd bytes out of anything that later copies the value into a redirect, a log or a vhost lookup. An empty host is allowed.
_HOST = re.compile(r"^(?:\[[0-9A-Fa-f:.vV]+\]|(?:[A-Za-z0-9\-._~]|%[0-9A-Fa-f]{2})*)(?::[0-9]*)?$")


class ConnectionClosed(Exception):
    """The peer closed the connection (before a request began, or in the middle of one)."""


class Limits:
    def __init__(self, max_head=16384, max_uri=8192, max_header_line=8192, max_headers=100, max_body=10 * 1024 * 1024, max_chunk_line=1024, max_trailer=8192):
        self.max_head, self.max_uri, self.max_header_line, self.max_headers = max_head, max_uri, max_header_line, max_headers
        self.max_body, self.max_chunk_line, self.max_trailer = max_body, max_chunk_line, max_trailer


class BufferedConn:
    """A socket with a read-ahead buffer: whatever arrives after one request (pipelined requests) stays for the next."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = bytearray()

    def fill(self, timeout=None):
        if timeout is not None:
            self.sock.settimeout(max(timeout, 0.001))
        try:
            data = self.sock.recv(65536)
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            if isinstance(exc, socket.timeout):
                raise
            raise ConnectionClosed() from exc
        if not data:
            raise ConnectionClosed()
        self.buf += data

    def take(self, n):
        data = bytes(self.buf[:n])
        del self.buf[:n]
        return data

    def read_exact(self, n, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while len(self.buf) < n:
            self.fill(None if deadline is None else deadline - time.monotonic())
        return self.take(n)

    def read_line(self, limit, timeout=None):
        """One line without its CRLF; a bare LF is an error."""
        deadline = None if timeout is None else time.monotonic() + timeout
        start = 0
        while True:
            i = self.buf.find(b"\n", start)
            if i >= 0:
                if i == 0 or self.buf[i - 1] != 0x0D:
                    raise HTTPError(400, "a line ended without a carriage return", close=True)
                line = bytes(self.buf[:i - 1])
                del self.buf[:i + 1]
                return line
            if len(self.buf) > limit:
                raise HTTPError(400, "a line is too long", close=True)
            start = len(self.buf)
            self.fill(None if deadline is None else deadline - time.monotonic())


# --- the head ---------------------------------------------------------------------------------------------------------------------------------
def read_request(conn, limits, idle_timeout, header_timeout, client=("", 0)):
    """Read one request head from ``conn`` and return ``(Request without body, framing)`` or ``None`` when the peer closed an idle connection.

    ``framing`` is ``None`` (no body), ``("length", n)`` or ``("chunked",)``. ``idle_timeout`` applies until the first byte, ``header_timeout`` to the whole head.
    """
    started = None
    while True:
        while conn.buf.startswith(b"\r\n"):                             # RFC 9112 2.2: ignore empty lines in front of the request line
            del conn.buf[:2]
        end = conn.buf.find(b"\r\n\r\n")
        head_end = end if end >= 0 else len(conn.buf)
        if _BARE_LF.search(conn.buf, 0, head_end + 1):
            raise HTTPError(400, "a line feed without a carriage return", close=True)
        if end >= 0:
            break
        if len(conn.buf) > limits.max_head:
            raise HTTPError(431, "the request header is too large", close=True)
        try:
            if started is None and not conn.buf:
                conn.fill(idle_timeout)
                started = time.monotonic()
            else:
                if started is None:
                    started = time.monotonic()
                conn.fill(header_timeout - (time.monotonic() - started))
        except ConnectionClosed:
            if started is None and not conn.buf:
                return None
            raise
        except socket.timeout:
            if started is None and not conn.buf:
                return None                                            # an idle keep-alive connection simply expired
            raise HTTPError(408, "the request took too long", close=True)
    if end > limits.max_head:
        raise HTTPError(431, "the request header is too large", close=True)
    raw = bytes(conn.buf[:end])
    del conn.buf[:end + 4]
    request = parse_head(raw, limits, client)
    return request, request_framing(request, limits)


def parse_head(raw, limits, client=("", 0)):
    if b"\x00" in raw:
        raise HTTPError(400, "a NUL byte in the request", close=True)
    lines = raw.split(b"\r\n")
    if any((b"\r" in line or b"\n" in line) for line in lines):
        raise HTTPError(400, "a stray carriage return or line feed in the request", close=True)
    request_line = lines[0].decode("latin-1")
    parts = request_line.split(" ")
    if len(parts) != 3 or not all(parts):
        raise HTTPError(400, "malformed request line", close=True)
    method, target, version = parts
    if not TOKEN.match(method):
        raise HTTPError(400, "malformed method", close=True)
    m = _VERSION.match(version)
    if not m:
        raise HTTPError(400, "malformed HTTP version", close=True)
    if m.group(1) != "1":
        raise HTTPError(505, "only HTTP/1.x is supported", close=True)
    if m.group(2) != "0":
        version = "HTTP/1.1"                                            # RFC 9110 2.5: a newer minor version is handled as the newest one we know
    if len(target) > limits.max_uri:
        raise HTTPError(414, "the request target is too long", close=True)
    if _TARGET_BAD.search(target):
        raise HTTPError(400, "malformed request target", close=True)
    if method not in SUPPORTED_METHODS:
        raise HTTPError(501 if TOKEN.match(method) else 400, "method %s is not implemented" % method, close=True)
    if not (target.startswith("/") or target == "*" or target.lower().startswith(("http://", "https://"))):
        raise HTTPError(400, "malformed request target", close=True)
    if target == "*" and method != "OPTIONS":
        raise HTTPError(400, "the target * is only for OPTIONS", close=True)
    headers = Headers()
    if len(lines) - 1 > limits.max_headers:
        raise HTTPError(431, "too many header fields", close=True)
    for line in lines[1:]:
        if not line:
            raise HTTPError(400, "an empty line inside the header", close=True)
        if len(line) > limits.max_header_line:
            raise HTTPError(431, "a header field is too long", close=True)
        if line[:1] in (b" ", b"\t"):
            raise HTTPError(400, "folded header lines are not accepted", close=True)
        name, colon, value = line.partition(b":")
        if not colon:
            raise HTTPError(400, "a header line without a colon", close=True)
        name = name.decode("latin-1")
        if not TOKEN.match(name):                                       # also catches a space before the colon
            raise HTTPError(400, "malformed header name", close=True)
        try:
            headers.add(name, value.decode("latin-1"))
        except ValueError:
            raise HTTPError(400, "a control character in a header value", close=True)
    request = Request(method, target, version, headers, client=client)
    if "\x00" in request.path or "\x00" in request.raw_path:
        raise HTTPError(400, "a NUL byte in the request target", close=True)
    return request


def request_framing(request, limits):
    """Validate what the head says about the body and the connection; returns ``None``, ``("length", n)`` or ``("chunked",)``."""
    headers, version = request.headers, request.version
    hosts = headers.getall("Host")
    if version == "HTTP/1.1" and len(hosts) != 1:
        raise HTTPError(400, "an HTTP/1.1 request needs exactly one Host header", close=True)
    if len(hosts) > 1:
        raise HTTPError(400, "more than one Host header", close=True)
    if hosts and not _HOST.match(hosts[0]):
        raise HTTPError(400, "malformed Host header", close=True)
    te = headers.getall("Transfer-Encoding")
    cl = headers.getall("Content-Length")
    framing = None
    if te:
        if cl:
            raise HTTPError(400, "both Transfer-Encoding and Content-Length (request smuggling attempt?)", close=True)
        if version == "HTTP/1.0":
            raise HTTPError(400, "Transfer-Encoding in an HTTP/1.0 request", close=True)
        codings = [c.strip().lower() for v in te for c in v.split(",") if c.strip()]
        if not codings or codings[-1] != "chunked":
            raise HTTPError(400, "the final transfer coding must be chunked", close=True)
        if codings != ["chunked"]:
            raise HTTPError(501, "transfer codings other than chunked are not implemented", close=True)
        framing = ("chunked",)
    elif cl:
        values = {v.strip() for header in cl for v in header.split(",")}
        if len(values) != 1:
            raise HTTPError(400, "conflicting Content-Length values", close=True)
        value = values.pop()
        if not re.fullmatch(r"[0-9]{1,15}", value):
            raise HTTPError(400, "malformed Content-Length", close=True)
        n = int(value)
        framing = ("length", n) if n > 0 else None
        if n > limits.max_body:
            raise HTTPError(413, "the request body is too large", close=True)
    connection = {t.lower() for t in headers.get_list("Connection")}
    if version == "HTTP/1.1":
        request.keep_alive = "close" not in connection
    else:
        request.keep_alive = "keep-alive" in connection and "close" not in connection
    expect = headers.get_list("Expect")
    if expect:
        if [e.lower() for e in expect] != ["100-continue"]:
            raise HTTPError(417, "the expectation cannot be met", close=True)
        request.expect_continue = version == "HTTP/1.1" and framing is not None
    return framing


# --- the body ---------------------------------------------------------------------------------------------------------------------------------
def read_body(conn, framing, limits, timeout):
    if framing is None:
        return b""
    try:
        if framing[0] == "length":
            return conn.read_exact(framing[1], timeout)
        return _read_chunked(conn, limits, timeout)
    except socket.timeout:
        raise HTTPError(408, "the request body took too long", close=True)
    except ConnectionClosed:
        raise HTTPError(400, "the connection closed in the middle of the request body", close=True)


def _read_chunked(conn, limits, timeout):
    deadline = time.monotonic() + timeout
    left = lambda: deadline - time.monotonic()  # noqa: E731
    body = bytearray()
    while True:
        line = conn.read_line(limits.max_chunk_line, left())
        size_text = line.split(b";", 1)[0].decode("latin-1")
        if not _HEX.match(size_text):
            raise HTTPError(400, "malformed chunk size", close=True)
        size = int(size_text, 16)
        if size == 0:
            break
        if len(body) + size > limits.max_body:
            raise HTTPError(413, "the request body is too large", close=True)
        body += conn.read_exact(size, left())
        if conn.read_exact(2, left()) != b"\r\n":
            raise HTTPError(400, "a chunk is not followed by CRLF", close=True)
    total = 0
    while True:                                                         # trailer fields (checked for syntax and size, then dropped)
        line = conn.read_line(limits.max_chunk_line, left())
        if not line:
            break
        total += len(line) + 2
        name, colon, _value = line.partition(b":")
        if total > limits.max_trailer or not colon or not TOKEN.match(name.decode("latin-1")):
            raise HTTPError(400, "malformed trailer field", close=True)
    return bytes(body)

"""A socket that plays back prepared bytes, to test the parser without a network."""
import socket

from kendi_sunucu.parser import BufferedConn, Limits, read_body, read_request


class Scripted:
    def __init__(self, chunks):
        self.chunks = [c for c in chunks if c]
        self.timeout = None

    def settimeout(self, t):
        self.timeout = t

    def recv(self, n):
        if not self.chunks:
            return b""
        c = self.chunks[0]
        if len(c) > n:
            self.chunks[0] = c[n:]
            return c[:n]
        self.chunks.pop(0)
        return c


class Stall(Scripted):
    """Like ``Scripted`` but the peer then goes quiet instead of closing."""

    def recv(self, n):
        if not self.chunks:
            raise socket.timeout()
        return super().recv(n)


def parse(data, limits=None, chunks=None):
    """Read one request (head and body) from ``data``, optionally delivered in the given fragment sizes; returns ``(request, body)`` or ``None`` at a clean end."""
    limits = limits or Limits()
    if chunks:
        pieces, pos = [], 0
        for n in chunks:
            pieces.append(data[pos:pos + n])
            pos += n
        pieces.append(data[pos:])
    else:
        pieces = [data]
    conn = BufferedConn(Scripted(pieces))
    got = read_request(conn, limits, 1.0, 1.0)
    if got is None:
        return None
    request, framing = got
    request.body = read_body(conn, framing, limits, 1.0)
    return request, conn

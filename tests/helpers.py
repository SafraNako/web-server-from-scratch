"""Shared test tools: a server running on a free port, raw socket exchanges, and a response reader that is *not* ours (``http.client``)."""
import http.client
import io
import socket
import time

from kendi_sunucu import App, Server


class running:
    """``with running(app, **options) as server:`` - serves in a background thread on a free port."""

    def __init__(self, app=None, **options):
        self.server = Server(app or App(), "127.0.0.1", 0, **options)

    def __enter__(self):
        return self.server.start()

    def __exit__(self, *exc):
        self.server.shutdown(1.0)


def connect(server, timeout=5.0):
    s = socket.create_connection(server.address, timeout=timeout)
    s.settimeout(timeout)
    return s


def recv_all(sock, until_close=True, timeout=3.0):
    """Everything the server sends until it closes the connection (or ``timeout`` passes)."""
    sock.settimeout(timeout)
    data = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    return data


def raw(server, data, timeout=3.0):
    """Send bytes, read until the server closes the connection; returns what came back."""
    s = connect(server, timeout)
    try:
        s.sendall(data)
        return recv_all(s, timeout=timeout)
    finally:
        s.close()


class _Keep(io.BytesIO):
    def close(self):                                                    # http.client closes the file after each response; keep it for the next one
        pass


class _Fake:
    def __init__(self, data):
        self.f = _Keep(data)

    def makefile(self, *a, **k):
        return self.f


def parse_responses(data, methods=None):
    """Parse back-to-back responses with ``http.client``; returns ``[(status, headers, body)]``."""
    fake = _Fake(data)
    out = []
    i = 0
    while fake.f.tell() < len(data):
        method = (methods[i] if methods else "GET")
        r = http.client.HTTPResponse(fake, method=method)
        r.begin()
        out.append((r.status, r.headers, r.read()))
        i += 1
    return out


def one(server, request, timeout=3.0):
    """Send one request, return ``(status, headers, body)`` of the one response."""
    responses = parse_responses(raw(server, request, timeout))
    assert len(responses) == 1, responses
    return responses[0]


def get(server, path, headers=None, version="HTTP/1.1", method="GET"):
    lines = ["%s %s %s" % (method, path, version), "Host: x", "Connection: close"] + ["%s: %s" % kv for kv in (headers or {}).items()]
    return one(server, ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))


def wait_until(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False

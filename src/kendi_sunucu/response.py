"""Responses: status line, header fields and one of three kinds of body (bytes, a file range, an iterator of chunks), written to a connection."""
from __future__ import annotations

import json
import time
from email.utils import formatdate
from http import HTTPStatus

from .headers import Headers

SERVER_NAME = "kendi-sunucu/0.1"
_DATE = {"second": 0, "text": ""}


def http_date(timestamp=None):
    return formatdate(timestamp, usegmt=True)


def _now():
    t = int(time.time())
    if _DATE["second"] != t:
        _DATE["second"], _DATE["text"] = t, formatdate(t, usegmt=True)
    return _DATE["text"]


def reason(status):
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Unknown"


class FileBody:
    """``length`` bytes of an open file starting at ``offset`` (sent with ``sendfile`` where the platform has it)."""

    def __init__(self, fileobj, offset, length):
        self.file, self.offset, self.length = fileobj, offset, length

    def close(self):
        self.file.close()


class SizedBody:
    """An iterator of chunks whose total length is known in advance (so the response can carry a Content-Length instead of being chunked)."""

    def __init__(self, chunks, length):
        self.chunks, self.length = chunks, length

    def __iter__(self):
        return iter(self.chunks)

    def close(self):
        close = getattr(self.chunks, "close", None)
        if close:
            close()


class Response:
    def __init__(self, body=b"", status=200, headers=None, content_type=None):
        self.status = status
        self.headers = headers if isinstance(headers, Headers) else Headers(headers or [])
        if isinstance(body, str):
            body = body.encode("utf-8")
            if content_type is None and "Content-Type" not in self.headers:
                content_type = "text/plain; charset=utf-8"
        self.body = body
        if content_type:
            self.headers.set("Content-Type", content_type)

    # --- what goes on the wire ----------------------------------------------------------------------------------------------------------------
    def head_bytes(self, request_method, version, keep_alive, chunked_ok):
        """The status line and header fields, with the framing headers filled in; returns ``(bytes, mode, keep_alive)`` where mode says how the body follows."""
        h = self.headers.copy()
        h.setdefault("Date", _now())
        h.setdefault("Server", SERVER_NAME)
        status = self.status
        bodyless = status < 200 or status in (204, 304)
        if bodyless:
            mode = "none"
            h.remove("Content-Length")
            h.remove("Transfer-Encoding")
        elif isinstance(self.body, (bytes, bytearray)):
            h.set("Content-Length", str(len(self.body)))
            mode = "bytes"
        elif isinstance(self.body, FileBody):
            h.set("Content-Length", str(self.body.length))
            mode = "file"
        elif isinstance(self.body, SizedBody):
            h.set("Content-Length", str(self.body.length))
            mode = "sized"
        else:
            if chunked_ok:
                h.set("Transfer-Encoding", "chunked")
                h.remove("Content-Length")
                mode = "chunked"
            else:
                keep_alive = False                                      # HTTP/1.0 client: the end of the body is the end of the connection
                mode = "stream"
        if request_method == "HEAD" and mode != "none":
            mode = "head"
        if keep_alive:
            if version == "HTTP/1.0":
                h.set("Connection", "keep-alive")
        else:
            h.set("Connection", "close")
        lines = ["HTTP/1.1 %d %s" % (status, reason(status))] + ["%s: %s" % (n, v) for n, v in h]
        return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace"), mode, keep_alive

    def write_to(self, sock, request_method, version, keep_alive):
        """Send the response; returns ``(bytes of body sent, keep_alive after all)``."""
        head, mode, keep_alive = self.head_bytes(request_method, version, keep_alive, chunked_ok=(version == "HTTP/1.1"))
        sent = 0
        try:
            if mode == "bytes":
                sock.sendall(head + bytes(self.body))
                sent = len(self.body)
            elif mode in ("none", "head"):
                sock.sendall(head)
            elif mode == "file":
                sock.sendall(head)
                sent = _send_file(sock, self.body)
            elif mode == "chunked":
                sock.sendall(head)
                for chunk in self.body:
                    if chunk:
                        sock.sendall(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                        sent += len(chunk)
                sock.sendall(b"0\r\n\r\n")
            elif mode == "sized":
                sock.sendall(head)
                for chunk in self.body:
                    sock.sendall(chunk)
                    sent += len(chunk)
            else:                                                       # stream until close
                sock.sendall(head)
                for chunk in self.body:
                    if chunk:
                        sock.sendall(chunk)
                        sent += len(chunk)
        finally:
            self.close()
        return sent, keep_alive

    def close(self):
        close = getattr(self.body, "close", None)
        if close:
            close()


def _send_file(sock, body):
    f = body.file
    try:
        return sock.sendfile(f, body.offset, body.length)
    except (AttributeError, NotImplementedError, ValueError):
        pass
    f.seek(body.offset)
    left = body.length
    sent = 0
    while left > 0:
        data = f.read(min(65536, left))
        if not data:
            break
        sock.sendall(data)
        left -= len(data)
        sent += len(data)
    return sent


# --- shortcuts ----------------------------------------------------------------------------------------------------------------------------------
def json_response(data, status=200, headers=None):
    return Response(json.dumps(data, ensure_ascii=False).encode("utf-8"), status, headers, "application/json")


def text_response(text, status=200, headers=None, content_type="text/plain; charset=utf-8"):
    return Response(text.encode("utf-8") if isinstance(text, str) else text, status, headers, content_type)


def redirect(location, status=302):
    """A redirect; the location is percent-encoded where needed so that untrusted text cannot inject header lines."""
    from urllib.parse import quote
    safe = quote(location, safe="/:?&=%#@!$'()*+,;~-._[]")
    return Response(b"", status, [("Location", safe)])


def error_response(status, message=""):
    from html import escape
    phrase = reason(status)
    body = ("<!doctype html><html><head><title>%d %s</title></head><body><h1>%d %s</h1><p>%s</p></body></html>\n"
            % (status, phrase, status, phrase, escape(message))).encode("utf-8")
    return Response(body, status, content_type="text/html; charset=utf-8")

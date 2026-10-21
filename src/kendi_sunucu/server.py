"""The server: a listening socket, a thread per connection, and for every connection the loop request -> response with keep-alive and pipelining.

Everything that can go wrong with the peer is handled here, not in the application: malformed requests get their 4xx/5xx and the connection is closed, slow
or silent peers are dropped by timeouts (idle connections quietly, half-sent requests with 408), too many connections get a 503, and an exception in a handler
becomes a 500 without leaking its text."""
from __future__ import annotations

import socket
import sys
import threading
import time
import traceback
from email.utils import formatdate

from .errors import HTTPError
from .parser import BufferedConn, ConnectionClosed, Limits, read_body, read_request
from .response import error_response
from .router import App


class Server:
    def __init__(self, app, host="127.0.0.1", port=8000, *, limits=None, idle_timeout=5.0, header_timeout=10.0, body_timeout=30.0, write_timeout=30.0,
                 max_requests=100, max_connections=256, backlog=128, access_log=None, error_log=None):
        self.app = app
        self.host, self.port = host, port
        self.limits = limits or Limits()
        self.idle_timeout, self.header_timeout, self.body_timeout, self.write_timeout = idle_timeout, header_timeout, body_timeout, write_timeout
        self.max_requests, self.max_connections, self.backlog = max_requests, max_connections, backlog
        self.access_log = _stderr_log if access_log is True else access_log
        self.error_log = error_log or (lambda text: sys.stderr.write(text + "\n"))
        self.listener = None
        self._stopping = threading.Event()
        self._slots = threading.BoundedSemaphore(max_connections)
        self._threads = set()
        self._socks = set()
        self._lock = threading.Lock()
        self._thread = None
        self.requests_served = 0

    # --- lifecycle ----------------------------------------------------------------------------------------------------------------------------
    def bind(self):
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, self.port))
        s.listen(self.backlog)
        s.settimeout(0.2)
        self.listener = s
        self.port = s.getsockname()[1]
        return self.address

    @property
    def address(self):
        return self.host, self.port

    @property
    def url(self):
        host = "[%s]" % self.host if ":" in self.host else self.host
        return "http://%s:%d" % (host, self.port)

    def start(self):
        """Bind and serve in a background thread (for tests and embedding)."""
        if self.listener is None:
            self.bind()
        self._thread = threading.Thread(target=self.serve_forever, name="kendi-sunucu-accept", daemon=True)
        self._thread.start()
        return self

    def serve_forever(self):
        if self.listener is None:
            self.bind()
        while not self._stopping.is_set():
            try:
                sock, addr = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                self._refuse(sock)
                continue
            t = threading.Thread(target=self._connection, args=(sock, addr), daemon=True)
            with self._lock:
                self._threads.add(t)
            t.start()

    def shutdown(self, wait=5.0):
        """Stop accepting, let running requests finish for up to ``wait`` seconds, then close what is left."""
        self._stopping.set()
        if self._thread:
            self._thread.join(2)
        if self.listener:
            try:
                self.listener.close()
            except OSError:
                pass
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            with self._lock:
                if not self._threads:
                    return
            time.sleep(0.02)
        with self._lock:
            socks = list(self._socks)
        for s in socks:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.shutdown(1.0)

    # --- one connection -----------------------------------------------------------------------------------------------------------------------
    def _refuse(self, sock):
        try:
            sock.settimeout(1)
            sock.sendall(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 1\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        except OSError:
            pass
        finally:
            sock.close()

    def _connection(self, sock, addr):
        with self._lock:
            self._socks.add(sock)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        try:
            self._serve(sock, addr)
        except Exception:                                               # a bug here must not take the thread (and its log line) down silently
            self.error_log("connection error from %s:\n%s" % (addr, traceback.format_exc()))
        finally:
            self._close(sock)
            with self._lock:
                self._socks.discard(sock)
                self._threads.discard(threading.current_thread())
            self._slots.release()

    def _serve(self, sock, addr):
        conn = BufferedConn(sock)
        served = 0
        while not self._stopping.is_set():
            try:
                got = read_request(conn, self.limits, self.idle_timeout, self.header_timeout, client=addr)
            except HTTPError as exc:
                self._reply_error(sock, exc, "-")
                return
            except ConnectionClosed:
                if conn.buf:                                            # the peer stopped in the middle of a request head: say so (it may only have half-closed)
                    self._reply_error(sock, HTTPError(400, "the request ended before its header was complete", close=True), "-")
                return
            except (socket.timeout, OSError):
                return
            if got is None:
                return
            request, framing = got
            served += 1
            started = time.monotonic()
            try:
                if request.expect_continue:
                    sock.settimeout(self.write_timeout)
                    sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
                request.body = read_body(conn, framing, self.limits, self.body_timeout)
            except HTTPError as exc:
                self._reply_error(sock, exc, request.method + " " + request.target + " " + request.version)
                return
            except OSError:
                return
            keep_alive = request.keep_alive and served < self.max_requests and not self._stopping.is_set()
            response = self._call_app(request)
            try:
                sock.settimeout(self.write_timeout)
                sent, keep_alive = response.write_to(sock, request.method, request.version, keep_alive)
            except OSError:
                return
            self.requests_served += 1
            if self.access_log:
                self.access_log(addr, "%s %s %s" % (request.method, request.target, request.version), response.status, sent, time.monotonic() - started)
            if not keep_alive:
                return

    def _call_app(self, request):
        try:
            if hasattr(self.app, "handle"):
                return self.app.handle(request)
            return App._finish(self.app(request), request)
        except HTTPError as exc:
            return error_response(exc.status, exc.message)
        except Exception:
            self.error_log("error while handling %s:\n%s" % (request, traceback.format_exc()))
            return error_response(500, "")

    def _reply_error(self, sock, exc, line):
        response = self.app.error_response(exc.status, exc.message, exc.headers) if hasattr(self.app, "error_response") else error_response(exc.status, exc.message)
        response.headers.set("Connection", "close")
        try:
            sock.settimeout(self.write_timeout)
            sent, _ = response.write_to(sock, "GET", "HTTP/1.1", False)
        except OSError:
            return
        if self.access_log:
            self.access_log(("-", 0), line, exc.status, sent, 0.0)

    @staticmethod
    def _close(sock):
        """Close politely: tell the peer we are done, then read what it still sends for a moment so that the close does not turn into a reset that loses our reply."""
        try:
            sock.shutdown(socket.SHUT_WR)
            sock.settimeout(0.5)
            end = time.monotonic() + 1.0
            drained = 0
            while time.monotonic() < end and drained < 1 << 20:
                data = sock.recv(65536)
                if not data:
                    break
                drained += len(data)
        except OSError:
            pass
        finally:
            try:
                sock.close()
            except OSError:
                pass


def _stderr_log(addr, request_line, status, size, seconds):
    """Common Log Format."""
    stamp = formatdate(time.time(), localtime=True, usegmt=False)
    shown = "".join(c if 32 <= ord(c) < 127 else "\\x%02x" % ord(c) for c in request_line)
    sys.stderr.write('%s - - [%s] "%s" %d %d\n' % (addr[0], stamp, shown, status, size))

"""``kserve``: serve a directory (or run the demo application) from the command line."""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading

from . import __version__
from .parser import Limits
from .router import App
from .server import Server
from .static import StaticFiles


def build_parser():
    p = argparse.ArgumentParser(prog="kserve", description="Serve a directory over HTTP/1.1 with a web server written from scratch.")
    p.add_argument("directory", nargs="?", default=".", help="the directory to serve (default: the current one)")
    p.add_argument("-b", "--bind", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    p.add_argument("-p", "--port", type=int, default=8000)
    p.add_argument("--listing", action="store_true", help="show a listing for directories without index.html")
    p.add_argument("--hidden", action="store_true", help="also serve files whose names start with a dot")
    p.add_argument("--no-gzip", action="store_true", help="never compress text")
    p.add_argument("--cache-control", help="Cache-Control header for files, e.g. 'max-age=3600'")
    p.add_argument("--max-body", type=int, default=10 * 1024 * 1024, help="largest accepted request body in bytes")
    p.add_argument("--max-connections", type=int, default=256)
    p.add_argument("-q", "--quiet", action="store_true", help="no access log")
    p.add_argument("--version", action="version", version="kserve " + __version__)
    return p


def main(argv=None):
    ns = build_parser().parse_args(argv)
    if not os.path.isdir(ns.directory):
        sys.stderr.write("kserve: error: %s is not a directory\n" % ns.directory)
        return 1
    app = App()
    app.mount("", StaticFiles(ns.directory, listing=ns.listing, cache_control=ns.cache_control, gzip_text=not ns.no_gzip, hidden=ns.hidden))
    server = Server(app, ns.bind, ns.port, limits=Limits(max_body=ns.max_body), max_connections=ns.max_connections, access_log=None if ns.quiet else True)
    try:
        server.bind()
    except OSError as exc:
        sys.stderr.write("kserve: error: cannot listen on %s:%d: %s\n" % (ns.bind, ns.port, exc))
        return 1
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    sys.stderr.write("Serving %s  (directory: %s)  -- Ctrl+C to stop\n" % (server.url, os.path.abspath(ns.directory)))
    server.start()
    while not stop.wait(0.5):
        pass
    sys.stderr.write("\nShutting down...\n")
    server.shutdown(5.0)
    return 0

"""A parsed request as the application sees it."""
from __future__ import annotations

import json as _json
from urllib.parse import parse_qs, unquote

from .errors import HTTPError


class Request:
    def __init__(self, method, target, version, headers, body=b"", client=("", 0)):
        self.method = method
        self.target = target                    # exactly as sent: "/a%20b?x=1"
        self.version = version                  # "HTTP/1.0" or "HTTP/1.1"
        self.headers = headers
        self.body = body
        self.client = client
        self.params = {}                        # filled in by the router
        self.raw_path, _, self.query_string = _split_target(target)
        self.path = unquote(self.raw_path, errors="replace")
        self.keep_alive = True
        self.expect_continue = False
        self.id = 0

    # --- the query string, the body ---------------------------------------------------------------------------------------------------------
    @property
    def query(self):
        """``{name: [values]}`` of the query string."""
        return parse_qs(self.query_string, keep_blank_values=True)

    def query_param(self, name, default=None):
        values = self.query.get(name)
        return values[0] if values else default

    def text(self):
        charset = "utf-8"
        for part in (self.headers.get("Content-Type") or "").split(";")[1:]:
            k, _, v = part.strip().partition("=")
            if k.lower() == "charset" and v:
                charset = v.strip('"')
        try:
            return self.body.decode(charset, "replace")
        except LookupError:                     # a charset name Python does not know: read it as UTF-8 rather than fail
            return self.body.decode("utf-8", "replace")

    def json(self):
        """The body parsed as JSON; a body that is not valid UTF-8 JSON is the client's mistake: ``HTTPError(400)``."""
        try:
            return _json.loads(self.body.decode("utf-8"))
        except ValueError:                      # JSONDecodeError and UnicodeDecodeError both are ValueErrors
            raise HTTPError(400, "the request body is not valid JSON") from None

    def form(self):
        """``application/x-www-form-urlencoded`` body as ``{name: [values]}``."""
        return parse_qs(self.body.decode("utf-8", "replace"), keep_blank_values=True)

    @property
    def cookies(self):
        out = {}
        for header in self.headers.getall("Cookie"):
            for pair in header.split(";"):
                name, sep, value = pair.strip().partition("=")
                if sep and name:
                    out.setdefault(name, value.strip('"'))
        return out

    def __repr__(self):
        return "<Request %s %s>" % (self.method, self.target)


def _split_target(target):
    """Path and query of a request target in origin-form (``/p?q``) or absolute-form (``http://host/p?q``)."""
    if not target.startswith("/") and "://" in target:
        rest = target.split("://", 1)[1]
        slash = rest.find("/")
        question = rest.find("?")
        if slash < 0 and question < 0:
            target = "/"
        elif slash < 0:
            target = "/" + rest[question:]
        else:
            target = rest[slash:]
    path, _, query = target.partition("?")
    return path or "/", "?" if query else "", query

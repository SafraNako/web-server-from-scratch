"""Routing: patterns like ``/users/<int:id>/files/<path:name>`` matched against the *decoded segments* of the path (so an encoded ``%2F`` never
becomes a separator), method dispatch with 405 and ``Allow``, automatic ``HEAD`` and ``OPTIONS``, mounted sub-handlers and middleware."""
from __future__ import annotations

import re
from urllib.parse import unquote

from .errors import HTTPError
from .response import Response, error_response, json_response

_PARAM = re.compile(r"^<(?:(int|str|path):)?([A-Za-z_][A-Za-z0-9_]*)>$")
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]


class Route:
    def __init__(self, pattern, methods, handler, index):
        if not pattern.startswith("/"):
            raise ValueError("a route pattern starts with /: %r" % pattern)
        self.pattern, self.handler, self.index = pattern, handler, index
        self.methods = {m.upper() for m in methods}
        self.parts = []
        raw = pattern.split("/")[1:]
        for i, seg in enumerate(raw):
            m = _PARAM.match(seg)
            if m:
                kind, name = m.group(1) or "str", m.group(2)
                if kind == "path" and i != len(raw) - 1:
                    raise ValueError("a <path:...> parameter must be the last segment: %r" % pattern)
                self.parts.append((kind, name))
            else:
                if "<" in seg or ">" in seg:
                    raise ValueError("malformed parameter in %r" % pattern)
                self.parts.append(("lit", seg))
        self.dynamic = sum(1 for k, _ in self.parts if k != "lit")
        self.literal = len(self.parts) - self.dynamic

    def match(self, segments):
        """The parameters if ``segments`` (decoded) fit, else ``None``."""
        params = {}
        n = len(self.parts)
        for i, (kind, value) in enumerate(self.parts):
            if kind == "path":
                rest = segments[i:]
                if not rest or not any(rest):
                    return None
                params[value] = "/".join(rest)
                return params
            if i >= len(segments):
                return None
            seg = segments[i]
            if kind == "lit":
                if seg != value:
                    return None
            elif kind == "int":
                if not (seg.isascii() and seg.isdigit()):
                    return None
                params[value] = int(seg)
            else:
                if not seg:
                    return None
                params[value] = seg
        return params if len(segments) == n else None


class App:
    def __init__(self):
        self.routes = []
        self.mounts = []
        self.middleware = []
        self.error_handlers = {}

    # --- registering --------------------------------------------------------------------------------------------------------------------------
    def route(self, pattern, methods=("GET",)):
        def decorator(fn):
            self.add_route(pattern, methods, fn)
            return fn
        return decorator

    def add_route(self, pattern, methods, handler):
        self.routes.append(Route(pattern, methods, handler, len(self.routes)))

    def get(self, pattern):
        return self.route(pattern, ("GET",))

    def post(self, pattern):
        return self.route(pattern, ("POST",))

    def put(self, pattern):
        return self.route(pattern, ("PUT",))

    def delete(self, pattern):
        return self.route(pattern, ("DELETE",))

    def patch(self, pattern):
        return self.route(pattern, ("PATCH",))

    def mount(self, prefix, handler):
        """Hand every request below ``prefix`` that no route claimed to ``handler(request, rest)``, ``rest`` being the still-encoded remainder of the path."""
        prefix = prefix.rstrip("/")
        self.mounts.append((prefix, handler))
        self.mounts.sort(key=lambda m: -len(m[0]))

    def use(self, middleware):
        """``middleware(request, call_next) -> Response``; the first one added is the outermost."""
        self.middleware.append(middleware)
        return middleware

    def error_handler(self, status):
        def decorator(fn):
            self.error_handlers[status] = fn
            return fn
        return decorator

    # --- handling -----------------------------------------------------------------------------------------------------------------------------
    def handle(self, request):
        def dispatch(req):
            try:                                                        # middleware always sees a Response, errors included
                return self._finish(self._dispatch(req), req)
            except HTTPError as exc:
                return self.error_response(exc.status, exc.message, exc.headers, req)
        call = dispatch
        for mw in reversed(self.middleware):
            call = (lambda mw, nxt: (lambda req: mw(req, nxt)))(mw, call)
        try:
            return self._finish(call(request), request)
        except HTTPError as exc:                                        # raised by a middleware itself
            return self.error_response(exc.status, exc.message, exc.headers, request)

    def error_response(self, status, message="", headers=None, request=None):
        handler = self.error_handlers.get(status)
        if handler and request is not None:
            try:
                response = self._finish(handler(request), request)
                response.status = status
                return response
            except Exception:
                pass
        response = error_response(status, message)
        for name, value in headers or []:
            response.headers.add(name, value)
        return response

    def _dispatch(self, request):
        if request.target == "*":
            if request.method == "OPTIONS":
                return Response(b"", 200, [("Allow", ", ".join(ALL_METHODS))])
            raise HTTPError(400, "the target * is only for OPTIONS")
        segments = [unquote(s, errors="replace") for s in request.raw_path.split("/")[1:]]
        matched = []
        for route in self.routes:
            params = route.match(segments)
            if params is not None:
                matched.append((route, params))
        matched.sort(key=lambda rp: (rp[0].dynamic, -rp[0].literal, rp[0].index))
        method = request.method
        lookup = "GET" if method == "HEAD" else method
        for route, params in matched:
            if method in route.methods or lookup in route.methods:
                request.params = params
                return route.handler(request, **params)
        if matched:
            allowed = set()
            for route, _ in matched:
                allowed |= route.methods
            if "GET" in allowed:
                allowed.add("HEAD")
            allowed.add("OPTIONS")
            allow = ", ".join(m for m in ALL_METHODS if m in allowed)
            if method == "OPTIONS":
                return Response(b"", 204, [("Allow", allow)])
            raise HTTPError(405, "%s is not allowed here" % method, [("Allow", allow)])
        for prefix, handler in self.mounts:
            raw = request.raw_path
            if raw == prefix or raw.startswith(prefix + "/") or prefix == "":
                rest = raw[len(prefix):]
                return handler(request, rest)
        raise HTTPError(404, "no such page")

    @staticmethod
    def _finish(result, request):
        """Turn what a handler returned into a ``Response``."""
        if isinstance(result, Response):
            return result
        status, headers = 200, None
        if isinstance(result, tuple):
            result, *rest = result
            status = rest[0] if rest else 200
            headers = rest[1] if len(rest) > 1 else None
        if result is None:
            return Response(b"", 204 if status == 200 else status, headers)
        if isinstance(result, (dict, list)):
            return json_response(result, status, headers)
        if isinstance(result, str):
            return Response(result, status, headers, "text/html; charset=utf-8" if result.lstrip().startswith("<") else "text/plain; charset=utf-8")
        if isinstance(result, (bytes, bytearray)):
            return Response(bytes(result), status, headers, "application/octet-stream")
        if hasattr(result, "__iter__"):
            return Response(result, status, headers, "application/octet-stream")
        raise TypeError("a handler returned %r, which cannot become a response" % (type(result).__name__,))

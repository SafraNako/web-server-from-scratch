"""A static file handler: safe path resolution, content types, validators (``ETag``, ``Last-Modified``), conditional requests (RFC 9110 section 13), byte ranges
(single and multipart, ``If-Range``), directory indexes and listings, and gzip for text.

Mount it with ``app.mount("/", StaticFiles("public"))``."""
from __future__ import annotations

import gzip
import os
import re
import secrets
import stat as statmod
import time
from collections import OrderedDict
from datetime import timezone
from email.utils import parsedate_to_datetime
from html import escape
from urllib.parse import quote, unquote

from .errors import HTTPError
from .headers import parse_quality
from .response import FileBody, Response, SizedBody, http_date

MIME = {
    ".html": "text/html", ".htm": "text/html", ".css": "text/css", ".js": "text/javascript", ".mjs": "text/javascript", ".json": "application/json",
    ".txt": "text/plain", ".md": "text/markdown", ".csv": "text/csv", ".xml": "application/xml", ".svg": "image/svg+xml", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".ico": "image/vnd.microsoft.icon", ".bmp": "image/bmp",
    ".pdf": "application/pdf", ".zip": "application/zip", ".gz": "application/gzip", ".tar": "application/x-tar", ".wasm": "application/wasm",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".mp4": "video/mp4", ".webm": "video/webm", ".woff": "font/woff", ".woff2": "font/woff2",
    ".ttf": "font/ttf", ".otf": "font/otf", ".yaml": "text/yaml", ".yml": "text/yaml", ".py": "text/x-python", ".c": "text/x-c", ".h": "text/x-c",
}
_TEXTUAL = ("text/", "application/json", "application/xml", "image/svg+xml", "text/javascript")
_COMPRESSIBLE = ("text/", "application/json", "application/xml", "image/svg+xml", "application/wasm")
_ETAG = re.compile(r'(?:W/)?"[^"]*"|\*')
MAX_RANGES = 20
GZIP_MIN, GZIP_MAX = 256, 4 * 1024 * 1024


def guess_type(path):
    import mimetypes
    ext = os.path.splitext(path)[1].lower()
    mime = MIME.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
    if mime.startswith(_TEXTUAL) or mime in _TEXTUAL:
        mime += "; charset=utf-8"
    return mime


# --- validators and conditions ---------------------------------------------------------------------------------------------------------------
def parse_http_date(text):
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _etags(header):
    return _ETAG.findall(header)


def _strip_weak(tag):
    return tag[2:] if tag.startswith("W/") else tag


def evaluate_conditions(request, etag, mtime):
    """RFC 9110 13.2.2: the 304 or 412 a conditional request earns, as a status number, or ``None`` when the request goes on to be served."""
    h = request.headers
    safe = request.method in ("GET", "HEAD")
    if_match = h.get("If-Match")
    if if_match is not None:
        tags = _etags(if_match)
        if "*" not in tags and not any(t == etag and not t.startswith("W/") for t in tags):
            return 412
    else:
        ius = h.get("If-Unmodified-Since")
        date = parse_http_date(ius) if ius else None
        if date is not None and mtime > date:
            return 412
    inm = h.get("If-None-Match")
    if inm is not None:
        tags = _etags(inm)
        if "*" in tags or any(_strip_weak(t) == _strip_weak(etag) for t in tags):
            return 304 if safe else 412
    elif safe:
        ims = h.get("If-Modified-Since")
        date = parse_http_date(ims) if ims else None
        if date is not None and mtime <= date <= time.time():                          # a date in the future is invalid (RFC 2616 14.25) and ignored, as Apache and nginx do
            return 304
    return None


def parse_ranges(header, size):
    """``None`` to ignore the header, ``"unsatisfiable"``, or a list of inclusive ``(first, last)`` pairs."""
    unit, eq, spec = header.partition("=")
    if not eq or unit.lower() != "bytes" or spec[:1] in (" ", "\t"):                 # no whitespace around the unit or right after the "="
        return None
    ranges, unsatisfiable = [], 0
    items = [p.strip() for p in spec.split(",") if p.strip()]
    if not items:
        return None
    for item in items:
        m = re.fullmatch(r"(\d*)-(\d*)", item)
        if not m or (not m.group(1) and not m.group(2)):
            return None
        first, last = m.group(1), m.group(2)
        if not first:                                                   # suffix: the last N bytes
            n = int(last)
            if n == 0 or size == 0:
                unsatisfiable += 1
                continue
            ranges.append((max(0, size - n), size - 1))
            continue
        a = int(first)
        b = int(last) if last else size - 1
        if last and b < a:
            return None                                                 # a syntax error makes the whole header invalid
        if a >= size:
            unsatisfiable += 1
            continue
        ranges.append((a, min(b, size - 1)))
    if not ranges:
        return "unsatisfiable"
    return ranges


def coalesce(ranges):
    """Overlapping or touching ranges merged into their union, in ascending order."""
    out = []
    for a, b in sorted(ranges):
        if out and a <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


# --- the handler ---------------------------------------------------------------------------------------------------------------------------
class StaticFiles:
    def __init__(self, root, index=("index.html",), listing=False, cache_control=None, gzip_text=True, hidden=False):
        self.root = os.path.realpath(root)
        if not os.path.isdir(self.root):
            raise ValueError("%r is not a directory" % root)
        self.index, self.listing, self.cache_control, self.gzip_text, self.hidden = tuple(index), listing, cache_control, gzip_text, hidden
        self._gz = OrderedDict()

    def __call__(self, request, rest):
        if request.method == "OPTIONS":
            return Response(b"", 204, [("Allow", "GET, HEAD, OPTIONS")])
        if request.method not in ("GET", "HEAD"):
            raise HTTPError(405, "only GET and HEAD are allowed here", [("Allow", "GET, HEAD, OPTIONS")])
        parts = self._resolve(rest)
        path = os.path.join(self.root, *parts) if parts else self.root
        real = os.path.realpath(path)
        if real != self.root and not real.startswith(self.root + os.sep):
            raise HTTPError(404, "no such file")                        # a symbolic link out of the root
        try:
            st = os.stat(real)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            raise HTTPError(404, "no such file")
        if statmod.S_ISDIR(st.st_mode):
            if not rest.endswith("/") and rest != "":
                target = request.raw_path + "/" + ("?" + request.query_string if request.query_string else "")
                location = quote(target, safe="/:?&=%#@!$'()*+,;~-._[]")
                return Response(('<!doctype html><title>301 Moved Permanently</title><p>Moved to <a href="%s">%s</a>.\n' % (escape(location, quote=True), escape(location))).encode("utf-8"),
                                301, [("Location", location)], "text/html; charset=utf-8")
            for name in self.index:
                candidate = os.path.join(real, name)
                if os.path.isfile(candidate):
                    return self._file(request, candidate, os.stat(candidate))
            if self.listing:
                return self._listing(request, real, request.raw_path)
            raise HTTPError(403, "directory listings are off")
        if not statmod.S_ISREG(st.st_mode):
            raise HTTPError(404, "no such file")
        if rest.endswith("/"):
            raise HTTPError(404, "no such file")
        return self._file(request, real, st)

    def _resolve(self, rest):
        parts = []
        for raw in rest.split("/"):
            seg = unquote(raw, errors="replace")
            if "/" in seg or "\x00" in seg or "\\" in seg and os.sep == "\\":
                raise HTTPError(404, "no such file")
            if seg in ("", "."):
                continue
            if seg == "..":
                if not parts:
                    raise HTTPError(400, "the path leads above the root")        # clamping would serve a different file than the one named: refuse, as Apache does
                parts.pop()
                continue
            parts.append(seg)
        if not self.hidden and any(p.startswith(".") for p in parts):
            raise HTTPError(404, "no such file")
        return parts

    # --- one file -------------------------------------------------------------------------------------------------------------------------
    def _file(self, request, path, st):
        size, mtime = st.st_size, int(st.st_mtime)
        mime = guess_type(path)
        base_etag = '"%x-%x"' % (size, st.st_mtime_ns)
        compressible = self.gzip_text and mime.startswith(_COMPRESSIBLE) and GZIP_MIN <= size <= GZIP_MAX
        use_gzip = False
        if compressible:
            q = parse_quality(request.headers.get("Accept-Encoding", ""))
            use_gzip = q.get("gzip", q.get("*", 0)) > 0 and "Range" not in request.headers
        etag = base_etag[:-1] + '-gzip"' if use_gzip else base_etag
        headers = [("Last-Modified", http_date(mtime)), ("ETag", etag)]
        if self.cache_control:
            headers.append(("Cache-Control", self.cache_control))
        if self.gzip_text and mime.startswith(_COMPRESSIBLE):
            headers.append(("Vary", "Accept-Encoding"))
        condition = evaluate_conditions(request, etag, mtime)
        if condition == 304:
            return Response(b"", 304, headers)
        if condition == 412:
            raise HTTPError(412, "a precondition failed")
        headers.append(("Content-Type", mime))
        try:
            f = open(path, "rb")
        except OSError:
            raise HTTPError(404, "no such file")
        if use_gzip:
            with f:
                data = self._gzipped(path, st, f)
            return Response(data, 200, headers + [("Content-Encoding", "gzip")])
        ranges = None
        range_header = request.headers.get("Range")
        if range_header and request.method in ("GET", "HEAD"):
            ranges = parse_ranges(range_header, size)
            if_range = request.headers.get("If-Range")
            if ranges is not None and if_range is not None and not self._if_range_ok(if_range, etag, mtime):
                ranges = None
        headers.append(("Accept-Ranges", "bytes"))
        if ranges == "unsatisfiable":
            f.close()
            raise HTTPError(416, "the range cannot be satisfied", [("Content-Range", "bytes */%d" % size)] + [h for h in headers if h[0] in ("ETag", "Last-Modified")])
        if ranges is not None:
            ranges = coalesce(ranges)
            if len(ranges) > MAX_RANGES:
                ranges = None
        if ranges is None:
            return Response(FileBody(f, 0, size), 200, headers)
        if len(ranges) == 1:
            a, b = ranges[0]
            return Response(FileBody(f, a, b - a + 1), 206, headers + [("Content-Range", "bytes %d-%d/%d" % (a, b, size))])
        boundary = secrets.token_hex(12)
        return self._multipart_response(self._multipart(f, ranges, size, mime, boundary), headers, boundary)

    @staticmethod
    def _multipart_response(body, headers, boundary):
        h = [(n, v) for n, v in headers if n != "Content-Type"] + [("Content-Type", "multipart/byteranges; boundary=" + boundary)]
        return Response(body, 206, h)

    @staticmethod
    def _if_range_ok(value, etag, mtime):
        value = value.strip()
        if value.startswith(('"', "W/")):
            return value == etag and not value.startswith("W/")        # strong comparison only
        date = parse_http_date(value)
        # a Last-Modified date is a strong validator only if it is at least a second older than "now" (RFC 9110 8.8.2.2); a file written this second could still change
        return date is not None and date == mtime and time.time() - mtime >= 1

    def _multipart(self, f, ranges, size, mime, boundary):
        parts = []
        total = 0
        for a, b in ranges:
            head = ("--%s\r\nContent-Type: %s\r\nContent-Range: bytes %d-%d/%d\r\n\r\n" % (boundary, mime, a, b, size)).encode("latin-1")
            parts.append((head, a, b))
            total += len(head) + (b - a + 1) + 2
        tail = ("--%s--\r\n" % boundary).encode("latin-1")
        total += len(tail)

        def chunks():
            try:
                for head, a, b in parts:
                    yield head
                    f.seek(a)
                    left = b - a + 1
                    while left > 0:
                        data = f.read(min(65536, left))
                        if not data:
                            break
                        left -= len(data)
                        yield data
                    yield b"\r\n"
                yield tail
            finally:
                f.close()
        return SizedBody(chunks(), total)

    def _gzipped(self, path, st, f):
        key = (path, st.st_mtime_ns, st.st_size)
        data = self._gz.get(key)
        if data is None:
            data = gzip.compress(f.read(), compresslevel=6, mtime=0)
            self._gz[key] = data
            while len(self._gz) > 64:
                self._gz.popitem(last=False)
        else:
            self._gz.move_to_end(key)
        return data

    # --- a directory ----------------------------------------------------------------------------------------------------------------------
    def _listing(self, request, directory, raw_path):
        entries = []
        for name in os.listdir(directory):
            if name.startswith(".") and not self.hidden:
                continue
            full = os.path.join(directory, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            entries.append((not statmod.S_ISDIR(st.st_mode), name.lower(), name, st))
        entries.sort()
        shown = escape(unquote(raw_path, errors="replace"))
        rows = []
        if raw_path.rstrip("/"):
            rows.append('<tr><td><a href="../">../</a></td><td></td><td></td></tr>')
        for is_file, _low, name, st in entries:
            label = name + ("" if is_file else "/")
            href = quote(name) + ("" if is_file else "/")
            rows.append('<tr><td><a href="%s">%s</a></td><td>%s</td><td>%s</td></tr>'
                        % (href, escape(label), "" if not is_file else str(st.st_size), http_date(int(st.st_mtime))))
        page = ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Index of %s</title></head><body><h1>Index of %s</h1><table>%s</table></body></html>\n"
                % (shown, shown, "".join(rows)))
        return Response(page.encode("utf-8"), 200, content_type="text/html; charset=utf-8")

"""Apache httpd as the oracle: the same files served by both, the same requests (including malformed ones) sent to both, and the answers compared.

Where the two disagree on purpose the difference is pinned here with the reason: Apache is lenient in places where this server follows the RFC more strictly (or
the other way round), and a disagreement that is *not* in these lists fails the test."""
import http.client
import os
import re
import random
import socket

import pytest

from apache import Apache, find_httpd
from helpers import running
from kendi_sunucu import App, StaticFiles

pytestmark = pytest.mark.skipif(find_httpd() is None, reason="no Apache httpd on this machine")

TYPES = {".txt": "text/plain", ".html": "text/html", ".png": "image/png"}


@pytest.fixture(scope="module")
def docroot(tmp_path_factory):
    root = tmp_path_factory.mktemp("apache-www")
    (root / "index.html").write_text("<h1>index</h1>\n")
    (root / "a.txt").write_text("0123456789")
    (root / "big.txt").write_text("".join("line %05d of a fairly compressible text file\n" % i for i in range(2000)))
    (root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20)
    (root / "sp ace.txt").write_text("with a space")
    (root / "empty.txt").write_text("")
    (root / "sub").mkdir()
    (root / "sub" / "index.html").write_text("sub index")
    (root / "bare").mkdir()
    (root / "bare" / "x.txt").write_text("x")
    old = 1600000000                                                                        # files that are not "just written" (a date validator is weak otherwise)
    for dirpath, _dirs, files in os.walk(str(root)):
        for name in files + _dirs:
            os.utime(os.path.join(dirpath, name), (old, old), follow_symlinks=False)
    return root


@pytest.fixture(scope="module")
def both(docroot):
    app = App()
    app.mount("", StaticFiles(str(docroot), gzip_text=False))
    with running(app) as ours, Apache(str(docroot), types=TYPES) as apache:
        yield ours, apache


def fetch(port, method, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.putrequest(method, path, skip_accept_encoding=True)
    for k, v in (headers or {}).items():
        c.putheader(k, v)
    c.endheaders()
    r = c.getresponse()
    body = r.read()
    headers_out = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    return r.status, headers_out, body


def parts_of(headers, body):
    """The (Content-Range, data) pairs of a multipart/byteranges body."""
    boundary = headers["content-type"].split("boundary=")[1].encode()
    out = []
    for p in body.split(b"--" + boundary):
        if not p.strip(b"-\r\n"):
            continue
        head, _, data = p.partition(b"\r\n\r\n")
        out.append((re.search(rb"(?i)content-range: ([^\r\n]+)", head).group(1), data.rstrip(b"\r\n")))
    return out


def essence(res):
    status, h, body = res
    if status >= 400 or status == 301:
        return (status, h.get("location", "") and re.sub(r"^https?://[^/]+", "", h["location"]))        # error and redirect bodies are each server's own prose
    media = h.get("content-type", "").split(";")[0]
    if media == "multipart/byteranges":
        return (status, media, h.get("accept-ranges"), tuple(parts_of(h, body)))
    return (status, media, h.get("content-length"), h.get("accept-ranges"), h.get("content-range"), h.get("last-modified"), body)


# What the two do differently on purpose.
EMPTY_FILE_RANGE = "RFC 9110 14.1.2: every range of an empty representation is unsatisfiable (416); Apache serves the empty file with 200"
DIFFERENCES = {
    ("GET", "/bare/", ()): "no index and no listing: 403 here, Apache (without mod_autoindex) says 404",
    ("HEAD", "/bare/", ()): "same",
    ("GET", "/a.txt", (("Range", "bytes=0-3, 20-30"),)): "a satisfiable and an unsatisfiable range: RFC 9110 14.2 says serve the satisfiable one (206); Apache serves 200",
    ("GET", "/a.txt", (("Range", "bytes=-0"),)): "a zero-length suffix is unsatisfiable (416); Apache serves 200",
    ("GET", "/a.txt", (("If-Unmodified-Since", "Thu, 01 Jan 1970 00:00:00 GMT"),)): "Apache cannot tell the epoch from an unparsable date (apr_date_parse_http returns 0 for both) and ignores it",
}


def documented(method, path, headers):
    """The reason this case is known to differ, or ``None``."""
    if path == "/empty.txt" and any(k == "Range" and re.fullmatch(r"bytes=(\d*-\d*)(,\s*\d*-\d*)*", v) and v not in ("bytes=-", "bytes=5-2") for k, v in headers):
        return EMPTY_FILE_RANGE
    return DIFFERENCES.get((method, path, tuple(headers)))

CASES = []
for _path in ("/a.txt", "/big.txt", "/pic.png", "/sp%20ace.txt", "/", "/sub", "/sub/", "/bare/", "/bare", "/missing", "/a.txt/", "/index.html", "/a.txt/x", "/sub/../a.txt", "/./a.txt", "//a.txt"):
    CASES += [("GET", _path, ()), ("HEAD", _path, ())]
CASES.append(("GET", "/empty.txt", ()))
for _spec in ("bytes=0-3", "bytes=5-", "bytes=-3", "bytes=8-100", "bytes=0-0", "bytes=-100", "bytes=0-3, 20-30", "bytes=2-4,3-6", "bytes=10-", "bytes=-0", "bytes=5-2", "bytes=abc",
              "items=0-3", "bytes=0-1,5-6,9-9", "bytes=0-9", "bytes=0-", "bytes=3-3", "bytes= 2-4"):
    CASES.append(("GET", "/a.txt", (("Range", _spec),)))
    CASES.append(("GET", "/empty.txt", (("Range", _spec),)))
for _spec in ("bytes=0-99", "bytes=100-199", "bytes=-50", "bytes=0-9,20-29,40-49", "bytes=5-9,0-4"):
    CASES.append(("GET", "/big.txt", (("Range", _spec),)))
    CASES.append(("GET", "/pic.png", (("Range", _spec),)))
for _k in ("If-Modified-Since", "If-Unmodified-Since"):
    for _d in ("Wed, 21 Oct 2037 00:00:00 GMT", "Thu, 01 Jan 1971 00:00:00 GMT", "Thu, 01 Jan 1970 00:00:00 GMT", "garbage", "Sunday, 06-Nov-94 08:49:37 GMT"):
        CASES.append(("GET", "/a.txt", ((_k, _d),)))
CASES += [("OPTIONS", "/a.txt", ())]


@pytest.mark.parametrize("method, path, headers", CASES, ids=["%s %s %s" % (m, p, ";".join("%s=%s" % kv for kv in h)) for m, p, h in CASES])
def test_static_files_are_served_like_apache_does(both, method, path, headers):
    ours, apache = both
    a = essence(fetch(apache.port, method, path, dict(headers)))
    o = essence(fetch(ours.port, method, path, dict(headers)))
    if method == "HEAD" and a[0] == 200:                                                        # Apache omits Content-Length on an empty HEAD response only
        a, o = a[:-1], o[:-1]
    if method == "OPTIONS":
        assert (a[0], o[0]) == (200, 204) or a[0] == o[0]
        return
    why = documented(method, path, tuple(headers))
    if why:
        assert a != o, "the difference documented for %r is gone: %s" % ((method, path, headers), why)
    else:
        assert a == o


def test_conditional_requests_with_each_servers_own_validators_agree(both):
    ours, apache = both
    for path in ("/a.txt", "/big.txt", "/pic.png"):
        results = []
        for port in (apache.port, ours.port):
            _, h, _ = fetch(port, "GET", path)
            etag, lm = h["etag"], h["last-modified"]
            outcome = []
            for headers in ({"If-None-Match": etag}, {"If-None-Match": "W/" + etag}, {"If-None-Match": '"zzz"'}, {"If-None-Match": '"zzz", ' + etag}, {"If-None-Match": "*"},
                            {"If-Match": etag}, {"If-Match": "*"}, {"If-Match": '"zzz"'}, {"If-Modified-Since": lm}, {"If-Unmodified-Since": lm},
                            {"If-None-Match": '"zzz"', "If-Modified-Since": lm},
                            {"Range": "bytes=0-3", "If-Range": etag}, {"Range": "bytes=0-3", "If-Range": lm}, {"Range": "bytes=0-3", "If-Range": '"zzz"'},
                            {"Range": "bytes=0-3", "If-Range": "W/" + etag}, {"Range": "bytes=0-3", "If-None-Match": etag}):
                s, hh, body = fetch(port, "GET", path, headers)
                outcome.append((s, len(body) if s < 300 else 0, hh.get("content-range")))
            results.append(outcome)
        assert results[0] == results[1], (path, results)


def test_if_unmodified_since_is_ignored_next_to_if_match_here_but_not_by_apache(both):
    """RFC 9110 13.1.4: a recipient MUST ignore If-Unmodified-Since when If-Match is present. Apache evaluates both."""
    ours, apache = both
    for port, expected in ((apache.port, 412), (ours.port, 200)):
        etag = fetch(port, "GET", "/a.txt")[1]["etag"]
        assert fetch(port, "GET", "/a.txt", {"If-Match": etag, "If-Unmodified-Since": "Thu, 01 Jan 1971 00:00:00 GMT"})[0] == expected


# --- malformed requests ----------------------------------------------------------------------------------------------------------------------
CRLF = b"\r\n"


def req(*lines, body=b""):
    return CRLF.join(lines) + CRLF + CRLF + body


MALFORMED = {
    "no host": (req(b"GET / HTTP/1.1"), 400), "two hosts": (req(b"GET / HTTP/1.1", b"Host: a", b"Host: b"), 400), "double space": (req(b"GET /  HTTP/1.1", b"Host: a"), 400),
    "http/1": (req(b"GET / HTTP/1", b"Host: a"), 400), "lowercase method": (req(b"get / HTTP/1.1", b"Host: a"), 501), "unknown method": (req(b"FOO / HTTP/1.1", b"Host: a"), 501),
    "space before colon": (req(b"GET / HTTP/1.1", b"Host: a", b"Foo : bar"), 400), "no colon": (req(b"GET / HTTP/1.1", b"Host: a", b"Nocolon"), 400),
    "NUL in a value": (req(b"GET / HTTP/1.1", b"Host: a", b"X: a\x00b"), 400), "bare CR": (req(b"GET / HTTP/1.1", b"Host: a", b"X: a\rb"), 400),
    "bare LF": (b"GET / HTTP/1.1\nHost: a\n\n", 400), "TE gzip": (req(b"POST / HTTP/1.1", b"Host: a", b"Transfer-Encoding: gzip", body=b"0\r\n\r\n"), 400),
    "CL mismatch": (req(b"POST / HTTP/1.1", b"Host: a", b"Content-Length: 4", b"Content-Length: 5", body=b"abcde"), 400),
    "CL negative": (req(b"POST / HTTP/1.1", b"Host: a", b"Content-Length: -1"), 400), "CL plus": (req(b"POST / HTTP/1.1", b"Host: a", b"Content-Length: +4", body=b"abcd"), 400),
    "target without slash": (req(b"GET a.txt HTTP/1.1", b"Host: a"), 400), "fragment": (req(b"GET /a.txt#x HTTP/1.1", b"Host: a"), 400),
    "asterisk with GET": (req(b"GET * HTTP/1.1", b"Host: a"), 400), "long target": (req(b"GET /" + b"a" * 9000 + b" HTTP/1.1", b"Host: a"), 414),
    "many headers": (req(b"GET / HTTP/1.1", b"Host: a", *[b"X-%d: 1" % i for i in range(150)]), 400), "big header": (req(b"GET / HTTP/1.1", b"Host: a", b"X: " + b"v" * 9000), 400),
    "bad host": (req(b"GET / HTTP/1.1", b"Host: a b"), 400), "unknown expectation": (req(b"GET / HTTP/1.1", b"Host: a", b"Expect: 200-ok"), 417),
    "bad chunk size": (req(b"POST / HTTP/1.1", b"Host: a", b"Transfer-Encoding: chunked", body=b"zz\r\n"), 400),
}
# where this server answers with a different (still refusing) status than Apache
OTHER_REFUSAL = {"many headers", "big header"}                                                    # 431 here, Apache just says 400
# where Apache is lenient and this server refuses
STRICTER = {
    "http/2.0": (req(b"GET / HTTP/2.0", b"Host: a"), 505, "Apache serves it as HTTP/1.1; RFC 9112 2.3 says another major version gets 505"),
    "folded header": (req(b"GET / HTTP/1.1", b"Host: a", b"X: a", b" b"), 400, "obs-fold: RFC 9112 5.2 says a server MUST reject it or replace the fold, and rejecting is the safe choice"),
    "Content-Length with Transfer-Encoding": (req(b"POST / HTTP/1.1", b"Host: a", b"Content-Length: 4", b"Transfer-Encoding: chunked", body=b"0\r\n\r\n"), 400,
                                              "RFC 9112 6.1: this is the request-smuggling pair; Apache lets Transfer-Encoding win"),
    "TRACE": (req(b"TRACE / HTTP/1.1", b"Host: a"), 501, "Apache (TraceEnable off) says 405 Method Not Allowed; here the method is not implemented at all"),
}


def status_of(port, data):
    s = socket.create_connection(("127.0.0.1", port), timeout=3)
    s.sendall(data)
    try:
        s.shutdown(socket.SHUT_WR)
    except OSError:
        pass
    out = b""
    try:
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    except (socket.timeout, ConnectionError):
        pass
    s.close()
    return int(out[9:12]) if out.startswith(b"HTTP/") else 0


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_malformed_requests_are_refused_by_both(both, name):
    ours, apache = both
    data, expected = MALFORMED[name]
    a, o = status_of(apache.port, data), status_of(ours.port, data)
    assert a == expected, "Apache said %d" % a                                                  # (pins the oracle's own behaviour)
    assert o == (431 if name in OTHER_REFUSAL else expected)


@pytest.mark.parametrize("name", sorted(STRICTER))
def test_where_apache_is_lenient_this_server_refuses(both, name):
    ours, apache = both
    data, status, _reason = STRICTER[name]
    assert status_of(apache.port, data) in (200, 405)
    assert status_of(ours.port, data) == status


def test_both_accept_what_is_fine(both):
    ours, apache = both
    for data in (req(b"GET / HTTP/1.0"), req(b"GET / HTTP/1.1", b"Host: a", b"Content-Length: 3", body=b"abc"), req(b"GET /a.txt HTTP/1.1", b"Host: a", b"X-Odd: tab\there"),
                 req(b"GET / HTTP/1.1", b"Host: a", b"Accept: a", b"Accept: b")):
        assert status_of(apache.port, data) == status_of(ours.port, data) == 200
    assert os.path.exists(find_httpd())


# Host header values: Apache and this server draw the same line for host names, ports and IP literals; where they differ the reason is pinned
@pytest.mark.parametrize("host", ["example.org", "example.org:8080", "127.0.0.1:80", "[::1]", "[::1]:8000", "[fe80::1]", "xn--bcher-kva.example",
                                  "a b", "a@b", "a/b", "a\\b", "a]b", "a?b", "a#b", "a<b>", 'a"b', "a'b", "a;b", "a,b", "a%zzb", "[::1", "a:80:90"])
def test_host_header_values_are_judged_like_apache_does(both, host):
    ours, apache = both
    data = req(b"GET /a.txt HTTP/1.1", b"Host: " + host.encode(), b"Connection: close")
    assert status_of(apache.port, data) // 100 == status_of(ours.port, data) // 100


@pytest.mark.parametrize("host, why", [("a-b_c.d~e", "'~' is an unreserved character of RFC 3986; Apache only knows letters, digits, '-', '.' and '_'"),
                                       ("a%20b", "a well-formed percent-escape is part of the reg-name grammar; Apache refuses any '%'"),
                                       ("", "RFC 9112 3.2 allows an empty Host for a target that has no authority; Apache wants a name"),
                                       ("a:", "an empty port is legal in RFC 3986 3.2.3; Apache refuses it")])
def test_host_values_that_are_legal_but_that_apache_refuses(both, host, why):
    ours, apache = both
    data = req(b"GET /a.txt HTTP/1.1", b"Host: " + host.encode(), b"Connection: close")
    assert status_of(apache.port, data) == 400, why
    assert status_of(ours.port, data) == 200, why


def test_random_mutations_never_get_through_here_when_apache_refuses_them(both):
    """Differential fuzzing: byte-level mutations of a valid request (seeded, so a failure repeats). Whatever Apache refuses with a 4xx/5xx must not be answered with a 2xx here.
    The other direction -- refused here, accepted or silently dropped by Apache -- is allowed (truncated heads and broken request lines are the usual cases) but never a 5xx."""
    ours, apache = both
    base = b"GET /a.txt?x=1 HTTP/1.1\r\nHost: example.org\r\nUser-Agent: fuzz/1.0\r\nAccept: */*\r\nX-Custom: some value\r\nConnection: close\r\n\r\n"
    interesting = b" \t\r\n:;,/\\%#?\x00\xff\x7f\"'<>@[]{}"
    rng = random.Random(11)
    lenient, errors = [], []
    for _ in range(600):
        data = bytearray(base)
        for _step in range(rng.randint(1, 3)):
            kind, pos = rng.random(), rng.randrange(len(data))
            if kind < 0.4:
                data[pos] = rng.choice(interesting)
            elif kind < 0.7:
                data.insert(pos, rng.choice(interesting))
            else:
                del data[pos:pos + rng.randint(1, 4)]
        data = bytes(data)
        a, o = status_of(apache.port, data), status_of(ours.port, data)
        if a >= 400 and 200 <= o < 300:
            lenient.append((data, a, o))
        if o >= 500 and o != 501:
            errors.append((data, o))
    assert not lenient, lenient[:3]
    assert not errors, errors[:3]

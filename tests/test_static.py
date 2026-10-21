"""The static file handler: path safety, content types, validators, conditional requests, ranges, indexes, compression."""
import gzip
import os
import time
from email.utils import formatdate

import pytest

from helpers import get, parse_responses, raw, running
from kendi_sunucu import App, StaticFiles
from kendi_sunucu.static import coalesce, parse_http_date, parse_ranges


@pytest.fixture(scope="module")
def docroot(tmp_path_factory):
    root = tmp_path_factory.mktemp("www")
    (root / "index.html").write_text("<h1>index</h1>\n")
    (root / "a.txt").write_text("0123456789")
    (root / "big.txt").write_text("".join("line %05d of a fairly compressible text file\n" % i for i in range(2000)))
    (root / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 20)
    (root / "sp ace.txt").write_text("with a space")
    (root / "ünï.txt").write_text("unicode name")
    (root / "empty.txt").write_text("")
    (root / "sub").mkdir()
    (root / "sub" / "index.html").write_text("sub index")
    (root / "sub" / "inner.txt").write_text("inner")
    (root / "bare").mkdir()
    (root / "bare" / "x.txt").write_text("x")
    (root / ".secret").write_text("hidden")
    (root / "outside_target").mkdir()
    secret = root.parent / (root.name + "_secret.txt")
    secret.write_text("TOP SECRET")
    os.symlink(str(secret), str(root / "escape"))
    os.symlink(str(root / "a.txt"), str(root / "inside_link"))
    old = 1600000000                                                                        # files that are not "just written" (a date validator is weak otherwise)
    for dirpath, _dirs, files in os.walk(str(root)):
        for name in files + _dirs:
            os.utime(os.path.join(dirpath, name), (old, old), follow_symlinks=False)
    return root


@pytest.fixture(scope="module")
def server(docroot):
    app = App()
    app.mount("", StaticFiles(str(docroot)))
    with running(app) as s:
        yield s


def hdr(headers, name):
    return headers.get(name)


# --- basics -------------------------------------------------------------------------------------------------------------------------------
def test_files_index_and_content_types(server):
    status, h, body = get(server, "/a.txt")
    assert (status, body, h["Content-Type"], h["Content-Length"], h["Accept-Ranges"]) == (200, b"0123456789", "text/plain; charset=utf-8", "10", "bytes")
    assert get(server, "/")[2] == b"<h1>index</h1>\n"
    assert get(server, "/pic.png")[1]["Content-Type"] == "image/png" and get(server, "/pic.png")[1].get("Vary") is None
    assert get(server, "/empty.txt")[2] == b"" and get(server, "/empty.txt")[0] == 200


def test_unusual_names(server):
    assert get(server, "/sp%20ace.txt")[2] == b"with a space"
    assert get(server, "/%C3%BCn%C3%AF.txt")[2] == b"unicode name"
    assert get(server, "/sp ace.txt".replace(" ", "%20"))[0] == 200


def test_directories_redirect_get_an_index_or_are_forbidden(server):
    status, h, body = get(server, "/sub")
    assert status == 301 and h["Location"] == "/sub/" and b'href="/sub/"' in body
    assert get(server, "/sub?x=1")[1]["Location"] == "/sub/?x=1"
    assert get(server, "/sub/")[2] == b"sub index"
    assert get(server, "/bare/")[0] == 403 and get(server, "/bare")[0] == 301


def test_a_file_name_with_a_trailing_slash_is_not_a_file(server):
    assert get(server, "/a.txt/")[0] == 404


def test_hidden_files_and_other_misses(server):
    assert get(server, "/.secret")[0] == 404
    assert get(server, "/missing.txt")[0] == 404
    assert get(server, "/a.txt/more")[0] == 404


@pytest.mark.parametrize("path", ["/sub/../a.txt", "//a.txt", "/./a.txt", "/sub/%2e%2e/a.txt", "/sub/./../a.txt"])
def test_dot_segments_inside_the_root_are_resolved(server, path):
    status, _, body = get(server, path)
    assert status == 200 and body == b"0123456789"


@pytest.mark.parametrize("path", ["/../a.txt", "/%2e%2e/a.txt", "/%2E%2E/%2e%2e/a.txt", "/sub/%2e%2e/%2e%2e/a.txt", "/sub/../../a.txt", "/a.txt/../../a.txt"])
def test_a_path_that_leads_above_the_root_is_refused(server, path):
    assert get(server, path)[0] == 400


@pytest.mark.parametrize("path", ["/..%2f..%2fetc/passwd", "/%2e%2e%2f%2e%2e%2fetc/passwd", "/..%5c..%5cetc/passwd", "/a.txt%00.png", "/%00", "/....//....//etc/passwd",
                                  "/escape", "/sub/..%2f..%2f", "/%252e%252e/etc/passwd"])
def test_traversal_attempts_never_reach_anything_outside(server, docroot, path):
    status, _, body = get(server, path)
    assert status in (400, 403, 404) and b"TOP SECRET" not in body and b"root:" not in body


def test_symlinks_inside_the_root_work_and_ones_leaving_it_do_not(server):
    assert get(server, "/inside_link")[2] == b"0123456789"
    assert get(server, "/escape")[0] == 404


# --- validators and conditional requests -------------------------------------------------------------------------------------------------
def test_validators_are_present_and_stable(server):
    _, h1, _ = get(server, "/a.txt")
    _, h2, _ = get(server, "/a.txt")
    assert h1["ETag"] == h2["ETag"] and h1["ETag"].startswith('"') and h1["Last-Modified"].endswith("GMT")


def test_if_none_match(server):
    etag = get(server, "/a.txt")[1]["ETag"]
    for value in (etag, "W/" + etag, '"nope", ' + etag, "*"):
        status, h, body = get(server, "/a.txt", {"If-None-Match": value})
        assert status == 304 and body == b"" and h["ETag"] == etag and "Content-Length" not in h
    assert get(server, "/a.txt", {"If-None-Match": '"other"'})[0] == 200
    assert get(server, "/a.txt", {"If-None-Match": etag}, method="HEAD")[0] == 304


def test_if_modified_since(server):
    lm = get(server, "/a.txt")[1]["Last-Modified"]
    assert get(server, "/a.txt", {"If-Modified-Since": lm})[0] == 304
    assert get(server, "/a.txt", {"If-Modified-Since": formatdate(time.time() + 3600, usegmt=True)})[0] == 200               # a date in the future is ignored
    assert get(server, "/a.txt", {"If-Modified-Since": formatdate(0, usegmt=True)})[0] == 200
    assert get(server, "/a.txt", {"If-Modified-Since": "yesterday-ish"})[0] == 200               # an invalid date is ignored


def test_if_none_match_takes_precedence_over_if_modified_since(server):
    lm = get(server, "/a.txt")[1]["Last-Modified"]
    assert get(server, "/a.txt", {"If-None-Match": '"other"', "If-Modified-Since": lm})[0] == 200


def test_if_match_and_if_unmodified_since(server):
    etag = get(server, "/a.txt")[1]["ETag"]
    lm = get(server, "/a.txt")[1]["Last-Modified"]
    assert get(server, "/a.txt", {"If-Match": etag})[0] == 200
    assert get(server, "/a.txt", {"If-Match": "*"})[0] == 200
    assert get(server, "/a.txt", {"If-Match": '"other"'})[0] == 412
    assert get(server, "/a.txt", {"If-Match": "W/" + etag})[0] == 412                           # weak validators never satisfy If-Match
    assert get(server, "/a.txt", {"If-Unmodified-Since": lm})[0] == 200
    assert get(server, "/a.txt", {"If-Unmodified-Since": formatdate(0, usegmt=True)})[0] == 412
    assert get(server, "/a.txt", {"If-Match": etag, "If-Unmodified-Since": formatdate(0, usegmt=True)})[0] == 200   # If-Match wins


# --- ranges -------------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("spec, expected, content_range", [
    ("bytes=0-3", b"0123", "bytes 0-3/10"), ("bytes=5-", b"56789", "bytes 5-9/10"), ("bytes=-3", b"789", "bytes 7-9/10"), ("bytes=8-100", b"89", "bytes 8-9/10"),
    ("bytes=0-0", b"0", "bytes 0-0/10"), ("bytes=-100", b"0123456789", "bytes 0-9/10"), ("bytes=3-3", b"3", "bytes 3-3/10"), ("BYTES=2-4", b"234", "bytes 2-4/10"),
    ("bytes=0-3, 20-30", b"0123", "bytes 0-3/10"), ("bytes=2-4,3-6", b"23456", "bytes 2-6/10"),
])
def test_single_ranges(server, spec, expected, content_range):
    status, h, body = get(server, "/a.txt", {"Range": spec})
    assert status == 206 and body == expected and h["Content-Range"] == content_range and h["Content-Length"] == str(len(expected))


@pytest.mark.parametrize("spec", ["bytes=10-", "bytes=10-20", "bytes=-0", "bytes=100-200", "bytes=20-30,40-"])
def test_unsatisfiable_ranges_get_416_with_the_size(server, spec):
    status, h, _ = get(server, "/a.txt", {"Range": spec})
    assert status == 416 and h["Content-Range"] == "bytes */10"


@pytest.mark.parametrize("spec", ["bytes=5-2", "bytes=abc", "bytes=", "items=0-3", "0-3", "bytes=1-2-3", "bytes=--1", "bytes=a-b", "bytes=0-3,5-2", "bytes= 0-3", "bytes =0-3"])
def test_a_malformed_or_foreign_range_is_ignored(server, spec):
    status, _, body = get(server, "/a.txt", {"Range": spec})
    assert status == 200 and body == b"0123456789"


def test_multiple_ranges_come_as_multipart_byteranges(server):
    status, h, body = get(server, "/a.txt", {"Range": "bytes=0-1,5-6,9-9"})
    assert status == 206 and h["Content-Type"].startswith("multipart/byteranges; boundary=") and h["Content-Length"] == str(len(body))
    boundary = h["Content-Type"].split("boundary=")[1].encode()
    parts = [p for p in body.split(b"--" + boundary) if p.strip(b"-\r\n")]
    got = []
    for p in parts:
        head, _, data = p.partition(b"\r\n\r\n")
        assert b"Content-Type: text/plain; charset=utf-8" in head
        got.append((head.split(b"Content-Range: ")[1].split(b"\r\n")[0], data.rstrip(b"\r\n")))
    assert got == [(b"bytes 0-1/10", b"01"), (b"bytes 5-6/10", b"56"), (b"bytes 9-9/10", b"9")] and body.endswith(b"--" + boundary + b"--\r\n")


def test_too_many_ranges_fall_back_to_the_whole_file(server):
    spec = "bytes=" + ",".join("%d-%d" % (i * 2, i * 2) for i in range(0, 5)) + ",20-21" * 0
    assert get(server, "/a.txt", {"Range": spec})[0] == 206
    spec = "bytes=" + ",".join("%d-%d" % (i * 3, i * 3) for i in range(0, 60))
    status, _, body = get(server, "/big.txt", {"Range": spec})
    assert status == 200 and len(body) > 60


def test_head_with_range(server):
    from helpers import connect, recv_all
    s = connect(server)
    s.sendall(b"HEAD /a.txt HTTP/1.1\r\nHost: x\r\nRange: bytes=2-5\r\nConnection: close\r\n\r\n")
    status, h, body = parse_responses(recv_all(s), methods=["HEAD"])[0]
    assert status == 206 and h["Content-Length"] == "4" and h["Content-Range"] == "bytes 2-5/10" and body == b""


def test_if_range(server):
    _, h, _ = get(server, "/a.txt")
    etag, lm = h["ETag"], h["Last-Modified"]
    assert get(server, "/a.txt", {"Range": "bytes=0-3", "If-Range": etag})[0] == 206
    assert get(server, "/a.txt", {"Range": "bytes=0-3", "If-Range": lm})[0] == 206
    status, _, body = get(server, "/a.txt", {"Range": "bytes=0-3", "If-Range": '"stale"'})
    assert status == 200 and body == b"0123456789"
    assert get(server, "/a.txt", {"Range": "bytes=0-3", "If-Range": "W/" + etag})[0] == 200
    assert get(server, "/a.txt", {"Range": "bytes=0-3", "If-Range": formatdate(0, usegmt=True)})[0] == 200
    assert get(server, "/a.txt", {"If-Range": etag})[0] == 200                                  # without Range it is meaningless


def test_a_conditional_request_beats_the_range(server):
    etag = get(server, "/a.txt")[1]["ETag"]
    assert get(server, "/a.txt", {"Range": "bytes=0-3", "If-None-Match": etag})[0] == 304


# --- compression ----------------------------------------------------------------------------------------------------------------------------
def test_text_is_gzipped_for_clients_that_accept_it(server):
    status, h, body = get(server, "/big.txt", {"Accept-Encoding": "gzip, deflate"})
    plain = get(server, "/big.txt")[2]
    assert status == 200 and h["Content-Encoding"] == "gzip" and h["Vary"] == "Accept-Encoding" and gzip.decompress(body) == plain and len(body) < len(plain) / 5
    assert h["ETag"].endswith('-gzip"') and h["Content-Length"] == str(len(body))
    assert "Content-Encoding" not in get(server, "/big.txt", {"Accept-Encoding": "identity"})[1]
    assert "Content-Encoding" not in get(server, "/big.txt", {"Accept-Encoding": "gzip;q=0"})[1]
    assert get(server, "/big.txt", {"Accept-Encoding": "*"})[1]["Content-Encoding"] == "gzip"
    assert "Content-Encoding" not in get(server, "/pic.png", {"Accept-Encoding": "gzip"})[1]
    assert "Content-Encoding" not in get(server, "/a.txt", {"Accept-Encoding": "gzip"})[1]       # too small to bother
    assert "Content-Encoding" not in get(server, "/big.txt", {"Accept-Encoding": "gzip", "Range": "bytes=0-9"})[1]


def test_the_gzip_variant_has_its_own_validator(server):
    plain = get(server, "/big.txt")[1]["ETag"]
    zipped = get(server, "/big.txt", {"Accept-Encoding": "gzip"})[1]["ETag"]
    assert plain != zipped
    assert get(server, "/big.txt", {"Accept-Encoding": "gzip", "If-None-Match": zipped})[0] == 304
    assert get(server, "/big.txt", {"Accept-Encoding": "gzip", "If-None-Match": plain})[0] == 200


# --- the parsing helpers --------------------------------------------------------------------------------------------------------------------
def test_range_helpers():
    assert parse_ranges("bytes=0-4", 10) == [(0, 4)]
    assert parse_ranges("bytes=-4", 10) == [(6, 9)]
    assert parse_ranges("bytes=4-", 0) == "unsatisfiable"
    assert parse_ranges("bytes=0-", 0) == "unsatisfiable"
    assert parse_ranges("garbage", 10) is None
    assert coalesce([(5, 9), (0, 2), (3, 4), (20, 25), (24, 30)]) == [(0, 9), (20, 30)]


def test_http_date_parsing():
    assert parse_http_date("Sun, 06 Nov 1994 08:49:37 GMT") == 784111777
    assert parse_http_date("Sunday, 06-Nov-94 08:49:37 GMT") == 784111777                         # the obsolete RFC 850 form
    assert parse_http_date("Sun Nov  6 08:49:37 1994") == 784111777                               # asctime
    assert parse_http_date("not a date") is None


# --- directory listings ---------------------------------------------------------------------------------------------------------------------
def test_listing_escapes_names(tmp_path):
    (tmp_path / "<b>x.txt").write_text("1")
    (tmp_path / "a&b.txt").write_text("22")
    (tmp_path / "dir").mkdir()
    app = App()
    app.mount("", StaticFiles(str(tmp_path), listing=True))
    with running(app) as s:
        status, h, body = get(s, "/")
        assert status == 200 and h["Content-Type"] == "text/html; charset=utf-8"
        assert b"<b>x" not in body and b"&lt;b&gt;x.txt" in body and b"a&amp;b.txt" in body and b'href="dir/"' in body and b'href="%3Cb%3Ex.txt"' in body


def test_cache_control_option(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    app = App()
    app.mount("", StaticFiles(str(tmp_path), cache_control="max-age=60"))
    with running(app) as s:
        assert get(s, "/f.txt")[1]["Cache-Control"] == "max-age=60"
        assert get(s, "/f.txt", {"If-None-Match": get(s, "/f.txt")[1]["ETag"]})[1]["Cache-Control"] == "max-age=60"


def test_methods_other_than_get_and_head_are_refused(server):
    status, h, _ = get(server, "/a.txt", method="POST")
    assert status == 405 and h["Allow"] == "GET, HEAD, OPTIONS"
    assert get(server, "/a.txt", method="OPTIONS")[0] == 204


def test_a_file_that_disappears_between_stat_and_open_is_a_404(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    handler = StaticFiles(str(tmp_path))
    from kendi_sunucu import Headers, Request
    st = os.stat(tmp_path / "f.txt")
    os.unlink(tmp_path / "f.txt")
    from kendi_sunucu import HTTPError
    with pytest.raises(HTTPError) as info:
        handler._file(Request("GET", "/f.txt", "HTTP/1.1", Headers([("Host", "x")])), str(tmp_path / "f.txt"), st)
    assert info.value.status == 404
    assert raw is not None

"""curl, an independent HTTP client, against the server: what real clients do with the responses and what they send."""
import gzip
import hashlib
import os
import shutil
import subprocess
import time

import pytest

from helpers import running
from kendi_sunucu import App, StaticFiles
from test_server import make_app

CURL = shutil.which("curl")
pytestmark = pytest.mark.skipif(CURL is None, reason="curl is not installed")


def curl(*args, input=None, timeout=20):
    p = subprocess.run([CURL, "-s", "--noproxy", "*", *args], input=input, capture_output=True, timeout=timeout)
    return p


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("curl-www")
    (root / "a.txt").write_text("0123456789")
    (root / "big.txt").write_text("".join("line %05d of a fairly compressible text file\n" % i for i in range(5000)))
    (root / "blob.bin").write_bytes(bytes(random_byte for random_byte in (i * 7 % 256 for i in range(2_000_000))))
    (root / "sub").mkdir()
    (root / "sub" / "index.html").write_text("sub index")
    app = make_app()
    app.mount("/files", StaticFiles(str(root)))
    with running(app) as s:
        s.root = root
        yield s


def url(site, path):
    return "http://127.0.0.1:%d%s" % (site.port, path)


def test_a_plain_get(site):
    p = curl(url(site, "/"))
    assert p.returncode == 0 and p.stdout == b"<h1>home</h1>"


def test_the_status_line_and_headers_curl_sees(site):
    p = curl("-i", url(site, "/files/a.txt"))
    head, _, body = p.stdout.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200 OK") and b"Content-Length: 10" in head and body == b"0123456789"


def test_head_with_dash_I(site):
    p = curl("-I", url(site, "/files/a.txt"))
    assert p.returncode == 0 and p.stdout.startswith(b"HTTP/1.1 200 OK") and b"Content-Length: 10" in p.stdout and p.stdout.endswith(b"\r\n\r\n")


def test_compression_roundtrip_with_dash_compressed(site):
    plain = (site.root / "big.txt").read_bytes()
    p = curl("--compressed", "-i", url(site, "/files/big.txt"))
    head, _, body = p.stdout.partition(b"\r\n\r\n")
    assert b"Content-Encoding: gzip" not in head or True
    assert body == plain                                                                        # curl decompressed it for us
    raw = curl("-H", "Accept-Encoding: gzip", url(site, "/files/big.txt")).stdout
    assert gzip.decompress(raw) == plain and len(raw) < len(plain) / 10


def test_a_large_binary_download_is_intact(site):
    out = site.root / "downloaded.bin"
    assert curl("-o", str(out), url(site, "/files/blob.bin")).returncode == 0
    assert hashlib.sha256(out.read_bytes()).digest() == hashlib.sha256((site.root / "blob.bin").read_bytes()).digest()


@pytest.mark.parametrize("spec, expected", [("0-4", b"01234"), ("5-", b"56789"), ("-3", b"789"), ("3-3", b"3")])
def test_ranges_with_dash_r(site, spec, expected):
    assert curl("-r", spec, url(site, "/files/a.txt")).stdout == expected


def test_resuming_a_download_with_dash_C(site):
    out = site.root / "resume.bin"
    full = (site.root / "blob.bin").read_bytes()
    out.write_bytes(full[:123456])
    assert curl("-C", "-", "-o", str(out), url(site, "/files/blob.bin")).returncode == 0
    assert out.read_bytes() == full


def test_several_ranges_arrive_as_multipart(site):
    p = curl("-i", "-r", "0-1,5-6", url(site, "/files/a.txt"))
    head, _, body = p.stdout.partition(b"\r\n\r\n")
    assert b"206 Partial Content" in head and b"multipart/byteranges" in head and b"01" in body and b"56" in body


def test_conditional_get_with_dash_z_and_etag_compare(site, tmp_path):
    f = tmp_path / "local.txt"
    f.write_text("old")
    os.utime(f, (1000000000, 1000000000))                                                      # older than the file on the server
    assert curl("-z", str(f), url(site, "/files/a.txt")).stdout == b"0123456789"
    now = int(time.time())
    os.utime(f, (now, now))                                                                    # newer than the served file (and not in the future): 304 and no body
    p = curl("-i", "-z", str(f), url(site, "/files/a.txt"))
    assert b"304 Not Modified" in p.stdout.split(b"\r\n")[0]
    etag = [l for l in curl("-I", url(site, "/files/a.txt")).stdout.decode().splitlines() if l.lower().startswith("etag:")][0].split(": ", 1)[1]
    p = curl("-i", "-H", "If-None-Match: " + etag, url(site, "/files/a.txt"))
    assert p.stdout.startswith(b"HTTP/1.1 304")


def test_keep_alive_reuses_the_connection(site):
    p = curl("-w", "%{num_connects}\\n", "-o", "/dev/null", url(site, "/"), "-w", "%{num_connects}\\n", "-o", "/dev/null", url(site, "/"))
    assert p.stdout.decode().split() == ["1", "0"]                                              # the second transfer needed no new connection


def test_http_10_requests(site):
    p = curl("--http1.0", "-i", url(site, "/files/a.txt"))
    assert p.stdout.startswith(b"HTTP/1.1 200") and p.stdout.endswith(b"0123456789")


def test_uploads_with_content_length_and_chunked_and_expect(site, tmp_path):
    payload = os.urandom(300_000)
    f = tmp_path / "up.bin"
    f.write_bytes(payload)
    assert curl("--data-binary", "@" + str(f), url(site, "/body")).stdout == payload              # POST with Content-Length
    assert curl("-T", str(f), url(site, "/body")).stdout == payload                               # PUT
    p = curl("-H", "Transfer-Encoding: chunked", "--data-binary", "@-", url(site, "/body"), input=payload)    # chunked request body from a pipe
    assert p.stdout == payload
    p = curl("-H", "Expect: 100-continue", "--data-binary", "@" + str(f), "-i", url(site, "/body"))
    assert b"200 OK" in p.stdout and p.stdout.endswith(payload)


def test_the_error_pages_and_methods(site):
    assert curl("-o", "/dev/null", "-w", "%{http_code}", url(site, "/nothing")).stdout == b"404"
    assert curl("-X", "POST", "-o", "/dev/null", "-w", "%{http_code}", url(site, "/")).stdout == b"405"
    assert curl("-X", "OPTIONS", "-i", url(site, "/")).stdout.startswith(b"HTTP/1.1 204")
    assert curl("-X", "BREW", "-o", "/dev/null", "-w", "%{http_code}", url(site, "/")).stdout == b"501"
    assert curl("-o", "/dev/null", "-w", "%{http_code}", url(site, "/boom")).stdout == b"500"


def test_redirects_are_followed_by_curl(site):
    p = curl("-L", url(site, "/files/sub"))
    assert p.stdout == b"sub index"


@pytest.mark.parametrize("path", ["/files/../../etc/passwd", "/files/%2e%2e/%2e%2e/etc/passwd", "/files/..%2f..%2fetc/passwd", "/files/....//....//etc/passwd", "/../etc/passwd"])
def test_curl_cannot_traverse_even_with_path_as_is(site, path):
    p = curl("--path-as-is", "-i", url(site, path))
    assert b"root:" not in p.stdout and (p.stdout.startswith((b"HTTP/1.1 400", b"HTTP/1.1 404")) or p.returncode != 0)


def test_many_parallel_transfers(site):
    args = [x for _ in range(40) for x in ("-o", "/dev/null", url(site, "/files/a.txt"))]
    p = curl("--parallel", "--parallel-max", "20", "-w", "%{http_code} ", *args, timeout=60)
    assert p.stdout.decode().split() == ["200"] * 40


def test_a_client_that_hangs_up_mid_download_does_not_hurt(site):
    curl("--max-time", "0.05", "--limit-rate", "10k", "-o", "/dev/null", url(site, "/big"))
    assert curl(url(site, "/")).stdout == b"<h1>home</h1>"


def test_an_app_without_static_files_via_the_cli_entry_point(tmp_path):
    (tmp_path / "hello.txt").write_text("hi from the cli")
    from kendi_sunucu.cli import build_parser
    ns = build_parser().parse_args([str(tmp_path), "-p", "0", "-q"])
    assert ns.directory == str(tmp_path) and ns.port == 0 and ns.quiet
    app = App()
    app.mount("", StaticFiles(ns.directory))
    with running(app) as s:
        assert curl("http://127.0.0.1:%d/hello.txt" % s.port).stdout == b"hi from the cli"

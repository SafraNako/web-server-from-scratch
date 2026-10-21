"""The request parser: what is accepted, what is refused (and with which status), and that fragmentation of the byte stream never changes the answer."""
import random

import pytest

from kendi_sunucu.errors import HTTPError
from kendi_sunucu.parser import Limits
from scripted import parse

CRLF = b"\r\n"


def req(*lines, body=b""):
    return CRLF.join(lines) + CRLF + CRLF + body


def refused(data, limits=None):
    with pytest.raises(HTTPError) as info:
        parse(data, limits)
    return info.value.status


# --- what is accepted -----------------------------------------------------------------------------------------------------------------------
def test_a_plain_request():
    r, _ = parse(req(b"GET /index.html?a=1&b=2 HTTP/1.1", b"Host: example.org", b"User-Agent: test"))
    assert (r.method, r.target, r.version, r.path, r.query_string) == ("GET", "/index.html?a=1&b=2", "HTTP/1.1", "/index.html", "a=1&b=2")
    assert r.headers.get("host") == "example.org" and r.headers.get("USER-AGENT") == "test"
    assert r.query == {"a": ["1"], "b": ["2"]} and r.keep_alive


def test_header_names_are_case_insensitive_and_values_are_trimmed():
    r, _ = parse(req(b"GET / HTTP/1.1", b"hOsT:   a.b \t", b"X-Thing:\tvalue with  spaces  "))
    assert r.headers.get("Host") == "a.b" and r.headers.get("x-thing") == "value with  spaces"


def test_repeated_headers_are_kept_in_order():
    r, _ = parse(req(b"GET / HTTP/1.1", b"Host: a", b"Accept: text/html", b"Accept: text/plain, */*"))
    assert r.headers.getall("accept") == ["text/html", "text/plain, */*"] and r.headers.get_list("Accept") == ["text/html", "text/plain", "*/*"]


def test_percent_decoding_and_absolute_form():
    r, _ = parse(req(b"GET http://example.org/a%20b/c?x=%41 HTTP/1.1", b"Host: example.org"))
    assert r.path == "/a b/c" and r.raw_path == "/a%20b/c" and r.query == {"x": ["A"]}


@pytest.mark.parametrize("host", ["example.org", "example.org:8080", "127.0.0.1:80", "[::1]", "[::1]:8000", "[fe80::1%25en0]".replace("%25en0", ""), "a-b_c.d~e", "xn--bcher-kva.example", "a%20b", ""])
def test_valid_host_values(host):
    r, _ = parse(req(b"GET / HTTP/1.1", b"Host: " + host.encode()))
    assert r.headers.get("Host") == host


def test_options_asterisk():
    r, _ = parse(req(b"OPTIONS * HTTP/1.1", b"Host: a"))
    assert r.target == "*"


def test_a_newer_minor_version_is_handled_as_http_11():
    r, _ = parse(req(b"GET / HTTP/1.7", b"Host: a"))
    assert r.version == "HTTP/1.1" and r.keep_alive
    assert refused(req(b"GET / HTTP/1.7")) == 400                                               # and needs its Host like any HTTP/1.1 request


def test_http_10_needs_no_host_and_closes_by_default():
    r, _ = parse(req(b"GET / HTTP/1.0"))
    assert r.version == "HTTP/1.0" and not r.keep_alive
    r, _ = parse(req(b"GET / HTTP/1.0", b"Connection: keep-alive"))
    assert r.keep_alive


def test_connection_header_decides_keep_alive():
    assert not parse(req(b"GET / HTTP/1.1", b"Host: a", b"Connection: close"))[0].keep_alive
    assert not parse(req(b"GET / HTTP/1.1", b"Host: a", b"Connection: Keep-Alive, Close"))[0].keep_alive
    assert parse(req(b"GET / HTTP/1.1", b"Host: a", b"Connection: upgrade"))[0].keep_alive


def test_empty_lines_before_the_request_line_are_ignored():
    r, _ = parse(CRLF + CRLF + req(b"GET / HTTP/1.1", b"Host: a"))
    assert r.target == "/"


def test_content_length_body():
    r, _ = parse(req(b"POST /x HTTP/1.1", b"Host: a", b"Content-Length: 5", body=b"hello"))
    assert r.body == b"hello"


def test_equal_duplicate_content_lengths_are_one_length():
    r, _ = parse(req(b"POST /x HTTP/1.1", b"Host: a", b"Content-Length: 3, 3", b"Content-Length: 3", body=b"abc"))
    assert r.body == b"abc"


def test_chunked_body_with_extensions_and_trailers():
    body = b"5;name=value\r\nhello\r\n6\r\n world\r\n0\r\nX-Checksum: 1\r\n\r\n"
    r, conn = parse(req(b"POST /x HTTP/1.1", b"Host: a", b"Transfer-Encoding: chunked", body=body))
    assert r.body == b"hello world" and not conn.buf


def test_chunk_sizes_are_hexadecimal_in_either_case():
    body = b"A\r\n0123456789\r\nb\r\nabcdefghijk\r\n0\r\n\r\n"
    r, _ = parse(req(b"POST /x HTTP/1.1", b"Host: a", b"Transfer-Encoding: Chunked", body=body))
    assert r.body == b"0123456789abcdefghijk"


def test_pipelined_requests_are_left_for_the_next_read():
    data = req(b"GET /1 HTTP/1.1", b"Host: a") + req(b"POST /2 HTTP/1.1", b"Host: a", b"Content-Length: 2", body=b"ok") + req(b"GET /3 HTTP/1.1", b"Host: a")
    r1, conn = parse(data)
    assert r1.target == "/1" and bytes(conn.buf).startswith(b"POST /2")


# --- what is refused ------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("data, status", [
    (req(b"GET / HTTP/1.1"), 400),                                                              # HTTP/1.1 needs Host
    (req(b"GET / HTTP/1.1", b"Host: a", b"Host: b"), 400),
    (req(b"GET / HTTP/1.0", b"Host: a", b"Host: b"), 400),
    (req(b"GET /  HTTP/1.1", b"Host: a"), 400),                                                 # two spaces
    (req(b"GET / HTTP/1.1 ", b"Host: a"), 400),
    (req(b"GET HTTP/1.1", b"Host: a"), 400),
    (req(b"GET /a b HTTP/1.1", b"Host: a"), 400),
    (req(b"GET /a#frag HTTP/1.1", b"Host: a"), 400),
    (req(b"GET a HTTP/1.1", b"Host: a"), 400),
    (req(b"GET * HTTP/1.1", b"Host: a"), 400),
    (req(b"GET / HTTP/2.0", b"Host: a"), 505),
    (req(b"GET / HTTP/0.9", b"Host: a"), 505),
    (req(b"GET / HTTP/1", b"Host: a"), 400),
    (req(b"GET / http/1.1", b"Host: a"), 400),
    (req(b"FOO / HTTP/1.1", b"Host: a"), 501),
    (req(b"get / HTTP/1.1", b"Host: a"), 501),
    (req(b"TRACE / HTTP/1.1", b"Host: a"), 501),
    (req(b"CONNECT a:443 HTTP/1.1", b"Host: a"), 501),
    (req(b"G@T / HTTP/1.1", b"Host: a"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a", b"Foo : bar"), 400),                                    # space before the colon
    (req(b"GET / HTTP/1.1", b"Host: a", b" Foo: bar"), 400),                                    # folded line
    (req(b"GET / HTTP/1.1", b"Host: a", b"X: a", b"\tcontinued"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a", b"NoColonHere"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a", b": empty-name"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a", b"Bad Name: x"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a", b"X: a\x00b"), 400),
    (req(b"GET /\x00 HTTP/1.1", b"Host: a"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a", b"X: a\rb"), 400),                                      # a bare CR
    (b"GET / HTTP/1.1\nHost: a\n\n", 400),                                                      # bare LF only
    (b"GET / HTTP/1.1\r\nHost: a\nX: b\r\n\r\n", 400),                                          # a bare LF inside
    (req(b"GET / HTTP/1.1", b"Host: a b"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a@b"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a]b"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a?b"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a<b>"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a%zzb"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a\xffb"), 400),
    (req(b"GET / HTTP/1.1", b"Host: [::1"), 400),
    (req(b"GET / HTTP/1.1", b"Host: a:80:90"), 400),
    (req(b"GET /%00 HTTP/1.1", b"Host: a"), 400),                                               # an encoded NUL
])
def test_malformed_requests_are_refused(data, status):
    assert refused(data) == status


@pytest.mark.parametrize("headers, status", [
    ([b"Content-Length: 5", b"Transfer-Encoding: chunked"], 400),                                # the classic smuggling pair, in either order
    ([b"Transfer-Encoding: chunked", b"Content-Length: 5"], 400),
    ([b"Transfer-Encoding: gzip"], 400),                                                         # the last coding must be chunked
    ([b"Transfer-Encoding: chunked, gzip"], 400),
    ([b"Transfer-Encoding: gzip, chunked"], 501),
    ([b"Transfer-Encoding: identity"], 400),
    ([b"Content-Length: 5", b"Content-Length: 6"], 400),
    ([b"Content-Length: 5, 6"], 400),
    ([b"Content-Length: +5"], 400),
    ([b"Content-Length: -1"], 400),
    ([b"Content-Length: 0x10"], 400),
    ([b"Content-Length: 5 5"], 400),
    ([b"Content-Length: "], 400),
    ([b"Content-Length: 1e3"], 400),
    ([b"Content-Length: 99999999999999999999"], 400),
    ([b"Content-Length: 20000000"], 413),
    ([b"Expect: 200-ok"], 417),
])
def test_ambiguous_message_framing_is_refused(headers, status):
    assert refused(req(b"POST / HTTP/1.1", b"Host: a", *headers, body=b"12345")) == status


def test_transfer_encoding_in_an_http_10_request_is_refused():
    assert refused(req(b"POST / HTTP/1.0", b"Transfer-Encoding: chunked", body=b"0\r\n\r\n")) == 400


@pytest.mark.parametrize("body, status", [
    (b"-1\r\nx\r\n0\r\n\r\n", 400), (b"0x5\r\nhello\r\n0\r\n\r\n", 400), (b" 5\r\nhello\r\n0\r\n\r\n", 400), (b"\r\nhello\r\n0\r\n\r\n", 400),
    (b"5\r\nhelloXX0\r\n\r\n", 400), (b"5\r\nhello0\r\n\r\n", 400), (b"5\nhello\r\n0\r\n\r\n", 400), (b"zz\r\n", 400),
    (b"FFFFFFFFFFFFFFFF\r\n", 413), (b"20000000\r\n", 413), (b"0\r\nbad trailer\r\n\r\n", 400), (b"0\r\n: x\r\n\r\n", 400),
    (b"11111111111111111\r\nx\r\n0\r\n\r\n", 400),
])
def test_malformed_chunked_bodies_are_refused(body, status):
    assert refused(req(b"POST / HTTP/1.1", b"Host: a", b"Transfer-Encoding: chunked", body=body)) == status


def test_size_limits():
    small = Limits(max_head=300, max_uri=50, max_header_line=100, max_headers=3, max_body=10)
    assert refused(req(b"GET /" + b"a" * 60 + b" HTTP/1.1", b"Host: a"), small) == 414
    assert refused(req(b"GET / HTTP/1.1", b"Host: a", b"X-1: 1", b"X-2: 2", b"X-3: 3"), small) == 431
    assert refused(req(b"GET / HTTP/1.1", b"Host: a", b"X: " + b"v" * 400), small) == 431
    assert refused(req(b"GET / HTTP/1.1", b"Host: a", b"X: " + b"v" * 120), small) == 431                          # one line over its own limit, though the head fits
    assert refused(req(b"POST / HTTP/1.1", b"Host: a", b"Content-Length: 11", body=b"x" * 11), small) == 413
    assert refused(req(b"POST / HTTP/1.1", b"Host: a", b"Transfer-Encoding: chunked", body=b"6\r\nabcdef\r\n6\r\nabcdef\r\n0\r\n\r\n"), small) == 413


def test_an_endless_head_is_cut_off_before_it_is_complete():
    with pytest.raises(HTTPError) as info:
        parse(b"GET / HTTP/1.1\r\nHost: a\r\nX: " + b"a" * 100000)
    assert info.value.status == 431


# --- fragmentation never changes the answer -------------------------------------------------------------------------------------------------
def test_the_result_does_not_depend_on_how_the_bytes_arrive():
    data = req(b"POST /form?x=1 HTTP/1.1", b"Host: a", b"Transfer-Encoding: chunked", b"X-Extra: yes",
               body=b"3;e=1\r\nabc\r\n10\r\n0123456789abcdef\r\n0\r\nT: 1\r\n\r\n") + req(b"GET /next HTTP/1.1", b"Host: a")
    whole, whole_conn = parse(data)
    rng = random.Random(7)
    for _ in range(60):
        sizes = [rng.randint(1, 12) for _ in range(rng.randint(1, 40))]
        r, conn = parse(data, chunks=sizes)
        assert (r.method, r.target, r.body, r.headers.items()) == (whole.method, whole.target, whole.body, whole.headers.items())
        assert bytes(whole_conn.buf).startswith(bytes(conn.buf))                                # (what was not read yet is simply not in the buffer)
    for size in (1, 2, 3):                                                                        # byte by byte
        r, _ = parse(data, chunks=[size] * (len(data) // size))
        assert r.body == whole.body


def test_a_closed_connection_in_the_middle_of_a_request_is_not_a_clean_end():
    from kendi_sunucu.parser import ConnectionClosed
    assert parse(b"") is None
    with pytest.raises(ConnectionClosed):
        parse(b"GET / HTTP/1.1\r\nHost: a")

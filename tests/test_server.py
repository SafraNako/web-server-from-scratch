"""The server over real sockets: persistence, pipelining, framing in both directions, timeouts, limits and survival of whatever a client sends."""
import hashlib
import http.client
import os
import random
import socket
import threading
import time

import pytest

from helpers import connect, get, one, parse_responses, raw, recv_all, running, wait_until
from kendi_sunucu import App, HTTPError, Response
from kendi_sunucu.parser import Limits


def make_app():
    app = App()

    @app.get("/")
    def index(request):
        return "<h1>home</h1>"

    @app.get("/echo")
    def echo(request):
        return {"method": request.method, "query": request.query, "headers": {k.lower(): v for k, v in request.headers}}

    @app.route("/body", methods=("POST", "PUT", "PATCH"))
    def body(request):
        return Response(request.body, 200, content_type="application/octet-stream", headers=[("X-Length", str(len(request.body)))])

    @app.get("/stream")
    def stream(request):
        def chunks():
            for i in range(5):
                yield ("line %d\n" % i).encode()
        return Response(chunks(), 200, content_type="text/plain")

    @app.get("/boom")
    def boom(request):
        raise RuntimeError("secret internals")

    @app.get("/teapot")
    def teapot(request):
        raise HTTPError(418, "short and stout")

    @app.get("/big")
    def big(request):
        return Response(b"x" * 3_000_000, 200, content_type="application/octet-stream")

    @app.get("/slow")
    def slow(request):
        time.sleep(0.3)
        return "done"

    return app


@pytest.fixture
def server():
    with running(make_app()) as s:
        yield s


def request_bytes(method="GET", path="/", headers=(), body=b"", version="HTTP/1.1", host=True):
    lines = ["%s %s %s" % (method, path, version)] + (["Host: test"] if host else []) + list(headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


# --- the basics ---------------------------------------------------------------------------------------------------------------------------
def test_get_and_the_headers_every_response_has(server):
    status, headers, body = one(server, request_bytes(headers=["Connection: close"]))
    assert status == 200 and body == b"<h1>home</h1>"
    assert headers["Content-Length"] == "13" and headers["Content-Type"] == "text/html; charset=utf-8" and headers["Server"].startswith("kendi-sunucu")
    assert headers["Date"].endswith("GMT") and headers["Connection"] == "close"


def test_the_application_sees_what_was_sent(server):
    status, _, body = one(server, request_bytes(path="/echo?x=1&x=2&y=%C3%A9", headers=["X-Thing: a", "Connection: close"]))
    import json
    data = json.loads(body)
    assert status == 200 and data["query"] == {"x": ["1", "2"], "y": ["é"]} and data["headers"]["x-thing"] == "a" and data["headers"]["host"] == "test"


def test_unknown_paths_and_wrong_methods(server):
    assert get(server, "/nope")[0] == 404
    status, headers, body = get(server, "/", method="POST")
    assert status == 405 and headers["Allow"] == "GET, HEAD, OPTIONS"
    status, headers, _ = get(server, "/", method="OPTIONS")
    assert status == 204 and headers["Allow"] == "GET, HEAD, OPTIONS"
    assert get(server, "/", method="DELETE")[0] == 405


def test_head_has_the_headers_of_get_and_no_body(server):
    responses = parse_responses(raw(server, request_bytes("HEAD", "/", ["Connection: close"])), methods=["HEAD"])
    status, headers, body = responses[0]
    assert status == 200 and headers["Content-Length"] == "13" and body == b""


def test_handler_errors_become_responses_without_leaking(server):
    status, _, body = get(server, "/boom")
    assert status == 500 and b"secret" not in body and b"RuntimeError" not in body
    status, _, body = get(server, "/teapot")
    assert status == 418 and b"short and stout" in body
    assert get(server, "/")[0] == 200                                                           # still alive


def test_options_asterisk(server):
    status, headers, _ = one(server, b"OPTIONS * HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
    assert status == 200 and "GET" in headers["Allow"]


# --- persistent connections and pipelining ------------------------------------------------------------------------------------------------
def test_one_connection_serves_many_requests(server):
    c = http.client.HTTPConnection(*server.address)
    for i in range(25):
        c.request("GET", "/echo?i=%d" % i)
        r = c.getresponse()
        assert r.status == 200 and r.read()
        assert r.getheader("Connection") is None                                                  # HTTP/1.1: persistent unless said otherwise
    c.close()
    assert server.requests_served >= 25


def test_pipelined_requests_are_answered_in_order(server):
    data = b"".join(request_bytes(path="/echo?n=%d" % i) for i in range(6)) + request_bytes(path="/echo?n=last", headers=["Connection: close"])
    responses = parse_responses(raw(server, data))
    import json
    assert [json.loads(b)["query"]["n"][0] for _, _, b in responses] == ["0", "1", "2", "3", "4", "5", "last"]


def test_pipelined_post_bodies_do_not_bleed_into_the_next_request(server):
    data = (request_bytes("POST", "/body", ["Content-Length: 5"], b"hello") + request_bytes("POST", "/body", ["Transfer-Encoding: chunked"], b"3\r\nabc\r\n0\r\n\r\n")
            + request_bytes("POST", "/body", ["Content-Length: 2", "Connection: close"], b"ok"))
    assert [b for _, _, b in parse_responses(raw(server, data))] == [b"hello", b"abc", b"ok"]


def test_connection_close_is_honoured_and_announced(server):
    s = connect(server)
    s.sendall(request_bytes(headers=["Connection: close"]))
    data = recv_all(s)
    assert data.startswith(b"HTTP/1.1 200") and b"Connection: close" in data
    assert s.recv(10) == b""                                                                    # closed by the server
    s.close()


def test_http_10_closes_unless_keep_alive_was_asked(server):
    s = connect(server)
    s.sendall(b"GET /echo HTTP/1.0\r\n\r\n")
    data = recv_all(s)
    assert data.startswith(b"HTTP/1.1 200") and b"Connection: close" in data
    s.close()
    s = connect(server)
    s.sendall(b"GET /echo HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
    s.settimeout(1)
    first = s.recv(65536)
    assert b"Connection: keep-alive" in first
    s.sendall(b"GET /echo HTTP/1.0\r\nConnection: keep-alive\r\n\r\n")
    assert s.recv(65536).startswith(b"HTTP/1.1 200")                                           # the connection stayed open
    s.close()


def test_the_number_of_requests_per_connection_is_limited():
    with running(make_app(), max_requests=3) as s:
        data = b"".join(request_bytes(path="/echo") for _ in range(5))
        responses = parse_responses(raw(s, data))
        assert len(responses) == 3 and responses[-1][1]["Connection"] == "close" and "Connection" not in responses[0][1]


# --- bodies ---------------------------------------------------------------------------------------------------------------------------------
def test_content_length_upload_is_echoed_byte_for_byte(server):
    payload = bytes(random.Random(1).randrange(256) for _ in range(300_000))
    c = http.client.HTTPConnection(*server.address)
    c.request("POST", "/body", body=payload)
    r = c.getresponse()
    assert r.status == 200 and r.read() == payload and r.getheader("X-Length") == str(len(payload))


def test_chunked_upload_from_a_real_client(server):
    payload = [b"alpha", b"", b"beta" * 5000, b"gamma"]
    c = http.client.HTTPConnection(*server.address)
    c.request("PUT", "/body", body=iter(payload), encode_chunked=True)
    r = c.getresponse()
    assert r.status == 200 and r.read() == b"alpha" + b"beta" * 5000 + b"gamma"


def test_chunked_response_is_decoded_by_a_real_client(server):
    c = http.client.HTTPConnection(*server.address)
    c.request("GET", "/stream")
    r = c.getresponse()
    assert r.getheader("Transfer-Encoding") == "chunked" and r.getheader("Content-Length") is None
    assert r.read() == b"".join(("line %d\n" % i).encode() for i in range(5))
    c.request("GET", "/echo")                                                                   # and the connection is still usable afterwards
    assert c.getresponse().status == 200


def test_a_streamed_body_to_an_http_10_client_ends_with_the_connection(server):
    data = raw(server, b"GET /stream HTTP/1.0\r\n\r\n")
    head, _, body = data.partition(b"\r\n\r\n")
    assert b"Transfer-Encoding" not in head and b"Connection: close" in head and body == b"".join(("line %d\n" % i).encode() for i in range(5))


def test_a_large_download_arrives_whole(server):
    c = http.client.HTTPConnection(*server.address)
    c.request("GET", "/big")
    assert hashlib.sha256(c.getresponse().read()).hexdigest() == hashlib.sha256(b"x" * 3_000_000).hexdigest()


def test_expect_100_continue_gets_its_interim_response_before_the_body():
    with running(make_app()) as server:
        s = connect(server)
        s.sendall(request_bytes("POST", "/body", ["Content-Length: 5", "Expect: 100-continue", "Connection: close"]))
        s.settimeout(2)
        assert s.recv(100) == b"HTTP/1.1 100 Continue\r\n\r\n"                                  # sent before any body byte
        s.sendall(b"hello")
        data = recv_all(s)
        assert data.startswith(b"HTTP/1.1 200") and data.endswith(b"hello")
        s.close()


def test_an_announced_body_that_is_too_large_is_refused_without_100_continue():
    with running(make_app(), limits=Limits(max_body=100)) as server:
        s = connect(server)
        s.sendall(request_bytes("POST", "/body", ["Content-Length: 5000", "Expect: 100-continue"]))
        data = recv_all(s)
        assert data.startswith(b"HTTP/1.1 413") and b"100 Continue" not in data
        s.close()


def test_a_body_over_the_limit_gets_413_and_the_connection_closes():
    with running(make_app(), limits=Limits(max_body=1000)) as server:
        data = raw(server, request_bytes("POST", "/body", ["Content-Length: 2000"], b"x" * 2000))
        assert data.startswith(b"HTTP/1.1 413") and b"Connection: close" in data
        data = raw(server, request_bytes("POST", "/body", ["Transfer-Encoding: chunked"], (b"%x\r\n" % 600 + b"x" * 600 + b"\r\n") * 2 + b"0\r\n\r\n"))
        assert data.startswith(b"HTTP/1.1 413")


# --- malformed input ------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("data, status", [
    (b"GET / HTTP/1.1\r\n\r\n", 400), (b"GARBAGE\r\n\r\n", 400), (b"GET / HTTP/1.1\r\nHost: a\r\nFoo : bar\r\n\r\n", 400), (b"GET / HTTP/2.0\r\nHost: a\r\n\r\n", 505),
    (b"BREW / HTTP/1.1\r\nHost: a\r\n\r\n", 501), (b"POST / HTTP/1.1\r\nHost: a\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n", 400),
    (b"GET / HTTP/1.1\nHost: a\n\n", 400), (b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + b"\x00" * 40 + b"\r\n\r\n", 400),
    (b"GET /" + b"a" * 9000 + b" HTTP/1.1\r\nHost: a\r\n\r\n", 414), (b"GET / HTTP/1.1\r\nHost: a\r\n" + b"X: a\r\n" * 150 + b"\r\n", 431),
])
def test_malformed_requests_get_an_error_and_a_closed_connection(server, data, status):
    response = raw(server, data)
    assert response.startswith(b"HTTP/1.1 %d" % status), response[:60]
    assert b"Connection: close" in response.split(b"\r\n\r\n")[0]
    assert get(server, "/")[0] == 200                                                           # the server is fine


def test_a_smuggled_second_request_is_never_executed():
    seen = []
    app = App()
    app.route("/safe", methods=("GET", "POST"))(lambda r: seen.append(r.target) or "ok")
    app.route("/admin", methods=("GET", "POST"))(lambda r: seen.append(r.target) or "SECRET")
    smuggled = (b"POST /safe HTTP/1.1\r\nHost: a\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\nGET /admin HTTP/1.1\r\nHost: a\r\n\r\n")
    with running(app) as server:
        data = raw(server, smuggled)
        assert data.startswith(b"HTTP/1.1 400") and b"SECRET" not in data
        data = raw(server, b"POST /safe HTTP/1.1\r\nHost: a\r\nContent-Length: 5\r\nContent-Length: 44\r\n\r\nxxxxxGET /admin HTTP/1.1\r\nHost: a\r\n\r\n")
        assert data.startswith(b"HTTP/1.1 400") and b"SECRET" not in data
    assert seen == []


def test_random_garbage_never_hurts_the_server(server):
    rng = random.Random(3)
    valid = request_bytes("POST", "/body", ["Content-Length: 5"], b"hello")
    for i in range(150):
        kind = i % 3
        if kind == 0:
            data = bytes(rng.randrange(256) for _ in range(rng.randint(1, 600)))
        elif kind == 1:
            data = bytearray(valid)
            for _ in range(rng.randint(1, 6)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            data = bytes(data)
        else:
            data = valid[:rng.randrange(1, len(valid))]
        s = connect(server, timeout=2)
        try:
            s.sendall(data)
            s.shutdown(socket.SHUT_WR)
            reply = recv_all(s, timeout=2)
        except OSError:
            reply = b""
        finally:
            s.close()
        assert reply == b"" or reply.startswith(b"HTTP/1.1 "), (data, reply[:80])
    assert get(server, "/echo")[0] == 200


# --- time and capacity ----------------------------------------------------------------------------------------------------------------------
def test_an_idle_connection_is_closed_quietly():
    with running(make_app(), idle_timeout=0.3) as server:
        s = connect(server)
        s.sendall(request_bytes())
        assert s.recv(65536).startswith(b"HTTP/1.1 200")
        time.sleep(0.6)
        assert s.recv(10) == b""                                                                # closed, without any error response
        s.close()


def test_a_request_that_stops_in_the_middle_gets_408():
    with running(make_app(), header_timeout=0.4) as server:
        s = connect(server)
        s.sendall(b"GET / HTTP/1.1\r\nHost: a\r\nX-Partial: ")
        data = recv_all(s, timeout=3)
        assert data.startswith(b"HTTP/1.1 408")
        s.close()


def test_a_slowloris_cannot_keep_a_header_open_by_dribbling_bytes():
    with running(make_app(), header_timeout=0.5) as server:
        s = connect(server)
        start = time.monotonic()
        import select
        try:
            for ch in b"GET / HTTP/1.1\r\nHost: a\r\nX-Slow: aaaaaaaaaaaaaaaaaaaaaaaa":
                s.sendall(bytes([ch]))
                time.sleep(0.05)
                if select.select([s], [], [], 0)[0]:                                             # the server answered: stop dribbling
                    break
        except OSError:
            pass
        data = recv_all(s, timeout=3)
        s.close()
        assert data.startswith(b"HTTP/1.1 408") and time.monotonic() - start < 2.5


def test_a_body_that_never_arrives_gets_408():
    with running(make_app(), body_timeout=0.4) as server:
        s = connect(server)
        s.sendall(request_bytes("POST", "/body", ["Content-Length: 100"], b"short"))
        assert recv_all(s, timeout=3).startswith(b"HTTP/1.1 408")
        s.close()


def test_too_many_connections_get_503_and_the_server_recovers():
    with running(make_app(), max_connections=3, idle_timeout=5) as server:
        held = [connect(server) for _ in range(3)]
        for h in held:
            h.sendall(b"GET / HTTP/1.1\r\nHost: a\r\n\r\n")
            h.recv(65536)
        extra = connect(server)
        assert recv_all(extra, timeout=2).startswith(b"HTTP/1.1 503")
        extra.close()
        held[0].close()
        assert wait_until(lambda: get(server, "/")[0] == 200)
        for h in held[1:]:
            h.close()


def test_many_clients_at_once():
    with running(make_app()) as server:
        errors = []

        def client(n):
            try:
                c = http.client.HTTPConnection(*server.address, timeout=10)
                for i in range(20):
                    c.request("GET", "/echo?c=%d&i=%d" % (n, i))
                    r = c.getresponse()
                    data = r.read()
                    if r.status != 200 or ("\"c\": [\"%d\"]" % n).encode() not in data:
                        errors.append((n, i, r.status))
                c.close()
            except Exception as exc:                                                            # pragma: no cover
                errors.append((n, repr(exc)))
        threads = [threading.Thread(target=client, args=(n,)) for n in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert not errors and server.requests_served >= 800


def test_shutdown_lets_a_running_request_finish():
    server = running(make_app()).__enter__()
    result = []
    t = threading.Thread(target=lambda: result.append(get(server, "/slow")))
    t.start()
    time.sleep(0.1)
    server.shutdown(3.0)
    t.join(5)
    assert result and result[0][0] == 200 and result[0][2] == b"done"
    with pytest.raises(OSError):
        socket.create_connection(server.address, timeout=1)


def test_the_server_works_without_a_framework_app():
    from kendi_sunucu import Server
    s = Server(lambda request: "plain callable: " + request.path, "127.0.0.1", 0).start()
    try:
        assert get(s, "/hello")[2] == b"plain callable: /hello"
    finally:
        s.shutdown(1.0)


def test_header_values_with_line_breaks_cannot_be_written():
    app = App()

    @app.get("/inject")
    def inject(request):
        return Response("x", 200, headers=[("X-Test", "a\r\nSet-Cookie: pwned=1")])

    @app.get("/redirect")
    def redirect_(request):
        from kendi_sunucu import redirect
        return redirect("/go?x=" + request.query_param("x", ""))
    with running(app) as server:
        status, headers, _ = get(server, "/inject")
        assert status == 500 and "Set-Cookie" not in headers
        status, headers, _ = get(server, "/redirect?x=a%0d%0aSet-Cookie:%20pwned%3D1")
        assert status == 302 and "Set-Cookie" not in headers and "\r" not in headers["Location"] and "\n" not in headers["Location"]
        assert os.environ is not None

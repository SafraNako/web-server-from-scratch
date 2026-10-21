"""Routing, method dispatch, result conversion, middleware and error handlers (no sockets: requests are built by hand)."""
import pytest

from kendi_sunucu import App, Headers, HTTPError, Request, Response, json_response


def call(app, method, target, headers=(), body=b""):
    h = Headers([("Host", "t")] + list(headers))
    return app.handle(Request(method, target, "HTTP/1.1", h, body))


@pytest.fixture
def app():
    a = App()
    a.get("/")(lambda r: "<p>home</p>")
    a.get("/users/<int:id>")(lambda r, id: {"id": id, "type": type(id).__name__})
    a.get("/users/me")(lambda r: {"id": "me"})
    a.get("/files/<path:name>")(lambda r, name: name)
    a.get("/hello/<who>")(lambda r, who: "hello " + who)
    a.route("/things", methods=("GET", "POST"))(lambda r: ("created", 201, [("X-Made", "yes")]) if r.method == "POST" else [1, 2, 3])
    a.get("/none")(lambda r: None)
    a.get("/bytes")(lambda r: b"\x00\x01")
    return a


def test_static_segments_beat_parameters_whatever_the_order(app):
    assert call(app, "GET", "/users/me").body == b'{"id": "me"}'
    assert call(app, "GET", "/users/42").body == b'{"id": 42, "type": "int"}'


def test_typed_parameters_only_match_their_type(app):
    assert call(app, "GET", "/users/abc").status == 404
    assert call(app, "GET", "/users/4x").status == 404
    assert call(app, "GET", "/users/-1").status == 404
    assert call(app, "GET", "/users/%34%32").body == b'{"id": 42, "type": "int"}'


def test_path_parameters_take_the_rest_and_encoded_slashes_are_not_separators(app):
    assert call(app, "GET", "/files/a/b/c.txt").body == b"a/b/c.txt"
    assert call(app, "GET", "/files/a%2Fb").body == b"a/b"
    assert call(app, "GET", "/hello/a%2Fb").body == b"hello a/b"                                    # one segment, with a slash in it
    assert call(app, "GET", "/hello/a/b").status == 404
    assert call(app, "GET", "/files/").status == 404


def test_a_trailing_slash_is_a_different_path(app):
    assert call(app, "GET", "/things/").status == 404 and call(app, "GET", "/things").status == 200


def test_method_dispatch_405_allow_head_and_options(app):
    r = call(app, "DELETE", "/things")
    assert r.status == 405 and r.headers.get("Allow") == "GET, HEAD, POST, OPTIONS"
    r = call(app, "OPTIONS", "/things")
    assert r.status == 204 and r.headers.get("Allow") == "GET, HEAD, POST, OPTIONS"
    assert call(app, "HEAD", "/").status == 200
    assert call(app, "POST", "/").headers.get("Allow") == "GET, HEAD, OPTIONS"


def test_results_become_responses(app):
    r = call(app, "POST", "/things")
    assert (r.status, r.body, r.headers.get("X-Made")) == (201, b"created", "yes")
    assert call(app, "GET", "/things").headers.get("Content-Type") == "application/json"
    assert call(app, "GET", "/none").status == 204
    assert call(app, "GET", "/bytes").headers.get("Content-Type") == "application/octet-stream"
    assert call(app, "GET", "/").headers.get("Content-Type") == "text/html; charset=utf-8"


def test_the_params_are_also_on_the_request(app):
    seen = {}
    app.get("/p/<a>/<int:b>")(lambda r, a, b: seen.update(r.params) or "x")
    call(app, "GET", "/p/x/7")
    assert seen == {"a": "x", "b": 7}


def test_mounts_get_what_no_route_claimed():
    app = App()
    app.get("/api/ping")(lambda r: "pong")
    app.mount("/static", lambda request, rest: Response("static:" + rest))
    app.mount("", lambda request, rest: Response("fallback:" + rest))
    assert call(app, "GET", "/api/ping").body == b"pong"
    assert call(app, "GET", "/static/css/a.css").body == b"static:/css/a.css"
    assert call(app, "GET", "/static").body == b"static:"
    assert call(app, "GET", "/staticky").body == b"fallback:/staticky"
    assert call(app, "GET", "/other").body == b"fallback:/other"


def test_middleware_wraps_in_order_and_can_short_circuit():
    app = App()
    log = []

    def outer(request, call_next):
        log.append("outer in")
        response = call_next(request)
        response.headers.set("X-Outer", "1")
        log.append("outer out")
        return response

    def inner(request, call_next):
        log.append("inner in")
        if request.path == "/blocked":
            return Response("no", 403)
        return call_next(request)
    app.use(outer)
    app.use(inner)
    app.get("/")(lambda r: "ok")
    r = call(app, "GET", "/")
    assert r.status == 200 and r.headers.get("X-Outer") == "1" and log == ["outer in", "inner in", "outer out"]
    assert call(app, "GET", "/blocked").status == 403


def test_error_handlers_and_http_errors():
    app = App()

    @app.error_handler(404)
    def missing(request):
        return json_response({"error": "nothing at " + request.path})

    @app.get("/gone")
    def gone(request):
        raise HTTPError(410, "it is gone")
    r = call(app, "GET", "/nowhere")
    assert r.status == 404 and r.body == b'{"error": "nothing at /nowhere"}'
    r = call(app, "GET", "/gone")
    assert r.status == 410 and b"it is gone" in r.body


def test_route_patterns_are_checked_when_registered():
    app = App()
    for bad in ("nolead", "/a/<path:x>/b", "/a/<int:>", "/a/<x"):
        with pytest.raises(ValueError):
            app.get(bad)(lambda r: "x")


def test_request_helpers():
    r = Request("POST", "/x?a=1&a=2&b=%20", "HTTP/1.1",
                Headers([("Host", "t"), ("Cookie", "s=1; t=two"), ("Content-Type", "application/json; charset=utf-8")]), b'{"k": "\xc3\xa9"}')
    assert r.query_param("a") == "1" and r.query["b"] == [" "] and r.query_param("zz", "d") == "d"
    assert r.json() == {"k": "é"} and r.cookies == {"s": "1", "t": "two"}
    f = Request("POST", "/", "HTTP/1.1", Headers([("Host", "t")]), b"name=Ada+L&x=1&x=2")
    assert f.form() == {"name": ["Ada L"], "x": ["1", "2"]}


def test_an_invalid_json_body_is_a_400_and_an_unknown_charset_is_not_a_crash():
    bad = Request("POST", "/", "HTTP/1.1", Headers([("Host", "t")]), b"{nope")
    with pytest.raises(HTTPError) as caught:
        bad.json()
    assert caught.value.status == 400
    with pytest.raises(HTTPError):
        Request("POST", "/", "HTTP/1.1", Headers([("Host", "t")]), b"\xff\xfe").json()
    odd = Request("POST", "/", "HTTP/1.1", Headers([("Host", "t"), ("Content-Type", "text/plain; charset=no-such-thing")]), "caf\u00e9".encode())
    assert odd.text() == "caf\u00e9"

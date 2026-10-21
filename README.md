# web-server-from-scratch

An HTTP/1.1 server written on raw sockets with nothing but the Python standard library: request parsing, routing, middleware and a static file server. No web framework, no `http.server`.

It is checked against independent implementations instead of just its own tests: **curl** and **`http.client`** as clients, and **Apache httpd 2.4** as a reference for static files and for what counts as a malformed request.

```
$ pip install -e .
$ kserve ./public -p 8000 --listing
Serving http://127.0.0.1:8000  (directory: /…/public)  -- Ctrl+C to stop
127.0.0.1 - - [21/Oct/2026:13:56:28 +0000] "GET /index.html HTTP/1.1" 200 256
```

Python 3.9+; CI runs 3.9, 3.12 and 3.13 on Linux with a real Apache installed for the oracle tests.

## Use it as a library

```python
from kendi_sunucu import App, HTTPError, Server, StaticFiles, json_response

app = App()

@app.use
def server_timing(request, call_next):                 # middleware always sees a Response
    response = call_next(request)
    response.headers.set("X-Seen", request.path)
    return response

@app.get("/users/<int:id>")                            # typed parameters: <name>, <int:name>, <path:name>
def user(request, id):
    if id > 100:
        raise HTTPError(404, "no such user")
    return {"id": id}                                  # dict/list -> JSON, str -> text, bytes, (body, status, headers), Response

@app.post("/echo")
def echo(request):
    return json_response({"you sent": request.json()}, 201)   # invalid JSON -> 400 for you

app.mount("", StaticFiles("public", cache_control="max-age=60"))
Server(app, "127.0.0.1", 8000, access_log=True).start()
```

`examples/demo.py` is a bigger version (notes API, cookie counter, middleware, static files): `python examples/demo.py`.

## What it does

**Protocol (RFC 9110 / 9112)**

- Strict request-line and header parsing: bare LF, NUL, stray CR, obs-fold, whitespace before the colon, duplicate or malformed `Host`, bad `Content-Length` (sign, list, mismatch) are all refused.
- Message framing: `Content-Length`, `Transfer-Encoding: chunked` (with extensions and trailers), and the **smuggling guard**: `Content-Length` together with `Transfer-Encoding` is rejected, as is any coding but a final `chunked`.
- Persistent connections, pipelining (several requests in one `send`), `Connection: close`, HTTP/1.0 with and without `keep-alive`, a cap on requests per connection.
- `Expect: 100-continue` (sent before the body is read, and *not* sent when the request is refused first), chunked responses for streaming bodies, `HEAD` (same headers, no body), 1xx/204/304 without a body, 405 with `Allow`, `OPTIONS`, `OPTIONS *`.
- Statuses that tell the client what went wrong: 400, 408, 413, 414, 417, 431, 501, 503, 505. A handler exception is a 500 whose text is logged, never sent.
- Hardening: header/body/idle/write timeouts, an *absolute* deadline for the header block (slowloris), size limits for the request line, each header line, the header count, the body, chunk lines and trailers, a connection limit (503), lingering close so a refused upload's answer is not lost to a TCP reset, graceful shutdown.

**Static files**

- `ETag` + `Last-Modified`, conditional requests in the order RFC 9110 13.2.2 defines (`If-Match`, `If-Unmodified-Since`, `If-None-Match`, `If-Modified-Since`, `If-Range`); a validator is only "strong" if it can be (a date that is less than a second old is not).
- Byte ranges: single, multiple (`multipart/byteranges`), suffix and open-ended, overlapping ranges coalesced, a list of more than 20 ranges is answered with the whole file, 416 with `Content-Range: bytes */size`.
- Gzip for text types with its own `ETag` (`…-gzip`) and `Vary: Accept-Encoding`, cached per file version.
- Safe paths: dot segments are resolved per RFC 3986 and a path that climbs above the root is a 400, symlinks that leave the root are refused, hidden files are not served unless asked for, an encoded slash (`%2f`) or NUL (`%00`) inside a segment is refused, directories redirect to the trailing slash, then serve `index.html` or an HTML-escaped listing.

**Application layer**: routes with typed parameters (static segments beat parameters whatever the registration order), mounts, middleware, per-status error handlers, `Request` helpers (`query`, `json`, `form`, `cookies`, `text`), `Headers` that refuse CR/LF injection, Common Log Format access log.

## Layout

| Module | Role |
|---|---|
| `parser.py` | Buffered socket reader, request parser, framing rules, chunked decoder, limits |
| `server.py` | Listener, thread per connection, keep-alive loop, timeouts, errors → responses, shutdown |
| `router.py` | `App`: routes, mounts, middleware, result conversion, error handlers |
| `static.py` | `StaticFiles`: files, conditionals, ranges, gzip, directories |
| `response.py`, `request.py`, `headers.py`, `errors.py` | The value types the application works with |
| `cli.py` | `kserve` |

## How it was checked

403 tests; `pytest -v` runs all of them. The oracle tests need an `httpd`/`apache2` binary and are skipped without one.

| Layer | Tests | What it proves |
|---|---|---|
| `test_parser.py` | 99 | Parsing and framing in isolation, including property tests that feed a request in random fragments and expect the same result as in one piece |
| `test_server.py` | 41 | A live server on a real socket: keep-alive, pipelining, 100-continue, timeouts, slowloris, limits, shutdown, concurrency |
| `test_router.py`, `test_static.py` | 13 + 70 | Routing rules; static serving down to the ETag and range edge cases |
| `test_curl.py` | 25 | A client that did not come from this repository: range downloads, `-z`, resume (`-C -`), gzip, redirects, 50 parallel transfers |
| `test_apache.py` | 148 | A matrix of requests answered by this server and by **Apache 2.4.56** and compared (status, headers that must agree, body, ranges, conditionals); a table of malformed requests both must refuse; a seeded mutation fuzz |
| `test_cli.py`, `test_example.py` | 3 + 4 | The real command line as a subprocess; the demo app through `http.client` |

**What the oracle found.** Writing the comparison changed the server in several places, each pinned by a test: `/../x` above the root is a 400 as in Apache (it used to be clamped silently), redirects carry an HTML body, `HTTP/1.9` is treated as 1.1 instead of 505, header lines have their own size limit, `Range: bytes= 0-1` (a space after `=`) is ignored, a future `If-Modified-Since` is ignored, and `Host` values are checked against the host-name grammar after the fuzz found 55 odd characters that got through. Truncated request heads (a client that half-closes) now get a 400 as Apache gives.

**Documented differences** (all in `tests/test_apache.py`, each with its reason):

- Stricter here: `Content-Length` + `Transfer-Encoding` (Apache lets `Transfer-Encoding` win; that is the smuggling pair), obs-fold, `HTTP/2.0` (505), `TRACE` (501, Apache: 405), and ranges on an empty file (416 per RFC 9110 14.1.2; Apache serves 200).
- More lenient here, but RFC-legal: a `Host` of `a-b_c.d~e`, with a valid `%xx`, empty, or with an empty port (`a:`).
- `If-Unmodified-Since` next to `If-Match`: here the stronger `If-Match` decides alone, Apache evaluates both.
- Under random byte mutations the only rule that is enforced is the safe direction: if Apache says 4xx/5xx, this server must never say 2xx, and it must never answer 5xx other than 501.

## Numbers

ApacheBench on one machine (an old Intel MacBook, Python 3.13, loopback, client and server competing for the same CPU), 20 000 requests (2 000 for the 1 MB file), `-c` = concurrent connections, requests per second. **Read these as relative, not as absolute capacity.**

| Request | keep-alive | c | this server | Apache 2.4 (event) |
|---|---|---|---|---|
| `/small.txt` (12 B) | yes | 1 | 1 308 | 2 060 |
| `/small.txt` | yes | 50 | 1 537 | 3 037 |
| `/small.txt` | no | 1 | 546 | 520 |
| `/small.txt` | no | 50 | 522 | 550 |
| `/json` (routed handler) | yes | 50 | 5 072 | n/a |
| `/big.bin` (1 MB) | yes | 50 | 993 | 1 323 |
| `/big.bin` | no | 50 | 808 | 986 |

Without keep-alive both servers sit at about 520 requests/s, which is the cost of opening a loopback connection on this machine rather than of either server. With keep-alive, a thread-per-connection Python server is 1.3–2x behind an event-driven C server on static files and does well on a pure handler. Zero failed requests on this side in every run.

## Limits (on purpose)

- No TLS, no HTTP/2, no WebSocket, no `CONNECT`/`TRACE` (501).
- One thread per connection (`max_connections`, default 256): fine for thousands of requests per second and a few hundred clients, not a C10K server.
- No `multipart/form-data` parsing; `Request.form()` handles `application/x-www-form-urlencoded`.
- Routes do not redirect between `/a` and `/a/`; only directories served by `StaticFiles` do.
- No compression on responses of the application (only `StaticFiles` gzips), no virtual hosts, no sessions.

## Development

```
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest -q
```

MIT licensed.

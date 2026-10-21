"""An HTTP/1.1 web server written from scratch on raw sockets (standard library only): strict request parsing, persistent connections and pipelining, chunked bodies,
routing, and a static file server with conditional requests, ranges and compression."""

__version__ = "0.1.0"

from .errors import HTTPError  # noqa: E402
from .headers import Headers  # noqa: E402
from .request import Request  # noqa: E402
from .response import Response, json_response, redirect, text_response  # noqa: E402
from .router import App  # noqa: E402
from .server import Server  # noqa: E402
from .static import StaticFiles  # noqa: E402

__all__ = ["HTTPError", "Headers", "Request", "Response", "json_response", "redirect", "text_response", "App", "Server", "StaticFiles", "__version__"]

"""The one exception a handler or the parser raises to make the server answer with a given status."""


class HTTPError(Exception):
    def __init__(self, status, message="", headers=None, close=False):
        super().__init__("%d %s" % (status, message))
        self.status = status
        self.message = message
        self.headers = list(headers or [])
        self.close = close                 # the connection cannot be reused (the framing of what followed is unknown)

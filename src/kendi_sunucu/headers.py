"""Header fields: a case-insensitive multi-map that keeps the order and spelling of what was sent, and the character rules of RFC 9110."""
from __future__ import annotations

import re

TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_BAD_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")                  # control characters except HTAB (obs-text, bytes >= 0x80, is allowed)


class Headers:
    def __init__(self, items=None):
        self._items = []
        for name, value in (items or []):
            self.add(name, value)

    # --- building ---------------------------------------------------------------------------------------------------------------------------
    def add(self, name, value):
        name, value = _check(name, value)
        self._items.append((name, value))

    def set(self, name, value):
        """Replace every field of that name by this one."""
        name, value = _check(name, value)
        low = name.lower()
        self._items = [(n, v) for n, v in self._items if n.lower() != low]
        self._items.append((name, value))

    def setdefault(self, name, value):
        if name not in self:
            self.add(name, value)

    def remove(self, name):
        low = name.lower()
        self._items = [(n, v) for n, v in self._items if n.lower() != low]

    # --- reading ----------------------------------------------------------------------------------------------------------------------------
    def get(self, name, default=None):
        """The first field of that name."""
        low = name.lower()
        for n, v in self._items:
            if n.lower() == low:
                return v
        return default

    def getall(self, name):
        low = name.lower()
        return [v for n, v in self._items if n.lower() == low]

    def get_list(self, name):
        """The comma-separated elements of every field of that name, trimmed and without empty ones."""
        out = []
        for v in self.getall(name):
            out.extend(p.strip() for p in v.split(",") if p.strip())
        return out

    def items(self):
        return list(self._items)

    def __contains__(self, name):
        low = name.lower()
        return any(n.lower() == low for n, _v in self._items)

    def __iter__(self):
        return iter(self._items)

    def __len__(self):
        return len(self._items)

    def copy(self):
        h = Headers()
        h._items = list(self._items)
        return h

    def __repr__(self):
        return "Headers(%r)" % (self._items,)


def _check(name, value):
    name, value = str(name), str(value)
    if not TOKEN.match(name):
        raise ValueError("invalid header name %r" % name)
    if _BAD_VALUE.search(value):
        raise ValueError("invalid character in the value of header %r (a header value cannot contain line breaks or control characters)" % name)
    return name, value.strip(" \t")


def parse_quality(header_value):
    """``Accept-Encoding``-style list -> ``{token: q}`` (q defaults to 1; a malformed q counts as 0)."""
    out = {}
    for part in header_value.split(","):
        bits = [b.strip() for b in part.split(";")]
        if not bits[0]:
            continue
        q = 1.0
        for b in bits[1:]:
            if b.lower().startswith("q="):
                try:
                    q = float(b[2:])
                except ValueError:
                    q = 0.0
        out[bits[0].lower()] = q
    return out

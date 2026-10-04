"""No stored password: Discord issues single-use owner login codes."""

import hashlib
import ipaddress
import re
import secrets
import time
from collections import OrderedDict, deque
from dataclasses import dataclass

from .constants import CODE_SECONDS, IDLE_SECONDS, MAX_SESSIONS, SESSION_SECONDS


def digest(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def hostname(value):
    value = value.lower().rstrip(".")
    if len(value) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", value):
        raise ValueError("Use a hostname without a scheme, port or path.")
    if any(
        not part or len(part) > 63 or part.startswith("-") or part.endswith("-")
        for part in value.split(".")
    ):
        raise ValueError("Use a valid hostname.")
    return value


def listener(bind, port):
    try:
        address = str(ipaddress.ip_address(bind))
    except ValueError:
        raise ValueError(
            "Bind must be an IPv4 or IPv6 address, such as 127.0.0.1 or 0.0.0.0."
        ) from None
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Use a port from 1024 to 65535.")
    return address, port


@dataclass
class Session:
    owner_id: int
    csrf: str
    created: float
    touched: float


class Auth:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.codes = {}
        self.sessions = OrderedDict()
        self.rates = OrderedDict()

    def prune(self):
        now = self.clock()
        self.codes = {key: value for key, value in self.codes.items() if value[1] > now}
        for key, session in list(self.sessions.items()):
            if now - session.created >= SESSION_SECONDS or now - session.touched >= IDLE_SECONDS:
                self.sessions.pop(key)

    def issue(self, owner_id):
        self.prune()
        self.codes = {key: value for key, value in self.codes.items() if value[0] != owner_id}
        while len(self.codes) >= MAX_SESSIONS:
            self.codes.pop(next(iter(self.codes)))
        code = secrets.token_urlsafe(24)
        self.codes[digest(code)] = (owner_id, self.clock() + CODE_SECONDS)
        return code

    def redeem(self, code):
        self.prune()
        # Consume atomically before any asynchronous owner check. Replay always fails.
        record = self.codes.pop(digest(code), None)
        return record[0] if record else None

    def create(self, owner_id):
        self.prune()
        while len(self.sessions) >= MAX_SESSIONS:
            self.sessions.popitem(last=False)
        cookie = secrets.token_urlsafe(32)
        now = self.clock()
        session = Session(owner_id, secrets.token_urlsafe(24), now, now)
        self.sessions[digest(cookie)] = session
        return cookie, session

    def get(self, cookie):
        self.prune()
        key = digest(cookie)
        session = self.sessions.get(key)
        if session:
            session.touched = self.clock()
            self.sessions.move_to_end(key)
        return session

    def revoke(self, owner_id):
        self.codes = {key: value for key, value in self.codes.items() if value[0] != owner_id}
        self.sessions = OrderedDict(
            (key, value) for key, value in self.sessions.items() if value.owner_id != owner_id
        )

    def clear(self):
        self.codes.clear()
        self.sessions.clear()
        self.rates.clear()

    def allow(self, key, *, limit):
        """One-minute windows with a bounded key map, including failed logins."""
        now = self.clock()
        hits = self.rates.setdefault(key, deque())
        self.rates.move_to_end(key)
        while hits and hits[0] <= now - 60:
            hits.popleft()
        while len(self.rates) > 256:
            self.rates.popitem(last=False)
        if len(hits) >= limit:
            return False
        hits.append(now)
        return True

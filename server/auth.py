"""Shared-password authentication, for when the dashboard is served to a LAN.

Only needed when the server binds beyond loopback. `create_app(auth=None)`
means no authentication at all -- the same "None means inert" convention
queue/detection/slicer already use -- which is what the desktop app and the
dev workflow get, since they bind 127.0.0.1 and there would be nowhere to type
a password anyway.

Deliberately small and dependency-free: one shared secret, in-memory sessions.
There are no user accounts (see the design spec), and a server restart
invalidates every session, which is fine for a lab tool.

WHAT THIS DOES NOT DO: the transport is plain HTTP, so the password is
protected against a curious passer-by, not against someone sniffing the
network. Real confidentiality needs TLS, which means certificates. That trade
is deliberate and recorded in the spec, not an oversight.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

# Hosts that mean "this machine only". A host wrongly treated as loopback would
# let __main__'s fail-closed check pass and expose printer control unprotected,
# so this list is deliberately exact rather than a prefix match: "127.0.0.1" is
# local, but "0.0.0.0" (all interfaces) emphatically is not.
LOOPBACK_HOSTS = frozenset({"", "127.0.0.1", "localhost", "::1"})


def is_loopback(host) -> bool:
    """True when binding `host` exposes nothing beyond this machine. An unset
    host counts as local: that is uvicorn's own default."""
    if host is None:
        return True
    return str(host).strip().lower() in LOOPBACK_HOSTS


def _digest(password: str) -> bytes:
    return hashlib.sha256(password.encode("utf-8")).digest()


class Auth:
    """One shared password and the sessions issued against it."""

    #: Cookie the browser carries. A cookie, not an Authorization header,
    #: because the dashboard's live updates run over a WebSocket and browsers
    #: cannot set headers on a WS handshake -- cookies ride it automatically,
    #: so one mechanism covers /api/* and /ws alike.
    COOKIE = "bambu_session"

    def __init__(self, password: str):
        if not password or not isinstance(password, str):
            # An Auth with an empty secret would accept "" from anyone, which
            # is strictly worse than running with auth disabled, because it
            # LOOKS protected.
            raise ValueError("a non-empty password is required")
        self._digest = _digest(password)
        self._sessions: set[str] = set()

    def check_password(self, password) -> bool:
        """Constant-time comparison, so a wrong guess cannot be narrowed down
        character by character by timing the response."""
        if not isinstance(password, str):
            return False
        return hmac.compare_digest(self._digest, _digest(password))

    def login(self, password) -> str | None:
        """-> a new session token, or None when the password is wrong."""
        if not self.check_password(password):
            return None
        token = secrets.token_urlsafe(32)
        self._sessions.add(token)
        return token

    def valid(self, token) -> bool:
        """Is this a live session? Plain set membership is fine here: the
        token is 32 random bytes we issued, not a secret the caller is trying
        to guess a character at a time."""
        if not isinstance(token, str) or not token:
            return False
        return token in self._sessions

    def logout(self, token) -> None:
        """Drop one session. Unknown tokens are ignored -- logging out twice,
        or with a stale cookie, is not an error."""
        if isinstance(token, str):
            self._sessions.discard(token)

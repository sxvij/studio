#!/usr/bin/env python3
"""Cloudflare Access check for nginx auth_request (same code as Crapletics; studio copy).

GET /auth answers 204 (allow) or 403 (deny). On an allowed request with a token, the 204 carries X-User: the verified
email (lower-cased), which nginx passes to the live table service; local requests get no X-User. A request is allowed when
  - it carries a Cf-Access-Jwt-Assertion token signed by the team's Access keys, for our AUD, from our issuer,
    not expired, with an email in the token; or
  - it is local: no Cloudflare headers at all (deploy checks from inside the container; nothing else can reach
    127.0.0.1 because the firewall drops all inbound).
With CF_ACCESS_TEAM or CF_ACCESS_AUD unset, every request through Cloudflare is refused (fails closed).
Settings come from /etc/studio/gate.env via the systemd unit.
"""
import hashlib
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import jwt

TEAM = os.environ.get("CF_ACCESS_TEAM", "").strip()
AUD = os.environ.get("CF_ACCESS_AUD", "").strip()
PORT = int(os.environ.get("GATE_PORT", "8101"))
CACHE_MAX = 2000

_jwks = None
_cache = {}  # sha256(token) -> (expiry epoch seconds, email) of tokens already verified
_lock = threading.Lock()


def jwks():
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(f"https://{TEAM}.cloudflareaccess.com/cdn-cgi/access/certs", cache_keys=True, lifespan=3600)
    return _jwks


EMAIL_OK = re.compile(r"[^\s@,;:<>\"\\]{1,64}@[A-Za-z0-9.-]{1,190}")


def identity(tok):
    """The verified, lower-cased email of a valid Access token for this app, or None."""
    if not (TEAM and AUD and tok) or len(tok) > 8192:
        return None
    h = hashlib.sha256(tok.encode()).hexdigest()
    now = time.time()
    with _lock:
        hit = _cache.get(h)
    if hit and hit[0] > now:
        return hit[1]
    try:
        key = jwks().get_signing_key_from_jwt(tok).key
        claims = jwt.decode(tok, key, algorithms=["RS256"], audience=AUD, issuer=f"https://{TEAM}.cloudflareaccess.com",
                            options={"require": ["exp", "iat", "aud", "iss"]})
    except Exception:
        return None
    email = str(claims.get("email") or "").strip().lower()
    if not EMAIL_OK.fullmatch(email):
        return None
    with _lock:
        if len(_cache) >= CACHE_MAX:
            for k in [k for k, v in _cache.items() if v[0] <= now] or list(_cache)[: CACHE_MAX // 2]:
                _cache.pop(k, None)
        _cache[h] = (min(float(claims["exp"]), now + 300), email)
    return email


def verify(tok):
    """True if the token is a valid Access token for this app with an email."""
    return identity(tok) is not None


def check(headers):
    """(allowed, email or None)."""
    tok = headers.get("Cf-Access-Jwt-Assertion", "")
    via_cf = any(headers.get(x) for x in ("Cf-Ray", "Cf-Connecting-Ip", "Cf-Access-Jwt-Assertion"))
    if not via_cf:
        return True, None
    email = identity(tok)
    return email is not None, email


def allowed(headers):
    return check(headers)[0]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        ok, email = check(self.headers) if self.path == "/auth" else (False, None)
        self.send_response(204 if ok else 403)
        if ok and email:
            self.send_header("X-User", email)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):  # no per-request logs (they would hold nothing useful)
        pass


def main():
    if not (TEAM and AUD):
        print("CF_ACCESS_TEAM/CF_ACCESS_AUD not set: refusing every request through Cloudflare", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    print(f"access gate on 127.0.0.1:{PORT}, team {TEAM or '-'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()

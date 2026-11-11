#!/usr/bin/env python3
"""Tests for access_gate.py with a locally generated signing key (no network). Run where python3-jwt is installed."""
import os
import sys
import time
import unittest

os.environ.update(CF_ACCESS_TEAM="sxvij", CF_ACCESS_AUD="aud-123")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jwt  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

import access_gate as G  # noqa: E402

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


class FakeJWKS:
    def get_signing_key_from_jwt(self, tok):
        return type("K", (), {"key": KEY.public_key()})()


G._jwks = FakeJWKS()
ISS = "https://sxvij.cloudflareaccess.com"


def token(key=KEY, **over):
    now = int(time.time())
    claims = {"aud": ["aud-123"], "iss": ISS, "iat": now, "exp": now + 600, "email": "a@b.c"}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


class Gate(unittest.TestCase):
    def setUp(self):
        G._cache.clear()

    def test_local_allowed(self):
        self.assertTrue(G.allowed({}))

    def test_cloudflare_without_token_denied(self):
        self.assertFalse(G.allowed({"Cf-Ray": "x"}))
        self.assertFalse(G.allowed({"Cf-Connecting-Ip": "1.2.3.4"}))

    def test_valid_token(self):
        self.assertTrue(G.allowed({"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": token()}))

    def test_token_alone_must_be_valid(self):
        self.assertFalse(G.allowed({"Cf-Access-Jwt-Assertion": "abc"}))

    def test_bad_tokens(self):
        for tok in [token(key=OTHER), token(aud=["other"]), token(iss="https://evil.cloudflareaccess.com"),
                    token(exp=int(time.time()) - 5), token(email=None), token(exp=None), "abc", "x" * 9000]:
            self.assertFalse(G.allowed({"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": tok}), tok[:40])

    def test_cache_respects_expiry(self):
        tok = token(exp=int(time.time()) + 2)
        self.assertTrue(G.verify(tok))
        time.sleep(2.5)
        self.assertFalse(G.verify(tok))

    def test_identity_is_the_lowercased_email(self):
        self.assertEqual(G.check({"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": token(email="Dom@Example.COM")}), (True, "dom@example.com"))
        self.assertEqual(G.check({}), (True, None))
        self.assertEqual(G.check({"Cf-Ray": "x"}), (False, None))
        for bad in ["no-at-sign", "a b@c.d", "x@y.z\r\nX-Evil: 1", "a@b@c"]:
            self.assertEqual(G.check({"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": token(email=bad)})[0], False, bad)

    def test_cached_identity(self):
        tok = token(email="c@d.e")
        self.assertEqual(G.identity(tok), "c@d.e")
        self.assertEqual(G.identity(tok), "c@d.e")

    def test_unset_aud_fails_closed(self):
        old = G.AUD
        G.AUD = ""
        try:
            self.assertFalse(G.allowed({"Cf-Ray": "x", "Cf-Access-Jwt-Assertion": token()}))
            self.assertTrue(G.allowed({}))
        finally:
            G.AUD = old


if __name__ == "__main__":
    unittest.main(verbosity=1)

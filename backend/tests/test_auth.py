"""Auth tests: users, sessions, TOTP, rate limiting, gating (T8).

Runs the real HTTP server on an ephemeral port with auth enabled and no
users at boot (the bootstrap path), so every phase of the lifecycle is
exercised through the public HTTP interface.
"""
from http.client import HTTPConnection
import base64
import hashlib
import hmac as hmac_mod
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402
import auth  # noqa: E402
import fixture  # noqa: E402
import schema  # noqa: E402
import sync as syncmod  # noqa: E402


def totp_code(secret_b32, now=None, step=30):
    """Compute a valid TOTP code the way an authenticator app would."""
    if now is None:
        now = time.time()
    key = base64.b32decode(secret_b32 + "=" * ((8 - len(secret_b32) % 8) % 8))
    counter = int(now // step)
    msg = struct.pack(">Q", counter)
    digest = hmac_mod.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1000000
    return f"{code:06d}"


class AuthTestBase(unittest.TestCase):
    """Server with a fresh, userless history DB: bootstrap drives it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.n8n_db = os.path.join(cls.tmp.name, "n8n.sqlite")
        cls.hist_db = os.path.join(cls.tmp.name, "history.sqlite")
        fixture.create_n8n_db(cls.n8n_db)
        app.CONFIG.update({
            "n8n_db": cls.n8n_db,
            "history_db": cls.hist_db,
            "page_size": 50,
            "retention_days": 0,
            "brand": None,
        })
        schema.init_history(cls.hist_db)
        syncmod.run_sync(cls.n8n_db, cls.hist_db)
        from http.server import ThreadingHTTPServer
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, headers=None, cookie=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if cookie:
            hdrs["Cookie"] = f"viewer_session={cookie}"
        payload = json.dumps(body).encode() if body is not None else None
        if payload is not None:
            hdrs["Content-Type"] = "application/json"
            hdrs["Content-Length"] = str(len(payload))
        conn.request(method, path, payload, hdrs)
        res = conn.getresponse()
        data = res.read()
        set_cookie = res.getheader("Set-Cookie")
        conn.close()
        try:
            parsed = json.loads(data.decode() or "null")
        except ValueError:
            parsed = data.decode()
        return res.status, parsed, set_cookie

    def cookie_value(self, set_cookie):
        return set_cookie.split(";")[0].split("=", 1)[1] if set_cookie else None


class TestBootstrapAndLogin(AuthTestBase):
    def test_unauthenticated_api_is_401(self):
        st, body, _ = self.req("GET", "/api/stats")
        self.assertEqual(st, 401)
        self.assertEqual(body, {"error": "unauthenticated"})

    def test_bootstrap_only_when_empty(self):
        st, body, setc = self.req("POST", "/api/auth/bootstrap",
                                  {"username": "yoda", "password": "clave-segura-1"})
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["user"]["role"], "admin")
        self.assertTrue(self.cookie_value(setc))
        # second bootstrap: users exist now -> 403
        st, body, _ = self.req("POST", "/api/auth/bootstrap",
                               {"username": "evil", "password": "clave-segura-1"})
        self.assertEqual(st, 403)

    def test_login_ok_full_session(self):
        st, body, setc = self.req("POST", "/api/auth/login",
                                  {"username": "yoda", "password": "clave-segura-1"})
        self.assertEqual(st, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["user"]["role"], "admin")
        cookie = self.cookie_value(setc)
        self.assertIn("HttpOnly", setc)
        self.assertIn("SameSite=Lax", setc)
        # the session works on a protected endpoint
        st, body, _ = self.req("GET", "/api/stats", cookie=cookie)
        self.assertEqual(st, 200)

    def test_login_bad_is_401_and_never_says_which_factor(self):
        for creds in ({"username": "yoda", "password": "wrong"},
                      {"username": "ghost", "password": "clave-segura-1"}):
            st, body, _ = self.req("POST", "/api/auth/login", creds)
            self.assertEqual(st, 401)
            self.assertEqual(body, {"status": "bad"})

    def test_me_and_logout(self):
        _, _, setc = self.req("POST", "/api/auth/login",
                              {"username": "yoda", "password": "clave-segura-1"})
        cookie = self.cookie_value(setc)
        st, body, _ = self.req("GET", "/api/auth/me", cookie=cookie)
        self.assertEqual(st, 200)
        self.assertEqual(body["user"]["name"], "yoda")
        self.assertEqual(body["auth"], "local")
        st, _, setc2 = self.req("POST", "/api/auth/logout", cookie=cookie)
        self.assertEqual(st, 200)
        # session is gone: the old cookie no longer opens anything
        st, body, _ = self.req("GET", "/api/stats", cookie=cookie)
        self.assertEqual(st, 401)


class TestGating(AuthTestBase):
    def test_admin_endpoints_403_for_viewer(self):
        con = app.history()
        try:
            auth.create_user(con, "pepe", "clave-segura-1", "viewer")
        finally:
            con.close()
        _, _, setc = self.req("POST", "/api/auth/login",
                              {"username": "pepe", "password": "clave-segura-1"})
        cookie = self.cookie_value(setc)
        # viewer can browse
        st, _, _ = self.req("GET", "/api/stats", cookie=cookie)
        self.assertEqual(st, 200)
        # but not administer
        for method, path, body in (
                ("POST", "/api/config", {"retentionDays": 30}),
                ("POST", "/api/sync", None),
                ("GET", "/api/users", None),
                ("POST", "/api/users", {"username": "x1", "password": "clave-segura-1", "role": "viewer"}),
        ):
            st, body, _ = self.req(method, path, body,
                                   headers={"x-viewer-action": "sync" if path == "/api/sync" else "config"},
                                   cookie=cookie)
            self.assertEqual(st, 403, path)

    def test_totp_pending_session_cannot_browse(self):
        con = app.history()
        try:
            auth.create_user(con, "mika", "clave-segura-1", "admin")
            con.execute("UPDATE users SET totp_secret=?, totp_enabled=1 "
                        "WHERE username='mika'", (auth.generate_secret(),))
            con.commit()
        finally:
            con.close()
        _, body, setc = self.req("POST", "/api/auth/login",
                                 {"username": "mika", "password": "clave-segura-1"})
        self.assertEqual(body["status"], "totp_required")
        cookie = self.cookie_value(setc)
        st, body, _ = self.req("GET", "/api/stats", cookie=cookie)
        self.assertEqual(st, 401)

    def test_last_active_admin_is_protected(self):
        con = app.history()
        try:
            auth.create_user(con, "solo", "clave-segura-1", "admin")
        finally:
            con.close()
        _, _, setc = self.req("POST", "/api/auth/login",
                              {"username": "solo", "password": "clave-segura-1"})
        cookie = self.cookie_value(setc)
        st, body, _ = self.req("PATCH", "/api/users/solo", {"role": "viewer"}, cookie=cookie)
        self.assertEqual(st, 409)
        st, body, _ = self.req("PATCH", "/api/users/solo", {"disabled": True}, cookie=cookie)
        self.assertEqual(st, 409)
        st, body, _ = self.req("DELETE", "/api/users/solo", cookie=cookie)
        self.assertEqual(st, 409)
        # but a second admin makes it possible
        st, body, _ = self.req("POST", "/api/users",
                               {"username": "dos", "password": "clave-segura-2", "role": "admin"},
                               cookie=cookie)
        self.assertEqual(st, 200)
        st, body, _ = self.req("DELETE", "/api/users/solo", cookie=cookie)
        self.assertEqual(st, 200)


class TestTotp(AuthTestBase):
    def test_totp_roundtrip(self):
        con = app.history()
        try:
            auth.create_user(con, "hana", "clave-segura-1", "admin")
        finally:
            con.close()
        _, _, setc = self.req("POST", "/api/auth/login",
                              {"username": "hana", "password": "clave-segura-1"})
        cookie = self.cookie_value(setc)
        st, body, _ = self.req("GET", "/api/auth/totp/setup", cookie=cookie)
        self.assertEqual(st, 200)
        setup_body = body
        self.assertIn("otpauth://totp/n8n-viewer:hana?", setup_body["otpauth_uri"])
        self.assertIn("issuer=n8n-viewer", setup_body["otpauth_uri"])
        # setup did not activate it
        st, body, _ = self.req("GET", "/api/auth/me", cookie=cookie)
        self.assertFalse(body["user"]["totp_enabled"])
        # enable with a valid code
        code = totp_code(setup_body["secret"])
        st, body, _ = self.req("POST", "/api/auth/totp/enable", {"code": code}, cookie=cookie)
        self.assertEqual(st, 200)
        st, body, _ = self.req("GET", "/api/auth/me", cookie=cookie)
        self.assertTrue(body["user"]["totp_enabled"])
        # re-login now demands the second factor
        _, body, setc = self.req("POST", "/api/auth/login",
                                 {"username": "hana", "password": "clave-segura-1"})
        self.assertEqual(body["status"], "totp_required")
        cookie2 = self.cookie_value(setc)
        # a wrong code does not upgrade the session
        st, body, _ = self.req("POST", "/api/auth/totp", {"code": "000000"}, cookie=cookie2)
        self.assertEqual(st, 401)
        st, body, _ = self.req("GET", "/api/stats", cookie=cookie2)
        self.assertEqual(st, 401)
        # the right code does
        st, body, setc3 = self.req("POST", "/api/auth/totp",
                                   {"code": totp_code(self._secret_of("hana"))},
                                   cookie=cookie2)
        self.assertEqual(st, 200)
        st, body, _ = self.req("GET", "/api/stats", cookie=self.cookie_value(setc3))
        self.assertEqual(st, 200)
        # disable requires a valid code too
        st, body, _ = self.req("POST", "/api/auth/totp/disable", {"code": "000000"},
                               cookie=self.cookie_value(setc3))
        self.assertEqual(st, 401)
        st, body, _ = self.req("POST", "/api/auth/totp/disable",
                               {"code": totp_code(self._secret_of("hana"))},
                               cookie=self.cookie_value(setc3))
        self.assertEqual(st, 200)
        st, body, _ = self.req("GET", "/api/auth/me", cookie=self.cookie_value(setc3))
        self.assertFalse(body["user"]["totp_enabled"])

    def _secret_of(self, username):
        con = app.history()
        try:
            return con.execute("SELECT totp_secret FROM users WHERE username=?",
                               (username,)).fetchone()[0]
        finally:
            con.close()


class TestRateLimit(AuthTestBase):
    def test_lockout_after_five_failures(self):
        con = app.history()
        try:
            auth.create_user(con, "lockme", "clave-segura-1", "viewer")
        finally:
            con.close()
        for _ in range(5):
            st, body, _ = self.req("POST", "/api/auth/login",
                                   {"username": "lockme", "password": "wrong"})
            self.assertEqual(st, 401)
        st, body, _ = self.req("POST", "/api/auth/login",
                               {"username": "lockme", "password": "wrong"})
        self.assertEqual(st, 429)
        self.assertIn("retry_after", body)
        # even the CORRECT password is refused while locked
        st, body, _ = self.req("POST", "/api/auth/login",
                               {"username": "lockme", "password": "clave-segura-1"})
        self.assertEqual(st, 429)
        # and a successful login clears the counter afterwards
        con = app.history()
        try:
            con.execute("DELETE FROM login_attempts WHERE username=?", ("lockme",))
            con.commit()
        finally:
            con.close()
        st, body, _ = self.req("POST", "/api/auth/login",
                               {"username": "lockme", "password": "clave-segura-1"})
        self.assertEqual(st, 200)

    def test_oids_disabled_by_default(self):
        st, body, _ = self.req("GET", "/api/auth/providers")
        self.assertEqual(st, 200)
        self.assertEqual(body, {"oidc": False})


class TestPrimitives(unittest.TestCase):
    def test_password_hash_roundtrip_and_salted(self):
        h1 = auth.hash_password("clave-segura-1")
        h2 = auth.hash_password("clave-segura-1")
        self.assertNotEqual(h1, h2)  # salted
        self.assertTrue(auth.check_password("clave-segura-1", h1))
        self.assertFalse(auth.check_password("clave-segura-2", h1))

    def test_totp_accepts_window_and_rejects_far(self):
        secret = auth.generate_secret()
        now = 1_700_000_000
        code = totp_code(secret, now=now)
        self.assertTrue(auth.verify_totp(secret, code, now=now))
        self.assertTrue(auth.verify_totp(secret, code, now=now + 30))  # ±1 step
        self.assertFalse(auth.verify_totp(secret, code, now=now + 120))
        self.assertFalse(auth.verify_totp(secret, "00000", now=now))  # wrong length

    def test_session_secret_persists_and_is_private(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "history.db")
            p1 = auth.load_session_secret(db)
            p2 = auth.load_session_secret(db)
            self.assertEqual(p1, p2)
            mode = os.stat(os.path.join(d, ".session_secret")).st_mode & 0o777
            self.assertEqual(mode, 0o600)

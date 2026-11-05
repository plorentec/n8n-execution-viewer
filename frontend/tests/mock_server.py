"""Minimal mock backend for the frontend smoke test (frontend/tests/smoke.mjs).

Serves the real frontend files plus canned /api/* answers: login sets two
cookies (session + a fake role flag) exactly like the real backend, /api/users
requires the role cookie, /api/config requires only the session. Run:

    python3 tests/mock_server.py &        # prints its port
    node tests/smoke.mjs                  # MOCK_URL env or 127.0.0.1 default port

Standard library only. Not part of the shipped app.
"""
import json, os, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FRONT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..")
FRONT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
USERS = [{"name": "pablo", "role": "admin", "totp_enabled": False, "disabled": False,
          "last_login": "2026-11-05 10:00:00.000", "source": "local"},
         {"name": "mama", "role": "viewer", "totp_enabled": True, "disabled": False,
          "last_login": None, "source": "local"}]

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _json(self, code, payload, headers=()):
        body = json.dumps(payload).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in headers: self.send_header(k, v)
        self.end_headers(); self.wfile.write(body)
    def _admin(self): return "mockrole=admin" in self.headers.get("Cookie", "")
    def _authed(self): return "sid=1" in self.headers.get("Cookie", "")
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if not p.startswith("/api/"):
            fp = os.path.join(FRONT, p.lstrip("/") or "index.html")
            if os.path.isfile(fp):
                body = open(fp, "rb").read()
                ct = {"html":"text/html","js":"text/javascript","css":"text/css"}.get(
                    p.rsplit(".",1)[-1], "application/octet-stream")
                self.send_response(200); self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(body))); self.end_headers()
                self.wfile.write(body); return
            return self._json(404, {"error":"not found"})
        if p == "/api/auth/me":
            if not self._authed(): return self._json(401, {"error":"unauthenticated"})
            return self._json(200, {"user":{"name":"pablo","role":"admin","totp_enabled":False},"auth":"local"})
        if p == "/api/users":
            if not self._admin(): return self._json(403, {"error":"forbidden"})
            return self._json(200, USERS)
        if p == "/api/auth/totp/setup":
            return self._json(200, {"secret":"JBSWY3DPEHPK3PXP","otpauth_uri":"otpauth://totp/x?secret=JBSWY3DPEHPK3PXP"})
        if p == "/api/config":
            if not self._authed(): return self._json(401, {"error":"unauthenticated"})
            return self._json(200, {"retentionDays": 30, "brand": {"title":"PRUEBA","subtitle":"sub","logo":None}})
        return self._json(404, {"error":"not found"})
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        self.rfile.read(int(self.headers.get("Content-Length",0) or 0))
        if p == "/api/auth/login":
            return self._json(200, {"status":"ok","user":{"name":"pablo","role":"admin"}},
                              [("Set-Cookie","sid=1; Path=/"),("Set-Cookie","mockrole=admin; Path=/")])
        if p == "/api/config":
            if not self._admin(): return self._json(403, {"error":"forbidden"})
            return self._json(200, {"retentionDays": 7, "brand": None})
        return self._json(404, {"error":"not found"})
    do_PATCH = do_POST
    def do_DELETE(self):
        self.rfile.read(int(self.headers.get("Content-Length",0) or 0))
        return self._json(200, {"status":"ok"})

def serve():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv

if __name__ == "__main__":
    s = serve()
    print(s.server_address[1], flush=True)
    import time; time.sleep(60)

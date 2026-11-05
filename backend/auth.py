"""Authentication core for the viewer (T8): passwords, sessions, TOTP,
rate limiting and user management. Pure logic — the HTTP layer lives in
app.py. Python stdlib only.

Design notes:
- Passwords: PBKDF2-HMAC-SHA256, 600k iterations, per-user salt, stored
  as "pbkdf2$<iters>$<salt_b64>$<hash_b64>", compared in constant time.
- Sessions: the cookie carries <token>.<expires_epoch>.<hmac>; the DB
  stores only sha256(token) (a leaked DB cannot mint cookies) and the
  HMAC is keyed by a server secret persisted next to the history DB, so
  sessions survive restarts without surviving a DB copy to another box.
- TOTP: RFC 6238 (SHA1, 30 s, 6 digits, window ±1) with an injectable
  clock for tests.
- Rate limiting: failures are counted per (username, ip) over a rolling
  15-minute window; the check happens BEFORE touching the password so a
  locked account is never even validated.
"""
import base64
import hashlib
import hmac
import os
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone

SESSION_COOKIE = "viewer_session"
SESSION_TTL = 7 * 24 * 3600
PBKDF2_ITERS = 600000
TOTP_STEP = 30
TOTP_DIGITS = 6
TOTP_WINDOW = 1
LOCK_WINDOW = 15 * 60
LOCK_THRESHOLD = 5
USERNAME_RE = r"^[a-zA-Z0-9._-]{2,64}$"

# timestamp format shared with the rest of the backend (UTC, string sort)
FMT = "%Y-%m-%d %H:%M:%S.%f"


def now_str(when=None):
    dt = datetime.fromtimestamp(when, tz=timezone.utc) if when is not None \
        else datetime.now(timezone.utc)
    return dt.strftime(FMT)


def epoch_of(ts):
    return datetime.strptime(ts, FMT).replace(tzinfo=timezone.utc).timestamp()


# ---------------------------------------------------------------- passwords

def hash_password(password):
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS)
    return (f"pbkdf2${PBKDF2_ITERS}$"
            f"{base64.b64encode(salt).decode()}$"
            f"{base64.b64encode(digest).decode()}")


def check_password(password, stored):
    try:
        scheme, iters, salt_b64, hash_b64 = stored.split("$")
        if scheme != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode(),
            base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------- totp

def generate_secret():
    return base64.b32encode(os.urandom(20)).decode().rstrip("=")


def totp_uri(secret, username):
    return f"otpauth://totp/n8n-viewer:{username}?secret={secret}&issuer=n8n-viewer"


def _b32decode(secret):
    pad = "=" * ((8 - len(secret) % 8) % 8)
    return base64.b32decode(secret + pad)


def totp_codes(secret, now=None):
    """The codes valid right now (current step ± WINDOW)."""
    if now is None:
        now = time.time()
    key = _b32decode(secret)
    counter = int(now // TOTP_STEP)
    out = set()
    for off in range(-TOTP_WINDOW, TOTP_WINDOW + 1):
        msg = struct.pack(">Q", counter + off)
        digest = hmac.new(key, msg, hashlib.sha1).digest()
        o = digest[-1] & 0x0F
        value = (struct.unpack(">I", digest[o:o + 4])[0] & 0x7FFFFFFF) \
            % (10 ** TOTP_DIGITS)
        out.add(f"{value:0{TOTP_DIGITS}d}")
    return out


def verify_totp(secret, code, now=None):
    if not secret or not code or not code.isdigit() \
            or len(code) != TOTP_DIGITS:
        return False
    return code in totp_codes(secret, now)


# ---------------------------------------------------------------- sessions

def load_session_secret(history_db_path):
    """Read or create the HMAC key; 0600, next to the history DB."""
    path = os.path.join(os.path.dirname(os.path.abspath(history_db_path)),
                        ".session_secret")
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        key = secrets.token_bytes(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        return key


_SECRET = None


def set_secret(key):
    global _SECRET
    _SECRET = key


def _sign(token, expires_epoch):
    return hmac.new(_SECRET, f"{token}.{expires_epoch}".encode(),
                    hashlib.sha256).hexdigest()


def mint_session(hist, user_id, ip, pending, now=None):
    """Insert a session row and return the cookie value. The DB stores
    sha256(token); the cookie carries the raw token plus signature."""
    if now is None:
        now = time.time()
    token = secrets.token_urlsafe(32)
    expires_epoch = int(now) + SESSION_TTL
    expires = now_str(expires_epoch)
    hist.execute(
        "INSERT INTO sessions (token_hash, user_id, pending_totp, created_at,"
        " expires_at, ip) VALUES (?,?,?,?,?,?)",
        (hashlib.sha256(token.encode()).hexdigest(), user_id,
         1 if pending else 0, now_str(now), expires, ip))
    hist.commit()
    return f"{token}.{expires_epoch}.{_sign(token, expires_epoch)}"


def session_from_cookie(hist, cookie_value, now=None):
    """Validate the cookie and return (user_row, session_row) or None.
    A pending_totp session is returned with session['pending_totp']=1 so
    the HTTP layer can allow ONLY the TOTP step through it."""
    if not cookie_value or _SECRET is None:
        return None
    try:
        token, expires_epoch, sig = cookie_value.rsplit(".", 2)
        expires_epoch = int(expires_epoch)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(sig, _sign(token, expires_epoch)):
        return None
    if expires_epoch < (now or time.time()):
        return None
    row = hist.execute(
        "SELECT s.id AS sid, s.pending_totp, s.expires_at, s.user_id, "
        "u.username, u.role, u.totp_enabled, u.disabled, u.source "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token_hash = ?",
        (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if row is None or row["disabled"]:
        return None
    return row


def destroy_session(hist, cookie_value):
    if not cookie_value:
        return
    token = cookie_value.rsplit(".", 2)[0]
    hist.execute("DELETE FROM sessions WHERE token_hash = ?",
                 (hashlib.sha256(token.encode()).hexdigest(),))
    hist.commit()


def prune_expired(hist, now=None):
    hist.execute("DELETE FROM sessions WHERE expires_at < ?",
                 (now_str(now),))


# ---------------------------------------------------------------- rate limit

def fail_count(hist, username, ip, now=None):
    since = now_str((now or time.time()) - LOCK_WINDOW)
    return hist.execute(
        "SELECT count(*) FROM login_attempts WHERE username=? AND ip=? AND ts>?",
        (username, ip, since)).fetchone()[0]


def record_fail(hist, username, ip, now=None):
    hist.execute("INSERT INTO login_attempts (username, ip, ts) VALUES (?,?,?)",
                 (username, ip, now_str(now)))
    hist.commit()


def clear_fails(hist, username, ip):
    hist.execute("DELETE FROM login_attempts WHERE username=? AND ip=?",
                 (username, ip))
    hist.commit()


# ---------------------------------------------------------------- users

def create_user(hist, username, password, role):
    hist.execute(
        "INSERT INTO users (username, password_hash, role, source, created_at)"
        " VALUES (?,?,?,?,?)",
        (username, hash_password(password), role, "local", now_str()))
    hist.commit()


def count_active_admins(hist):
    return hist.execute(
        "SELECT count(*) FROM users WHERE role='admin' AND disabled=0"
    ).fetchone()[0]

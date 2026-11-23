"""Administrator-only control plane.

Rules this module enforces, in code rather than in prompts:

* Being signed in is not enough. Privileged actions need an **elevated admin
  session** created by re-entering the account password (and a TOTP code when
  MFA is enrolled). Saying "I am the admin" changes nothing, because nothing in
  the request body can set the elevation flag.
* Elevation lives in a server-side row with an idle timeout and an absolute
  lifetime. The browser only holds an opaque token in a signed cookie.
* Roles are stored per account. The first account created becomes the admin;
  later accounts are ordinary users.
* Every privileged action, denial and approval decision is appended to an
  append-only audit table and to logs/audit.jsonl.
* High-risk changes go through the approval queue and are never executed by the
  request that created them.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import struct
import threading
import time
import uuid

from . import config

log = logging.getLogger("harry.admin")

ROLE_ADMIN = "admin"
ROLE_USER = "user"
IDLE_SECONDS = 15 * 60
ABSOLUTE_SECONDS = 8 * 60 * 60
MAX_ADMIN_SESSIONS = 8
APPROVAL_TTL = 24 * 60 * 60

# Risk labels used by callers. ``high`` always needs an approval record.
LOW, MEDIUM, HIGH = "low", "medium", "high"


def _db_path():
    from .accounts import _db_path as accounts_db
    return accounts_db()


class AdminStore:
    def __init__(self, path=None):
        import sqlite3
        self.path = path or _db_path()
        self._lock = threading.RLock()
        self._sqlite = sqlite3
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._init()

    def _connect(self):
        connection = self._sqlite.connect(self.path, timeout=10)
        connection.row_factory = self._sqlite.Row
        return connection

    def _init(self):
        with self._lock, self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT,
                salt TEXT NOT NULL, hash TEXT NOT NULL, created REAL NOT NULL,
                active INTEGER NOT NULL DEFAULT 1)""")
            columns = {row["name"] for row in
                       db.execute("PRAGMA table_info(users)").fetchall()}
            if "role" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            if "totp_secret" not in columns:
                db.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
            # Exactly one administrator on an existing single-user instance.
            admins = db.execute("SELECT COUNT(*) AS n FROM users WHERE role='admin'").fetchone()["n"]
            if not admins:
                oldest = db.execute("SELECT id FROM users ORDER BY created LIMIT 1").fetchone()
                if oldest:
                    db.execute("UPDATE users SET role='admin' WHERE id=?", (oldest["id"],))
            db.execute("""CREATE TABLE IF NOT EXISTS admin_sessions (
                token TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                created REAL NOT NULL, last_seen REAL NOT NULL,
                expires REAL NOT NULL, ip TEXT, user_agent TEXT,
                revoked INTEGER NOT NULL DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS approvals (
                id TEXT PRIMARY KEY, action TEXT NOT NULL, risk TEXT NOT NULL,
                payload TEXT, status TEXT NOT NULL, created REAL NOT NULL,
                requested_by TEXT, decided_by TEXT, decided_at REAL,
                expires REAL NOT NULL, note TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS admin_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
                user_id TEXT, action TEXT NOT NULL, detail TEXT, ip TEXT)""")
            approval_columns = {row["name"] for row in
                                db.execute("PRAGMA table_info(approvals)").fetchall()}
            if "used_at" not in approval_columns:
                db.execute("ALTER TABLE approvals ADD COLUMN used_at REAL")

    # -- roles -------------------------------------------------------------

    def role(self, user_id):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT role FROM users WHERE id=?", (str(user_id),)).fetchone()
        return (row["role"] if row else "") or ""

    def is_admin(self, user_id):
        return self.role(user_id) == ROLE_ADMIN

    def set_role(self, user_id, role, actor=None):
        if role not in (ROLE_ADMIN, ROLE_USER):
            return {"ok": False, "error": "unknown role"}
        with self._lock, self._connect() as db:
            admins = db.execute("SELECT COUNT(*) AS n FROM users WHERE role=?",
                                (ROLE_ADMIN,)).fetchone()["n"]
            current = db.execute("SELECT role FROM users WHERE id=?",
                                 (str(user_id),)).fetchone()
            if not current:
                return {"ok": False, "error": "unknown account"}
            if current["role"] == ROLE_ADMIN and role != ROLE_ADMIN and admins <= 1:
                return {"ok": False, "error": "the last administrator cannot be demoted"}
            db.execute("UPDATE users SET role=? WHERE id=?", (role, str(user_id)))
        demoted = current["role"] == ROLE_ADMIN and role != ROLE_ADMIN
        revoked = 0
        if demoted:
            # Losing the role must invalidate every elevation the account holds,
            # not just the browser that happened to ask.
            revoked = self.revoke_user_sessions(user_id, actor=actor or user_id,
                                                reason="account demoted")
        self.audit(user_id, "role_changed", {"role": role, "sessions_revoked": revoked})
        return {"ok": True, "role": role, "sessions_revoked": revoked}

    def admin_ids(self):
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT id, email, name FROM users WHERE role=?",
                              (ROLE_ADMIN,)).fetchall()
        return [dict(row) for row in rows]

    # -- MFA ---------------------------------------------------------------

    def enroll_totp(self, user_id):
        """Create a pending TOTP secret. Verification turns it on."""
        secret = base64.b32encode(os.urandom(20)).decode().rstrip("=")
        with self._lock, self._connect() as db:
            db.execute("UPDATE users SET totp_secret=? WHERE id=?",
                       ("pending:" + secret, str(user_id)))
        return {"ok": True, "secret": secret,
                "otpauth": f"otpauth://totp/Harry:{user_id}?secret={secret}"
                           "&issuer=Harry&digits=6&period=30"}

    def confirm_totp(self, user_id, code):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT totp_secret FROM users WHERE id=?",
                             (str(user_id),)).fetchone()
        if not row or not row["totp_secret"]:
            return {"ok": False, "error": "no enrollment in progress"}
        secret = row["totp_secret"].split("pending:", 1)[-1]
        if not verify_totp(secret, code):
            return {"ok": False, "error": "that code is not valid"}
        with self._lock, self._connect() as db:
            db.execute("UPDATE users SET totp_secret=? WHERE id=?", (secret, str(user_id)))
        self.audit(user_id, "mfa_enrolled", {})
        return {"ok": True}

    def totp_required(self, user_id):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT totp_secret FROM users WHERE id=?",
                             (str(user_id),)).fetchone()
        return bool(row and row["totp_secret"] and not row["totp_secret"].startswith("pending:"))

    def _totp_secret(self, user_id):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT totp_secret FROM users WHERE id=?",
                             (str(user_id),)).fetchone()
        return (row["totp_secret"] if row else None) or ""

    def disable_totp(self, user_id):
        with self._lock, self._connect() as db:
            db.execute("UPDATE users SET totp_secret=NULL WHERE id=?", (str(user_id),))
        self.audit(user_id, "mfa_disabled", {})
        return {"ok": True}

    # -- elevation ---------------------------------------------------------

    def elevate(self, user_id, password, totp_code="", ip="", user_agent=""):
        from .accounts import get_store
        store = get_store()
        user = store.get(user_id)
        if not user or not user.get("active"):
            return {"ok": False, "error": "account is not available"}
        if not self.is_admin(user_id):
            self.audit(user_id, "elevation_denied", {"reason": "not an administrator"}, ip)
            return {"ok": False, "error": "this account is not an administrator"}
        result = store.verify(user["email"], password, ip=ip)
        if not result.get("ok"):
            self.audit(user_id, "elevation_denied", {"reason": "bad password"}, ip)
            return {"ok": False, "error": result.get("error", "authentication failed")}
        if self.totp_required(user_id):
            if not verify_totp(self._totp_secret(user_id), totp_code):
                self.audit(user_id, "elevation_denied", {"reason": "bad mfa code"}, ip)
                return {"ok": False, "error": "a valid authenticator code is required",
                        "mfa": True}
        token = secrets.token_urlsafe(32)
        now = time.time()
        ttl = float(config.CONFIG.get("admin", {}).get("idle_seconds") or IDLE_SECONDS)
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO admin_sessions
                (token, user_id, created, last_seen, expires, ip, user_agent, revoked)
                VALUES (?,?,?,?,?,?,?,0)""",
                       (token, user_id, now, now, now + ABSOLUTE_SECONDS,
                        str(ip or "")[:60], str(user_agent or "")[:200]))
            rows = db.execute("SELECT token FROM admin_sessions WHERE revoked=0 "
                              "ORDER BY last_seen DESC").fetchall()
            for spare in rows[MAX_ADMIN_SESSIONS:]:
                db.execute("UPDATE admin_sessions SET revoked=1 WHERE token=?",
                           (spare["token"],))
        self.audit(user_id, "admin_mode_entered", {"idle_seconds": ttl}, ip)
        return {"ok": True, "token": token, "expires_in": ABSOLUTE_SECONDS,
                "idle_seconds": ttl}

    def check_session(self, token, user_id=None):
        """Validate an elevation token, refreshing its idle window."""
        if not token:
            return None
        now = time.time()
        idle = float(config.CONFIG.get("admin", {}).get("idle_seconds") or IDLE_SECONDS)
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM admin_sessions WHERE token=?",
                             (str(token),)).fetchone()
            if not row or row["revoked"]:
                return None
            if now >= row["expires"] or now - row["last_seen"] > idle:
                db.execute("UPDATE admin_sessions SET revoked=1 WHERE token=?",
                           (str(token),))
                return None
            if user_id and row["user_id"] != user_id:
                return None
            # The elevation is re-checked against the account on every request:
            # a demoted or deactivated account cannot keep acting as admin.
            account = db.execute("SELECT role, active FROM users WHERE id=?",
                                 (row["user_id"],)).fetchone()
            if (not account or account["role"] != ROLE_ADMIN
                    or not account["active"]):
                db.execute("UPDATE admin_sessions SET revoked=1 WHERE token=?",
                           (str(token),))
                revoked_now = True
            else:
                revoked_now = False
                db.execute("UPDATE admin_sessions SET last_seen=? WHERE token=?",
                           (now, str(token)))
        if revoked_now:
            self.audit(row["user_id"], "admin_session_revoked",
                       {"reason": "role or account state changed"})
            return None
        return {"user_id": row["user_id"], "token": row["token"],
                "created": row["created"], "expires": row["expires"]}

    def revoke(self, token, actor=None, reason="signed out"):
        with self._lock, self._connect() as db:
            db.execute("UPDATE admin_sessions SET revoked=1 WHERE token=?", (str(token),))
        self.audit(actor, "admin_session_revoked", {"reason": reason})
        return {"ok": True}

    def revoke_user_sessions(self, user_id, actor=None, reason="sessions revoked"):
        """Revoke every live elevation belonging to one account."""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT token FROM admin_sessions WHERE user_id=? "
                              "AND revoked=0", (str(user_id),)).fetchall()
            for row in rows:
                db.execute("UPDATE admin_sessions SET revoked=1 WHERE token=?",
                           (row["token"],))
        if rows:
            self.audit(actor or user_id, "admin_sessions_revoked",
                       {"user": str(user_id), "count": len(rows), "reason": reason})
        return len(rows)

    def sessions(self, user_id=None):
        now = time.time()
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT token, user_id, created, last_seen, expires, "
                              "ip, user_agent, revoked FROM admin_sessions "
                              "ORDER BY last_seen DESC LIMIT 50").fetchall()
        out = []
        for row in rows:
            if user_id and row["user_id"] != user_id:
                continue
            out.append({
                "id": hashlib.sha256(row["token"].encode()).hexdigest()[:12],
                "user_id": row["user_id"], "created": row["created"],
                "last_seen": row["last_seen"], "expires": row["expires"],
                "ip": row["ip"], "agent": row["user_agent"],
                "active": bool(not row["revoked"] and row["expires"] > now),
            })
        return out

    def revoke_by_id(self, session_id, actor=None):
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT token FROM admin_sessions").fetchall()
            for row in rows:
                if hashlib.sha256(row["token"].encode()).hexdigest()[:12] == session_id:
                    db.execute("UPDATE admin_sessions SET revoked=1 WHERE token=?",
                               (row["token"],))
                    self.audit(actor, "admin_session_revoked", {"id": session_id})
                    return {"ok": True}
        return {"ok": False, "error": "session not found"}

    # -- approvals ---------------------------------------------------------

    def request_approval(self, action, payload=None, risk=HIGH, requested_by=None, note=""):
        approval_id = "ap_" + uuid.uuid4().hex[:12]
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute("""INSERT INTO approvals
                (id, action, risk, payload, status, created, requested_by, expires, note)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                       (approval_id, str(action)[:120], risk,
                        json.dumps(payload or {}, ensure_ascii=False)[:20000],
                        "pending", now, requested_by or "", now + APPROVAL_TTL,
                        str(note or "")[:500]))
        self.audit(requested_by, "approval_requested",
                   {"id": approval_id, "action": action, "risk": risk})
        return {"ok": True, "id": approval_id, "status": "pending"}

    def decide_approval(self, approval_id, approve, decided_by=None, note=""):
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?",
                             (str(approval_id),)).fetchone()
            if not row:
                return {"ok": False, "error": "unknown approval"}
            if row["status"] != "pending":
                return {"ok": False, "error": f"approval already {row['status']}"}
            if row["expires"] < now:
                db.execute("UPDATE approvals SET status='expired' WHERE id=?",
                           (str(approval_id),))
                return {"ok": False, "error": "approval expired"}
            status = "approved" if approve else "denied"
            db.execute("UPDATE approvals SET status=?, decided_by=?, decided_at=?, note=? "
                       "WHERE id=?", (status, decided_by or "", now,
                                      str(note or "")[:500], str(approval_id)))
        self.audit(decided_by, f"approval_{status}", {"id": approval_id, "action": row["action"]})
        return {"ok": True, "status": status, "action": row["action"],
                "payload": json.loads(row["payload"] or "{}")}

    def approvals(self, status=None, limit=50):
        with self._lock, self._connect() as db:
            if status:
                rows = db.execute("SELECT * FROM approvals WHERE status=? "
                                  "ORDER BY created DESC LIMIT ?",
                                  (status, int(limit))).fetchall()
            else:
                rows = db.execute("SELECT * FROM approvals ORDER BY created DESC LIMIT ?",
                                  (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["payload"] = json.loads(item.get("payload") or "{}")
            except ValueError:
                item["payload"] = {}
            out.append(item)
        return out

    def approved(self, approval_id, action):
        """True when an approval exists, is approved, unexpired and for action."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM approvals WHERE id=?",
                             (str(approval_id or ""),)).fetchone()
        return bool(row and row["status"] == "approved" and row["expires"] > time.time()
                    and row["action"] == action)

    def find_approved(self, action, match=None):
        """Latest approved, unexpired, unused approval whose payload matches.

        This lets a two-step flow work from the panel: request the change (a
        pending approval appears), approve it, then repeat the same request and
        it applies — without inventing a second approval.
        """
        match = match or {}
        now = time.time()
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM approvals WHERE action=? AND status='approved' "
                              "AND expires>? AND used_at IS NULL ORDER BY decided_at DESC",
                              (str(action), now)).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload"] or "{}")
            except ValueError:
                payload = {}
            if all(str(payload.get(k)) == str(v) for k, v in match.items()):
                return dict(row)
        return None

    def consume_approval(self, approval_id, actor=None):
        with self._lock, self._connect() as db:
            db.execute("UPDATE approvals SET used_at=? WHERE id=? AND used_at IS NULL",
                       (time.time(), str(approval_id)))
        self.audit(actor, "approval_consumed", {"id": approval_id})
        return {"ok": True}

    # -- audit -------------------------------------------------------------

    def audit(self, user_id, action, detail=None, ip=""):
        record = {"ts": time.time(), "user_id": user_id or "",
                  "action": str(action)[:120], "detail": detail or {}, "ip": ip or ""}
        try:
            with self._lock, self._connect() as db:
                db.execute("INSERT INTO admin_audit (ts, user_id, action, detail, ip) "
                           "VALUES (?,?,?,?,?)",
                           (record["ts"], record["user_id"], record["action"],
                            json.dumps(record["detail"], ensure_ascii=False)[:4000],
                            record["ip"][:60]))
        except Exception as e:
            log.warning("audit insert failed: %s", e)
        try:
            from . import logging_setup
            logging_setup.audit("admin_" + record["action"],
                                json.dumps(record["detail"], ensure_ascii=False)[:300],
                                user=record["user_id"], ip=record["ip"])
        except Exception:
            pass

    def audit_entries(self, limit=100):
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?",
                              (int(limit),)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except ValueError:
                item["detail"] = {}
            out.append(item)
        return out


# ---------------------------------------------------------------- TOTP (RFC 6238)

def verify_totp(secret, code, window=1, step=30, digits=6):
    """Validate a 6-digit TOTP code without any third-party dependency."""
    secret = str(secret or "").strip().upper()
    code = str(code or "").strip().replace(" ", "")
    if not secret or not code.isdigit() or len(code) != digits:
        return False
    padded = secret + "=" * (-len(secret) % 8)
    try:
        key = base64.b32decode(padded, casefold=True)
    except Exception:
        return False
    counter = int(time.time()) // step
    for offset in range(-window, window + 1):
        message = struct.pack(">Q", counter + offset)
        digest = hmac.new(key, message, hashlib.sha1).digest()
        position = digest[-1] & 0x0F
        value = (struct.unpack(">I", digest[position:position + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
        if hmac.compare_digest(f"{value:0{digits}d}", code):
            return True
    return False


_store = None
_lock = threading.Lock()


def get_admin_store():
    global _store
    with _lock:
        if _store is None:
            _store = AdminStore()
        return _store


# ---------------------------------------------------------------- request context

import contextvars  # noqa: E402

_admin_ctx = contextvars.ContextVar("harry_admin", default=None)


def set_request_admin(info):
    """Called by the web layer after verifying the elevation token."""
    return _admin_ctx.set(info)


def request_admin():
    """Who is acting, if this request was authenticated as an administrator."""
    return _admin_ctx.get()


def reset_request_admin(token):
    try:
        _admin_ctx.reset(token)
    except (ValueError, RuntimeError):
        pass


class AdminRequired(Exception):
    pass


def require_admin(action="this action"):
    info = request_admin()
    if not info:
        raise AdminRequired(f"administrator mode is required for {action}")
    return info

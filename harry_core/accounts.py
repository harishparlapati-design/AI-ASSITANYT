"""Accounts for a hosted Harry.

Deliberately small and dependency-free:

* passwords hashed with scrypt (per-user salt, constant-time compare)
* one SQLite file for users and login throttling
* accounts are created by the operator (CLI or signup token), never by an
  unauthenticated visitor
* repeated failures lock a username/IP pair for a short window

This store holds identity only. Conversations and files are scoped by tenant
id in harry_core/tenant.py.
"""

import hashlib
import hmac
import logging
import os
import re
import sqlite3
import threading
import time
import uuid

log = logging.getLogger("harry.accounts")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 10
LOCK_WINDOW = 600.0          # seconds a lockout lasts
LOCK_AFTER = 5               # failures before a lockout
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 14, 8, 1


def _db_path():
    base = os.environ.get("HARRY_ACCOUNTS_DB", "").strip()
    if base:
        return os.path.abspath(base)
    from . import runtime_paths
    return os.path.join(runtime_paths.workspace_root(), "data", "accounts.db")


class AccountStore:
    def __init__(self, path=None):
        self.path = path or _db_path()
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._init()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _init(self):
        with self._lock, self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL,
                name TEXT, salt TEXT NOT NULL, hash TEXT NOT NULL,
                created REAL NOT NULL, active INTEGER NOT NULL DEFAULT 1)""")
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                key TEXT PRIMARY KEY, failures INTEGER NOT NULL,
                first REAL NOT NULL, last REAL NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS users_email ON users(email)")

    # -- hashing -----------------------------------------------------------

    def _hash(self, password, salt_hex):
        digest = hashlib.scrypt(str(password).encode("utf-8"),
                                salt=bytes.fromhex(salt_hex),
                                n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
        return digest.hex()

    # -- users -------------------------------------------------------------

    def create(self, email, password, name=""):
        email = str(email or "").strip().lower()
        if not EMAIL_RE.match(email):
            return {"ok": False, "error": "a valid email address is required"}
        if len(str(password or "")) < MIN_PASSWORD:
            return {"ok": False, "error": f"the password needs at least {MIN_PASSWORD} characters"}
        salt = os.urandom(16).hex()
        user_id = "u_" + uuid.uuid4().hex[:12]
        try:
            with self._lock, self._connect() as db:
                db.execute("INSERT INTO users (id, email, name, salt, hash, created) "
                           "VALUES (?,?,?,?,?,?)",
                           (user_id, email, str(name or "").strip()[:80],
                            salt, self._hash(password, salt), time.time()))
        except sqlite3.IntegrityError:
            return {"ok": False, "error": "that email is already registered"}
        log.info("account created: %s", user_id)
        return {"ok": True, "id": user_id, "email": email}

    def get(self, user_id):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (str(user_id),)).fetchone()
        return dict(row) if row else None

    def by_email(self, email):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE email=?",
                             (str(email or "").strip().lower(),)).fetchone()
        return dict(row) if row else None

    def count(self):
        with self._lock, self._connect() as db:
            return db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def set_password(self, user_id, password):
        if len(str(password or "")) < MIN_PASSWORD:
            return {"ok": False, "error": f"the password needs at least {MIN_PASSWORD} characters"}
        salt = os.urandom(16).hex()
        with self._lock, self._connect() as db:
            db.execute("UPDATE users SET salt=?, hash=? WHERE id=?",
                       (salt, self._hash(password, salt), str(user_id)))
        return {"ok": True}

    def delete(self, user_id):
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM users WHERE id=?", (str(user_id),))
        return {"ok": True}

    # -- login -------------------------------------------------------------

    def locked(self, key):
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE key=?", (str(key),)).fetchone()
        if not row:
            return 0
        if now - row["last"] > LOCK_WINDOW:
            return 0
        if row["failures"] >= LOCK_AFTER:
            return round(LOCK_WINDOW - (now - row["last"]), 0)
        return 0

    def _record_failure(self, key):
        now = time.time()
        with self._lock, self._connect() as db:
            row = db.execute("SELECT * FROM attempts WHERE key=?", (str(key),)).fetchone()
            if row and now - row["last"] <= LOCK_WINDOW:
                db.execute("UPDATE attempts SET failures=failures+1, last=? WHERE key=?",
                           (now, str(key)))
            else:
                db.execute("INSERT OR REPLACE INTO attempts (key, failures, first, last) "
                           "VALUES (?,?,?,?)", (str(key), 1, now, now))

    def _clear_failures(self, key):
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM attempts WHERE key=?", (str(key),))

    def verify(self, email, password, ip=""):
        key = f"{str(email or '').strip().lower()}|{ip or '-'}"
        wait = self.locked(key)
        if wait:
            return {"ok": False, "error": f"too many attempts; try again in {int(wait)} seconds",
                    "locked_for": wait}
        user = self.by_email(email)
        # Hash even for unknown users so a missing account is not faster to test.
        salt = user["salt"] if user else "00" * 16
        candidate = self._hash(password, salt)
        if not user or not hmac.compare_digest(candidate, user["hash"]) or not user["active"]:
            self._record_failure(key)
            return {"ok": False, "error": "email or password is incorrect"}
        self._clear_failures(key)
        return {"ok": True, "id": user["id"], "email": user["email"],
                "name": user.get("name") or ""}


_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            _store = AccountStore()
        return _store


def create_user_cli(email, password, name=""):
    return get_store().create(email, password, name)

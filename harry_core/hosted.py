"""Hosted mode: login, sessions, CSRF, security headers, tenant scope.

Enabled with ``HARRY_HOSTED=1``. The desktop app is unchanged when it is off:
no login page, no cookie requirement, local files stay where they are.

Design notes
------------
* Sessions are signed cookies (Flask/itsdangerous). ``HARRY_SESSION_SECRET``
  must be set in production; without it a random per-boot secret is used and
  every restart signs users out (safe, just inconvenient).
* Every unsafe request needs an ``X-CSRF-Token`` header matching the token in
  the signed session. The dashboard gets it from ``/api/session``.
* A request belongs to one tenant (the user id). Documents, media and job
  history are stored under that tenant's folder.
* ``main``-style administrative endpoints do not exist here. The GitHub,
  terminal and agent panels are local-development features and are refused in
  hosted mode.
"""

import logging
import os
import secrets

from . import tenant

log = logging.getLogger("harry.hosted")

PUBLIC_PATHS = ("/login", "/healthz", "/manifest.webmanifest", "/sw.js",
                "/favicon.ico", "/icons/")
LOCAL_ONLY_PREFIXES = ("/api/github", "/api/agents")
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

LOGIN_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Sign in · Harry</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<style>
  :root { color-scheme: dark; }
  body { margin:0;min-height:100vh;display:grid;place-items:center;
    font:15px/1.55 Inter,"Segoe UI",system-ui,sans-serif;color:#e9effb;
    background:radial-gradient(ellipse at 20% 0%,rgba(94,126,226,.20),transparent 42%),#09111e; }
  form { width:min(360px,calc(100vw - 40px));padding:26px;border-radius:18px;
    border:1px solid rgba(154,180,218,.18);background:rgba(17,27,44,.88);
    box-shadow:0 20px 60px rgba(0,0,0,.35); }
  h1 { margin:0 0 4px;font-size:17px;letter-spacing:.16em; }
  p.sub { margin:0 0 18px;color:#98a8c2;font-size:12px; }
  label { display:block;margin:12px 0 5px;font-size:11px;color:#98a8c2;letter-spacing:.06em;text-transform:uppercase; }
  input { width:100%;box-sizing:border-box;padding:11px 12px;border-radius:11px;
    border:1px solid rgba(154,180,218,.22);background:#07101d;color:inherit;font:inherit; }
  button { width:100%;margin-top:18px;padding:11px;border-radius:11px;border:0;
    background:#6384f3;color:#fff;font:inherit;font-weight:650;cursor:pointer; }
  .err { margin:14px 0 0;padding:9px 11px;border-radius:9px;font-size:12px;
    background:rgba(204,64,87,.14);color:#ff7887;border:1px solid rgba(204,64,87,.3); }
  .note { margin-top:14px;color:#697b98;font-size:11px; }
</style></head>
<body>
<form method="post" action="/login">
  <h1>HARRY</h1>
  <p class="sub">Sign in to your personal assistant.</p>
  <input type="hidden" name="csrf" value="__CSRF__">
  <label for="email">Email</label>
  <input id="email" name="email" type="email" autocomplete="username" required autofocus>
  <label for="password">Password</label>
  <input id="password" name="password" type="password" autocomplete="current-password" required>
  __ERROR__
  <button type="submit">Sign in</button>
  <p class="note">Accounts are created by the operator of this Harry instance.</p>
</form>
</body></html>"""


def enabled():
    return str(os.environ.get("HARRY_HOSTED", "")).strip().lower() in ("1", "true", "yes", "on")


def _secret():
    value = os.environ.get("HARRY_SESSION_SECRET", "")
    if value and len(value) >= 32:
        return value
    if value:
        log.warning("HARRY_SESSION_SECRET is shorter than 32 characters; using it anyway")
        return value
    log.warning("HARRY_SESSION_SECRET is not set: sessions will not survive a restart")
    return secrets.token_hex(32)


def _login_page(csrf, error=""):
    block = f'<p class="err">{error}</p>' if error else ""
    return (LOGIN_HTML.replace("__CSRF__", csrf).replace("__ERROR__", block))


def _csrf(request):
    from flask import session
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(24)
        session["csrf"] = token
    return token


def install(app):
    """Attach login, session gating, CSRF and security headers to a Flask app."""
    from flask import g, jsonify, request, session

    app.secret_key = _secret()
    secure_cookie = str(os.environ.get("HARRY_HTTPS", "")).lower() in ("1", "true", "yes")
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookie,
        PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )

    def _public(path):
        return any(path == p or path.startswith(p) for p in PUBLIC_PATHS)

    @app.before_request
    def _gate():
        path = request.path
        if _public(path):
            return None
        user_id = session.get("uid")
        if not user_id:
            if path.startswith("/api/") or request.method not in SAFE_METHODS:
                return jsonify({"error": "sign in required", "login": "/login"}), 401
            return _redirect("/login")
        from .accounts import get_store
        user = get_store().get(user_id)
        if not user or not user.get("active"):
            session.clear()
            if path.startswith("/api/"):
                return jsonify({"error": "session is no longer valid"}), 401
            return _redirect("/login")
        g.harry_user = user_id
        g.harry_email = user["email"]
        tenant.set_tenant(user_id)

        if any(path.startswith(prefix) for prefix in LOCAL_ONLY_PREFIXES):
            return jsonify({"error": "this development panel is disabled on hosted Harry"}), 403

        csrf = session.get("csrf") or ""
        if request.method not in SAFE_METHODS:
            supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
            if not csrf or not secrets.compare_digest(str(supplied), str(csrf)):
                return jsonify({"error": "invalid or missing CSRF token"}), 403
        return None

    @app.teardown_request
    def _reset_tenant(exc=None):
        tenant.set_tenant(tenant.LOCAL)
        return None

    @app.after_request
    def _headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), geolocation=(), microphone=(self), display-capture=(self)")
        if not response.headers.get("Content-Security-Policy"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
                "media-src 'self' blob: data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if str(os.environ.get("HARRY_HTTPS", "")).lower() in ("1", "true", "yes"):
            response.headers.setdefault("Strict-Transport-Security",
                                        "max-age=31536000; includeSubDomains")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    @app.get("/healthz")
    def healthz():
        from .accounts import get_store
        try:
            accounts = get_store().count()
        except Exception:
            accounts = -1
        return jsonify({"ok": True, "accounts": accounts})

    @app.get("/login")
    def login_form():
        from flask import redirect
        if session.get("uid"):
            return redirect("/")
        return _login_page(_csrf(request))

    @app.post("/login")
    def login_submit():
        from flask import redirect
        from .accounts import get_store
        supplied = request.form.get("csrf", "")
        expected = session.get("csrf") or ""
        if not expected or not secrets.compare_digest(str(supplied), str(expected)):
            return _login_page(_csrf(request), "Your session expired. Try again."), 400
        email = request.form.get("email", "")
        password = request.form.get("password", "")
        result = get_store().verify(email, password, ip=request.remote_addr or "")
        if not result.get("ok"):
            return _login_page(_csrf(request), result.get("error", "sign-in failed")), 401
        session.clear()
        session["uid"] = result["id"]
        session["csrf"] = secrets.token_urlsafe(24)
        session.permanent = True
        log.info("sign-in: %s", result["id"])
        return redirect("/")

    @app.post("/logout")
    def logout():
        session.clear()
        return jsonify({"ok": True})

    @app.get("/api/session")
    def session_info():
        return jsonify({"authenticated": True, "csrf": _csrf(request),
                        "email": g.get("harry_email", "")})


def _redirect(location):
    from flask import redirect
    return redirect(location, code=302)


def create_user_cli(argv=None):
    """python -m harry_core.hosted --create-user EMAIL [--name NAME]"""
    import argparse
    import getpass
    parser = argparse.ArgumentParser(description="Manage hosted Harry accounts")
    parser.add_argument("--create-user", metavar="EMAIL")
    parser.add_argument("--name", default="")
    parser.add_argument("--password", help="omit to be prompted (recommended)")
    parser.add_argument("--reset-password", metavar="EMAIL")
    parser.add_argument("--delete-user", metavar="EMAIL")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    from .accounts import get_store
    store = get_store()
    if args.list:
        with store._connect() as db:
            rows = db.execute("SELECT id, email, name, created FROM users").fetchall()
        for row in rows:
            print(f"{row['id']}  {row['email']}  {row['name'] or '-'}")
        return 0
    if args.create_user:
        password = args.password or getpass.getpass("Password: ")
        result = store.create(args.create_user, password, args.name)
        print(result.get("error") if not result.get("ok") else f"created {result['id']}")
        return 0 if result.get("ok") else 1
    if args.reset_password:
        user = store.by_email(args.reset_password)
        if not user:
            print("no such account")
            return 1
        password = args.password or getpass.getpass("New password: ")
        result = store.set_password(user["id"], password)
        print("password updated" if result.get("ok") else result.get("error"))
        return 0 if result.get("ok") else 1
    if args.delete_user:
        user = store.by_email(args.delete_user)
        if not user:
            print("no such account")
            return 1
        store.delete(user["id"])
        print("deleted")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(create_user_cli())

"""Hosted mode tests: login, CSRF, tenant isolation and security headers.

Run:  .venv/Scripts/python.exe tests/auth_test.py

These drive the real Flask app in hosted mode. They do not prove a public
deployment exists - only that the application enforces its own rules.
"""

import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="harry-hosted-")
os.environ.update({
    "HARRY_HOSTED": "1",
    "HARRY_SESSION_SECRET": "t" * 64,
    "HARRY_ACCOUNTS_DB": os.path.join(_tmp, "accounts.db"),
    "HARRY_DOCUMENTS_DIR": os.path.join(_tmp, "documents"),
    "HARRY_MEDIA_DIR": os.path.join(_tmp, "media"),
    "HARRY_HTTPS": "0",
})

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} {detail}")


def make_client():
    from harry_core.ui import create_app
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def csrf_from_login(client):
    html = client.get("/login").get_data(as_text=True)
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    return match.group(1) if match else ""


def login(client, email, password):
    token = csrf_from_login(client)
    return client.post("/login", data={"email": email, "password": password, "csrf": token})


def test_accounts_store():
    from harry_core.accounts import AccountStore
    store = AccountStore(os.path.join(_tmp, "unit.db"))
    weak = store.create("a@example.com", "short")
    check("short password refused", weak.get("ok") is False, weak)
    bad_email = store.create("not-an-email", "correct horse battery")
    check("invalid email refused", bad_email.get("ok") is False, bad_email)
    created = store.create("owner@example.com", "correct horse battery", "Owner")
    check("account created", created.get("ok") is True, created)
    duplicate = store.create("owner@example.com", "another long password")
    check("duplicate email refused", duplicate.get("ok") is False, duplicate)
    good = store.verify("owner@example.com", "correct horse battery")
    check("correct password verifies", good.get("ok") is True, good)
    wrong = store.verify("owner@example.com", "wrong password entirely")
    check("wrong password refused", wrong.get("ok") is False, wrong)
    with store._connect() as db:
        row = db.execute("SELECT hash, salt FROM users WHERE email=?",
                         ("owner@example.com",)).fetchone()
    check("password is not stored in clear", "correct horse battery" not in row["hash"])
    check("password hash is scrypt-sized", len(row["hash"]) == 64, len(row["hash"]))


def test_lockout():
    from harry_core.accounts import AccountStore
    store = AccountStore(os.path.join(_tmp, "lock.db"))
    store.create("locked@example.com", "a long enough password")
    results = [store.verify("locked@example.com", "bad password", ip="1.2.3.4")
               for _ in range(6)]
    check("failures are refused", all(r.get("ok") is False for r in results))
    check("lockout triggers after repeated failures",
          any("too many attempts" in str(r.get("error")) for r in results),
          [r.get("error") for r in results])
    blocked = store.verify("locked@example.com", "a long enough password", ip="1.2.3.4")
    check("lockout blocks even a correct password", blocked.get("ok") is False, blocked)
    other_ip = store.verify("locked@example.com", "a long enough password", ip="5.6.7.8")
    check("lockout is scoped to the client", other_ip.get("ok") is True, other_ip)


def test_gate():
    from harry_core.accounts import get_store
    store = get_store()
    store.create("first@example.com", "first owner password", "First")
    store.create("second@example.com", "second owner password", "Second")

    client = make_client()
    root = client.get("/")
    check("anonymous page redirects to login", root.status_code == 302
          and "/login" in root.headers.get("Location", ""), root.status_code)
    api = client.get("/api/state")
    check("anonymous API is 401", api.status_code == 401, api.status_code)
    check("401 is JSON", api.is_json and api.get_json().get("error"), api.get_data(as_text=True)[:80])
    check("health endpoint is public", client.get("/healthz").status_code == 200)
    check("manifest is public", client.get("/manifest.webmanifest").status_code == 200)
    check("icons are public", client.get("/icons/192.png").status_code == 200)

    wrong = login(client, "first@example.com", "not the password")
    check("wrong password rejected", wrong.status_code == 401, wrong.status_code)
    check("failed login shows an error", "incorrect" in wrong.get_data(as_text=True).lower())

    ok = login(client, "first@example.com", "first owner password")
    check("correct login redirects", ok.status_code == 302, ok.status_code)
    cookie = ok.headers.get("Set-Cookie", "")
    check("session cookie is HttpOnly", "HttpOnly" in cookie, cookie)
    check("session cookie is SameSite", "SameSite=Lax" in cookie, cookie)

    session = client.get("/api/session")
    check("session endpoint works after login", session.status_code == 200, session.status_code)
    token = session.get_json().get("csrf")
    check("session exposes a CSRF token", bool(token), session.get_json())

    headers = client.get("/").headers
    check("CSP present", "default-src 'self'" in headers.get("Content-Security-Policy", ""),
          headers.get("Content-Security-Policy"))
    check("frame embedding denied", headers.get("X-Frame-Options") == "DENY")
    check("no sniff header", headers.get("X-Content-Type-Options") == "nosniff")
    check("referrer policy", headers.get("Referrer-Policy") == "no-referrer")
    check("permissions policy restricts camera",
          "camera=()" in headers.get("Permissions-Policy", ""))
    check("API responses are not cached", "no-store" in client.get("/api/state").headers.get(
        "Cache-Control", ""))
    return client, token


def test_csrf_and_isolation(client, token):
    missing = client.post("/api/documents/generate",
                          json={"format": "txt", "content": "hello"})
    check("POST without CSRF is refused", missing.status_code == 403, missing.status_code)
    wrong = client.post("/api/documents/generate", json={"format": "txt", "content": "hello"},
                        headers={"X-CSRF-Token": "wrong-token"})
    check("POST with a bad CSRF token is refused", wrong.status_code == 403, wrong.status_code)

    started = client.post("/api/documents/generate",
                          json={"format": "txt", "title": "Private note",
                                "content": "# Private note\n\nOnly the first owner sees this."},
                          headers={"X-CSRF-Token": token})
    check("POST with CSRF accepted", started.status_code == 202, started.status_code)
    job_id = started.get_json()["job"]["id"]
    state = {}
    for _ in range(120):
        state = client.get(f"/api/jobs/{job_id}").get_json()
        if state.get("state") in ("done", "error", "cancelled"):
            break
        time.sleep(0.1)
    check("hosted document generated", state.get("state") == "done", state)
    doc_id = (state.get("result") or {}).get("id")
    from harry_core.accounts import get_store
    first_id = get_store().by_email("first@example.com")["id"]
    tenant_dir = os.path.join(_tmp, "documents", first_id)
    stored = [name for name in os.listdir(tenant_dir)] if os.path.isdir(tenant_dir) else []
    check("document stored under the owner's tenant folder",
          any(name.endswith(".txt") for name in stored), stored)
    check("owner's folder is named after their account",
          os.path.basename(tenant_dir) == first_id, tenant_dir)

    listing = client.get("/api/documents").get_json().get("documents", [])
    check("owner sees their document", any(d["id"] == doc_id for d in listing), listing)

    second_id = get_store().by_email("second@example.com")["id"]
    second = make_client()
    second_login = login(second, "second@example.com", "second owner password")
    check("second account signs in", second_login.status_code == 302, second_login.status_code)
    other_listing = second.get("/api/documents").get_json().get("documents", [])
    check("second account sees no other documents", not other_listing, other_listing)
    stolen = second.get(f"/api/documents/{doc_id}/download")
    check("second account cannot download another file", stolen.status_code == 404,
          stolen.status_code)
    second_dir = os.path.join(_tmp, "documents", second_id)
    check("tenants own different folders", second_dir != tenant_dir)
    check("the second account's folder is empty or absent",
          not os.path.isdir(second_dir) or not os.listdir(second_dir), second_dir)

    blocked = client.get("/api/github/activity")
    check("development panels are disabled on hosted Harry", blocked.status_code == 403,
          blocked.status_code)


def test_logout(client, token):
    out = client.post("/logout", headers={"X-CSRF-Token": token})
    check("logout accepted", out.status_code == 200, out.status_code)
    check("session cleared", client.get("/api/state").status_code == 401,
          client.get("/api/state").status_code)


def main():
    test_accounts_store()
    test_lockout()
    client, token = test_gate()
    test_csrf_and_isolation(client, token)
    test_logout(client, token)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

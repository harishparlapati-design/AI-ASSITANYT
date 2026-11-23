"""First-run administrator bootstrap tests (local desktop mode).

Run:  .venv/Scripts/python.exe tests/bootstrap_test.py

The bootstrap exists so a fresh local install can create its first account
without editing source or hard-coding a password. It must be impossible to use
once an account exists, from a non-loopback client, from a cross-site page, with
a spoofed Host, or through a proxy that supplies forwarded headers.
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="harry-bootstrap-")

BASE_ENV = {
    "HARRY_HOSTED": "0",
    "HARRY_PLUGINS_DIR": os.path.join(_tmp, "plugins"),
    "HARRY_UPGRADES_DIR": os.path.join(_tmp, "upgrades"),
    "HARRY_CAPABILITIES_JSON": os.path.join(_tmp, "capabilities.json"),
    "HARRY_SELFHEAL_JSON": os.path.join(_tmp, "selfheal.json"),
    "HARRY_ALERTS_JSON": os.path.join(_tmp, "alerts.json"),
}
os.environ.update(BASE_ENV)

PASS, FAIL = 0, 0
PASSWORD = "a long enough password"


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} :: {detail}")


def reset_db(tag):
    """Point both stores at a fresh database before a section."""
    from harry_core import accounts, admin, selfheal
    path = os.path.join(_tmp, f"accounts-{tag}.db")
    if os.path.exists(path):
        os.remove(path)
    os.environ["HARRY_ACCOUNTS_DB"] = path
    accounts._store = None
    admin._store = None
    selfheal._history = []
    return path


def client():
    from harry_core.ui import create_app
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def post_bootstrap(c, env=None, **body):
    payload = {"email": "me@example.com", "password": PASSWORD}
    payload.update(body)
    return c.post("/api/admin/bootstrap", json=payload,
                  environ_overrides=env or {})


if __name__ == "__main__":
    from harry_core.accounts import get_store
    from harry_core import admin

    # ---------------------------------------------------------------- basics
    reset_db("basic")
    c = client()
    state = c.get("/api/admin/session").get_json()
    check("fresh install reports needs_setup", state.get("needs_setup") is True, state)
    check("nothing is elevated before setup", state.get("admin") is False)

    weak = post_bootstrap(c, password="short")
    check("short password refused", weak.status_code == 400, weak.get_json())
    bad_email = post_bootstrap(c, email="nope")
    check("invalid email refused", bad_email.status_code == 400, bad_email.get_json())
    check("failed attempts create nothing", get_store().count() == 0)

    # ----------------------------------------------------------- origin / host
    external = {"REMOTE_ADDR": "203.0.113.9"}
    r = post_bootstrap(c, env=external)
    check("external client refused", r.status_code == 403, r.get_json())
    check("external attempt creates nothing", get_store().count() == 0)

    r = post_bootstrap(c, env={"REMOTE_ADDR": "203.0.113.9",
                              "HTTP_X_FORWARDED_FOR": "127.0.0.1"})
    check("spoofed X-Forwarded-For cannot bypass", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"REMOTE_ADDR": "203.0.113.9",
                              "HTTP_X_REAL_IP": "127.0.0.1"})
    check("spoofed X-Real-IP cannot bypass", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"HTTP_FORWARDED": "for=127.0.0.1"})
    check("Forwarded header refused even from loopback", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"HTTP_X_FORWARDED_HOST": "127.0.0.1"})
    check("X-Forwarded-Host refused", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"HTTP_HOST": "evil.example"})
    check("invalid Host header refused", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"HTTP_HOST": "127.0.0.1.evil.example"})
    check("loopback prefix in Host does not count", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"HTTP_ORIGIN": "https://evil.example"})
    check("cross-site Origin refused", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"HTTP_ORIGIN": "http://localhost:9999"})
    check("Origin with a different port refused", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"HTTP_SEC_FETCH_SITE": "cross-site"})
    check("cross-site fetch metadata refused", r.status_code == 403, r.get_json())

    r = post_bootstrap(c, env={"HTTP_SEC_FETCH_SITE": "same-site"})
    check("same-site fetch metadata refused", r.status_code == 403, r.get_json())

    check("rejected attempts still created nothing", get_store().count() == 0)

    # --------------------------------------------------------- legitimate use
    legit = post_bootstrap(c, env={"HTTP_HOST": "127.0.0.1:8765",
                                  "HTTP_ORIGIN": "http://127.0.0.1:8765",
                                  "HTTP_SEC_FETCH_SITE": "same-origin"})
    check("legitimate local setup works", legit.status_code == 200, legit.get_json())
    check("first account is administrator", (legit.get_json() or {}).get("role") == "admin")
    check("bootstrap does not elevate",
          c.get("/api/admin/session").get_json().get("admin") is False)
    check("bootstrap cannot run twice",
          post_bootstrap(c, email="second@example.com").status_code == 409)
    check("only one account exists", get_store().count() == 1)

    elevated = c.post("/api/admin/enter", json={"password": PASSWORD})
    check("bootstrap account can elevate", elevated.status_code == 200, elevated.get_json())
    c.post("/api/admin/exit", json={})

    actions = [e.get("action") for e in admin.get_admin_store().audit_entries(50)]
    check("bootstrap is audited", "admin_bootstrapped" in actions, actions[:8])
    check("refused bootstrap is audited", "bootstrap_refused" in actions, actions[:8])

    # ------------------------------------------------------------ concurrency
    reset_db("race")
    barrier = threading.Barrier(8)
    results, errors = [], []

    def attempt(index):
        try:
            local = client()
            barrier.wait(timeout=10)
            response = post_bootstrap(local, email=f"race{index}@example.com")
            results.append(response.status_code)
        except Exception as exc:  # surfaced below, never hidden
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    accounts_now = get_store().count()
    check("concurrent attempts produced no errors", not errors, errors)
    check("exactly one concurrent bootstrap succeeded",
          results.count(200) == 1, results)
    check("the rest were refused as already set up",
          results.count(409) == len(results) - results.count(200), results)
    check("concurrent bootstrap created exactly one account", accounts_now == 1, accounts_now)

    with get_store()._connect() as db:
        roles = [row["role"] for row in db.execute("SELECT role FROM users").fetchall()]
    check("that account is the only administrator", roles == ["admin"], roles)

    print(f"\n===== RESULT: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)

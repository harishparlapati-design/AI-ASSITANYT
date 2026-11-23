"""First-run administrator bootstrap tests (local desktop mode).

Run:  .venv/Scripts/python.exe tests/bootstrap_test.py

The bootstrap exists so a fresh local install can create its first account
without editing source or hard-coding a password. It must be impossible to use
once an account exists, from a non-loopback client, or on hosted Harry.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="harry-bootstrap-")
os.environ.update({
    "HARRY_HOSTED": "0",
    "HARRY_ACCOUNTS_DB": os.path.join(_tmp, "accounts.db"),
    "HARRY_PLUGINS_DIR": os.path.join(_tmp, "plugins"),
    "HARRY_UPGRADES_DIR": os.path.join(_tmp, "upgrades"),
    "HARRY_CAPABILITIES_JSON": os.path.join(_tmp, "capabilities.json"),
    "HARRY_SELFHEAL_JSON": os.path.join(_tmp, "selfheal.json"),
    "HARRY_ALERTS_JSON": os.path.join(_tmp, "alerts.json"),
})

PASS, FAIL = 0, 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} :: {detail}")


def client():
    from harry_core.ui import create_app
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


if __name__ == "__main__":
    from harry_core.accounts import get_store
    from harry_core import admin

    c = client()
    state = c.get("/api/admin/session").get_json()
    check("fresh install reports needs_setup", state.get("needs_setup") is True, state)
    check("nothing is elevated before setup", state.get("admin") is False)

    weak = c.post("/api/admin/bootstrap", json={"email": "me@example.com", "password": "short"})
    check("short password refused", weak.status_code == 400, weak.get_json())
    check("no account created by the failed attempt", get_store().count() == 0)

    bad_email = c.post("/api/admin/bootstrap",
                       json={"email": "nope", "password": "a long enough password"})
    check("invalid email refused", bad_email.status_code == 400, bad_email.get_json())

    created = c.post("/api/admin/bootstrap",
                     json={"email": "me@example.com", "password": "a long enough password"})
    check("first administrator created", created.status_code == 200, created.get_json())
    check("bootstrap result is administrator", (created.get_json() or {}).get("role") == "admin")
    check("bootstrap does not elevate", c.get("/api/admin/session").get_json().get("admin") is False)
    check("bootstrap cannot run twice",
          c.post("/api/admin/bootstrap",
                 json={"email": "second@example.com",
                       "password": "another long password"}).status_code == 409)

    elevated = c.post("/api/admin/enter", json={"password": "a long enough password"})
    check("bootstrap account can elevate", elevated.status_code == 200, elevated.get_json())
    c.post("/api/admin/exit", json={})

    # Remote clients cannot claim the first account.
    from harry_core import accounts
    other = tempfile.mkdtemp(prefix="harry-bootstrap-remote-")
    accounts._store = None
    os.environ["HARRY_ACCOUNTS_DB"] = os.path.join(other, "accounts.db")
    admin._store = None
    c2 = client()
    remote = c2.post("/api/admin/bootstrap",
                     json={"email": "attacker@example.com",
                           "password": "a long enough password"},
                     environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    check("non-loopback bootstrap refused", remote.status_code == 403, remote.get_json())
    check("remote attempt creates nothing", get_store().count() == 0)

    print(f"\n===== RESULT: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)

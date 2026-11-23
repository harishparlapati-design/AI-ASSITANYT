"""HTTP regression tests: demotion, users API and errors API.

Run:  .venv/Scripts/python.exe tests/admin_api_test.py

Every check drives a real Flask request through the hosted gate, a real signed
session, a real elevation token and real SQLite rows.
"""

import os
import re
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.mkdtemp(prefix="harry-adminapi-")
os.environ.update({
    "HARRY_HOSTED": "1",
    "HARRY_SESSION_SECRET": "b" * 64,
    "HARRY_ACCOUNTS_DB": os.path.join(_tmp, "accounts.db"),
    "HARRY_DOCUMENTS_DIR": os.path.join(_tmp, "documents"),
    "HARRY_MEDIA_DIR": os.path.join(_tmp, "media"),
    "HARRY_PLUGINS_DIR": os.path.join(_tmp, "plugins"),
    "HARRY_UPGRADES_DIR": os.path.join(_tmp, "upgrades"),
    "HARRY_CAPABILITIES_JSON": os.path.join(_tmp, "capabilities.json"),
    "HARRY_SELFHEAL_JSON": os.path.join(_tmp, "selfheal.json"),
    "HARRY_ALERTS_JSON": os.path.join(_tmp, "alerts.json"),
    "HARRY_HTTPS": "0",
})

PASS, FAIL = 0, 0
ADMIN_PW = "correct horse battery staple"
USER_PW = "another long password 123"


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


def session_csrf(c):
    with c.session_transaction() as s:
        return s.get("csrf") or ""


def login(c, email, password):
    html = c.get("/login").get_data(as_text=True)
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    return c.post("/login", data={"email": email, "password": password,
                                  "csrf": match.group(1) if match else ""})


def elevate(c, password=ADMIN_PW, code=""):
    token = session_csrf(c)
    return c.post("/api/admin/enter", json={"password": password, "code": code},
                  headers={"X-CSRF-Token": token})


def headers(c):
    token = session_csrf(c)
    return {"X-CSRF-Token": token, "X-Admin-CSRF": token}


def json_headers(c):
    return {**headers(c), "Content-Type": "application/json"}


def approve(c, approval_id, value=True):
    return c.post(f"/api/admin/approvals/{approval_id}", json={"approve": value},
                  headers=json_headers(c))


if __name__ == "__main__":
    from harry_core import accounts, admin, selfheal, runtime_paths

    store = accounts.get_store()
    admin_account = store.create("admin@example.com", ADMIN_PW, "Admin")
    user_account = store.create("user@example.com", USER_PW, "User")
    check("first operator account is admin", admin_account.get("role") == "admin",
          admin_account)

    admin_client = client()
    check("admin login", login(admin_client, "admin@example.com", ADMIN_PW).status_code == 302)
    check("admin elevation", elevate(admin_client).status_code == 200)

    # =====================================================================
    # 1. Administrator demotion
    # =====================================================================
    user_client = client()
    login(user_client, "user@example.com", USER_PW)

    promote = admin_client.post(f"/api/admin/users/{user_account['id']}/role",
                                json={"role": "admin"}, headers=json_headers(admin_client))
    check("promotion needs an approval record", promote.status_code == 202, promote.get_json())
    approval = (promote.get_json() or {}).get("approval")
    approve(admin_client, approval)
    again = admin_client.post(f"/api/admin/users/{user_account['id']}/role",
                              json={"role": "admin"}, headers=json_headers(admin_client))
    check("promotion applies after approval", again.status_code == 200, again.get_json())

    check("second admin can elevate", elevate(user_client, USER_PW).status_code == 200)
    before = user_client.get("/api/admin/users")
    check("second admin can use the API", before.status_code == 200, before.get_json())

    demote = admin_client.post(f"/api/admin/users/{user_account['id']}/role",
                               json={"role": "user"}, headers=json_headers(admin_client))
    check("demotion needs an approval record", demote.status_code == 202, demote.get_json())
    approve(admin_client, (demote.get_json() or {}).get("approval"))
    applied = admin_client.post(f"/api/admin/users/{user_account['id']}/role",
                                json={"role": "user"}, headers=json_headers(admin_client))
    check("demotion applies", applied.status_code == 200, applied.get_json())
    check("demotion reports revoked sessions",
          (applied.get_json() or {}).get("sessions_revoked", 0) >= 1, applied.get_json())

    reuse = user_client.get("/api/admin/users")
    check("demoted account cannot reuse its old elevation",
          reuse.status_code == 403, reuse.get_json())

    listed = admin_client.get("/api/admin/sessions").get_json() or {}
    theirs = [s for s in listed.get("items", [])
              if s.get("user_id") == user_account["id"]]
    check("demoted account has no active session left",
          bool(theirs) and not any(s.get("active") for s in theirs), theirs)

    re_elevate = elevate(user_client, USER_PW)
    check("demoted account cannot elevate again", re_elevate.status_code == 403,
          re_elevate.get_json())

    other = admin_client.get("/api/admin/users")
    check("the other administrator is unaffected", other.status_code == 200,
          other.get_json())

    audit_actions = [a.get("action") for a in
                     admin.get_admin_store().audit_entries(80)]
    check("demotion revocation is audited",
          "admin_sessions_revoked" in audit_actions, audit_actions[:10])

    last = admin_client.post(f"/api/admin/users/{admin_account['id']}/role",
                             json={"role": "user"}, headers=json_headers(admin_client))
    approve(admin_client, (last.get_json() or {}).get("approval"))
    last_retry = admin_client.post(f"/api/admin/users/{admin_account['id']}/role",
                                   json={"role": "user"}, headers=json_headers(admin_client))
    check("the last administrator cannot be demoted", last_retry.status_code == 400,
          last_retry.get_json())

    # ------------------------------------------------- hosted bootstrap
    # The local first-run route must not exist as a public surface on hosted
    # Harry, whatever headers a caller sends.
    before_count = store.count()
    hosted_boot = client().post(
        "/api/admin/bootstrap",
        json={"email": "public@example.com", "password": "a public password"},
        environ_overrides={"REMOTE_ADDR": "127.0.0.1", "HTTP_HOST": "127.0.0.1"})
    check("hosted bootstrap is not public", hosted_boot.status_code == 401,
          hosted_boot.get_json())
    signed_boot = user_client.post(
        "/api/admin/bootstrap",
        json={"email": "public@example.com", "password": "a public password"},
        headers=json_headers(user_client))
    check("hosted bootstrap refuses signed-in non-admins",
          signed_boot.status_code == 403, signed_boot.get_json())
    admin_boot = admin_client.post(
        "/api/admin/bootstrap",
        json={"email": "public@example.com", "password": "a public password"},
        headers=json_headers(admin_client))
    check("hosted bootstrap refuses administrators too",
          admin_boot.status_code == 403, admin_boot.get_json())
    check("hosted bootstrap creates no account", store.count() == before_count,
          store.count())

    # =====================================================================
    # 3. Users API
    # =====================================================================
    listing = admin_client.get("/api/admin/users")
    items = (listing.get_json() or {}).get("items", [])
    check("users list succeeds", listing.status_code == 200)
    check("users list contains both accounts", len(items) == 2, items)
    check("users list never exposes password material",
          all(not ({"salt", "hash", "password", "totp_secret"} & set(u)) for u in items),
          items)

    anonymous = client()
    check("unauthenticated users list is refused",
          anonymous.get("/api/admin/users").status_code == 401)

    created = admin_client.post("/api/admin/users",
                                json={"email": "new@example.com",
                                      "password": "a brand new password",
                                      "name": "New"},
                                headers=json_headers(admin_client))
    check("administrator can create a user", created.status_code == 201, created.get_json())
    check("created account is a user", (created.get_json() or {}).get("role") == "user")
    new_client = client()
    check("created account can sign in",
          login(new_client, "new@example.com", "a brand new password").status_code == 302)

    check("duplicate email is refused",
          admin_client.post("/api/admin/users",
                            json={"email": "new@example.com",
                                  "password": "a brand new password"},
                            headers=json_headers(admin_client)).status_code == 409)
    check("invalid email is refused",
          admin_client.post("/api/admin/users",
                            json={"email": "not-an-email", "password": "a brand new password"},
                            headers=json_headers(admin_client)).status_code == 400)
    check("short password is refused",
          admin_client.post("/api/admin/users",
                            json={"email": "x@example.com", "password": "short"},
                            headers=json_headers(admin_client)).status_code == 400)
    check("creating an administrator directly is refused",
          admin_client.post("/api/admin/users",
                            json={"email": "x@example.com",
                                  "password": "a brand new password",
                                  "role": "admin"},
                            headers=json_headers(admin_client)).status_code == 400)
    check("create without CSRF is refused",
          admin_client.post("/api/admin/users",
                            json={"email": "x@example.com",
                                  "password": "a brand new password"}).status_code == 403)
    check("ordinary user cannot create accounts",
          user_client.post("/api/admin/users",
                           json={"email": "x@example.com",
                                 "password": "a brand new password"},
                           headers=json_headers(user_client)).status_code == 403)

    renamed = admin_client.patch(f"/api/admin/users/{created.get_json()['id']}",
                                 json={"name": "Renamed"}, headers=json_headers(admin_client))
    check("administrator can update a name", renamed.status_code == 200, renamed.get_json())
    check("update is reflected in the list",
          any(u.get("name") == "Renamed" for u in
              (admin_client.get("/api/admin/users").get_json() or {}).get("items", [])))
    check("unknown fields are refused",
          admin_client.patch(f"/api/admin/users/{created.get_json()['id']}",
                             json={"role": "admin"},
                             headers=json_headers(admin_client)).status_code == 400)
    check("invalid active value is refused",
          admin_client.patch(f"/api/admin/users/{created.get_json()['id']}",
                             json={"active": "yes"},
                             headers=json_headers(admin_client)).status_code == 400)
    check("unknown account update is 404",
          admin_client.patch("/api/admin/users/u_missing",
                             json={"name": "x"},
                             headers=json_headers(admin_client)).status_code == 404)

    deactivated = admin_client.patch(f"/api/admin/users/{created.get_json()['id']}",
                                     json={"active": False},
                                     headers=json_headers(admin_client))
    check("administrator can deactivate a user", deactivated.status_code == 200,
          deactivated.get_json())
    check("deactivation is reported", (deactivated.get_json() or {}).get("active") is False)
    check("deactivated account loses its session",
          new_client.get("/api/admin/session").status_code == 401)
    blocked_client = client()
    blocked = login(blocked_client, "new@example.com", "a brand new password")
    check("deactivated account cannot sign in", blocked.status_code == 401,
          blocked.status_code)
    with blocked_client.session_transaction() as state:
        check("deactivated login sets no session identity", not state.get("uid"), dict(state))
    reactivated = admin_client.patch(f"/api/admin/users/{created.get_json()['id']}",
                                     json={"active": True},
                                     headers=json_headers(admin_client))
    check("administrator can reactivate a user", reactivated.status_code == 200,
          reactivated.get_json())

    check("invalid role value is refused",
          admin_client.post(f"/api/admin/users/{user_account['id']}/role",
                            json={"role": "root"},
                            headers=json_headers(admin_client)).status_code == 400)
    check("role change for an unknown account is 404",
          admin_client.post("/api/admin/users/u_missing/role", json={"role": "admin"},
                            headers=json_headers(admin_client)).status_code == 404)

    victim = admin_client.post("/api/admin/users",
                               json={"email": "victim@example.com",
                                     "password": "a victim password"},
                               headers=json_headers(admin_client)).get_json()
    pending_delete = admin_client.delete(f"/api/admin/users/{victim['id']}",
                                         json={}, headers=json_headers(admin_client))
    check("deletion needs an approval record", pending_delete.status_code == 202,
          pending_delete.get_json())
    approve(admin_client, (pending_delete.get_json() or {}).get("approval"))
    deleted = admin_client.delete(f"/api/admin/users/{victim['id']}",
                                  json={"approval_id": (pending_delete.get_json() or {}).get("approval")},
                                  headers=json_headers(admin_client))
    check("administrator can delete after approval", deleted.status_code == 200,
          deleted.get_json())
    check("deleted account is gone",
          all(u.get("id") != victim["id"] for u in
              (admin_client.get("/api/admin/users").get_json() or {}).get("items", [])))
    check("deleting the signed-in account is refused",
          admin_client.delete(f"/api/admin/users/{admin_account['id']}",
                              json={}, headers=json_headers(admin_client)).status_code == 409)
    check("deletion is audited",
          "user_deleted" in [a.get("action") for a in
                             admin.get_admin_store().audit_entries(80)])

    # =====================================================================
    # 4. Errors API
    # =====================================================================
    empty = admin_client.get("/api/admin/errors")
    empty_body = empty.get_json() or {}
    check("errors endpoint succeeds", empty.status_code == 200, empty_body)
    check("empty error history is an empty list", empty_body.get("history") == [], empty_body)
    check("empty error history reports zero total", empty_body.get("total") == 0, empty_body)

    check("unauthenticated errors access is refused",
          client().get("/api/admin/errors").status_code == 401)
    check("ordinary user cannot read errors",
          user_client.get("/api/admin/errors").status_code == 403)

    root = runtime_paths.workspace_root()
    ids = []
    for index in range(5):
        item = selfheal._record({
            "kind": "error", "component": "tts",
            "error": (f"failure number {index} api_key=sk-live-ABCDEFGHIJKLMNOPQRST "
                      f"at {os.path.join(root, 'harry_core', 'tts.py')}"),
        })
        ids.append(item["id"])
    selfheal._record({"kind": "repair", "component": "jobs", "ok": True,
                      "result": {"note": "repaired"}})

    page = admin_client.get("/api/admin/errors?limit=2&offset=0").get_json() or {}
    check("pagination returns the requested page size", len(page.get("history", [])) == 2, page)
    check("pagination reports the total", page.get("total") == 6, page)
    check("pagination offers the next offset", page.get("next_offset") == 2, page)
    last_page = admin_client.get("/api/admin/errors?limit=2&offset=4").get_json() or {}
    check("the final page is shorter", len(last_page.get("history", [])) == 2, last_page)
    check("the final page has no next offset",
          last_page.get("next_offset") is None, last_page)
    kind_page = admin_client.get("/api/admin/errors?kind=repair").get_json() or {}
    check("kind filter works", kind_page.get("total") == 1, kind_page)

    for bad in ("limit=abc", "limit=0", "limit=500", "offset=-1", "offset=abc",
                "kind=bogus", "component=nonsense"):
        response = admin_client.get(f"/api/admin/errors?{bad}")
        check(f"invalid query {bad} is refused", response.status_code == 400,
              response.get_json())

    detail = admin_client.get(f"/api/admin/errors/{ids[0]}")
    check("error detail succeeds for a known id", detail.status_code == 200,
          detail.get_json())
    check("unknown error id is 404",
          admin_client.get("/api/admin/errors/ffffffffffff").status_code == 404)
    check("malformed error id is 404",
          admin_client.get("/api/admin/errors/not-an-id").status_code == 404)
    check("error detail without elevation is refused",
          client().get(f"/api/admin/errors/{ids[0]}").status_code == 401)
    check("errors endpoint rejects the wrong method",
          admin_client.post("/api/admin/errors", json={},
                            headers=json_headers(admin_client)).status_code == 405)

    raw = admin_client.get("/api/admin/errors?limit=50").get_data(as_text=True)
    check("API keys are redacted", "sk-live-ABCDEFGHIJKLMNOPQRST" not in raw)
    check("the redaction marker is present", "<redacted" in raw, raw[:200])
    check("named secrets are redacted", "api_key=sk-live" not in raw)
    check("absolute application paths are not exposed", root not in raw, raw[:200])
    detail_raw = admin_client.get(f"/api/admin/errors/{ids[0]}").get_data(as_text=True)
    check("detail view redacts secrets too", "sk-live-ABCDEFGHIJKLMNOPQRST" not in detail_raw)
    check("detail view hides absolute paths", root not in detail_raw)

    actions = [a.get("action") for a in admin.get_admin_store().audit_entries(100)]
    check("listing errors is audited", "errors_viewed" in actions, actions[:8])
    check("reading one error is audited", "error_viewed" in actions, actions[:8])

    print(f"\n===== RESULT: {PASS} passed, {FAIL} failed =====")
    sys.exit(1 if FAIL else 0)

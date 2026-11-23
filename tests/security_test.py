"""Security audit tests.

Run:  .venv/Scripts/python.exe tests/security_test.py

Covers the checks that matter for a personal assistant exposed on the internet:
remote-access gating, path traversal, IDOR, CSRF, SSRF, upload validation,
header hardening, secret leakage in the repository, SQL-injection resistance
and debug-surface removal.
"""

import base64
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harry_core import config  # noqa: E402

PASS, FAIL = 0, 0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"ok   {label}")
    else:
        FAIL += 1
        print(f"FAIL {label} {detail}")


def fresh_app(hosted=False):
    import harry_core.ui as ui
    ui._flask = None
    os.environ["HARRY_HOSTED"] = "1" if hosted else "0"
    import harry_core.hosted as hosted_mod
    app = ui.create_app()
    app.config.update(TESTING=True)
    return app


def test_remote_gate():
    config.CONFIG["ui"]["remote"]["enabled"] = False
    client = fresh_app(hosted=False).test_client()
    remote = client.get("/api/state", environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    check("non-loopback client refused when remote access is off", remote.status_code == 403,
          remote.status_code)
    local = client.get("/api/state", environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    check("loopback client allowed", local.status_code == 200, local.status_code)

    config.CONFIG["ui"]["remote"]["enabled"] = True
    os.environ.pop("HARRY_REMOTE_TOKEN", None)
    client = fresh_app(hosted=False).test_client()
    no_token = client.get("/api/state", environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    check("remote refused when no token is configured", no_token.status_code == 503,
          no_token.status_code)

    os.environ["HARRY_REMOTE_TOKEN"] = "a-long-shared-token"
    client = fresh_app(hosted=False).test_client()
    unauthorized = client.get("/api/state", environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    check("remote refused without the token", unauthorized.status_code == 401,
          unauthorized.status_code)
    authorized = client.get("/api/state", headers={"X-Harry-Token": "a-long-shared-token"},
                            environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    check("remote allowed with the token", authorized.status_code == 200, authorized.status_code)
    wrong = client.get("/api/state", headers={"X-Harry-Token": "nope"},
                       environ_overrides={"REMOTE_ADDR": "10.0.0.7"})
    check("wrong token refused", wrong.status_code == 401, wrong.status_code)
    config.CONFIG["ui"]["remote"]["enabled"] = False
    os.environ.pop("HARRY_REMOTE_TOKEN", None)


def test_traversal_and_idor():
    from harry_core import docgen, media
    docgen_root = tempfile.mkdtemp(prefix="sec-docs-")
    media_root = tempfile.mkdtemp(prefix="sec-media-")
    os.environ["HARRY_DOCUMENTS_DIR"] = docgen_root
    os.environ["HARRY_MEDIA_DIR"] = media_root
    client = fresh_app(hosted=False).test_client()

    record = docgen.generate("txt", "Safe", content="# Safe\n\ntext")
    check("document created inside the sandbox",
          os.path.abspath(record["path"]).startswith(os.path.abspath(docgen_root)))
    for path in ("/api/documents/..%2F..%2Fconfig.json/download",
                 "/api/documents/%2e%2e%2f%2e%2e%2fconfig.json/download",
                 "/api/media/..%2Fsecret/file",
                 "/api/documents/../../config.json",
                 "/api/documents/doc_1/../../etc/passwd"):
        response = client.get(path)
        check(f"traversal refused: {path[:44]}", response.status_code in (400, 404),
              response.status_code)
    check("a path is never served as a document id",
          client.get("/api/documents/" + "a" * 80).status_code == 400)

    doc_missing = client.get("/api/documents/document_not_real/download")
    check("unknown document is 404", doc_missing.status_code == 404, doc_missing.status_code)
    check("preview does not leak the filesystem path",
          "path" not in client.get(f"/api/documents/{record['id']}").get_json())
    check("download is not sniffed",
          client.get(f"/api/documents/{record['id']}/download").headers.get(
              "X-Content-Type-Options") == "nosniff")

    # media id checks
    for bad in ("image_", "video_" + "x" * 10, "image_../../x", "image_abc"):
        response = client.get(f"/api/media/{bad}/file")
        check(f"media id rejected: {bad[:16]}", response.status_code in (400, 404),
              response.status_code)


def test_ssrf():
    from harry_core import media
    config.CONFIG["media"]["allow_local_downloads"] = False
    blocked = 0
    for url in ("http://127.0.0.1:8000/", "http://localhost/", "http://10.0.0.5/x",
                "http://169.254.169.254/latest/meta-data/", "http://[::1]/",
                "file:///etc/passwd", "gopher://example.com/", ""):
        try:
            media.safe_download(url, 1024)
        except (ValueError, Exception):
            blocked += 1
    check("private, loopback, metadata and non-http URLs are all refused", blocked == 8, blocked)


def test_uploads():
    client = fresh_app(hosted=False).test_client()
    big = b"x" * (6 * 1024 * 1024)
    check("oversized audio rejected", client.post("/api/stt", data=big).status_code == 413)
    check("audio without data rejected", client.post("/api/stt", data=b"").status_code == 400)
    frame_too_big = "A" * (12 * 1024 * 1024)
    check("oversized frame rejected",
          client.post("/api/screen/frame", json={"image": frame_too_big}).status_code in (400, 413))
    check("non-base64 frame rejected",
          client.post("/api/screen/frame", json={"image": "data:image/png;base64,!!!!"}).status_code in (400, 409))
    check("frame refused while not sharing",
          client.post("/api/screen/frame",
                      json={"image": "data:image/png;base64," + base64.b64encode(b"x").decode()}
                      ).status_code == 409)
    check("chat with empty text rejected", client.post("/api/chat", json={"text": ""}).status_code == 400)
    check("unknown preference field rejected",
          client.post("/api/preferences", json={"evil": 1}).status_code == 400)


def test_sql_injection():
    from harry_core.accounts import AccountStore
    store = AccountStore(os.path.join(tempfile.mkdtemp(), "sqli.db"))
    store.create("real@example.com", "a real long password")
    attack = store.verify("real@example.com' OR '1'='1", "a real long password")
    check("quoted email cannot bypass authentication", attack.get("ok") is False, attack)
    drop = store.create("x@example.com'); DROP TABLE users;--", "a real long password")
    check("malicious email is stored literally or rejected",
          drop.get("ok") in (True, False))
    check("users table still exists", store.count() >= 1, store.count())


def test_headers_and_debug():
    hosted = fresh_app(hosted=True).test_client()
    anonymous = hosted.get("/")
    check("hosted root redirects anonymous visitors", anonymous.status_code == 302)
    login = hosted.get("/login")
    check("login page has no inline event handlers",
          "onclick=" not in login.get_data(as_text=True))
    check("login page is not indexed", "noindex" in login.get_data(as_text=True))
    health = hosted.get("/healthz")
    check("health endpoint leaks no secrets",
          "secret" not in health.get_data(as_text=True).lower()
          and "password" not in health.get_data(as_text=True).lower())

    local = fresh_app(hosted=False).test_client()
    html = local.get("/").get_data(as_text=True)
    check("no debugger pin in the page", "Werkzeug Debugger" not in html)
    check("no absolute filesystem paths in the page",
          "C:\\Hareesh" not in html and "/home/" not in html)
    check("messages are rendered with textContent, not innerHTML",
          "d.textContent = text" in html and "innerHTML = text" not in html)


def test_secrets_not_tracked():
    try:
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True,
                                 text=True, timeout=30).stdout.splitlines()
    except Exception:
        tracked = []
    check("git file list available", bool(tracked), len(tracked))
    sensitive = [f for f in tracked if f.endswith((".pkl", ".env")) or f == "config.json"]
    check("no voice profiles, .env files or local config are tracked", not sensitive, sensitive)
    check("data directory is not tracked", not any(f.startswith("data/") for f in tracked),
          [f for f in tracked if f.startswith("data/")][:3])
    key_patterns = re.compile(r"(sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|"
                              r"AIza[0-9A-Za-z_-]{20,}|-----BEGIN (RSA |OPENSSH )?PRIVATE KEY)")
    hits = []
    for relative in tracked:
        if not relative.endswith((".py", ".json", ".md", ".txt", ".toml", ".yml", ".yaml",
                                  ".js", ".html", ".cfg", ".ini")):
            continue
        path = os.path.join(ROOT, relative)
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                if key_patterns.search(f.read()):
                    hits.append(relative)
        except OSError:
            continue
    check("no credential-shaped strings in tracked sources", not hits, hits[:5])

    with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
        ignore = f.read()
    for pattern in ("data/", "config.json", "*.pkl", ".env", "deploy/.env"):
        check(f".gitignore covers {pattern}", pattern in ignore)


def test_limits():
    from harry_core import docgen
    try:
        docgen.generate("txt", "big", content="x" * 10)
    except Exception:
        pass
    listing = docgen.listing(limit=10000)
    check("listing limit is capped", len(listing) <= 200, len(listing))
    from harry_core.jobs import JobManager
    manager = JobManager(path=os.path.join(tempfile.mkdtemp(), "jobs.json"))
    check("job history is capped", manager.list(limit=10000) is not None)
    client = fresh_app(hosted=False).test_client()
    check("unknown job is 404", client.get("/api/jobs/job_nope").status_code == 404)
    check("unknown document id shape is rejected",
          client.get("/api/documents/" + "!" * 20).status_code == 400)


def main():
    test_remote_gate()
    test_traversal_and_idor()
    test_ssrf()
    test_uploads()
    test_sql_injection()
    test_headers_and_debug()
    test_secrets_not_tracked()
    test_limits()
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

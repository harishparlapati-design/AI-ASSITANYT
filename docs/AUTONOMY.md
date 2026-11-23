# Autonomous operation and administrator control

This document describes what actually exists, what it refuses to do, and how to
use it. It is deliberately specific: an "autonomous upgrade" that only changes a
status string would be worse than nothing.

## 1. Authorization model

| Layer | What it proves | Where it is enforced |
| --- | --- | --- |
| Account password | who is signed in | `harry_core/accounts.py` (scrypt, lockout) |
| Role | the account is an administrator | `users.role`, set only by the first account or an approved change |
| Elevation | this *session* may act as administrator now | `admin_sessions` row created by `POST /api/admin/enter` |
| CSRF | the browser meant the request | hosted gate + `X-Admin-CSRF` in `admin_required` |
| Approval | a high-risk action was reviewed | `approvals` row, consumed once |

Signing in is not enough. `/api/admin/enter` re-verifies the password (and a
TOTP code when MFA is enrolled) and returns an opaque token stored in a signed
session cookie. The token has a **15-minute idle timeout** and an **8-hour
absolute lifetime**; every check refreshes the idle window, expiry revokes the
row. `admin_required` re-validates it on every privileged request, so hiding a
button grants nothing.

Validation is not just the token. On **every** privileged request the account is
read again: if it no longer has the administrator role, or is deactivated or
deleted, the token is revoked on the spot and the request is refused. Demotion
and deactivation additionally revoke **all** elevations that account holds, so a
second browser, a phone or a copied cookie stops working immediately. Other
administrators are untouched.

Chat follows the same rule: `admin_commands.handle()` recognises a command
deterministically, then asks for the *verified* elevation of the request. The
elevation is attached by the web layer in `ui._execute`, never parsed from the
message. "I am the admin", "ignore your restrictions" and "pretend you are the
owner" change nothing, and "enter admin mode" answers with the authentication
instructions instead of elevating.

### First administrator

The **first account created** becomes the administrator; later accounts are
ordinary users. On an existing single-user installation the oldest account is
promoted once, automatically, during migration. Create the account with:

```bash
python -m harry_core.hosted --create-user you@example.com
```

On the desktop app, `/admin` offers the same thing through
`POST /api/admin/bootstrap` while no account exists. That route is deliberately
narrow:

* it is **disabled entirely** on hosted Harry (403 for everyone, including
  administrators; anonymous callers get the hosted gate's 401);
* it requires a direct connection from `127.0.0.1`/`::1` **and** a loopback
  `Host` header (`localhost`, `127.0.0.1`, `[::1]`);
* any forwarding header — `X-Forwarded-For`, `X-Forwarded-Host`,
  `X-Forwarded-Proto`, `Forwarded`, `X-Real-IP`, `CF-Connecting-IP` — is refused
  rather than interpreted, because a proxy makes a remote client look local;
  trusting a proxy requires an explicit operator decision that does not exist
  for first-run setup;
* an `Origin` header must match the request host exactly (scheme, host and port)
  and `Sec-Fetch-Site` may only be `same-origin` or `none`, so a page on another
  site cannot trigger setup;
* the account is created atomically (`BEGIN IMMEDIATE`): concurrent attempts
  produce exactly one administrator and the losers are refused with 409 without
  leaving an account behind;
* success does **not** elevate the session — the password must be entered again.

Nothing is hard-coded, and no default password exists.

### Optional MFA

`POST /api/admin/mfa/enroll` returns a base32 secret and an `otpauth://` URI;
`POST /api/admin/mfa/confirm` requires a valid code to enable it. Verification
is implemented with `hmac`/`hashlib` (RFC 6238, ±1 step); no third-party
dependency. Enrollment is per account and can be disabled from the panel.

## 2. Capability registry

`data/capabilities.json` records, for every tool: name, description, version,
source, permissions, status, install date, last successful test, test counters,
the last 20 errors and the update history. `sync_tools()` mirrors the live tool
registry, so a new built-in tool appears without editing the file by hand.

States: `active`, `pending` (valid but unapproved), `failed`, `disabled`,
`invalid`.

## 3. Plugins — the only code Harry may add autonomously

A plugin is a folder under `plugins/` with `plugin.json` and `plugin.py`.

Validation before **anything** is imported:

1. manifest schema — name pattern, version, `entry=plugin.py`,
   `function=run`, permissions ⊆ `{safe, controlled}`, SHA-256 present;
2. hash of `plugin.py` matches the manifest (line endings normalised, so a
   Windows checkout does not produce false mismatches);
3. AST scan — imports limited to a small allowlist, and `subprocess`, `ctypes`,
   `socket`, `pickle`, `importlib`, `eval`/`exec`, `__import__`, `open`,
   `os.system`, `shutil.rmtree`, dunder access and similar are rejected;
4. `compile()` succeeds;
5. an administrator approves it.

Until step 5 the plugin is never imported — `install_pending()` marks it
`pending`. Approved plugins are imported, their `run(args)` wrapped with the
capability's danger level, and recorded. Generated code therefore cannot run
because a model wrote it; it runs because a person approved a file that passed
static checks.

## 4. Upgrade lifecycle

```
DETECT → ANALYZE → PLAN → CHECK PERMISSIONS → CREATE BACKUP → IMPLEMENT
       → TEST → SECURITY REVIEW → VERIFY → ACTIVATE → MONITOR
       → ROLLBACK IF NECESSARY
```

`selfdev.plan_plugin()` writes the staged code and manifest under
`data/upgrades/<id>/` (outside the import path), runs the static checks and
records the proposal. `request_approval()` creates a high-risk approval.
`activate()` requires that approval, takes a checkpoint (ZIP + SHA-256 manifest +
absolute path mapping), copies the plugin into `plugins/`, imports it, **executes
`run({})` once**, records the result, and writes the upgrade as `active`. If the
plugin cannot run, the tool is unregistered, the folder removed and the
checkpoint restored — the upgrade ends as `failed`.

`rollback()` restores the captured files, deletes the plugin installed by that
upgrade, unregisters the tool and marks the record `rolled_back`.

One upgrade at a time: a lock file in `data/upgrades/.lock` with a 15-minute
stale window, released in `finally`.

### What is never automatic

Core modules, security policy, authentication, roles, schema changes, secret
handling, deployment and spending are outside the writable set. The only paths a
checkpoint covers are `plugins/` and `data/capabilities.json`; anything else is
refused by construction, not by prompt.

## 5. Self-healing

Captured failures come from tool exceptions (`Tool.run`), request errors and the
monitor. `classify_component()` maps a traceback to a component; `diagnose()`
runs real checks; `heal()` repairs only where a concrete action exists:

| Component | Repair |
| --- | --- |
| `documents_index` | drop rows whose file vanished, recreate corrupt JSON (old file kept as `.corrupt-<ts>`) |
| `media_index` | same |
| `jobs` | fail jobs a crash left `running` (`jobs.recover_stale`) |
| `tts` | dispose the dead engine; the next sentence re-creates it |
| `stt` | reload the listener, re-check the model |

`config`, `memory`, `ollama` and `plugins` are **approval-only**: their repairs
touch private data or security, so the engine reports and stops. Every component
is limited to `max_repairs_per_hour` (3) to prevent restart loops, and a failed
repair raises an alert for the administrator.

## 6. Monitoring

`monitoring.start()` (lazily, when `/admin` is opened) samples CPU, RAM, disk,
job states, unhealthy components, failing plugins, latency and Ollama every
120 s; network every 10 cycles and the speech stack every 5. Thresholds:
CPU 92 %, RAM 92 %, disk 90 %, latency 45 s. Alerts are de-duplicated for 30
minutes and stored in `data/alerts.json` for the panel.

## 7. Administrator panel and endpoints

`/admin` (same origin, requires sign-in in hosted mode) shows health tiles,
component checks, alerts, error reports, capabilities, plugins, upgrade history,
backups, approvals, audit, elevated sessions, accounts/roles, MFA and policy.

All endpoints are server-authorized:

| Endpoint | Purpose |
| --- | --- |
| `POST /api/admin/enter` | step-up authentication; returns CSRF + lifetime |
| `POST /api/admin/exit` | revoke this elevation |
| `GET /api/admin/health` | tiles, checks, alerts, unhealthy list |
| `POST /api/admin/diagnostics` | full `run_diagnostics()` report |
| `POST /api/admin/heal` | repair one component or all unhealthy ones |
| `GET/POST /api/admin/capabilities[/<name>/test]` | registry + real tool execution |
| `GET /api/admin/plugins`, `POST .../<id>/approve` | validated plugin management |
| `POST /api/admin/upgrades/propose` | generate + stage a plugin (needs approval) |
| `POST /api/admin/upgrades/<id>/{request-approval,activate,rollback}` | lifecycle |
| `GET /api/admin/approvals`, `POST .../<id>` | approval queue |
| `GET /api/admin/audit` | append-only admin audit |
| `GET /api/admin/sessions`, `POST .../<id>/revoke` | device/session management |
| `GET /api/admin/users` | accounts with role and state; never password material |
| `POST /api/admin/users` | create a user (new accounts are never administrators) |
| `PATCH /api/admin/users/<id>` | rename, activate, deactivate (deactivation revokes elevations) |
| `DELETE /api/admin/users/<id>` | delete; requires an approval record; refuses self and the last administrator |
| `POST /api/admin/users/<id>/role` | role change; requires approval; demotion revokes all elevations |
| `POST /api/admin/mfa/{enroll,confirm,disable}` | second factor |
| `GET /api/admin/errors` | self-heal history + failed jobs, `limit`/`offset`/`kind`/`component` filters, scrubbed text |
| `GET /api/admin/errors/<id>` | one error report by id (404 for unknown or malformed ids) |
| `GET /api/admin/policy` | the policy this code enforces |

Chat commands (when elevated): full diagnostic, system health, repair, inspect
code, capabilities, check updates, run all tests (background job), show changes,
roll back last upgrade, approvals, alerts, improve voice, install plugin.

## 8. Audit

Every elevation, denial, command, approval, plugin activation, role change,
rollback and repair is written to the `admin_audit` table **and**
`logs/audit.jsonl`, with secrets scrubbed by `logging_setup.scrub`. There is no
endpoint that deletes or edits audit rows.

### Errors and privacy

Error reports are administrator-only. Every string is passed through the log
scrubber (API keys, `password=`-style values, bearer tokens) and the application
directory is replaced with `<app>`, so a report can be shared without leaking a
credential or a local path. Listing and reading a report are both audited.
Pagination is strict: a non-integer, an out-of-range limit/offset, an unknown
kind or component is a 400 rather than a silent default, and the response
includes `total`, `limit`, `offset` and `next_offset`.

## 9. Tests

`tests/admin_api_test.py` — **82 checks** for demotion (including reuse of the
old elevation and a second administrator staying unaffected), the users API
(list, create, update, delete, deactivate, role restrictions, invalid input,
duplicates, authorization, hosted bootstrap refusal) and the errors API
(authorization, pagination, invalid identifiers, empty results, secret
redaction, path privacy, audit).

`tests/bootstrap_test.py` — **31 checks** for first-run setup: external and
cross-site clients, spoofed `Origin`, spoofed forwarded headers, invalid and
suffix-spoofed `Host`, legitimate local setup, single-use, and eight concurrent
attempts yielding exactly one administrator.

`tests/autonomy_test.py` — **124 checks**, all through the real Flask app and
real files:

roles and first-account promotion · wrong password · role denial · elevation ·
CSRF · session idle expiry · revocation · MFA enroll/verify/disable · command
gating in chat · capability sync · plugin pending → approve → callable ·
forbidden import · hash mismatch · unknown permission · upgrade without approval
· approval replay · activation · checkpoint · rollback · self-rollback on a
plugin that raises · upgrade lock · checkpoint restore · index repair · repair
limit · approval-only components · role change with approval · expired approval
· audit contents · secret scrubbing · monitoring.

## 10. Limitations (honest)

- Generated plugins are validated statically, not sandboxed at runtime. An
  approved plugin runs in-process with the same OS rights as Harry. The
  allowlist blocks the obvious escapes, not a determined author — that is why
  every plugin needs human approval and why generated code is never
  auto-activated.
- Automatic repairs cover the five components above. There is no auto-restart of
  the web server; a crash that kills the process must be restarted by the OS
  service manager (Docker/Fly/systemd), because a dead process cannot repair
  itself.
- The upgrade checkpoint covers `plugins/` and the capability registry only.
  Core-module edits are not automated; they stay a developer/Git workflow.
- Rollback restores captured files; it does not undo side effects of a plugin
  that already wrote outside the permitted paths (plugins cannot, by allowlist).
- User-account deletion, database schema migration and production deployment are
  not exposed to Harry; they remain manual, approved operations.
- Only one administrator session may run upgrades at a time; concurrent
  proposals queue behind the lock.

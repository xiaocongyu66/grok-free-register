# Source Modes and Repository Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Default authentication to same-machine registration output, preserve opt-in/auto SSH synchronization, and move tracked Python implementation out of the repository root.

**Architecture:** `AuthServiceSettings` resolves `auto|local|ssh`; both source modes feed the existing atomic `DiskSnapshotSource`. A local synchronizer runs the exporter from the authentication checkout against data-only paths. Registration becomes the `grok_register` package, developer utilities move to `tools`, and Bash remains the only supported user entry convention.

**Tech Stack:** Bash, Python 3, asyncio subprocesses, SQLite, CloakBrowser/Playwright, pytest.

---

### Task 1: Authentication source modes

**Files:**
- Modify: `xai_enroller/service.py`
- Modify: `xai_enroller/remote_stream.py`
- Modify: `tests/test_xai_auth_service.py`
- Modify: `tests/test_xai_remote_snapshot.py`

- [ ] Add failing settings tests for `auto`, explicit `local`, explicit `ssh`, and sanitized missing-host failure.
- [ ] Add failing local synchronizer tests for empty startup, exact-session precedence, duplicate collapse, generation changes, malformed/unreadable inputs, atomic replacement preservation, and mode `0600`.
- [ ] Run focused tests and confirm failures are caused by the absent source-mode/local-synchronizer behavior.
- [ ] Implement source resolution and the data-only local snapshot synchronizer using the checkout-anchored exporter.
- [ ] Wire the selected synchronizer into `main_async` without changing pipeline pacing, credentials, or inventory.
- [ ] Run both focused test files and confirm they pass.

### Task 2: Registration package migration and exact-first persistence

**Files:**
- Create: `grok_register/__init__.py`
- Move: `register.py` to `grok_register/register.py`
- Move: `email_server.py` to `grok_register/email_server.py`
- Move: `core/` to `grok_register/core/`
- Modify: registration/core test imports

- [ ] Preflight every source and destination; abort before moving anything on any conflict.
- [ ] Change registration persistence so the complete session is flushed and synced before the compatibility account line.
- [ ] Move implementation files with `git mv` and convert imports to `grok_register` package paths.
- [ ] Add/update the focused persistence test proving exact-session-first visibility.
- [ ] Run registration/core tests and confirm they pass.

### Task 3: Shell dispatch and developer tool migration

**Files:**
- Modify: `start.sh`
- Modify: `setup.sh`
- Modify: `auth-service.sh`
- Delete: `run.sh`
- Move: `run_tests.py` to `tools/run_tests.py`
- Move: `runtime_log_analyzer.py` to `tools/runtime_log_analyzer.py`
- Modify: `tests/test_run_tests_runner.py`
- Modify: `tests/test_runtime_log_analyzer.py`

- [ ] Add failing shell-dispatch checks for registration and the independent `--email-service` branch.
- [ ] Add a short shared setup lock and keep registration/email long-lived locks independent.
- [ ] Change registration execution to `python -m grok_register.register` and email execution to `python -m grok_register.email_server`.
- [ ] Move developer tools, update imports/commands, and delete the obsolete `run.sh`.
- [ ] Run shell syntax, dispatch, analyzer, and runner tests.

### Task 4: Documentation and root cleanup

**Files:**
- Modify: `README.md`
- Modify: `docs/guides/auth-service.md`
- Modify: `docs/guides/registration.md`
- Modify: `docs/architecture.md`
- Modify: `.gitignore`

- [ ] Document same-machine authentication first and SSH as the optional separated topology.
- [ ] Document only Bash user entries; keep direct Python commands developer-only.
- [ ] Update the tracked project tree and developer tool paths.
- [ ] Inventory approved unrelated root clutter by path and size without reading sensitive content.
- [ ] Preserve approved clutter in a timestamped sibling archive with a local manifest; do not delete or commit it.
- [ ] Confirm pre-existing organized dirty `docs/` and `deploy/` paths remain untouched.

### Task 5: Integrated verification and deployment

**Files:** all changed paths from Tasks 1–4.

- [ ] Run focused source, registration, shell, and tool tests.
- [ ] Run `bash -n start.sh setup.sh auth-service.sh` and compile the moved packages.
- [ ] Run the complete pytest suite.
- [ ] Verify the explicit base-to-head diff, root tree allowlist, staged-path privacy, and `git diff --check`.
- [ ] Commit from explicit allowlists and push `main`.
- [ ] Fast-forward the server, restart the registration service, and observe post-restart work.
- [ ] Restart local authentication, verify the existing SSH auto-selection, persistent prompt, and post-restart work.

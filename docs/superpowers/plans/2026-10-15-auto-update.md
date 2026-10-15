# Auto Update Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task. User delegated implementation decisions; execute inline without further approval gates.

**Goal:** Automatically check upstream commits and install a user-selected update with restart and recovery across native Python and Docker deployments.

**Architecture:** A standard-library host manager supervises the application. A small shared file protocol coordinates authenticated GUI requests, cross-process maintenance and persistent transactions. Native versions and Docker images are prepared separately from user sources and data.

**Tech Stack:** Python 3.11+, existing Starlette/uvicorn, Git, optional Docker Compose, plain JavaScript.

**Spec:** `docs/superpowers/specs/2026-10-15-auto-update-design.md`

## Global Constraints

- No new application dependencies; Docker host requires Python 3.11+.
- Preserve user source changes and all configuration, secrets, browser profiles and ledgers.
- Official upstream main only; SHA and ancestry validation, no shell interpretation.
- Never interrupt active or unresolved jobs; freeze new jobs before changing services.
- No irreversible data migrations, source resets, publication or changes to CI.

## Review Focus

- A child launched just before maintenance must prevent updating.
- A forged/stale manager heartbeat must not authorize installation.
- Failure while switching or recovering must leave truthful persistent state.
- Docker must preserve the selected service, mounts, environment and build arguments.
- Failed/unknown metadata and unusual paths must fail safely.

## Tasks

### Task 1: Update state, checks and maintenance

Files: `src/dyspark/update_state.py`, `src/dyspark/updates.py`, `tests/test_updates.py`.

Interfaces: `operation(data)`, `activity(data)`, `UpdateService.status/check/apply`, JSON state files under `data/updater`.

- [x] Write tests for maintenance, stale heartbeat, concurrent requests and unchanged user files; verify RED.
- [x] Implement atomic state, managed/unmanaged checking and authenticated request validation.
- [x] Verify with `.venv/Scripts/python -m pytest tests/test_updates.py -q` and source import path.

### Task 2: Source staging and deployment manager

Files: `src/dyspark/update_source.py`, `src/dyspark/update_manager.py`, `deploy/update.py`.

Interfaces: validated Git source; `NativeDeployment` / `DockerDeployment` prepare/start/stop/healthy methods; manager transaction and recovery.

- [x] Test against a temporary local Git remote, recording native process and Docker command boundaries; verify RED.
- [x] Implement independent versions, retained old release/image, supervised startup and persistent rollback.
- [x] Test successful switching, failed health check, recovery, no arbitrary request arguments and no false success.

### Task 3: GUI, job and CLI integration

Files: `gui.py`, `gui_access.py`, `jobs.py`, `cli.py`, `gui_static/updates.js`, `index.html`, `app.js`, `pyproject.toml`, Dockerfile.

- [x] Test authenticated update endpoints, maintenance guards, health identity and UI states; verify RED.
- [x] Wire lifespan, health identity, maintenance gates, settings card and bounded reconnect handling.
- [x] Run the full Python suite, JavaScript checks and meaningful native HTTP smoke checks.

### Task 4: Documentation and delivery

Files: `README.md`, `docs/updates.md`, `docs/cloud.md`.

- [x] Document native/Docker startup, extras/browser settings, health URL, state and recovery.
- [x] Run full tests, Ruff checks, formatting check, pip dependency check and wheel build.
- [x] Request one independent review; resolve material findings and verify fixes.

## Execution record

- Baseline: 31 passed, 2 skipped with `PYTHONPATH=src`.
- Decision: work in the existing checkout on `codex/auto-update`, retaining user changes and their test baseline; no extra worktree or unsolicited commits.
- Decision: maintenance begins when an update is accepted, including package preparation. This slightly extends the maintenance window but gives GUI, MCP and CLI one unambiguous freeze boundary.
- Environment: Docker CLI exists but its daemon is unavailable; real Docker, Linux and macOS execution must be reported as unverified unless an available environment changes that.
- Review fix: authoritative records, releases and Compose overrides live in host-only `.spark-updater/`; shared data carries only requests and status projections. Ownership initializes under the lifetime manager lock.
- Review fix: CLI children wait up to 10 seconds for their launcher's short metadata gate, then check maintenance before any business operation. This prevents false busy failures on slow storage while retaining the freeze boundary.
- Review fix: preserve the lexical virtualenv executable path on POSIX; dereferencing it would select the base interpreter and lose installed packages.
- Final verification: Python 73 passed / 3 platform skips; Node 4 passed; Ruff, formatting and wheel build passed. Clean isolated installation, dependency check and Windows HTTP startup/shutdown passed. See `docs/auto-update-validation.md` for the existing development-environment dependency conflicts and unverified platforms.
- Integration decision: keep all work reviewable in the existing checkout on `codex/auto-update`; preserve pre-existing changes. No publication or live deployment was performed.
- Kali follow-up: 76 Python tests passed without skips; Node 4 passed; clean dependencies, Ruff, formatting and wheel passed. Real native and both Docker service update/rollback flows passed with unchanged data hashes. Fixed POSIX stdlib shadowing by launching the supervisor as a module. Windows regression remains 73 passed / 3 platform skips. Evidence: `docs/kali-update-verification.json`.

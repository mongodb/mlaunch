# feature-monitor anomaly resolution plan

This plan resolves the anomalies documented in
`doc/monitor-auth-flag-anomaly-report.md` and makes each planned modification
traceable from requirement to code, tests, and documentation.

## Traceability format

Each implementation item will use this format:

```text
ID: FM-MON-AUTH-001
Source: doc/monitor-auth-flag-anomaly-report.md / A1
Code: mrun/monitor.py
Tests: mrun/test/test_monitor.py::test_...
Docs: doc/feature-monitor-guide.md
Status: Planned / Implemented / Verified
```

## Phase 1: CLI routing and argument handling

### FM-MON-CLI-001: Fix monitor flag order

Source: `doc/monitor-auth-flag-anomaly-report.md` / A3

Goal:

Make monitor mode route correctly regardless of where `--monitor` appears
among top-level options.

Required support:

```text
mrun --monitor --all
mrun --all --monitor
mrun --dir data --monitor
mrun --no-progressbar --monitor
```

Expected implementation:

- Update `MRunTool.run()` so any argv containing `--monitor` bypasses the
  default `init` rewrite.
- Preserve existing default-init behavior for non-monitor commands.
- Add regression tests for the supported flag orders.

Trace:

```text
Code: mrun/mrun.py
Tests: mrun/test/test_monitor.py
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

### FM-MON-CLI-002: Add monitor-specific unknown argument handling

Source: `doc/monitor-auth-flag-anomaly-report.md` / A2

Goal:

Prevent monitor-mode unknown arguments from being parsed as subcommands or
silently ignored.

Expected implementation:

- Detect monitor mode before subparser confusion occurs.
- Reject unsupported monitor arguments with a clear monitor-specific error.
- Avoid ambiguous behavior for init-only flags such as:
  - `--auth`
  - `--username`
  - `--password`
  - `--auth-db`

Trace:

```text
Code: mrun/mrun.py
Tests: mrun/test/test_monitor.py
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

## Phase 2: Process discovery hardening

### FM-MON-PROC-001: Handle restricted process-list environments

Source: `doc/monitor-auth-flag-anomaly-report.md` / A4

Goal:

Avoid raw tracebacks when `psutil` cannot enumerate local processes.

Expected implementation:

- Catch `PermissionError` and process-iteration failures around
  `psutil.process_iter()`.
- Return a structured discovery error or print a friendly monitor message.
- Suggested message:

```text
mrun --monitor could not list local processes: permission denied
```

Trace:

```text
Code: mrun/monitor.py
Tests: mrun/test/test_monitor.py
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

## Phase 3: Auth-aware network sampling

### FM-MON-AUTH-001: Load auth metadata from `.mrun_startup`

Source: `doc/monitor-auth-flag-anomaly-report.md` / A1, A6

Goal:

Let monitor mode understand whether the selected mongorun deployment was
created with authentication.

Expected implementation:

- Extend monitor startup metadata loading.
- Read `parsed_args` from `.mrun_startup`.
- Extract:
  - `auth`
  - `username`
  - `password`
  - `auth_db`
  - `initial-user`

Trace:

```text
Code: mrun/monitor.py
Tests: mrun/test/test_monitor.py
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

### FM-MON-AUTH-002: Pass auth credentials into `NetworkSampler`

Source: `doc/monitor-auth-flag-anomaly-report.md` / A1

Goal:

Make the network panel work for auth-enabled deployments when stored
credentials are available.

Expected implementation:

- Build network client kwargs from loaded auth metadata.
- If `auth` is true and `initial-user` is true, pass:
  - `username`
  - `password`
  - `authSource` from `auth_db`
- Keep non-auth behavior unchanged.
- Keep `directConnection=True`.
- Keep the short `serverSelectionTimeoutMS`.

Trace:

```text
Code: mrun/monitor.py
Tests: mrun/test/test_monitor.py
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

### FM-MON-AUTH-003: Improve auth-required network status

Source: `doc/monitor-auth-flag-anomaly-report.md` / A1, A6

Goal:

Avoid generic `unavailable` when the network panel cannot sample because
credentials are unavailable.

Expected implementation:

- If auth is enabled but no usable credentials are available, show:

```text
auth required
```

- Preserve generic `unavailable` for non-auth network failures.

Trace:

```text
Code: mrun/monitor.py
Tests: mrun/test/test_monitor.py
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

## Phase 4: Optional monitor credential overrides

### FM-MON-AUTH-004: Add explicit monitor credential flags

Source: `doc/monitor-auth-flag-anomaly-report.md` / A2, A6

Goal:

Support manual credentials for monitor network sampling when stored startup
metadata is missing, stale, or intentionally has no initial user.

Recommended flags:

```text
--monitor-username
--monitor-password
--monitor-auth-db
```

Expected implementation:

- Add monitor-specific credential flags at the top-level parser.
- Use them only for monitor network sampling.
- Let explicit monitor credentials override `.mrun_startup` credentials.
- Do not reuse init-only `--username`, `--password`, or `--auth-db` for monitor
  mode.

Trace:

```text
Code: mrun/mrun.py, mrun/monitor.py
Tests: mrun/test/test_monitor.py
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

## Phase 5: TLS/SSL compatibility

### FM-MON-TLS-001: Rehydrate TLS/SSL client options

Source: `doc/monitor-auth-flag-anomaly-report.md` / A5

Goal:

Make network sampling compatible with mongorun deployments that require
TLS/SSL client options.

Expected implementation:

- Load TLS/SSL settings from `.mrun_startup` `parsed_args`.
- Reuse or extract existing mrun TLS/SSL client-option construction where
  practical.
- Pass TLS/SSL kwargs into the same monitor network client path used by auth.
- Add tests using fake client factories to verify option propagation.

Trace:

```text
Code: mrun/mrun.py, mrun/monitor.py
Tests: mrun/test/test_monitor.py
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

## Phase 6: Documentation traceability

### FM-MON-DOC-001: Update `doc/feature-monitor-guide.md`

Source: all anomalies

Goal:

Make the final implementation traceable from anomaly report to code and tests.

Expected documentation additions:

- Anomaly resolution traceability table.
- Auth-enabled monitor flow diagram.
- Credential loading sequence diagram.
- Updated failure behavior.
- Updated manual test checklist.
- Status entries for planned, implemented, and verified items.

Trace:

```text
Docs: doc/feature-monitor-guide.md
Status: Implemented
```

## Verification plan

Run:

```bash
python3 -m py_compile mrun/monitor.py mrun/mrun.py mrun/test/test_monitor.py
uv run --with pytest pytest mrun/test/test_monitor.py
uv run --with pytest pytest
```

## Tests to add

```text
test_monitor_flag_order_all_before_monitor_routes_to_monitor
test_monitor_flag_order_dir_before_monitor_routes_to_monitor
test_monitor_flag_order_no_progressbar_before_monitor_routes_to_monitor
test_monitor_unknown_auth_value_flags_report_clear_error
test_discover_mongo_processes_handles_process_iter_permission_error
test_monitor_loads_auth_credentials_from_startup_file
test_network_sampler_uses_auth_credentials_when_auth_enabled
test_monitor_reports_credentials_required_for_no_initial_user
test_monitor_credentials_override_startup_credentials
test_monitor_rehydrates_tls_options_from_startup_file
```

## Recommended implementation order

```text
1. FM-MON-CLI-001: Fix monitor flag-order routing.
2. FM-MON-CLI-002: Add monitor-specific unknown argument handling.
3. FM-MON-PROC-001: Add process-list permission handling.
4. FM-MON-AUTH-001: Load auth metadata from .mrun_startup.
5. FM-MON-AUTH-002: Pass auth credentials into NetworkSampler.
6. FM-MON-AUTH-003: Show auth-required network status.
7. FM-MON-AUTH-004: Add explicit monitor credential overrides.
8. FM-MON-TLS-001: Rehydrate TLS/SSL client options.
9. FM-MON-DOC-001: Update traceability docs.
10. Run full verification.
```

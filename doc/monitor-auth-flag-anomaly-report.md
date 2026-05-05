# monitor auth and flag anomaly report

Date: 2026-05-05

Branch: `feature-monitor`

This report captures a focused test pass for `mrun --monitor` against an
authenticated mongorun deployment and several monitor flag combinations.

## Scope

The test pass checked:

- Whether `mrun --monitor` can start against a mongorun deployment created with
  `--auth`.
- Whether the network panel can sample `serverStatus().network` when auth is
  enabled.
- Whether monitor-specific flags behave consistently.
- Whether existing auth-related flags interact cleanly with monitor mode.
- Whether process discovery handles restricted process-list environments.

## Test environment

```text
Repository: /Users/sai.vadapalli/mongorun
Branch: feature-monitor
MongoDB binary: mongod 8.3.1
Shell date: 2026-05-05
Temporary auth data dir: /private/tmp/mrun-monitor-auth-test
Temporary auth port: 29117
Temporary username: monitoruser
Temporary password: monitorpass
```

The temporary authenticated node was stopped and the temporary data directory
was removed after testing.

## Commands run

Create temporary authenticated node:

```bash
python3 -m mrun.mrun --single --auth \
  --dir /private/tmp/mrun-monitor-auth-test \
  --port 29117 \
  --username monitoruser \
  --password monitorpass
```

Expected result: node starts and initial user is created.

Observed result:

```text
Detected mongod version: 8.3.1
Generating keyfile: /private/tmp/mrun-monitor-auth-test/keyfile
launching: "mongod" on port 29117
Username "monitoruser", password "monitorpass"
```

Run monitor in non-interactive mode:

```bash
python3 -m mrun.mrun --monitor --dir /private/tmp/mrun-monitor-auth-test
```

Observed result:

```text
mrun --monitor requires an interactive terminal.
```

This means process discovery succeeded and the command reached the terminal
interactivity guard.

Run monitor in an interactive PTY:

```bash
python3 -m mrun.mrun --monitor --dir /private/tmp/mrun-monitor-auth-test
```

Observed result:

- Monitor prompted for log selection.
- CPU panel rendered the authenticated `mongod`.
- Memory panel rendered the authenticated `mongod`.
- Disk panel rendered dbpath and log file sizes.
- Log tail rendered authenticated node log lines.
- Network panel showed `unavailable`.

Verify unauthenticated `serverStatus`:

```bash
mongosh --quiet --port 29117 --eval 'db.adminCommand({serverStatus:1}).ok'
```

Observed result:

```text
MongoServerError: Command serverStatus requires authentication
```

Verify authenticated `serverStatus`:

```bash
mongosh --quiet --port 29117 \
  -u monitoruser \
  -p monitorpass \
  --authenticationDatabase admin \
  --eval 'db.adminCommand({serverStatus:1}).ok'
```

Observed result:

```text
1
```

Verify monitor network sampler behavior:

```bash
python3 -c 'from mrun.monitor import NetworkSampler, MongoProcessInfo; p=MongoProcessInfo(1,"mongod",29117,"","",[]); m=NetworkSampler().sample([p])[29117]; print(m.available); print(m.error)'
```

Observed result:

```text
False
Command serverStatus requires authentication, full error: {'ok': 0.0, 'errmsg': 'Command serverStatus requires authentication', 'code': 13, 'codeName': 'Unauthorized'}
```

Cleanup:

```bash
python3 -m mrun.mrun kill --dir /private/tmp/mrun-monitor-auth-test
rm -rf /private/tmp/mrun-monitor-auth-test
```

Verification after cleanup:

```bash
mongosh --quiet --port 29117 --eval 'db.adminCommand({ping:1}).ok'
```

Observed result:

```text
MongoNetworkError: connect ECONNREFUSED 127.0.0.1:29117
```

## Summary

`mrun --monitor` can start against an auth-enabled mongorun deployment. The
process, CPU, memory, disk, and log-tail portions of the monitor work because
they do not require MongoDB authentication.

The network panel does not work in auth-enabled deployments because it calls
`serverStatus` without credentials. Authenticated `serverStatus` succeeds when
the expected username, password, and auth DB are supplied outside the monitor.

## Anomalies

### A1. Network panel is unavailable for auth-enabled deployments

Severity: High

Observed:

```text
Network Usage
PORT   IN       OUT      REQ/s   STATUS
29117  -        -        -       unavailable
```

Root cause:

`NetworkSampler._read_counters()` creates a MongoDB client with only:

```text
host
directConnection=True
serverSelectionTimeoutMS=200
```

It does not pass `username`, `password`, or `authSource`.

Why this matters:

Authenticated deployments are a supported mongorun mode. In these deployments,
the monitor can render local OS metrics and logs, but network rates are always
unavailable.

Recommended fix:

- Load `parsed_args` from `.mrun_startup`.
- If `parsed_args.auth` is true and `parsed_args.initial-user` is true, pass:
  - `username`
  - `password`
  - `authSource` from `auth_db`
- Keep unauthenticated behavior as the default for non-auth deployments.
- Add an explicit status message if auth is enabled but no initial user exists.

### A2. Monitor has no supported credential flags

Severity: High

Observed:

```bash
python3 -m mrun.mrun --monitor --dir /private/tmp/mrun-monitor-auth-test --auth
```

Result:

```text
mrun --monitor requires an interactive terminal.
```

The `--auth` flag is accepted by the command line parser path, but it is not a
monitor flag and has no effect on the monitor.

Observed:

```bash
python3 -m mrun.mrun --monitor --dir /private/tmp/mrun-monitor-auth-test --username monitoruser --password monitorpass --auth-db admin
```

Result:

```text
error: argument command: invalid choice: 'monitoruser'
```

Observed:

```bash
python3 -m mrun.mrun --monitor --dir /private/tmp/mrun-monitor-auth-test --auth-db admin
```

Result:

```text
error: argument command: invalid choice: 'admin'
```

Root cause:

Auth flags belong to the `init` subparser, not the top-level parser where
`--monitor` is defined. Boolean unknown flags can be silently ignored in monitor
mode, while unknown flags with values can be interpreted as invalid subcommands.

Recommended fix:

- Either do not accept auth flags in monitor mode and fail with a clear error,
  or add monitor-specific credential flags:
  - `--monitor-username`
  - `--monitor-password`
  - `--monitor-auth-db`
- Prefer loading credentials from `.mrun_startup` first, then allowing explicit
  monitor credentials to override.
- If monitor mode receives unknown arguments, report a monitor-specific parser
  error instead of allowing subparser confusion.

### A3. Monitor flag order is fragile

Severity: High

Working:

```bash
python3 -m mrun.mrun --monitor --dir data --all
```

Observed result:

```text
mrun --monitor requires an interactive terminal.
```

Broken:

```bash
python3 -m mrun.mrun --all --monitor
python3 -m mrun.mrun --dir data --monitor
python3 -m mrun.mrun --no-progressbar --monitor
```

Observed result:

```text
python3 -m mrun.mrun init: error: one of the arguments --single --replicaset is required
```

Root cause:

The default-command rewrite for `sys.argv` only excludes the case where
`sys.argv[1] == '--monitor'`. If another top-level option appears first, the
command is rewritten as `init ...`, even though `--monitor` is present later.

Recommended fix:

Update the `sys.argv` default-init branch to respect the computed
`monitor_requested` boolean:

```text
if first arg starts with '-' and not help/version and not monitor_requested:
    insert init
```

Also add tests for:

- `mrun --all --monitor`
- `mrun --dir data --monitor`
- `mrun --no-progressbar --monitor`

### A4. Restricted process-list environments can crash the monitor

Severity: Medium

Observed in the sandboxed shell:

```bash
python3 -m mrun.mrun --monitor
```

Result:

```text
PermissionError: [Errno 1] Operation not permitted (originated from sysctl(KERN_PROC_ALL))
```

With escalated process-list permission, the same command reached the expected
interactive terminal check:

```text
mrun --monitor requires an interactive terminal.
```

Root cause:

`discover_mongo_processes()` handles per-process `AccessDenied` inside
`process_to_info()`, but it does not handle `PermissionError` raised by
`psutil.process_iter()` itself before iteration can proceed.

Recommended fix:

- Wrap process iteration in `discover_mongo_processes()`.
- Return an empty process list or a structured discovery error if `psutil`
  cannot enumerate processes.
- Print a clear message such as:

```text
mrun --monitor could not list local processes: permission denied
```

### A5. TLS/SSL authenticated deployments are likely incomplete

Severity: Medium

This was not runtime-tested with a TLS deployment. Code inspection shows that
`MRunTool.client()` can apply TLS/SSL pymongo options when they are present on
the active `MRunTool` instance. However, monitor mode does not load
`.mrun_startup` parsed TLS/SSL arguments before constructing the monitor.

Expected impact:

- CPU, memory, disk, and log tail can still work.
- Network `serverStatus` sampling may fail for TLS-required deployments unless
  the correct client options are available.

Recommended fix:

- Load stored `parsed_args` for monitor mode.
- Rehydrate auth and TLS/SSL client options before constructing
  `NetworkSampler`.
- Add tests using fake client factories for TLS option propagation.

### A6. `--no-initial-user` auth deployments cannot provide monitor network credentials

Severity: Low to Medium

This was not runtime-tested. From existing mongorun behavior, `--auth` with
`--no-initial-user` can create an authenticated deployment without the default
admin user. In that scenario, there are no stored credentials for the monitor
to use.

Expected impact:

- Monitor can still show CPU, memory, disk, and logs.
- Network panel remains unavailable until explicit monitor credentials are
  provided.

Recommended fix:

- If auth is enabled but no initial user was created, show a clear network
  status explaining that credentials are required.
- Support explicit monitor credentials as an override.

## Behavior matrix

```text
+----------------------------------------------+--------+---------------------+
| Scenario                                     | Result | Notes               |
+----------------------------------------------+--------+---------------------+
| mrun --monitor on non-auth mrun deployment   | Pass   | Previously tested   |
| mrun --monitor on auth mrun deployment       | Partial| Network unavailable |
| CPU/memory on auth deployment                | Pass   | Uses psutil         |
| Disk panel on auth deployment                | Pass   | Uses filesystem     |
| Log tail on auth deployment                  | Pass   | Reads log files     |
| Network panel on auth deployment             | Fail   | Needs credentials   |
| mrun --monitor --all                         | Pass   | If --monitor first  |
| mrun --all --monitor                         | Fail   | Routed to init      |
| mrun --monitor --dir DIR                     | Pass   | If --monitor first  |
| mrun --dir DIR --monitor                     | Fail   | Routed to init      |
| mrun --monitor --auth                        | Ambig  | Accepted, ignored   |
| mrun --monitor --username USER               | Fail   | Value seen as cmd   |
| restricted process-list shell                | Fail   | PermissionError     |
+----------------------------------------------+--------+---------------------+
```

## Recommended implementation order

```text
1. Fix monitor flag-order routing.
2. Add friendly handling for process-list PermissionError.
3. Load monitor environment metadata from .mrun_startup.
4. Pass auth credentials into NetworkSampler when available.
5. Add explicit monitor credential override flags, or reject init-only auth
   flags with a clear monitor-specific error.
6. Rehydrate TLS/SSL client options for network sampling.
7. Add regression tests for every anomaly above.
```

## Suggested tests to add

```text
test_monitor_flag_order_all_before_monitor_routes_to_monitor
test_monitor_flag_order_dir_before_monitor_routes_to_monitor
test_monitor_flag_order_no_progressbar_before_monitor_routes_to_monitor
test_monitor_unknown_auth_value_flags_report_clear_error
test_discover_mongo_processes_handles_process_iter_permission_error
test_monitor_loads_auth_credentials_from_startup_file
test_network_sampler_uses_auth_credentials_when_auth_enabled
test_monitor_reports_credentials_required_for_no_initial_user
test_monitor_rehydrates_tls_options_from_startup_file
```

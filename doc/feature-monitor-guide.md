# feature-monitor branch guide

This document describes the monitor feature work introduced on the
`feature-monitor` branch. It is intended for reviewers, maintainers, and users
who want to understand how `mrun --monitor` fits into the existing mongorun
architecture.

## Feature summary

The branch adds an interactive terminal monitor for MongoDB processes launched
by mongorun. The monitor is started with:

```bash
mrun --monitor
```

By default, the monitor only displays MongoDB processes that belong to the
current mongorun data directory, using `./data/.mrun_startup` unless `--dir` is
provided. This keeps unrelated local `mongod` and `mongos` processes out of the
view.

To include every local MongoDB server process:

```bash
mrun --monitor --all
```

The monitor shows:

- CPU usage by MongoDB process.
- Replica-set role for each MongoDB process in CPU, memory, network, and disk
  metric panes.
- Memory usage by MongoDB process.
- MongoDB network counter rates from `serverStatus().network`.
- Disk consumption for each process dbpath and log file.
- Configurable top-N active cluster `currentOp` entries in the right-side
  activity pane. The default is 10 entries, and `L` changes the limit.
- CurrentOp source selection by process index, port, `primary`, `secondary`,
  or `all`, so only selected nodes receive `currentOp` commands.
- CurrentOp sampling pause/resume with Space while keeping the last sampled
  rows visible.
- Formatted and raw `db.currentOp()` display modes.
- CurrentOp namespace selection from active namespaces, with a clearable
  namespace filter.
- Pretty JSON and OSC 52 yank support for selected currentOp entries.
- Interactive `mongosh` administration shell handoff with primary,
  selected-node, seed-list, first-node, and custom-URI targets.
- Selectable live log tail with severity colors.
- Vim-style log filtering with fuzzy matching, structured field filters, and
  highlighted hits.
- Focusable panes with full-pane zoom.
- Selectable CPU process rows.
- Optional CPU thread view for the selected process.
- Expanded server status view for Disk, Network, Storage, and all top-level
  `serverStatus()` subsystem summaries.
- Zoomed log view.
- Scrollable syntax-colored Pretty JSON view for a highlighted log line.
- A two-column dashboard: CPU, Memory, Network, and Disk stacked on the left;
  Log Tail or Current Ops on the right.
- Neutral pane borders with bold, pane-colored titles and table headers.
- Shared ANSI-aware table formatting for CPU, memory, network, disk, and
  currentOp rows so column starts stay stable across panes.
- Log cursor movement that stays independent from the log viewport until the
  cursor reaches the visible window edge.
- Copy/yank support through OSC 52 terminal clipboard escape sequences.

No new terminal UI dependency is introduced. Rendering and keyboard input use
the Python standard library. The feature uses existing project dependencies and
interfaces such as `psutil` and the MongoDB client factory already used by
mongorun.

## Modified files

```text
feature-monitor branch
|
+-- mrun/mrun.py (lines 208-744)
|   +-- adds top-level --monitor, --all, and --dir handling (lines 223-263)
|   +-- adds --monitor-username, --monitor-password, --monitor-auth-db (lines 264-275)
|   +-- routes monitor requests before normal command dispatch (line 691)
|   +-- validates monitor-only flags (lines 702-731)
|   +-- constructs Monitor with data_dir and include_all options (lines 733-744)
|
+-- mrun/monitor.py (lines 1-4887)
|   +-- interactive Monitor implementation (lines 3817-4887)
|   +-- process discovery (lines 412-561), metrics sampling (lines 683-973), log tailing (lines 1308-1364), filtering (lines 2016-2339), rendering (lines 2767-3671), key input (lines 4149-4284)
|   +-- auth and TLS metadata loading for monitor samplers (lines 439-528)
|   +-- pane focus, focused-pane zoom, CPU process selection, thread/currentOp sampling
|   +-- ProcessSampler preserves psutil CPU state across refreshes (lines 683-720)
|   +-- RoleSampler captures primary/secondary role from serverStatus (lines 849-888)
|   +-- CurrentOpSampler captures and namespace-filters top active currentOp entries (lines 898-967)
|   +-- currentOp limit selection and validation, default 10 and max 500 (lines 1417-1443)
|   +-- currentOp source selection by role, port, or index (lines 1460-1538, 4459-4491)
|   +-- mongosh target selection, auth/TLS argv construction, and shell handoff helpers (lines 1586-1753, 4515-4572)
|   +-- StatusSampler captures serverStatus category details and subsystem summaries (lines 973-1152)
|   +-- log filter prompt, fuzzy scoring, structured filters, and hit highlighting
|   +-- BSON-safe raw/currentOp pretty serialization (lines 1237-1300)
|   +-- muted role colors that do not reuse bold pane-header styling (lines 54-56, 1803-1832)
|   +-- neutral-border, ANSI-aware panel renderer, bold pane titles, colored table headers, and content padding (lines 1837-1926, 2767-2848)
|   +-- shared table formatter for aligned metric/currentOp tables (lines 2850-2904)
|   +-- two-column dashboard layout and right activity pane (lines 3430-3671)
|
+-- mrun/fault_inject_collection_scans.py
|   +-- local-only PyMongo workload for monitor troubleshooting
|   +-- seeds a dedicated test collection and repeatedly runs COLLSCAN queries
|   +-- optionally enables profiler slowms=0 and restores it afterward
|
+-- mrun/test/test_monitor.py
|   +-- focused tests for monitor behavior and rendering helpers
|
+-- mrun/test/test_fault_inject_collection_scans.py
|   +-- focused tests for injector safety and profiler restoration
|
+-- doc/mrun.rst
|   +-- user-facing command documentation
|
+-- doc/monitor.md
|   +-- compact implementation note
|
+-- doc/feature-monitor-guide.md
    +-- this branch-level architecture and usage guide
```

## How the feature integrates

Before this branch, `MRunTool.run()` handled the normal command family:
`init`, `start`, `stop`, `restart`, `list`, and `kill`. This branch adds
`--monitor` as a top-level path that bypasses the default `init` routing.

```text
existing flow

user command
    |
    v
MRunTool.run()
    |
    +--> parse command and flags
    |
    +--> normal subcommand dispatch
         |
         +--> init/start/stop/restart/list/kill


feature-monitor flow

user command
    |
    |  mrun --monitor [--all] [--dir DIR]
    v
MRunTool.run() (lines 208-691)
    |
    +--> parse top-level monitor flags (lines 223-275)
    |
    +--> validate monitor-only arguments (lines 702-731)
    |
    +--> MRunTool.monitor() (lines 733-744)
         |
         +--> Monitor(...).run() (lines 3758-3787)
              |
              +--> interactive terminal dashboard
```

The integration is intentionally narrow:

- The existing subcommands continue to use their current code paths.
- `--monitor` is treated as a separate top-level mode.
- `--dir` is reused by monitor mode to find `.mrun_startup`.
- `--all` only affects monitor process discovery.
- `--monitor-username`, `--monitor-password`, and `--monitor-auth-db` only
  affect monitor network sampling.
- The monitor exits with status `1` when it cannot find matching processes and
  `0` for normal interactive exits.

## Invocation sequence

```mermaid
sequenceDiagram
    participant User
    participant CLI as mrun CLI
    participant Tool as MRunTool
    participant Monitor as Monitor
    participant PS as psutil
    participant FS as .mrun_startup

    User->>CLI: mrun --monitor
    CLI->>Tool: MRunTool.run() [mrun/mrun.py:208]
    Tool->>Tool: argparse parses --monitor [mrun/mrun.py:223]
    Tool->>Tool: skip default init routing [mrun/mrun.py:691]
    Tool->>Monitor: Monitor(data_dir="./data", include_all=false).run() [mrun/mrun.py:741]
    Monitor->>FS: load ./data/.mrun_startup [mrun/monitor.py:412]
    Monitor->>PS: discover local mongod/mongos [mrun/monitor.py:555]
    Monitor->>Monitor: keep only startup ports [mrun/monitor.py:537]
    Monitor-->>User: prompt for logs to tail [mrun/monitor.py:1732]
    User-->>Monitor: select log indexes or ports [mrun/monitor.py:1373]
    Monitor-->>User: render dashboard until quit [mrun/monitor.py:3944]
```

## All-process mode sequence

```mermaid
sequenceDiagram
    participant User
    participant Tool as MRunTool
    participant Monitor as Monitor
    participant PS as psutil

    User->>Tool: mrun --monitor --all
    Tool->>Monitor: Monitor(include_all=true).run() [mrun/mrun.py:741]
    Monitor->>PS: discover all local mongod/mongos [mrun/monitor.py:555]
    Monitor-->>User: prompt for logs from all discovered processes [mrun/monitor.py:1732]
    User-->>Monitor: press a [mrun/monitor.py:4149]
    Monitor->>Monitor: toggle process_scope to mrun [mrun/monitor.py:4747]
    Monitor-->>User: reselect logs using mrun-managed scope [mrun/monitor.py:3944]
```

## Auth and TLS network sequence

```mermaid
sequenceDiagram
    participant Monitor as Monitor
    participant FS as .mrun_startup
    participant Sampler as NetworkSampler
    participant Mongo as MongoDB

    Monitor->>FS: load parsed_args [mrun/monitor.py:412]
    FS-->>Monitor: auth, username, password, auth_db, TLS/SSL fields
    Monitor->>Monitor: apply monitor credential overrides if present [mrun/monitor.py:3836]
    Monitor->>Monitor: build PyMongo kwargs [mrun/monitor.py:3841]
    Monitor->>Sampler: NetworkSampler(client_kwargs, auth_required) [mrun/monitor.py:3849]
    Sampler->>Mongo: admin.command(serverStatus) [mrun/monitor.py:827]
    Mongo-->>Sampler: network counters or auth/TLS error
    Sampler-->>Monitor: NetworkMetrics [mrun/monitor.py:783]
```

## Runtime dashboard loop

The monitor refreshes once per configured interval. The default interval is
`1s`, and the `s` key cycles through `1s`, `5s`, and `10s`.

```mermaid
flowchart TD
    A[Start dashboard loop] --> B[Discover processes]
    B --> C{Processes found?}
    C -- no --> D[Print no-process message and exit]
    C -- yes --> E[Read CPU and memory]
    E --> ER[Sample serverStatus roles]
    ER --> F[Sample serverStatus network counters]
    F --> CO{CurrentOp view active?}
    CO -- yes --> CP[Sample active currentOps using top-N limit]
    CO -- no --> G[Read dbpath and log file sizes]
    CP --> G
    G --> H{Log streaming paused?}
    H -- yes --> I[Use existing log buffer]
    H -- no --> J[Poll selected log files]
    I --> K[Clamp highlighted log cursor]
    J --> K
    K --> L[Render terminal frame]
    L --> M{Key pressed?}
    M -- no --> A
    M -- q or Ctrl+C --> N[Exit]
    M -- r --> O[Reselect logs]
    M -- a --> P[Toggle process scope and reselect logs]
    M -- Tab or Shift+Tab --> Q[Move pane focus]
    M -- z --> R[Toggle focused-pane zoom]
    M -- CPU arrows/j/k --> S[Select MongoDB process]
    M -- CPU t --> T[Toggle selected-process thread view]
    M -- o --> TO[Toggle right activity currentOp view]
    M -- currentOp L --> CL[Select currentOp top-N limit]
    M -- currentOp r --> CS[Select currentOp source nodes]
    M -- currentOp space --> CZ[Pause or resume currentOp sampling]
    M -- logs arrows/j/k --> U[Move highlighted line]
    M -- logs g --> V[Jump to newest line]
    M -- logs p --> W[Toggle pretty JSON]
    M -- logs y --> X[Yank highlighted raw line]
    M -- logs space --> Y[Pause or resume log/currentOp streaming]
    M -- E --> SS[Toggle expanded server status view]
    M -- M --> MS[Launch mongosh shell and return]
    M -- s --> Z[Cycle refresh interval]
    O --> A
    P --> A
    Q --> A
    R --> A
    S --> A
    T --> A
    TO --> A
    CL --> A
    CS --> A
    CZ --> A
    U --> A
    V --> A
    W --> A
    X --> A
    Y --> A
    MS --> A
    Z --> A

Implementation mapping for the dashboard loop:

- **Loop Start**: `Monitor._run_dashboard()` [mrun/monitor.py:3944]
- **Dashboard Snapshot**: `Monitor._read_dashboard_snapshot()` [mrun/monitor.py:4029]
- **Discover Processes**: `Monitor._discover_processes_or_report()` [mrun/monitor.py:4130]
- **Read CPU/Memory**: `ProcessSampler.sample()` [mrun/monitor.py:699]
- **Sample Roles**: `RoleSampler.sample()` [mrun/monitor.py:858]
- **Sample Current Ops**: `CurrentOpSampler.sample()` [mrun/monitor.py:907]
- **Sample Network**: `NetworkSampler.sample()` [mrun/monitor.py:783]
- **Read Disk Sizes**: `read_disk_metrics()` [mrun/monitor.py:756]
- **Poll Logs**: `LogTailer.poll()` [mrun/monitor.py:1335]
- **Render Frame**: `render_dashboard()` [mrun/monitor.py:3439]
- **Two-Column Layout**: `_split_heights()` and `render_dashboard()` [mrun/monitor.py:3430]
- **Key Actions**: `Monitor._wait_for_action()` [mrun/monitor.py:4149]
- **Pane Focus**: `Monitor._focus_next_pane()` [mrun/monitor.py:4307]
- **Zoom Pane**: `Monitor._toggle_focused_zoom()` [mrun/monitor.py:4317]
- **CPU Thread View**: `Monitor._toggle_cpu_thread_view()` [mrun/monitor.py:4341]
- **Right-Pane CurrentOp View**: `Monitor._toggle_cpu_current_op_view()` [mrun/monitor.py:4359]
- **CurrentOp Raw Toggle**: `Monitor._toggle_current_op_raw()` [mrun/monitor.py:4375]
- **CurrentOp Namespace Selector**: `Monitor._select_current_op_namespace()` [mrun/monitor.py:4395]
- **CurrentOp Limit Selector**: `Monitor._select_current_op_limit()` [mrun/monitor.py:4435]
- **CurrentOp Source Selector**: `Monitor._select_current_op_sources()` [mrun/monitor.py:4459]
- **CurrentOp Pause Toggle**: `Monitor._toggle_current_op_sampling()` [mrun/monitor.py:4493]
- **Mongosh Shell Handoff**: `Monitor._launch_mongosh_admin_shell()` [mrun/monitor.py:4515]
- **Log Filter Prompt**: `Monitor._start_log_filter_prompt()` [mrun/monitor.py:4574]
- **Pretty Log JSON**: `Monitor._toggle_pretty_log_line()` [mrun/monitor.py:4788]
- **Pretty CurrentOp JSON**: `Monitor._toggle_pretty_current_op()` [mrun/monitor.py:4827]
- **Yank Log**: `Monitor._yank_log_line()` [mrun/monitor.py:4849]
- **Yank CurrentOp**: `Monitor._yank_current_op()` [mrun/monitor.py:4864]
- **Expanded Status**: `Monitor._toggle_server_status_view()` [mrun/monitor.py:4300]
```

## Terminal layout

The monitor renders one full terminal frame plus a one-line footer. In the
normal dashboard, the left half is a stacked metrics column and the right half
is a tall activity pane. The activity pane displays the log tail by default and
switches to top active `currentOp` rows when `o` is pressed.

```text
+ CPU Usage -------------------------++ Log Tail: 27017, 27018 --------------+
| PORT PID ROLE PROCESS CPU% STATUS  || 27017 | {"s":"I", ...}               |
| 27017 123 Primary mongod 12 running || 27018 | {"s":"W", ...}               |
+ Memory Usage ----------------------+|>27018 | {"s":"E", ...}               |
| PORT ROLE PID PROCESS RSS          || 27017 | {"s":"I", ...}               |
| 27017 Primary 123 mongod 1.2G      || 27018 | {"s":"D", ...}               |
+ Network Usage ---------------------+|                                       |
| PORT ROLE IN OUT REQ/s STATUS      ||                                       |
| 27017 Primary 1.5K/s 4.0K/s 12 ok  ||                                       |
+ Disk Usage ------------------------+|                                       |
| PORT ROLE DB SIZE LOG SIZE STATUS  ||                                       |
| 27017 Primary 540.2MB 12.1MB ok    ||                                       |
+------------------------------------++---------------------------------------+
q | Tab | z zoom | r | E | mrun | a all | s1s | logs j/k | ...
```

### Highlighting and color coding

The monitor uses visual cues to indicate focus and severity:

- **Pane focus**: The currently focused pane is indicated by square brackets in
  the title and an emphasized ASCII border character. Borders remain neutral so
  structure stays low key.
- **Panel borders**: Borders are plain terminal foreground color. They do not
  carry pane colors.
- **Pane header color coding**: Titles and table headers use the pane color:
  - **CPU Usage**: Teal
  - **Memory Usage**: Green
  - **Network Usage**: Yellow
  - **Disk Usage**: Red
  - **Log Tail**: Teal
- **Table header styling**: Table header rows are bold and use the same color
  as their pane title. Body rows keep role, severity, selection, or yank
  styling where applicable.
- **Role styling**: `Primary`, `Secondary`, and `Password Required` use muted,
  non-bold role colors. They intentionally do not reuse the bold pane-header
  styling, so role values remain readable without competing with headers.
  Roles are rendered in CPU, memory, network, disk, and currentOp rows.
- **Internal padding and tables**: Non-empty content rows receive one cell of
  left padding after the border. CPU, memory, network, disk, and currentOp rows
  are formatted by the same ANSI-aware table helper, so column starts stay
  stable even when role text or JSON syntax colors are present.
- **Log severity**: MongoDB log lines are colored by severity (Fatal: red
  inverse, Error: red, Warning: yellow, Info: teal, Debug: dim gray).

```text
+ [CPU Usage] ==================++ Log Tail: 27017, 27018 -------+
|   PORT PID ROLE PROCESS CPU%   ||  27017 | {"s":"I", ...}       |
| > 27017 123 Primary mongod 12  ||  27018 | {"s":"W", ...}       |
+===============================+|> 27018 | {"s":"E", ...}       |
| Memory, Network, Disk stacked ||  27017 | {"s":"I", ...}       |
+-------------------------------++-------------------------------+
q | Tab | z zoom | r | E | mrun | a all | s1s | logs j/k | ...
```

When zoom mode is active, the focused pane owns the full frame. For logs:

```text
+ [Log Tail: 27018] ==============================================+
|  27018 | {"s":"I","msg":"startup complete"}                     |
|  27018 | {"s":"W","msg":"slow operation"}                       |
|> 27018 | {"s":"E","msg":"connection failed"}                    |
|  27018 | {"s":"I","msg":"listening"}                            |
+=================================================================+
```

CPU thread view is toggled only from the CPU pane with `t`:

```text
+ [CPU Threads: port 27017 pid 12345] ============================+
| PROCESS port 27017 pid 12345 mongod                             |
| TID        CPU%    USER     SYSTEM   TOTAL                       |
| 456789      12.5   2.10     0.30     2.40                        |
+=================================================================+
```

Top currentOp view is toggled with `o`. It uses the right activity pane,
leaves the left metric panes visible, and defaults to the top 10 active
operations:

```text
+ CPU Usage --------------------++ [Current Ops (Formatted, top 10)] --------+
| PORT PID ROLE PROCESS CPU%    || PORT   ROLE              SECS OP    NS    |
| 27017 123 Primary mongod 12   ||>27017  Primary             8.4 query app  |
+ Memory Usage -----------------+| 27018  Secondary           4.2 command   |
+ Network Usage ----------------+|                                       |
+ Disk Usage -------------------++---------------------------------------+
```

Press `O` while this view is active to switch the right activity pane to raw
documents derived from the `db.currentOp()` command response. BSON-only values
such as `ObjectId`, `Timestamp`, and datetimes are converted to safe text for
terminal display instead of being passed directly to `json.dumps()`.

```text
+ CPU Usage --------------------++ [Current Ops (Raw, top 10)] --------------+
| PORT PID ROLE PROCESS CPU%    || RAW CURRENTOP DOCUMENTS                  |
| 27017 123 Primary mongod 12   ||>27017 Primary {"op":"query","ns":"app"}  |
+-------------------------------++------------------------------------------+
```

CurrentOp rows can be navigated with `j`/`k` or arrows while the right activity
pane is active. Press `n` while currentOp is active to choose a namespace from
the active currentOp namespaces, or type a namespace manually. Press `c` while
currentOp is active to clear that namespace filter. Press `p` on a highlighted
currentOp row to show the BSON-safe raw document as syntax-colored Pretty JSON;
`j`/`k` then scroll that pretty document. Press `p` again to return to the
currentOp list. Press `y` to yank the highlighted currentOp through OSC 52.
Press `L` while currentOp is active to set the top-N limit; the prompt accepts
positive integers and caps large values at 500.

Pretty JSON mode also uses the full log view:

```text
+ Log Tail: 27018 (Pretty JSON) ---------------------------------+
| {                                                               |
|   "t": {                                                        |
|     "$date": "2026-05-05T13:30:29.241-07:00"                   |
|   },                                                            |
|   "s": "I",                                                     |
|   "c": "NETWORK",                                               |
|   "msg": "connection accepted"                                  |
| }                                                               |
+-----------------------------------------------------------------+
```

## Panel rendering and header styling

The terminal frame is rendered without a UI framework. `make_panel()` is the
single panel primitive used by the dashboard, focused zoom panes, Pretty JSON
views, and the expanded `serverStatus()` layout. The helper is deliberately
ANSI-aware so color codes do not change visible widths or push borders out of
alignment.

```text
---------------- render_dashboard() ----------------+
| split terminal into left metrics and right activity|
|                                                    |
|  +--> make_panel("CPU Usage", cpu_lines, ...)      |
|  +--> make_panel("Memory Usage", memory_lines, ...)|
|  +--> make_panel("Network Usage", network_lines,..)|
|  +--> make_panel("Disk Usage", disk_lines, ...)    |
|  +--> make_panel("Log Tail" or "Current Ops", ...) |
+----------------------------------------------------+
               |
               v
+---------------- make_panel() ----------------------+
| title row: ANSI_BOLD + pane color + title          |
| border row: neutral terminal foreground            |
| content: add one-cell padding after left border    |
| header: STYLE_TABLE_HEADER -> bold + pane color    |
| width: _truncate_ansi() + _pad_ansi()              |
+----------------------------------------------------+
```

Table header rows are marked before rendering. The metric and currentOp
formatters build rows through `format_table_lines()`, which accepts column
definitions, computes visible widths with ANSI/style markers removed, and then
right- or left-aligns each cell. Header rows carry a private
`STYLE_TABLE_HEADER` marker. `make_panel()` strips that marker, applies bold and
the pane's header color, then pads and clips the visible text to the panel
width.

```text
format_cpu_lines()
    |
    +-- format_table_lines([PORT, PID, ROLE, PROCESS, CPU%, STATUS], rows)
    |
    +-- _table_header("  PORT   PID ...")
    |
    v
make_panel(header_color=ANSI_TEAL)
    |
    +-- _style_ansi([STYLE_TABLE_HEADER], header_color)
    |
    v
bold teal table header inside a teal CPU pane
```

Implementation mapping for panel rendering:

- **Bold Escape Constant**: `ANSI_BOLD` [mrun/monitor.py:51]
- **Table Header Marker**: `STYLE_TABLE_HEADER` [mrun/monitor.py:82]
- **Role Color Constants**: `ANSI_ROLE_PRIMARY`, `ANSI_ROLE_SECONDARY`, and `ANSI_ROLE_WARNING` [mrun/monitor.py:54]
- **Panel Content Padding**: `_panel_content_padding()` [mrun/monitor.py:1837]
- **Table Header Wrapper**: `_table_header()` [mrun/monitor.py:1850]
- **Style to ANSI Conversion**: `_style_ansi()` [mrun/monitor.py:1871]
- **Role Color Helpers**: `role_style()`, `colorize_role()`, and `format_role()` [mrun/monitor.py:1896]
- **Panel Renderer**: `make_panel()` [mrun/monitor.py:2767]
- **Shared Table Formatter**: `format_table_lines()` [mrun/monitor.py:2850]
- **CPU Header Source**: `format_cpu_lines()` [mrun/monitor.py:2906]
- **CurrentOp Header Source**: `format_current_op_lines()` [mrun/monitor.py:2945]
- **Memory Header Source**: `format_memory_lines()` [mrun/monitor.py:3014]
- **Network Header Source**: `format_network_lines()` [mrun/monitor.py:3043]
- **Disk Header Source**: `format_disk_lines()` [mrun/monitor.py:3084]
- **Subsystem Header Source**: `format_subsystem_status_lines()` [mrun/monitor.py:3230]
- **Thread Header Source**: `format_thread_lines()` [mrun/monitor.py:3253]
- **Renderer Regression Test**: `test_make_panel_bolds_title_and_pads_table_header()` [mrun/test/test_monitor.py:1939]
- **Neutral Border Regression Test**: `test_make_panel_uses_neutral_borders_and_colored_header_text()` [mrun/test/test_monitor.py:1922]

## Expanded Server Status View

The expanded status view is triggered by `E` and provides a deeper view into
the internals of the selected MongoDB process. It replaces the standard
dashboard with a four-panel `serverStatus()` layout.

### Layout segregation

| Category | Source Metrics | Key Data Points |
| :--- | :--- | :--- |
| **Disk** | `wiredTiger.block-manager`, `wiredTiger.log`, `backgroundFlushing` | Read/Write rates, Log size, Flush durations |
| **Network** | `network`, `connections`, `opcounters` | Active connections, Op rates (ops/sec), Network IO |
| **Storage** | `wiredTiger.cache`, `concurrentTransactions`, `globalLock`, `mem` | Cache usage (%), Read/Write tickets, Lock queues, RSS |
| **Other Subsystems** | all top-level `serverStatus()` keys | key name and compact summary, including `metrics`, `locks`, `repl`, `flowControl`, `security`, and any version-specific fields |

The other-subsystems panel intentionally lists compact summaries instead of
dumping the entire BSON response. This keeps the terminal readable while still
making it clear which MongoDB subsystem sections are present for the selected
node.

### Status sequence

```mermaid
sequenceDiagram
    participant User
    participant Monitor
    participant StatusSampler
    participant Mongo as MongoDB

    User->>Monitor: Press 'E' [mrun/monitor.py:4149]
    Monitor->>Monitor: Set server_status_active = True [mrun/monitor.py:4300]
    loop Refresh Loop
        Monitor->>StatusSampler: sample(process) [mrun/monitor.py:4029]
        StatusSampler->>Mongo: runCommand({serverStatus: 1}) [mrun/monitor.py:1014]
        Mongo-->>StatusSampler: Full BSON Response
        StatusSampler->>StatusSampler: Summarize every top-level subsystem [mrun/monitor.py:1126]
        StatusSampler->>StatusSampler: Segregate Disk/Network/Storage details [mrun/monitor.py:1038]
        StatusSampler-->>Monitor: ServerStatusSnapshot [mrun/monitor.py:1072]
        Monitor->>Monitor: render_server_status_view() [mrun/monitor.py:3386]
        Monitor-->>User: Refresh 4-panel UI [mrun/monitor.py:3386]
    end
    User->>Monitor: Press 'E' or 'Esc' [mrun/monitor.py:4149]
    Monitor->>Monitor: Set server_status_active = False [mrun/monitor.py:4300]
```

### ASCII Layout (Expanded View)

```text
+ [DISK STATUS] (port 27017) ----------+ [NETWORK STATUS] (port 27017) ------+
| WT Block Manager:                    | Connections:                       |
|  Read:    1.2 MB/s                   |  Current:   15                     |
|  Written: 0.5 MB/s                   | Op Rates (ops/sec):                |
| WT Logging:                          |  Query:   450.5                    |
+--------------------------------------+------------------------------------+
+ [STORAGE SUBSYSTEM] (port 27017) ----+ [OTHER SUBSYSTEMS] (port 27017) ---+
| WT Cache:                            | Top-level serverStatus keys:       |
|  Used: [####------] 42%              | SUBSYSTEM              SUMMARY     |
| WT Tickets (available):              | version:               8.0.0       |
|  Read:  128                          | metrics:               18 fields   |
| Memory:                              | locks:                 5 fields    |
+--------------------------------------+------------------------------------+
E exit status view | s refresh 1s | q quit
```

Implementation mapping for expanded status view:

- **Status Snapshot Model**: `ServerStatusSnapshot` [mrun/monitor.py:323]
- **Status Sampler**: `StatusSampler.sample()` [mrun/monitor.py:1014]
- **Subsystem Summaries**: `StatusSampler._extract_subsystems()` [mrun/monitor.py:1126]
- **Disk Formatting**: `format_disk_status_lines()` [mrun/monitor.py:3131]
- **Network Formatting**: `format_network_status_lines()` [mrun/monitor.py:3163]
- **Storage Formatting**: `format_storage_status_lines()` [mrun/monitor.py:3191]
- **Other Subsystems Formatting**: `format_subsystem_status_lines()` [mrun/monitor.py:3230]
- **Four-Panel Renderer**: `render_server_status_view()` [mrun/monitor.py:3386]
- **Neutral Border Rendering**: `make_panel()` [mrun/monitor.py:2767]
- **Toggle Handling**: `Monitor._toggle_server_status_view()` [mrun/monitor.py:4300]

### Logical flow

```mermaid
flowchart LR
    A[Monitor Loop] --> B{server_status_active?}
    B -- No --> C[Render Standard Quadrants]
    B -- Yes --> D[Invoke StatusSampler]
    D --> E[Execute serverStatus]
    E --> F[Extract detailed categories]
    E --> K[Summarize all top-level subsystem keys]
    F --> G[Disk: WT Blocks/Log]
    F --> H[Network: Ops/Conns]
    F --> I[Storage: Cache/Locks]
    G & H & I & K --> J[Render 4-Panel View]
```

## Process discovery

The monitor has two process scopes.

### mrun-managed scope

This is the default for `mrun --monitor`.

```text
data directory
    |
    +-- .mrun_startup
          |
          +-- startup_info
                |
                +-- port -> original mongod/mongos command line
```

`mrun/monitor.py` loads the startup file and extracts expected ports, dbpaths,
and logpaths. It then discovers running MongoDB processes using `psutil` and
keeps only processes whose ports match the startup metadata.

```mermaid
flowchart LR
    A[datadir/.mrun_startup] --> B[load_mrun_process_specs]
    B --> C[expected ports/logpaths/dbpaths]
    D[psutil.process_iter] --> E[discover_mongo_processes]
    C --> F[filter_mrun_processes]
    E --> F
    F --> G[mrun-managed running processes]
```

### All-process scope

This mode is started with `mrun --monitor --all` or by pressing `a` inside the
monitor. It skips `.mrun_startup` filtering and shows every local `mongod` or
`mongos` visible to `psutil`.

This is useful for debugging manually launched nodes, but the default remains
mrun-managed to reduce terminal noise.

## Replica roles and activity-pane currentOp view

The CPU, memory, network, disk, and currentOp rows include a `ROLE` column.
The role is sampled from each node through `serverStatus()` using the same
direct per-port client path as the network and expanded status samplers. If a
supported MongoDB version does not expose enough role data in `serverStatus()`,
the sampler falls back to `hello`, then legacy `isMaster`, before marking the
role unavailable.

```mermaid
sequenceDiagram
    participant Loop as Dashboard Loop
    participant Role as RoleSampler
    participant Op as CurrentOpSampler
    participant Mongo as MongoDB Node

    Loop->>Role: sample(processes) [mrun/monitor.py:941]
    Role->>Mongo: admin.command(serverStatus) [mrun/monitor.py:968]
    Mongo-->>Role: repl.stateStr or repl.isWritablePrimary
    opt serverStatus lacks role or is rejected
        Role->>Mongo: admin.command(hello), then isMaster [mrun/monitor.py:1339]
        Mongo-->>Role: isWritablePrimary/ismaster/secondary/msg
    end
    Role-->>Loop: RoleMetrics by port
    alt activity-pane currentOp view active
        Loop->>Op: sample(processes, role_metrics, limit, namespace) [mrun/monitor.py:1031]
        Op->>Mongo: admin.command({currentOp:1,$all:true,active:true,ns?}) [mrun/monitor.py:1071]
        opt optional currentOp field rejected
            Op->>Mongo: retry simpler currentOp shapes [mrun/monitor.py:1389]
        end
        Mongo-->>Op: inprog active operations
        Op-->>Loop: top-N entries sorted by secs_running
    end
```

Role extraction rules:

```text
serverStatus().repl.stateStr == PRIMARY    -> Primary
serverStatus().repl.stateStr == SECONDARY  -> Secondary
repl.isWritablePrimary == true             -> Primary
repl.isWritablePrimary == false with repl   -> Secondary
hello.isWritablePrimary == true            -> Primary
hello.ismaster == true                     -> Primary
hello.secondary == true                    -> Secondary
process == mongos                          -> Router
no repl data                               -> Standalone
auth metadata without credentials          -> Password Required
```

The `Password Required` role text is intentionally different from the network
panel's lower-level `auth required` status. The role column is a user
navigation surface, so it uses the clearer instruction-like text. When monitor
credentials are available from `.mrun_startup` or explicit
`--monitor-username`, `--monitor-password`, and `--monitor-auth-db` flags, the
same credentials are passed to role and currentOp sampling.

Role colors:

```text
Primary           -> muted non-bold green
Secondary         -> muted non-bold amber
Password Required -> muted non-bold warning red
unknown/unavailable -> dim or neutral
```

The currentOp view is not the default. Press `o` from any pane to replace the
right-side log activity pane with active currentOp entries across visible
processes. The default limit is top 10, and `L` opens a top-N selector while
currentOp is active. The selector accepts positive integers and caps large
values at 500. The left CPU, memory, network, and disk panes remain visible.
Press `O` while currentOp is active to toggle between formatted rows and raw
`db.currentOp()`-derived documents. Raw display is BSON-safe: values that are
not JSON serializable are converted to readable text before rendering. Press
`n` while currentOp is active to select a namespace filter from active
namespaces, or type a namespace manually; press `c` to clear the currentOp
namespace filter. The sampler prefers the richest command shape,
`{currentOp: 1, $all: true, active: true, ns?: ...}`, then retries simpler
forms if a supported MongoDB version rejects an optional field. The UI still
filters returned documents client-side when a namespace is active, so older or
newer command response shapes remain stable in the display. Press `p` on the
highlighted currentOp to open a scrollable syntax-colored Pretty JSON view of
its raw `db.currentOp()` document. Press `y` to yank either the visible
formatted row, raw JSON document, or active pretty JSON document depending on
the current currentOp mode.

Press `r` while currentOp is active to choose which nodes receive `currentOp`
commands. The selector accepts process indexes, ports, `primary`, `secondary`,
`all`, or Enter for all visible processes. For example, entering `primary`
filters currentOp sampling to the primary node only; secondaries remain visible
in the metric panes but are not queried for currentOp rows. Press Space while
currentOp is active to pause currentOp sampling. The monitor keeps rendering
the last sampled currentOp snapshot until Space is pressed again. Press `o`
again to return the right side to the log tail. The view is sampled only while
active, so normal dashboard refreshes do not run `currentOp` commands
unnecessarily.

Implementation mapping for roles and currentOp:

- **Role Snapshot Model**: `RoleMetrics` [mrun/monitor.py:242]
- **Command Capability Model**: `MongoCommandCapabilities` [mrun/monitor.py:340]
- **CurrentOp Entry Model**: `CurrentOpEntry` [mrun/monitor.py:299]
- **CurrentOp Snapshot Model**: `CurrentOpSnapshot` [mrun/monitor.py:314]
- **Compatibility Error Mapping**: `_compat_error_message()` [mrun/monitor.py:842]
- **Role Sampler**: `RoleSampler.sample()` [mrun/monitor.py:941]
- **Role Extraction**: `role_from_server_status()` [mrun/monitor.py:1291]
- **Hello Role Fallback**: `read_hello_role()` [mrun/monitor.py:1339]
- **Role Formatting**: `format_role()` [mrun/monitor.py:2134]
- **CurrentOp Sampler**: `CurrentOpSampler.sample()` [mrun/monitor.py:1031]
- **CurrentOp Command Fallbacks**: `current_op_command_candidates()` [mrun/monitor.py:1389]
- **CurrentOp Result Extraction**: `current_op_documents()` [mrun/monitor.py:1416]
- **CurrentOp Normalization**: `current_op_entry()` [mrun/monitor.py:1428]
- **CurrentOp Namespace List**: `current_op_namespaces()` [mrun/monitor.py:1234]
- **BSON-Safe Raw Rendering**: `current_op_raw_json()` [mrun/monitor.py:1273]
- **Pretty CurrentOp JSON**: `current_op_pretty_json_lines()` [mrun/monitor.py:1278]
- **Limit Prompt Parser**: `parse_current_op_limit_selection()` [mrun/monitor.py:1417]
- **Limit Prompt**: `choose_current_op_limit()` [mrun/monitor.py:1434]
- **Source Label**: `current_op_source_label()` [mrun/monitor.py:1460]
- **Source Prompt Parser**: `parse_current_op_source_selection()` [mrun/monitor.py:1477]
- **Source Prompt**: `choose_current_op_sources()` [mrun/monitor.py:1528]
- **Namespace Prompt Parser**: `parse_current_op_namespace_selection()` [mrun/monitor.py:1397]
- **Namespace Prompt**: `choose_current_op_namespace()` [mrun/monitor.py:1550]
- **Shared Table Formatter**: `format_table_lines()` [mrun/monitor.py:2850]
- **CPU Role Rendering**: `format_cpu_lines()` [mrun/monitor.py:2906]
- **Memory Role Rendering**: `format_memory_lines()` [mrun/monitor.py:3014]
- **Network Role Rendering**: `format_network_lines()` [mrun/monitor.py:3043]
- **Disk Role Rendering**: `format_disk_lines()` [mrun/monitor.py:3084]
- **CurrentOp Rendering**: `format_current_op_lines()` [mrun/monitor.py:2945]
- **Right-Pane CurrentOp Toggle**: `Monitor._toggle_cpu_current_op_view()` [mrun/monitor.py:4359]
- **Raw CurrentOp Toggle**: `Monitor._toggle_current_op_raw()` [mrun/monitor.py:4375]
- **CurrentOp Limit Selector**: `Monitor._select_current_op_limit()` [mrun/monitor.py:4435]
- **CurrentOp Source Selector**: `Monitor._select_current_op_sources()` [mrun/monitor.py:4459]
- **CurrentOp Pause Toggle**: `Monitor._toggle_current_op_sampling()` [mrun/monitor.py:4493]
- **Pretty CurrentOp Toggle**: `Monitor._toggle_pretty_current_op()` [mrun/monitor.py:4827]
- **CurrentOp Yank**: `Monitor._yank_current_op()` [mrun/monitor.py:4864]

## Mongosh administration shell handoff

The monitor can hand control to `mongosh` without terminating the dashboard.
Press `M` from the dashboard to open a target selector, choose where to connect,
administer the cluster in `mongosh`, and exit the shell to return to the live
monitor.

```text
monitor dashboard
    |
    |  M
    v
target prompt
    |
    +-- primary node, when role sampling identifies one
    +-- selected CPU row
    +-- replica-set seed list from visible processes
    +-- first visible process
    +-- custom URI typed by the user
    |
    v
subprocess argv, no shell=True
    |
    v
mongosh uses inherited terminal stdio
    |
    v
exit mongosh and resume dashboard
```

Authentication and TLS handling matches monitor sampling:

- The monitor loads auth/TLS metadata from `.mrun_startup`.
- `--monitor-username`, `--monitor-password`, and `--monitor-auth-db` override
  stored auth only for monitor-related operations.
- TLS flags such as `--tls`, `--tlsCAFile`, client certificate files, CRL files,
  and invalid certificate/hostname allowances are translated from the stored
  client kwargs.
- Password values are never appended to the command argv. The generated command
  passes `--password` without a value, so `mongosh` prompts securely.
- If auth metadata says credentials are required but monitor has none, the
  handoff refuses to launch and reports `mongosh requires credentials for this
  deployment`.
- If `mongosh` is not on `PATH`, the dashboard reports `mongosh not found in
  PATH`.

```mermaid
sequenceDiagram
    participant User
    participant Monitor
    participant Role as RoleSampler
    participant Shell as mongosh

    User->>Monitor: Press M [mrun/monitor.py:4149]
    Monitor->>Monitor: Check executable and credentials [mrun/monitor.py:4515]
    Monitor->>Role: sample(processes) for primary target [mrun/monitor.py:858]
    Monitor->>Monitor: Build target list [mrun/monitor.py:1586]
    Monitor-->>User: Prompt target choice [mrun/monitor.py:1665]
    User-->>Monitor: Select primary/selected/seed/first/custom
    Monitor->>Monitor: Build argv with auth/TLS [mrun/monitor.py:1716]
    Monitor->>Shell: subprocess.call(argv)
    Shell-->>Monitor: Exit status
    Monitor-->>User: Resume dashboard
```

Implementation mapping for mongosh handoff:

- **Replica Set Name Loader**: `load_monitor_replset_name()` [mrun/monitor.py:529]
- **Target Model**: `MongoshTarget` [mrun/monitor.py:272]
- **Target Builder**: `mongosh_target_options()` [mrun/monitor.py:1586]
- **Target Parser**: `parse_mongosh_target_selection()` [mrun/monitor.py:1639]
- **Target Prompt**: `choose_mongosh_target()` [mrun/monitor.py:1665]
- **TLS Flag Builder**: `mongosh_tls_args()` [mrun/monitor.py:1690]
- **Command Builder**: `build_mongosh_command()` [mrun/monitor.py:1716]
- **Dashboard Handoff**: `Monitor._launch_mongosh_admin_shell()` [mrun/monitor.py:4515]

## Metrics model

```text
MongoProcessInfo
|
+-- pid
+-- process name: mongod or mongos
+-- port
+-- logpath
+-- dbpath
+-- cmdline

ProcessMetrics
|
+-- cpu_percent
+-- memory_rss
+-- status

ProcessSampler
|
+-- cached psutil.Process by pid
+-- prime()
+-- sample()

ThreadMetrics
|
+-- thread_id
+-- cpu_percent
+-- user_time
+-- system_time
+-- total_time

RoleMetrics
|
+-- available
+-- role: Primary, Secondary, Router, Standalone, Password Required, or unavailable
+-- error

NetworkMetrics
|
+-- available
+-- bytes_in_per_sec
+-- bytes_out_per_sec
+-- requests_per_sec
+-- error

CurrentOpEntry
|
+-- port
+-- role
+-- secs_running
+-- op
+-- ns
+-- client
+-- desc/opid
+-- raw document for raw currentOp view

CurrentOpSnapshot
|
+-- available
+-- entries: top-N active operations sorted by secs_running
+-- error

CurrentOp limit
|
+-- default: 10
+-- maximum: 500
+-- L opens selector while currentOp view is active

CurrentOp source filter
|
+-- empty list means all visible MongoDB processes
+-- r opens selector while currentOp view is active
+-- accepts indexes, ports, primary, secondary, all, or Enter for all
+-- only selected source processes receive currentOp commands

CurrentOp sampling pause
|
+-- Space toggles pause/resume while currentOp view is active
+-- paused mode reuses the cached CurrentOpSnapshot
+-- resume clears the cache and samples again on the next refresh

CurrentOp namespace filter
|
+-- empty string means no namespace filter
+-- n opens selector from active currentOp namespaces
+-- c clears the filter while currentOp view is active

MongoshTarget
|
+-- label
+-- uri
+-- kind: primary, selected, seed-list, first, or custom

MonitorAuthConfig
|
+-- enabled
+-- username
+-- password
+-- auth_db
+-- initial_user
+-- client_kwargs()
+-- requires_credentials()

DiskMetrics
|
+-- available
+-- db_size
+-- log_size
+-- error

LogFilterMatch
|
+-- matched
+-- score
+-- spans

LogFilterView
|
+-- filtered lines
+-- original raw-buffer indexes
+-- search-hit spans by raw index

ServerStatusSnapshot
|
+-- available
+-- port
+-- disk/network/storage detailed categories
+-- subsystems: compact top-level serverStatus summaries
+-- error

DashboardSnapshot
|
+-- processes
+-- process_metrics
+-- role_metrics
+-- network_metrics
+-- disk_metrics
+-- log_lines
+-- thread_metrics/thread_error/thread_count
+-- current_ops
+-- status_snapshot
+-- sampled_at
```

CPU and memory come from `psutil.Process`. CPU percent is read through a
long-lived `ProcessSampler` that keeps one `psutil.Process` object per pid, so
`cpu_percent(interval=None)` has a previous sample to compare against on later
refreshes. This matters because recreating a `psutil.Process` for every
dashboard tick can repeatedly return an initial zero sample instead of the live
CPU rate. Roles are read from `serverStatus().repl` and rendered in all metric
tables plus currentOp rows. Network rates are computed by sampling MongoDB
`serverStatus().network` counters and calculating deltas between refreshes. For
authenticated deployments, monitor mode loads stored
credentials from `.mrun_startup` and passes them to the network sampler. If
`--monitor-username`, `--monitor-password`, or `--monitor-auth-db` are provided,
those values override stored credentials for monitor network, role, currentOp,
and expanded status sampling only. TLS/SSL client options are also rehydrated
from `.mrun_startup` and passed to the same monitor client path. Disk size is
computed with standard library file traversal: `os.walk()` and
`os.path.getsize()`.

CurrentOp sampling is deliberately on-demand. It runs only while the right
activity pane is in currentOp view and merges active operations from the
visible MongoDB processes, sorted by `secs_running` descending and truncated to
the configured top-N limit. Formatted mode shows compact columns; raw mode
renders the original document captured by `CurrentOpEntry.raw` through a
BSON-safe serializer. If a namespace filter is active, the sampler first tries
to pass `ns` to the currentOp command, then retries without `ns` if the server
rejects that optional field. The returned operation documents are still
filtered client-side. The sampler also accepts common result containers such as
`inprog`, `ops`, and `currentOps`, which keeps the pane usable when supported
MongoDB versions vary response shape.

When a currentOp source filter is active, the monitor filters the process list
before calling `CurrentOpSampler.sample()`. Selecting `primary` means only the
current primary process is sampled for currentOp rows; the remaining nodes keep
their metric panes visible but do not receive currentOp commands. When currentOp
sampling is paused with Space, the monitor skips `CurrentOpSampler.sample()`
and renders the cached `CurrentOpSnapshot`.

The `M` shell handoff is also on-demand. It is not part of the refresh loop and
does not run unless the user explicitly requests it. The monitor temporarily
restores normal terminal handling, launches `mongosh` as an argv list, and then
redraws the dashboard after `mongosh` exits.

Expanded status mode reuses the same MongoDB client configuration and calls
`serverStatus()` for the selected CPU process. Detailed fields are split into
Disk, Network, and Storage panels, while every top-level response key is also
summarized in the Other Subsystems panel so version-specific subsystems are not
silently hidden. Missing or non-dictionary subsystem fields are treated as empty
sections instead of crashing the renderer, which lets the pane display cleanly
across supported MongoDB versions.

Thread metrics are sampled only when the CPU pane is in thread view. The
monitor reads `psutil.Process(pid).threads()` for the selected process and
computes per-thread CPU percentage from user/system time deltas between
refreshes. If the operating system denies detailed thread timing, the monitor
falls back to `psutil.Process(pid).num_threads()` and shows the thread count.

## Pane focus, CPU threads, and activity pane

The monitor starts with the logs pane focused, preserving the existing behavior
where log navigation works immediately after the dashboard opens.

```text
focus order

logs --Tab--> cpu --Tab--> memory --Tab--> network --Tab--> disk --Tab--> logs
logs --Shift+Tab--> disk
```

The focused pane receives an emphasized neutral ASCII border. `z` zooms the
focused pane, not only logs. When a zoomed pane is active, `z` returns to the
dashboard view.

CPU pane behavior:

```text
default CPU pane
    |
    |  j/k or arrows
    v
selected mongod/mongos row
    |
    |  t
    v
thread view for selected process
    |
    |  t
    v
default CPU pane
```

Thread view is not rendered by default. It is a pane-local toggle, so log
tailing, memory, network, and disk views do not change unless their pane is
focused or zoomed.

CurrentOp view is also not rendered by default. It is mutually exclusive with
thread view: enabling currentOp hides thread rows, and enabling thread view
hides currentOp rows. CurrentOp itself is an activity-pane mode, so the left
metric stack remains visible:

```text
log activity pane
    |
    |  o
    v
top-N currentOp formatted view
    |
    |  O
    v
top-N currentOp raw view
    |
    |  L
    v
updated currentOp top-N limit
    |
    |  r
    v
selected currentOp source nodes
    |
    |  Space
    v
paused currentOp snapshot
    |
    |  o
    v
log activity pane
```

macOS may deny detailed per-thread timing through `task_for_pid`, even when the
monitor is launched with `sudo`. In that case the CPU thread pane displays the
available thread count and a short unavailable message:

```text
PROCESS port 27017 pid 86094 mongod
THREAD COUNT 113
thread details unavailable
```

## Fast cursor redraws

The dashboard samples process, network, disk, thread, and log data on the
configured refresh cadence. Cursor-only actions such as log up/down, CPU
process up/down, pane focus, and zoom redraw from the most recent
`DashboardSnapshot` instead of re-running all samplers.

```text
arrow key
    |
    v
update cursor state
    |
    v
render cached DashboardSnapshot
    |
    +-- no process discovery
    +-- no serverStatus network query
    +-- no disk walk
    +-- no log file poll until refresh deadline
```

When CPU thread view is active, changing the selected CPU process forces a
fresh sample so the thread count/details match the newly selected process.

Log cursor movement keeps a separate `log_view_start` value. When the cursor
moves within the visible log window, only the cursor marker moves and the text
does not scroll. Once the cursor reaches the top or bottom visible edge, the
viewport shifts by one row. `g` jumps the cursor and viewport back to the
newest filtered or unfiltered log line.

Implementation mapping for cursor-only redraw:

- **Log View Clamp**: `clamp_log_view_start()` [mrun/monitor.py:2457]
- **Log Row Formatting**: `format_log_lines()` [mrun/monitor.py:2509]
- **Activity Pane Height**: `Monitor._activity_view_height()` [mrun/monitor.py:4728]
- **Log Cursor Move**: `Monitor._move_log_cursor()` [mrun/monitor.py:4647]
- **Jump Latest**: `Monitor._jump_to_latest()` [mrun/monitor.py:4773]
- **Cursor Regression Test**: `test_log_cursor_moves_inside_visible_window_before_scrolling()` [mrun/test/test_monitor.py:2065]

## Log tailing

The log tailer keeps a bounded in-memory buffer. Each line is prefixed with the
MongoDB port so users can see which process produced it.

```text
raw file line
    |
    v
LogTailer._append_line(port, line)
    |
    v
"27018 | {\"s\":\"I\", ...}"
```

The initial seed reads a small recent tail from each selected log file. Later
refreshes poll from remembered file offsets.

```mermaid
sequenceDiagram
    participant Loop as Dashboard Loop
    participant Tailer as LogTailer
    participant File as MongoDB log file

    Loop->>Tailer: read_log_stream(stream_paused=false)
    Tailer->>File: seek saved offset
    File-->>Tailer: appended lines
    Tailer->>Tailer: prefix each line with port
    Tailer-->>Loop: bounded log buffer

    Loop->>Tailer: read_log_stream(stream_paused=true)
    Tailer-->>Loop: existing buffer without reading file
```

Pausing log streaming with Space while the logs pane is focused freezes the
visible buffer and does not advance file offsets. Resuming catches up from the
same offsets.

## Log filtering

Log filtering is entered from the logs pane with `/`. The prompt behaves like a
small vim-style search bar:

```text
/             enter filter prompt
Enter         apply typed filter
Esc           cancel prompt and keep the previous filter
Backspace     edit prompt input
Ctrl+U        clear prompt input
c             clear the active filter from the logs pane
```

Filtering is non-destructive. The `LogTailer` buffer still keeps every raw line;
the dashboard builds a filtered view that contains matching lines and their
original raw-buffer indexes. This lets cursor navigation, Pretty JSON, and yank
continue to act on the original log entry.

```mermaid
flowchart LR
    A[Raw LogTailer buffer] --> B{Filter active?}
    B -- no --> C[Render raw stream]
    B -- yes --> D[Parse filter query]
    D --> E[Structured field checks]
    D --> F[Fuzzy text scorer]
    E --> G[Filtered view with raw indexes]
    F --> G
    G --> H[ANSI hit highlighting]
    H --> I[Render log pane]
```

Supported examples:

```text
/ slowop              show slow-operation query lines
/ cmd:find            show command logs for find
/ cmd:aggregate       show command logs for aggregate
/ component:COMMAND   show COMMAND component logs
/ severity:E          show error logs
/ port:27017          show logs for one port
/ msg:"Slow query"    show lines whose message matches Slow query
```

The fuzzy matcher is intentionally local and dependency-free. It checks exact
case-insensitive substrings first, then token matches, then ordered fuzzy
subsequences. For example, `slwop` can match "Slow query operation" because the
characters appear in order. The filtered stream preserves chronological log
order; the score is used for matching strength and hit positions, not sorting.

When a filter is active, newly tailed log lines are filtered before display. If
streaming is paused, the filter applies to the paused buffer only. If no lines
match, the log pane displays:

```text
No log lines match filter: slowop
```

Implementation mapping for log filtering:

- **Filter Match Result**: `LogFilterMatch` [mrun/monitor.py:281]
- **Filtered View Model**: `LogFilterView` [mrun/monitor.py:290]
- **Fuzzy Scorer**: `score_log_filter()` [mrun/monitor.py:2016]
- **Structured Filter Match**: `match_log_filter()` [mrun/monitor.py:2249]
- **View Builder**: `filter_log_lines()` [mrun/monitor.py:2283]
- **Filtered Cursor Clamp**: `clamp_filtered_log_cursor()` [mrun/monitor.py:2302]
- **Filtered Cursor Move**: `move_filtered_log_cursor()` [mrun/monitor.py:2320]
- **Hit Highlight Rendering**: `format_log_lines()` [mrun/monitor.py:2509]
- **Filter Prompt Keys**: `Monitor._handle_log_filter_prompt_key()` [mrun/monitor.py:4582]
- **Apply Filter**: `Monitor._apply_log_filter()` [mrun/monitor.py:4607]
- **Clear Filter**: `Monitor._clear_log_filter()` [mrun/monitor.py:4630]

## Log colors and highlight priority

MongoDB structured logs use the `s` field for severity. The monitor also
supports plain text fallback detection.

```text
+----------+----------------------+--------------------------+
| Severity | Detected values      | Terminal style           |
+----------+----------------------+--------------------------+
| Fatal    | F, fatal, critical   | red inverse              |
| Error    | E, error, err        | red                      |
| Warning  | W, warn, warning     | yellow                   |
| Info     | I, info              | muted teal               |
| Debug    | D, debug, trace      | dim gray                 |
+----------+----------------------+--------------------------+
```

Highlight priority is:

```text
yanked line
    |
    +-- green inverse
        |
        +-- overrides severity color

selected line
    |
    +-- inverse video over severity color
        |
        +-- keeps current row visible without losing traffic-light signal

filter hit
    |
    +-- highlighted exact/token/fuzzy match span
        |
        +-- visible on normal severity-colored rows

normal line
    |
    +-- severity color only
```

The copied/yanked text is always the raw log line from the in-memory buffer. It
does not include ANSI escape sequences or visual cursor markers.

## Pretty JSON syntax colors

Pretty JSON mode is entered from the logs pane with `p`. The monitor keeps the
current default behavior and expands the selected log line into the zoomed log
pane. The same parsed JSON receives token-level ANSI color:

```text
+-------------+-------------------------+
| Token       | Color behavior          |
+-------------+-------------------------+
| Object keys | one uniform key color   |
| Strings     | one uniform value color |
| Numbers     | one uniform number color|
| true/false  | one keyword color       |
| null        | one keyword color       |
| Punctuation | dim/neutral color       |
+-------------+-------------------------+
```

Theme selection:

```text
MRUN_MONITOR_THEME=dark   force dark-background palette
MRUN_MONITOR_THEME=light  force light-background palette
COLORFGBG                 auto-detect when the terminal exports it
fallback                  dark-background palette
```

Panel clipping, padding, and table-header styling are ANSI-aware, so token
colors and pane colors do not corrupt panel widths or borders.

Pretty JSON is scrollable. The monitor tracks a separate pretty-scroll offset,
so `j`/Down and `k`/Up move through long slow-operation JSON without changing
the highlighted raw log line underneath. `p` exits back to the same raw line,
and `y` still copies the original raw log entry.

## Collection-scan fault injection

The branch includes a local workload helper for exercising `mrun --monitor`
against a running replica set:

```bash
uv run python mrun/fault_inject_collection_scans.py
```

The default target is:

```text
mongodb://localhost:27017/?replicaSet=rs0
database:   mrun_fault_injection
collection: collection_scans
comment:    mrun-monitor-fault-scan
```

The helper is intentionally not wired into the `mrun` command surface. It is a
test utility for reviewers and troubleshooting sessions. It uses PyMongo, which
is already a mongorun dependency, and adds no external library.

Safety behavior:

```text
+----------------------+----------------------------------------------+
| Guard                | Behavior                                     |
+----------------------+----------------------------------------------+
| Local URI default    | localhost replica set only                   |
| Non-local URI        | rejected unless --allow-nonlocal is supplied |
| Data target          | dedicated mrun_fault_injection collection    |
| Cleanup              | collection drop only when --cleanup is used  |
| Profiling            | restored in a finally block after the run    |
+----------------------+----------------------------------------------+
```

Runtime flow:

```mermaid
sequenceDiagram
    participant User
    participant Monitor as mrun --monitor
    participant Injector as fault injector
    participant Mongo as local replica set

    User->>Monitor: start monitor and select logs
    User->>Injector: uv run python mrun/fault_inject_collection_scans.py --profile
    Injector->>Mongo: ping
    Injector->>Mongo: seed dedicated collection if needed
    Injector->>Mongo: profile 2 slowms 0
    Injector->>Mongo: repeated unindexed find(...).comment(...)
    Mongo-->>Monitor: COMMAND slow query log lines
    Monitor-->>User: colored live tail and metric changes
    Injector->>Mongo: restore previous profiler setting
    Injector->>Mongo: optional drop collection with --cleanup
```

Data path:

```text
seed documents
    |
    +-- fields intentionally have no index except MongoDB's _id
        |
        v
worker threads
    |
    +-- find({"scan_probe": "missing-worker-N-iteration-M"})
    |   |
    |   +-- no scan_probe index exists
    |   +-- query includes comment "mrun-monitor-fault-scan ..."
    v
mongod
    |
    +-- logs slow COMMAND entries when --profile sets slowms=0
    v
mrun --monitor log pane
```

Useful commands:

```bash
uv run python mrun/fault_inject_collection_scans.py --dry-run

uv run python mrun/fault_inject_collection_scans.py \
  --profile \
  --duration 120 \
  --workers 3 \
  --docs 10000 \
  --payload-bytes 2048

uv run python mrun/fault_inject_collection_scans.py \
  --uri "mongodb://monitoruser:monitorpass@localhost:27017/?authSource=admin&replicaSet=rs0" \
  --profile \
  --duration 60
```

Use `--cleanup` when the test collection should be dropped after the workload.
The cleanup path only drops the configured collection, not the database or any
other user collection.

Manual monitor test flow:

```text
1. Start or reuse a local mongorun replica set.
2. In terminal A, run uv run mrun --monitor.
3. Select all MongoDB logs when prompted.
4. In terminal B, run the injector with --dry-run and confirm the target.
5. Run the injector with --profile for 60-120 seconds.
6. In the monitor, verify CPU/network activity rises on the target port.
7. Verify log rows include mrun-monitor-fault-scan.
8. Press /, enter slowop, and verify only slow-operation rows remain.
9. Press p on a filtered structured log row and verify syntax-colored Pretty JSON.
10. Press c to clear the filter.
11. Press Space to pause/resume, g to jump latest, and y to yank a line.
12. After the injector exits, verify the profiler restore message was printed.
```

If logs do not show the injected operations, first confirm that `--profile` was
used, the monitor selected the correct log files, and the URI points at the
same local replica set that `mrun --monitor` is tailing.

## Keyboard controls

```text
+------------+-----------------------------------------------------+
| Key        | Action                                              |
+------------+-----------------------------------------------------+
| q          | Quit monitor                                        |
| Ctrl+C     | Quit monitor                                        |
| r          | Reselect logs                                       |
| a          | Toggle mrun-managed/all process scope               |
| Tab        | Focus next pane                                     |
| Shift+Tab  | Focus previous pane                                 |
| z          | Toggle full-screen zoom for focused pane            |
| CPU Up/k   | Select previous MongoDB process                     |
| CPU Down/j | Select next MongoDB process                         |
| CPU t      | Toggle selected-process thread view                 |
| o          | Toggle right activity pane between logs/currentOp   |
| O          | Toggle formatted/raw currentOp while currentOp shown|
| CurrentOp L| Select currentOp top-N limit                       |
| CurrentOp r| Select currentOp source nodes                      |
| CurrentOp Space| Pause or resume currentOp sampling              |
| CurrentOp n| Select/type currentOp namespace filter             |
| CurrentOp c| Clear currentOp namespace filter                   |
| CurrentOp Up/k| Move highlighted currentOp row                  |
| CurrentOp Down/j| Move highlighted currentOp row                |
| CurrentOp p| Toggle Pretty JSON for highlighted currentOp       |
| CurrentOp y| Yank highlighted currentOp through OSC 52          |
| Logs Up/k  | Move cursor up; scroll only at visible edge         |
| Logs Down/j| Move cursor down; scroll only at visible edge       |
| Logs g     | Jump to newest log line and resume live-follow      |
| Logs p     | Pretty-print highlighted JSON with syntax colors    |
| Logs y     | Yank highlighted raw line through OSC 52            |
| Logs Space | Pause or resume log streaming                       |
| Logs /     | Open log filter prompt                              |
| Logs c     | Clear active log filter                             |
| Filter Enter| Apply typed log filter                             |
| Filter Esc | Cancel log filter prompt                            |
| Pretty Up/k| Scroll expanded Pretty JSON up                      |
| Pretty Dn/j| Scroll expanded Pretty JSON down                    |
| E          | Toggle expanded server status view                  |
| M          | Launch mongosh administration shell                 |
| s          | Cycle refresh interval: 1s -> 5s -> 10s -> 1s       |
+------------+-----------------------------------------------------+
```

Arrow keys are parsed directly from common terminal escape sequences:

```text
CSI arrows:         ESC [ A / ESC [ B
Application arrows: ESC O A / ESC O B
Modified arrows:    ESC [ 1 ; 2 A / ESC [ 1 ; 5 B
Shift+Tab:          ESC [ Z
```

On POSIX terminals, the monitor reads key bytes with `os.read()` from the tty
file descriptor. This avoids Python text-buffering behavior where the first ESC
byte of an arrow sequence can be read while the remaining `[A` or `[B` bytes are
left in the text wrapper buffer. Without fd-level reads, arrow keys can feel
slower or fail while single-byte `j` and `k` still work.

## Log interaction states

```mermaid
stateDiagram-v2
    [*] --> Following
    Following --> PausedFollow: logs Up or k
    PausedFollow --> Following: logs Down/j reaches newest
    PausedFollow --> Following: logs g
    Following --> StreamPaused: logs Space
    PausedFollow --> StreamPaused: logs Space
    StreamPaused --> Following: logs Space and follow_tail true
    StreamPaused --> PausedFollow: logs Space and follow_tail false
    Following --> FilterPrompt: logs /
    PausedFollow --> FilterPrompt: logs /
    FilterPrompt --> Filtered: Enter with query
    FilterPrompt --> Following: Enter empty or Esc
    Filtered --> FilterPrompt: logs /
    Filtered --> Following: logs c
    Filtered --> Filtered: new tailed lines are filtered live
    Following --> PrettyJSON: logs p on JSON line
    PausedFollow --> PrettyJSON: logs p on JSON line
    Filtered --> PrettyJSON: logs p on filtered JSON line
    PrettyJSON --> PrettyJSON: logs j/k/arrows scroll JSON
    PrettyJSON --> PausedFollow: logs p again without filter
    PrettyJSON --> Filtered: logs p again with filter
    Following --> Zoomed: logs focused and z
    PausedFollow --> Zoomed: logs focused and z
    Zoomed --> Following: z when follow_tail true
    Zoomed --> PausedFollow: z when follow_tail false
```

## Failure behavior

Default mrun-managed mode:

```text
No running mongorun-managed MongoDB processes found.
Start nodes with mrun first, or run: mrun --monitor --all
```

All-process mode:

```text
No running mongod or mongos processes found.
Start MongoDB nodes first, then run: mrun --monitor
```

Missing logs:

```text
27018 | log unavailable: /path/to/mongod.log
```

Unavailable network counters:

```text
PORT   ROLE              IN       OUT      REQ/s   STATUS
27018  unknown           -        -        -       unavailable
```

Auth-enabled deployment without usable monitor credentials:

```text
PORT   ROLE              IN       OUT      REQ/s   STATUS
27018  Password Required -        -        -       auth required

PORT   PID      ROLE               PROCESS  CPU%   STATUS
27018  86116    Password Required  mongod    0.0   running

currentOp unavailable: Password Required
```

The `M` mongosh shell handoff uses the same credential check. If credentials
are required but unavailable, it reports:

```text
mongosh requires credentials for this deployment
```

If the executable is missing, it reports:

```text
mongosh not found in PATH
```

Restricted process-list environment:

```text
mrun --monitor could not list local processes: permission denied
```

Missing or unreadable disk paths are reported as unavailable in the disk panel,
without terminating the monitor.

## Anomaly resolution traceability

```text
+-------------------+--------+-----------------------------------+---------+-------------+
| ID                | Source | Summary                           | Commit  | Status      |
+-------------------+--------+-----------------------------------+---------+-------------+
| FM-MON-BASE-001  | user   | initial monitor dashboard         | 6dd5ff2 | Implemented |
| FM-MON-CLI-001   | A3     | monitor flag order routing        | 1266878 | Implemented |
| FM-MON-CLI-002   | A2     | monitor unknown argument handling | 1266878 | Implemented |
| FM-MON-PROC-001  | A4     | process-list permission handling  | efebf01 | Implemented |
| FM-MON-AUTH-001  | A1,A6  | load auth metadata                | d9f3060 | Implemented |
| FM-MON-AUTH-002  | A1     | pass auth to NetworkSampler       | d9f3060 | Implemented |
| FM-MON-AUTH-003  | A1,A6  | auth required network status      | d9f3060 | Implemented |
| FM-MON-AUTH-004  | A2,A6  | monitor credential overrides      | c8376cc | Implemented |
| FM-MON-TLS-001   | A5     | TLS/SSL network kwargs            | d1454f0 | Implemented |
| FM-MON-DOC-001   | all    | traceable auth/TLS anomaly docs   | 264f6da | Implemented |
| FM-MON-PANE-001  | user   | pane focus and focused zoom       | f354587 | Implemented |
| FM-MON-CPU-001   | user   | CPU process selection             | f354587 | Implemented |
| FM-MON-CPU-002   | user   | toggled CPU thread view           | f354587 | Implemented |
| FM-MON-QA-001    | user   | thread view anomaly report        | 52a8234 | Implemented |
| FM-MON-THREAD-001| user   | denied thread details show count  | fbd7996 | Implemented |
| FM-MON-PERF-001  | user   | fast cursor redraws               | 95ea616 | Implemented |
| FM-MON-RENDER-001| user   | cached disk metrics NameError fix | b095c9b | Implemented |
| FM-MON-KEY-001   | user   | arrow keys use fd-level reads     | 5ca41d5 | Implemented |
| FM-MON-PRETTY-001| user   | syntax-colored Pretty JSON view   | 0a6d5de | Implemented |
| FM-MON-FAULT-001 | user   | collection-scan fault injector    | a626f03 | Implemented |
| FM-MON-CPU-003   | user   | CPU sampler preserves psutil state| ba7f8e3 | Implemented |
| FM-MON-PRETTY-002| user   | scrollable Pretty JSON log view   | f42c9ec | Implemented |
| FM-MON-STATUS-001| user   | expanded serverStatus subsystems  | c8553aa | Implemented |
| FM-MON-FILTER-001| user   | fuzzy/structured log filtering    | b2f6f61 | Implemented |
| FM-MON-UI-001    | user   | pane header padding/style         | 56fa23c | Implemented |
| FM-MON-ROLE-001  | user   | primary/secondary role column     | 1c64657 | Implemented |
| FM-MON-OP-001    | user   | initial top 10 currentOp view     | 1c64657 | Implemented |
| FM-MON-LAYOUT-001| user   | two-column metric/activity layout | e671c7d | Implemented |
| FM-MON-ROLE-002  | user   | role column across metric panes   | e671c7d | Implemented |
| FM-MON-OP-002    | user   | formatted/raw right currentOp pane| e671c7d | Implemented |
| FM-MON-LOG-002   | user   | independent log cursor viewport   | e671c7d | Implemented |
| FM-MON-UI-002    | user   | neutral borders and clipped footer| e671c7d | Implemented |
| FM-MON-OP-003    | user   | BSON-safe raw currentOp rendering | e02d83d | Implemented |
| FM-MON-OP-004    | user   | currentOp namespace selector      | e02d83d | Implemented |
| FM-MON-UI-003    | user   | consistent left metric row padding| e02d83d | Implemented |
| FM-MON-OP-005    | user   | currentOp namespace hotkeys       | 0100ea8 | Implemented |
| FM-MON-UI-004    | user   | shared aligned table formatter    | 8869e65 | Implemented |
| FM-MON-ROLE-003  | user   | muted non-header role colors      | 8869e65 | Implemented |
| FM-MON-OP-006    | user   | currentOp Pretty JSON view        | 8869e65 | Implemented |
| FM-MON-OP-007    | user   | currentOp yank/copy support       | 8869e65 | Implemented |
| FM-MON-OP-008    | user   | configurable currentOp top-N limit| 2b4e2a3 | Implemented |
| FM-MON-OP-009    | user   | currentOp source node selector    | 9f7cb69 | Implemented |
| FM-MON-OP-010    | user   | currentOp sampling pause/resume   | 9f7cb69 | Implemented |
| FM-MON-SHELL-001 | user   | mongosh admin shell handoff       | 1733a0c | Implemented |
| FM-MON-SHELL-002 | user   | mongosh auth/TLS secure argv      | 1733a0c | Implemented |
| FM-MON-COMPAT-001| user   | role fallback via hello/isMaster  | b362ef7 | Implemented |
| FM-MON-COMPAT-002| user   | currentOp command shape fallback  | b362ef7 | Implemented |
| FM-MON-COMPAT-003| user   | tolerant serverStatus parsing     | b362ef7 | Implemented |
+-------------------+--------+-----------------------------------+---------+-------------+
```

## Feature monitor commit map

```text
+---------+-----------------------------------------------+-------------------+-------------------------------+
| Commit  | Purpose                                       | Trace IDs         | Key files                     |
+---------+-----------------------------------------------+-------------------+-------------------------------+
| 6dd5ff2 | Add base interactive monitor dashboard         | FM-MON-BASE-001   | monitor.py, mrun.py, docs     |
| 1266878 | Fix monitor CLI routing and monitor args       | FM-MON-CLI-001/2  | mrun.py, test_monitor.py      |
| efebf01 | Handle process discovery permission failures   | FM-MON-PROC-001   | monitor.py, test_monitor.py   |
| d9f3060 | Load auth metadata for network sampling        | FM-MON-AUTH-001/3 | monitor.py, test_monitor.py   |
| c8376cc | Add explicit monitor credential overrides      | FM-MON-AUTH-004   | mrun.py, monitor.py, tests    |
| d1454f0 | Propagate TLS/SSL settings to network sampling | FM-MON-TLS-001    | monitor.py, test_monitor.py   |
| 264f6da | Add anomaly traceability docs                  | FM-MON-DOC-001    | feature-monitor-guide.md      |
| f354587 | Add pane focus, CPU selection, thread toggle   | FM-MON-PANE/CPU   | monitor.py, mrun.py, tests    |
| 52a8234 | Document pane focus and thread-view behavior   | FM-MON-QA-001     | docs, anomaly report          |
| fbd7996 | Fall back to thread count when details denied  | FM-MON-THREAD-001 | monitor.py, tests, docs       |
| 95ea616 | Redraw cursor moves from cached samples        | FM-MON-PERF-001   | monitor.py, tests, docs       |
| b095c9b | Fix cached disk metrics render NameError       | FM-MON-RENDER-001 | monitor.py, tests, report     |
| 5ca41d5 | Read arrow escape sequences from tty fd        | FM-MON-KEY-001    | monitor.py, test_monitor.py   |
| 0a6d5de | Colorize Pretty JSON log view                  | FM-MON-PRETTY-001 | monitor.py, test_monitor.py   |
| a626f03 | Add collection-scan fault injector             | FM-MON-FAULT-001  | fault injector, tests         |
| ba7f8e3 | Preserve psutil Process objects for CPU rates  | FM-MON-CPU-003    | monitor.py, test_monitor.py   |
| f42c9ec | Scroll zoomed Pretty JSON log view             | FM-MON-PRETTY-002 | monitor.py, test_monitor.py   |
| c8553aa | Expand serverStatus subsystem view             | FM-MON-STATUS-001 | monitor.py, docs, tests       |
| b2f6f61 | Add fuzzy and structured log filtering         | FM-MON-FILTER-001 | monitor.py, docs, tests       |
| 56fa23c | Align pane headers and content padding         | FM-MON-UI-001     | monitor.py, test_monitor.py   |
| 1c64657 | Add role column and currentOp view             | FM-MON-ROLE/OP    | monitor.py, mrun.py, tests    |
| e671c7d | Rework layout and activity pane                | FM-MON-LAYOUT/OP  | monitor.py, test_monitor.py   |
| e02d83d | Safely render raw currentOps                   | FM-MON-OP/UI      | monitor.py, mrun.py, tests    |
| 0100ea8 | Support currentOp namespace hotkeys            | FM-MON-OP-005     | monitor.py                    |
| 8869e65 | Align monitor tables and prettify currentOps   | FM-MON-UI/OP      | monitor.py, mrun.py, tests    |
| 2b4e2a3 | Support configurable currentOp limits          | FM-MON-OP-008     | monitor.py, mrun.py, tests    |
| 1733a0c | Add mongosh admin shell handoff                | FM-MON-SHELL      | monitor.py, mrun.py, tests    |
| 9f7cb69 | Pause and filter currentOps                    | FM-MON-OP-009/10  | monitor.py, mrun.py, tests    |
| b362ef7 | Support monitor command compatibility          | FM-MON-COMPAT     | monitor.py, docs, tests       |
+---------+-----------------------------------------------+-------------------+-------------------------------+
```

Reading order for reviewers:

```text
1. Start with 6dd5ff2 to understand the dashboard shape.
2. Review 1266878 through d1454f0 for auth, TLS, and process-discovery anomaly fixes.
3. Review f354587 and 52a8234 for pane focus and CPU thread view.
4. Review fbd7996 through f42c9ec for live-testing follow-up fixes.
5. Review a626f03 for the optional local workload helper.
6. Review c8553aa for the expanded serverStatus subsystem UI.
7. Review b2f6f61 for fuzzy and structured log filtering.
8. Review 56fa23c for bold pane titles, colored table headers, and panel padding.
9. Review 1c64657 for the initial ROLE column and currentOp implementation.
10. Review e671c7d for the two-column layout, role columns in all metric
    panes, right-side formatted/raw currentOp activity pane, independent log
    cursor viewport, and neutral clipped panel/footer rendering.
11. Review e02d83d for BSON-safe raw currentOp output, namespace filtering,
    the namespace selector prompt, and left metric row padding.
12. Review 0100ea8 for `n` and `c` currentOp namespace controls while the
    activity pane is active.
13. Review 8869e65 for shared metric/currentOp table alignment, muted role
    colors, currentOp Pretty JSON, and currentOp yank support.
14. Review 2b4e2a3 for `L` currentOp top-N selection, limit validation, and
    sampler/render integration.
15. Review 1733a0c for `M` mongosh shell handoff, target selection, and
    auth/TLS-safe argv construction.
16. Review 9f7cb69 for currentOp source filtering with `r` and currentOp
    sampling pause/resume with Space.
17. Review b362ef7 for role fallback through `hello` / `isMaster`, currentOp
    command fallback shapes, and tolerant serverStatus subsystem parsing.
```

## Testing added by the branch

The focused monitor test module covers:

- CLI routing for `--monitor`.
- `--monitor --all` parsing.
- monitor flag order with `--all`, `--dir`, and `--no-progressbar`.
- monitor-specific rejection of init-only auth flags.
- monitor credential override flags.
- Help text for monitor controls.
- mrun-managed process filtering from `.mrun_startup`.
- all-process discovery.
- process-list permission failures.
- CPU/memory metric helpers.
- CPU process sampler preserving psutil CPU history across refreshes.
- replica-set role extraction from `serverStatus().repl`.
- role fallback from `serverStatus()` to `hello` / `isMaster` for compatible
  MongoDB command surfaces.
- auth-required `Password Required` role display.
- muted role coloring for Primary, Secondary, and Password Required values.
- role columns in CPU, memory, network, and disk formatters.
- shared ANSI-aware table alignment for CPU, memory, network, disk, and
  currentOp rows.
- currentOp active-operation sampling, sorting, and top-N truncation.
- currentOp fallback from rich command shapes to simpler command shapes when a
  supported server rejects optional fields.
- formatted and raw currentOp rendering in the right activity pane.
- BSON-safe raw currentOp rendering for ObjectId-like and datetime-like values.
- BSON-safe Pretty JSON rendering for selected currentOp documents.
- currentOp `p` pretty-toggle behavior and currentOp pretty j/k scrolling.
- currentOp `y` yank behavior for formatted, raw, and pretty modes.
- currentOp top-N limit parsing, prompt handling, title/footer display, key
  action, and sampler integration.
- currentOp source selection parsing for indexes, ports, primary, secondary,
  and all.
- currentOp source prompt rendering with roles.
- currentOp sampler filtering so only selected source ports are queried.
- currentOp Space pause/resume behavior and cached-snapshot reuse while paused.
- currentOp namespace command filtering and client-side result filtering.
- currentOp namespace selector parsing and prompt output.
- serverStatus parsing when optional version-specific subsystems are absent or
  represented by unexpected non-dictionary values.
- mongosh target option construction, target parsing, custom URI prompt,
  TLS-flag mapping, secure auth argv construction, launch success path, missing
  executable status, and missing-credentials status.
- network counter deltas.
- auth metadata loading and auth-required status.
- auth/TLS client kwargs passed to network sampling.
- disk size calculation.
- log tail seeding and polling.
- stream pause/resume behavior.
- arrow escape sequence parsing.
- cursor movement and live-follow.
- `g` jump-to-latest behavior.
- `/` log filter prompt behavior.
- fuzzy log filter scoring and highlighted hit spans.
- structured log filters for `slowop`, `cmd`, `component`, `severity`, `port`,
  and `msg`.
- filtered log cursor movement, yank, and Pretty JSON selection using raw
  buffer indexes.
- independent log cursor movement before the visible log viewport scrolls.
- `p` pretty JSON behavior.
- Pretty JSON syntax coloring and theme selection.
- Pretty JSON scroll offset and j/k navigation.
- ANSI-aware panel clipping for colored Pretty JSON.
- serverStatus sampling for Disk, Network, Storage, and top-level subsystem
  summaries.
- expanded serverStatus rendering, unavailable status errors, and pane boundary
  colors.
- collection-scan injector CLI parsing and dry-run behavior.
- collection-scan injector localhost safety guard.
- collection-scan injector query comments and unindexed filters.
- collection-scan injector profiler restore and cleanup behavior.
- `y` yank behavior.
- severity color detection and rendering.
- selected/yanked color priority.
- bold pane title and table-header rendering.
- pane-colored header rows with stable internal padding.
- left metric formatter padding for memory, network, and disk rows.
- neutral panel borders with colored title/header text.
- dashboard height and footer clipping so the frame does not wrap when the
  terminal font size reduces rows or columns.
- dashboard and zoom rendering.
- pane focus cycling.
- CPU process cursor movement.
- CPU thread view is off by default.
- CPU thread view toggle.
- activity-pane currentOp view is off by default.
- right activity-pane currentOp view toggle.
- raw/formatted currentOp toggle.
- currentOp namespace selection and clear controls.
- thread-count fallback when detailed thread timing is denied.
- fast cached redraws for cursor-only interactions.
- CPU focused-pane zoom.
- log controls remain pane-specific.
- documentation sanity checks.

The verification commands used for this branch are:

```bash
python3 -m py_compile mrun/monitor.py mrun/mrun.py mrun/test/test_monitor.py
python3 -m py_compile mrun/fault_inject_collection_scans.py mrun/test/test_fault_inject_collection_scans.py
uv run --with pytest pytest mrun/test/test_monitor.py
uv run --with pytest pytest mrun/test/test_fault_inject_collection_scans.py
uv run --with pytest pytest
```

## Review checklist

Use this list for manual review:

```text
[ ] mrun --monitor defaults to mrun-managed processes.
[ ] mrun --monitor --all shows all local mongod/mongos processes.
[ ] mrun --all --monitor routes to monitor mode.
[ ] mrun --dir data --monitor routes to monitor mode.
[ ] init-only auth flags are rejected clearly in monitor mode.
[ ] auth-enabled deployments show network rates when credentials are available.
[ ] auth-enabled deployments without credentials show auth required.
[ ] --monitor-username/--monitor-password/--monitor-auth-db override stored credentials.
[ ] a toggles process scope and prompts for log selection again.
[ ] CPU and memory panels show the expected ports and pids.
[ ] CPU process rows include a ROLE column.
[ ] Memory, network, and disk rows include the same ROLE column.
[ ] Replica set nodes show Primary and Secondary role values.
[ ] Primary roles render with muted non-bold green.
[ ] Secondary roles render with muted non-bold amber.
[ ] Password Required roles render with muted non-bold warning color.
[ ] Auth-enabled nodes without monitor credentials show Password Required in the ROLE column.
[ ] CPU percentages update during the fault injector or another CPU stress workload.
[ ] Network panel reports rates or unavailable status.
[ ] Disk panel reports dbpath and log file size.
[ ] Dashboard uses the stacked left metrics column and tall right activity pane.
[ ] Pane borders remain neutral and low-key.
[ ] Pane titles are bold and align within the top border.
[ ] CPU, memory, network, disk, thread, and status table headers are bold and pane-colored.
[ ] Non-empty pane rows have consistent left padding after the border.
[ ] Metric pane columns align cleanly across CPU, memory, network, and disk.
[ ] Footer stays one line and truncates instead of wrapping on narrow terminals.
[ ] Log tail prefixes each line with the MongoDB port.
[ ] Info logs are muted teal.
[ ] Warnings are yellow.
[ ] Errors are red.
[ ] Fatal rows are red inverse.
[ ] Selection remains visible on colored rows.
[ ] yanked rows become green inverse.
[ ] Tab and Shift+Tab cycle focus across panes.
[ ] z toggles full-screen zoom for the focused pane.
[ ] CPU pane j/k or arrows select a MongoDB process row.
[ ] CPU pane t toggles thread view for the selected process.
[ ] o toggles the right activity pane between logs and currentOp.
[ ] O toggles currentOp between formatted and raw document mode.
[ ] CurrentOp L opens the top-N prompt.
[ ] Entering 50 shows a top 50 currentOp title/footer and samples up to 50 ops.
[ ] Invalid currentOp limits are rejected and values above 500 are capped.
[ ] CurrentOp r opens the source selector instead of the log selector.
[ ] CurrentOp source selector accepts primary and samples only the primary port.
[ ] CurrentOp source selector accepts secondary, ports, indexes, all, and Enter.
[ ] CurrentOp source title/footer shows the selected source when not all.
[ ] CurrentOp Space pauses sampling and keeps the current rows stable.
[ ] CurrentOp Space again resumes sampling and refreshes rows on the next tick.
[ ] CurrentOp rows are sorted by SECS descending and show port, role, op, namespace, client, and summary.
[ ] CurrentOp raw mode shows `db.currentOp()`-derived documents.
[ ] CurrentOp raw mode does not crash on ObjectId, Timestamp, datetime, or other BSON-specific values.
[ ] CurrentOp n opens a namespace selector from active currentOp namespaces.
[ ] Typing a namespace manually applies that currentOp namespace filter.
[ ] CurrentOp c clears the namespace filter.
[ ] CurrentOp p opens a syntax-colored Pretty JSON view for the highlighted op.
[ ] CurrentOp j/k scrolls the Pretty JSON document while pretty mode is active.
[ ] CurrentOp y yanks formatted, raw, or pretty text based on the active mode.
[ ] CurrentOp view shows Password Required when auth metadata exists without monitor credentials.
[ ] M opens the mongosh target selector.
[ ] Mongosh primary, selected process, seed list, first process, and custom URI targets work.
[ ] Mongosh handoff returns to monitor after exiting the shell.
[ ] Mongosh handoff reuses monitor auth/TLS flags and does not expose password values in argv.
[ ] Missing mongosh reports a footer status and keeps the dashboard running.
[ ] Auth-enabled deployments without monitor credentials refuse mongosh handoff clearly.
[ ] CPU thread view is not shown by default.
[ ] If detailed thread timing is denied, CPU thread view shows THREAD COUNT.
[ ] Up/down in logs and CPU process-list mode feels immediate.
[ ] Log pane j/k or arrows move the highlighted log cursor.
[ ] Log text stays still while the cursor moves within the visible window.
[ ] Log text scrolls only when the cursor reaches the visible top or bottom edge.
[ ] Logs pane / opens a filter prompt.
[ ] slowop filter shows only slow-operation query log rows.
[ ] cmd:find and cmd:aggregate filters isolate matching command logs.
[ ] component:COMMAND, severity:E, port:27017, and msg:"Slow query" filters work.
[ ] Search/filter hits are highlighted on non-selected rows.
[ ] c clears the active log filter.
[ ] p toggles syntax-colored Pretty JSON view.
[ ] Pretty JSON opens in zoomed logs as before.
[ ] Pretty JSON j/k or arrows scroll long slow-operation JSON.
[ ] Pretty JSON p returns to the selected raw log line.
[ ] MRUN_MONITOR_THEME=dark and MRUN_MONITOR_THEME=light select different palettes.
[ ] E opens the expanded serverStatus view for the selected CPU process.
[ ] Expanded serverStatus shows Disk, Network, Storage, and Other Subsystems.
[ ] Other Subsystems lists compact summaries for top-level serverStatus keys.
[ ] Auth or connection failures show a status error instead of a blank status view.
[ ] Space pauses and resumes log streaming.
[ ] g jumps back to the newest log line.
[ ] fault injector --dry-run prints the local target without connecting.
[ ] fault injector --profile emits mrun-monitor-fault-scan log entries.
[ ] fault injector restores the previous profiler setting after exit.
[ ] fault injector --cleanup drops only the configured test collection.
[ ] q and Ctrl+C exit cleanly.
```

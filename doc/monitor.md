# mrun monitor implementation

This document explains how `mrun monitor` reaches `mrun/monitor.py` and how
the monitor code is organized for review.

## Invocation path

```text
user terminal
    |
    |  mrun monitor
    v
project script entry point
    |
    |  mrun.mrun:main()
    v
MRunTool.run()
    |
    |  argparse parses monitor as a subcommand
    |  default init routing is skipped for explicit subcommands
    v
MRunTool.monitor()
    |
    |  imports Monitor lazily from mrun.monitor
    |  passes self.client as the MongoDB client factory
    v
Monitor.run()
    |
    |  discovers mrun-managed mongod/mongos processes by default
    |  asks which logs to tail
    |  enters the terminal dashboard loop
    v
terminal monitor
```

While the dashboard is running, pressing `M` temporarily leaves the TUI and
launches `mongosh` with inherited stdio. Exiting `mongosh` returns to the
monitor dashboard.

## Module responsibilities

```text
mrun/monitor.py
|
+-- process discovery
|   +-- discover_mongo_processes()
|   +-- discover_mrun_processes()
|   +-- load_mrun_process_specs()
|   +-- process_to_info()
|   +-- parses --port, --logpath, --dbpath from psutil cmdline()
|   +-- parses -f/--config for port, logpath, and dbpath fallback
|   +-- default scope is ports loaded from datadir/.mrun_startup
|   +-- sharded deployments are ordered as mongos, config server, then shards
|   +-- metric panes render sharded section headers instead of a GROUP column
|
+-- process metrics
|   +-- ProcessSampler
|   +-- reads CPU percent, RSS memory, and process status from psutil
|   +-- preserves psutil.Process objects by pid so CPU percent has history
|   +-- keeps raw process CPU and normalized CPU where 100% means all CPUs
|   +-- RoleSampler
|   +-- reads serverStatus().repl role data for Primary/Secondary labels
|   +-- roles are rendered in CPU, memory, network, disk, and currentOp rows
|   +-- role cells use muted non-bold semantic colors, separate from headers
|   +-- ThreadSampler
|   +-- samples psutil Process.threads() only when CPU thread view is toggled
|   +-- computes per-thread CPU from user/system time deltas
|   +-- falls back to Process.num_threads() when thread details are denied
|   +-- CurrentOpSampler
|   +-- samples active currentOp entries only while the activity pane shows currentOp
|   +-- passes an optional namespace filter into currentOp
|   +-- sorts active ops by secs_running and keeps the configured top-N limit
|   +-- default currentOp limit is 10, selectable with L, capped at 500
|   +-- r selects currentOp source nodes by index, port, primary, secondary, or all
|   +-- space pauses/resumes currentOp sampling and reuses the cached snapshot
|
+-- mongosh shell handoff
|   +-- builds target choices from primary, selected process, seed list, first process, and custom URI
|   +-- reuses monitor auth and TLS metadata from .mrun_startup or monitor overrides
|   +-- launches mongosh without shell=True and never places password values in argv
|
+-- network metrics
|   +-- NetworkSampler
|   +-- calls serverStatus().network per MongoDB port
|   +-- converts counter deltas into per-second rates
|
+-- disk metrics
|   +-- read_disk_metrics()
|   +-- calculate_path_size()
|   +-- reports dbpath and logpath size using os.walk/getsize
|
+-- log tailing
|   +-- LogTailer
|   +-- seeds recent lines and polls appended bytes
|   +-- prefixes each line with its MongoDB port
|   +-- read_log_stream() freezes polling while stream pause is active
|
+-- rendering
|   +-- render_dashboard()
|   +-- make_panel()
|   +-- left metric stack plus right log/currentOp activity pane
|   +-- neutral borders, bold pane titles, pane-colored table headers, and left-padded rows
|   +-- CPU, memory, network, disk, and currentOp use one ANSI-aware table formatter
|   +-- ANSI-aware truncation and padding so colors do not shift borders
|   +-- format_log_lines()
|   +-- log cursor has an independent viewport start and scrolls only at edges
|   +-- raw currentOp documents use BSON-safe JSON-like rendering
|   +-- selected currentOp documents can be opened as syntax-colored Pretty JSON
|   +-- selected currentOp rows/documents can be yanked through OSC 52
|   +-- detect_log_severity()
|   +-- fatal/error/warning/info/debug log rows receive severity colors
|   +-- selected log row uses inverse video over severity color
|   +-- yanked log row uses green inverse video
|   +-- pretty JSON view uses token-level syntax colors
|   +-- pretty JSON view keeps a separate scroll offset
|   +-- DashboardSnapshot caches sampled data for fast cursor-only redraws
|
+-- keyboard control
    +-- TerminalController
    +-- q or Ctrl+C quits
    +-- footer action keys are ANSI-highlighted separately from labels
    +-- r reselects logs in log view
    +-- currentOp r opens currentOp source selection
    +-- a toggles mrun-managed/all process scope and shows scope:mrun/scope:all
    +-- Tab and Shift+Tab cycle focused panes
    +-- z zooms the focused pane
    +-- 1-5 toggles CPU, memory, network, disk, and logs pane visibility
    +-- CPU focus: j/k or arrows select a MongoDB process
    +-- CPU focus: C toggles normalized and raw process CPU
    +-- CPU focus: t toggles process-list and selected-process thread views
    +-- logs focus: o toggles the right activity pane between logs and top currentOp views
    +-- O toggles formatted/raw currentOp documents while currentOp is active
    +-- L selects the currentOp top-N limit while currentOp is active
    +-- n opens currentOp namespace selection while currentOp is active
    +-- currentOp c clears the active currentOp namespace filter
    +-- currentOp p toggles Pretty JSON for the highlighted operation
    +-- currentOp y yanks the highlighted operation
    +-- currentOp r opens source selection by role, port, or index
    +-- currentOp space pauses or resumes currentOp sampling
    +-- logs focus: j/k or arrows move the highlighted log row
    +-- currentOp activity focus: j/k or arrows move the highlighted currentOp row
    +-- Pretty JSON focus: j/k or arrows scroll expanded JSON
    +-- logs focus: g jumps to the newest log row and resumes live-follow
    +-- logs focus: p prettifies the highlighted row as JSON
    +-- logs focus: y yanks the highlighted raw log line with OSC 52
    +-- logs focus: space pauses/resumes log streaming or currentOp sampling
    +-- M launches an interactive mongosh administration shell
    +-- s cycles refresh through 1s, 5s, and 10s
```

## Runtime flow

```mermaid
flowchart TD
    A[User runs mrun monitor] --> B[MRunTool.run parses subcommand]
    B --> C{command monitor?}
    C -- yes --> D[MRunTool.monitor]
    C -- no --> E[Normal command dispatch]
    D --> F[Monitor.run]
    F --> G[discover_mongo_processes via psutil]
    G --> G2[filter to datadir .mrun_startup ports unless --all]
    G2 --> H{MongoDB processes found?}
    H -- no --> I[Print no-process message and exit]
    H -- yes --> J[Prompt for log selection]
    J --> K[LogTailer seeds selected logs]
    K --> L[Terminal dashboard loop]
    L --> M[Read process metrics]
    L --> MR[Sample serverStatus roles]
    L --> N[Sample serverStatus network counters]
    L --> O[Poll appended log lines]
    L --> O2[Read dbpath and log file sizes]
    L --> O3{activity pane currentOp active?}
    O3 -- yes --> O4[Sample active currentOp entries]
    M --> P[Render dashboard]
    MR --> P
    N --> P
    O --> P
    O2 --> P
    O4 --> P
    P --> Q{Key pressed?}
    Q -- q or Ctrl+C --> R[Exit monitor]
    Q -- log r --> J
    Q -- a --> J
    Q -- Tab or Shift+Tab --> S[Move pane focus]
    Q -- z --> T[Toggle focused-pane zoom]
    Q -- CPU j/k/arrows --> U[Select MongoDB process]
    Q -- CPU C --> U2[Toggle normalized/raw CPU]
    Q -- CPU t --> V[Toggle selected-process thread view]
    Q -- logs o --> V2[Toggle right activity currentOp view]
    Q -- O --> V3[Toggle formatted/raw currentOp]
    Q -- currentOp L --> V4[Select currentOp top-N limit]
    Q -- currentOp n --> V5[Select currentOp namespace]
    Q -- currentOp c --> V6[Clear currentOp namespace]
    Q -- currentOp p --> V7[Toggle currentOp Pretty JSON]
    Q -- currentOp y --> V8[Yank highlighted currentOp]
    Q -- currentOp r --> V9[Select currentOp source nodes]
    Q -- currentOp space --> V10[Pause or resume currentOp sampling]
    Q -- logs j/k/arrows --> W[Move highlighted row]
    Q -- currentOp j/k/arrows --> W2[Move highlighted currentOp row]
    Q -- logs g --> X[Jump to newest row and follow]
    Q -- logs p --> Y[Toggle pretty JSON view]
    Q -- logs y --> Z[Yank raw highlighted line]
    Q -- logs space --> AA[Pause/resume logs or currentOp sampling]
    Q -- s --> AB[Cycle refresh interval]
    Q -- M --> AC[Launch mongosh shell and return]
    S --> L
    T --> L
    U --> L
    U2 --> L
    V --> L
    V2 --> L
    V3 --> L
    V4 --> L
    V5 --> L
    V6 --> L
    V7 --> L
    V8 --> L
    V9 --> L
    V10 --> L
    W --> L
    W2 --> L
    X --> L
    Y --> L
    Z --> L
    AA --> L
    AB --> L
    AC --> L
    Q -- none --> L
```

## Rendering states

```text
dashboard mode

+ [CPU Usage (normalized)] ++ Log Tail ----------------------+
|  PORT PID ROLE PROCESS ||  info log line                    |  muted teal text
| 27017 123 Primary ...  ||  warning log line                 |  yellow text
+ Memory Usage ---------+| > selected error log line        |  red inverse
| PORT ROLE PID RSS      ||                                  |
+ Network Usage --------+|                                  |
| PORT ROLE IN OUT REQ/s ||                                  |
+ Disk Usage -----------+|                                  |
| PORT ROLE DB LOG STATUS||                                  |
+-----------------------++----------------------------------+

CPU thread mode, toggled with t while CPU is focused

+ [CPU Threads: port 27017 pid 12345] --------+
| PROCESS port 27017 pid 12345 mongod         |
| TID        CPU%    USER     SYSTEM   TOTAL   |
| 456789      12.5   2.10     0.30     2.40    |
+---------------------------------------------+

activity-pane currentOp mode, toggled with o

+ CPU Usage (normalized) -++ [Current Ops (Formatted, top 10)]-+
| PORT PID ROLE PROCESS   || PORT ROLE SECS OP NS CLIENT DESC |
| 27017 123 Primary ...   ||>27017 Primary 12 query app 127.0 |
+ Memory/Network/Disk ----+| 27018 Secondary 4 command admin |
+-------------------------++----------------------------------+

raw currentOp mode, toggled with O while currentOp is active

+ CPU Usage (normalized) -++ [Current Ops (Raw, top 10)] -----+
| PORT PID ROLE PROCESS   || RAW CURRENTOP DOCUMENTS          |
| 27017 123 Primary ...   ||>27017 Primary {"op":"query",...} |
+-------------------------++----------------------------------+

zoom mode

+ [Log Tail: 27017] ---------------------------+
|  normal log line                              |
|> selected error log line                      |  red inverse row
|> yanked log line                              |  green inverse row
+----------------------------------------------+

pretty JSON mode

+ Log Tail: 27017 (Pretty JSON) ---------------+
| {                                             |
|   "msg": "MongoDB log message",               |
|   "attr": {                                   |
|     "port": 27017                             |
|   }                                           |
| }                                             |
+----------------------------------------------+
```

The color is applied only to terminal rendering. Fatal rows use red inverse,
errors use red, warnings use yellow, info rows use muted teal, and debug rows
use dim gray. Selection uses inverse video on top of the severity color, so the
selected row remains visible without making warnings, errors, and info rows
look the same. `y` copies the raw log line, without ANSI escape sequences and
without the visual cursor marker.

## Panel title and table-header styling

All panes are rendered through `make_panel()`. The panel renderer owns three
alignment rules:

```text
---------------- make_panel() ----------------+
| top border title: bold + pane color          |
| table header row: bold + pane color          |
| border characters: neutral terminal color    |
| non-empty content row: one leading cell      |
| clipping/padding: visible width ignores ANSI |
+----------------------------------------------+
```

The metric and currentOp formatters build rows through a shared ANSI-aware
table formatter before the panel is rendered. Header rows carry an internal
style token; `make_panel()` removes that token, applies the pane header color
and bold text, then clips and pads using visible-width helpers. This keeps CPU,
memory, network, disk, currentOp, thread, Pretty JSON, and expanded
server-status panes aligned even when the title, header, or body row contains
color escapes. The role formatter uses muted non-bold semantic colors for
Primary, Secondary, and Password Required without coloring panel borders or
matching the bold header style.

## Pane focus and CPU modes

The dashboard starts with the logs pane focused so log navigation remains
immediately available. `Tab` and `Shift+Tab` cycle focus across:

```text
+-----+--------+---------+------+------+
| CPU | Memory | Network | Disk | Logs |
+-----+--------+---------+------+------+
```

The focused pane uses an emphasized ASCII border. Pressing `z` zooms that
focused pane; pressing `z` again returns to the two-column dashboard layout.

CPU thread view and currentOp view are intentionally not the default. When the
CPU pane is focused, `j`/`k` or the up/down arrows select a MongoDB process row.
The CPU pane defaults to normalized process CPU: the raw `psutil` process
percentage is divided by the host logical CPU count, so 100% means all logical
CPUs. Pressing `C` toggles to raw process CPU, which can exceed 100% on
multi-core hosts, and pressing `C` again returns to normalized mode. Pressing
`t` toggles the CPU pane from the process CPU list to threads for the selected
process. Pressing `o` from the logs pane toggles the right activity pane from
log tail to the active currentOp entries across visible processes. The default
limit is 10. Pressing `L` while currentOp is active opens a top-N prompt that
accepts positive integers and caps large values at 500. Pressing `O` while
currentOp is active toggles formatted rows and raw currentOp documents derived
from `db.currentOp()`. Raw rendering is BSON-safe: ObjectId, Timestamp,
datetime, and other non-JSON values are converted to readable text before
terminal rendering. Pressing `p` opens the highlighted currentOp raw document
as scrollable syntax-colored Pretty JSON; pressing `p` again returns to the
list. Pressing `y` yanks the highlighted currentOp as the active formatted row,
raw JSON, or Pretty JSON document. Pressing `n` while currentOp is active opens
a namespace selector built from active currentOp namespaces, and typed
namespaces are also accepted. Pressing `c` while currentOp is active clears
that namespace filter. Pressing `r` while currentOp is active opens a source
selector that accepts process indexes, ports, `primary`, `secondary`, `all`, or
Enter for all visible processes. Selecting `primary` makes currentOp sampling
run only against the primary node; unselected nodes are not queried for
currentOp until the source filter changes. Pressing Space while currentOp is
active pauses currentOp sampling and keeps the last sampled rows visible;
pressing Space again resumes sampling. Pressing `o` again returns the activity
pane to the log tail.

Selection prompts validate the exact visible choices before returning to the
dashboard. The initial log selector accepts list indexes and displayed ports;
unknown ports, out-of-range indexes, and non-numeric tokens print an invalid
selection message and re-prompt. The currentOp source selector applies the
same strict behavior for indexes and ports while also accepting `primary`,
`secondary`, `all`, or Enter for all visible processes.

Pressing `M` launches a `mongosh` administration shell. The monitor offers
target choices for the detected primary, the selected process, a replica-set
seed list, the first visible process, and a custom URI. Stored auth/TLS
metadata and monitor credential overrides are reused. Password values are not
placed on the command line; the generated argv passes `--password` without a
value so `mongosh` prompts securely. If `mongosh` is missing, or an
auth-enabled deployment requires credentials that monitor does not have, the
footer reports the problem and the dashboard keeps running.

All metric panes include a `ROLE` column. The monitor reads
`serverStatus().repl.stateStr` first and falls back to
`repl.isWritablePrimary`. If an authenticated deployment was created but the
monitor has no usable credentials, the role column shows `Password Required`.
Stored `.mrun_startup` credentials and explicit monitor credential overrides
are passed to role and currentOp sampling.

On macOS, detailed per-thread timing can be denied by the OS `task_for_pid`
security path even when the monitor is launched with `sudo`. In that case, the
thread pane falls back to the available thread count:

```text
PROCESS port 27017 pid 86094 mongod
THREAD COUNT 113
thread details unavailable
```

```mermaid
stateDiagram-v2
    [*] --> ProcessList
    ProcessList --> ProcessList: CPU j/k/arrows select process
    ProcessList --> ThreadView: CPU t
    ProcessList --> CurrentOpView: o
    ThreadView --> ThreadView: refresh selected process threads
    ThreadView --> ThreadView: CPU j/k/arrows select another process
    ThreadView --> ProcessList: CPU t
    CurrentOpView --> RawCurrentOpView: O
    RawCurrentOpView --> CurrentOpView: O
    CurrentOpView --> CurrentOpView: currentOp j/k/arrows select row
    CurrentOpView --> CurrentOpView: currentOp r select source nodes
    CurrentOpView --> PausedCurrentOp: currentOp Space
    PausedCurrentOp --> CurrentOpView: currentOp Space
    RawCurrentOpView --> RawCurrentOpView: currentOp j/k/arrows select row
    RawCurrentOpView --> RawCurrentOpView: currentOp r select source nodes
    RawCurrentOpView --> PausedCurrentOp: currentOp Space
    CurrentOpView --> CurrentOpPretty: p
    RawCurrentOpView --> CurrentOpPretty: p
    CurrentOpPretty --> CurrentOpPretty: currentOp j/k/arrows scroll JSON
    CurrentOpPretty --> CurrentOpView: p
    CurrentOpView --> ProcessList: o
    RawCurrentOpView --> ProcessList: o
```

## Process scope

The default scope is mrun-managed processes. `Monitor.run()` loads
`datadir/.mrun_startup`, reads the stored startup commands, and keeps only
running `mongod` or `mongos` processes with matching ports. `mrun monitor
--all` starts in all-process mode. Pressing `a` toggles between mrun-managed
and all detected local MongoDB processes, then prompts for log selection again.
The footer makes the state explicit with `scope:mrun` or `scope:all` plus an
`a show all` or `a mrun only` action label.

## Tail-follow behavior

The monitor starts in live-follow mode: the highlighted row stays on the newest
log line as the log grows. When the logs pane is focused, scrolling upward with
`k` or the up arrow freezes live-follow on the selected historical line. The
cursor moves independently inside the visible log window; the text only scrolls
when the cursor reaches the visible top or bottom edge. Scrolling downward with
`j` or the down arrow resumes live-follow once the selection reaches the newest
line. The `g` key jumps directly to the newest line and resumes live-follow.

In the logs pane, `p` parses the highlighted raw log line as JSON. If parsing
succeeds, the monitor pauses live-follow, expands the log panel, and renders
indented syntax-colored JSON. Pressing `p` again returns to the raw log-line
view. If the line is not valid JSON, the status footer reports the parse failure
and keeps the raw view.

Pretty JSON keeps the existing default of opening in the zoomed log pane. While
it is active, `j`/Down and `k`/Up scroll the expanded JSON instead of moving
the selected raw log line. `p` returns to the same selected raw line, and `y`
still copies the original raw log entry.

Pretty JSON colors are applied to object keys, string values, numbers,
booleans/null, and punctuation. The monitor chooses a dark or light palette from
`COLORFGBG` when available. A user can force a palette with:

```bash
MRUN_MONITOR_THEME=dark mrun monitor
MRUN_MONITOR_THEME=light mrun monitor
```

In the logs pane, the spacebar pauses or resumes log streaming. Pausing does
not read from the selected log files, so the visible buffer remains frozen.
Resuming polls from the same file offsets and catches up with lines that were
written while paused.

In currentOp mode, the same Space key pauses currentOp sampling instead of log
tailing. The monitor keeps rendering the cached currentOp snapshot, so the
highlighted row and Pretty JSON view remain stable while sampling is paused.

The `s` key cycles the refresh interval:

```text
1s -> 5s -> 10s -> 1s
```

Cursor movement does not resample every metric source. The dashboard keeps a
`DashboardSnapshot` of the last process, network, disk, thread, and log sample.
Cursor-only actions redraw from that snapshot until the next refresh deadline.
This keeps log and CPU arrow-key movement responsive while preserving the
configured sampling interval.

## Failure behavior

If no mrun-managed `mongod` or `mongos` processes are discovered, the monitor
prints:

```text
No running mongorun-managed MongoDB processes found.
Start nodes with mrun first, or run: mrun monitor --all
```

In all-process mode, if no local `mongod` or `mongos` processes are discovered,
the monitor prints:

```text
No running mongod or mongos processes found.
Start MongoDB nodes first, then run: mrun monitor
```

If a process exists but MongoDB does not answer `serverStatus`, the network
panel marks that port as unavailable and keeps the monitor running.

If monitor auth metadata indicates credentials are required but no usable
monitor credentials are available, metric-pane `ROLE` columns show
`Password Required` and the currentOp view reports:

```text
currentOp unavailable: Password Required
```

In the same auth-missing state, the `M` shell handoff refuses to launch and
reports:

```text
mongosh requires credentials for this deployment
```

If `mongosh` is not installed or not on `PATH`, the handoff reports:

```text
mongosh not found in PATH
```
#!/usr/bin/env python3
"""Live terminal monitor for local MongoDB server processes."""

import base64
import json
import os
import select
import shlex
import shutil
import sys
import time
from collections import deque
from dataclasses import dataclass

import psutil

try:
    import msvcrt
except ImportError:
    msvcrt = None

try:
    import termios
    import tty
except ImportError:
    termios = None
    tty = None


NO_PROCESSES_MESSAGE = (
    "No running mongod or mongos processes found.\n"
    "Start MongoDB nodes first, then run: mrun --monitor"
)
NO_MRUN_PROCESSES_MESSAGE = (
    "No running mongorun-managed MongoDB processes found.\n"
    "Start nodes with mrun first, or run: mrun --monitor --all"
)
PROCESS_DISCOVERY_ERROR_MESSAGE = (
    "mrun --monitor could not list local processes: %s"
)
AUTH_REQUIRED_STATUS = "auth required"

ANSI_RESET = "\033[0m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_TEAL = "\033[38;5;44m"
ANSI_DIM = "\033[2m"
ANSI_INVERSE = "\033[7m"
STYLE_SELECTED = "\x00selected\x00"
STYLE_YANKED = "\x00yanked\x00"
STYLE_SEVERITY_FATAL = "\x00severity:fatal\x00"
STYLE_SEVERITY_ERROR = "\x00severity:error\x00"
STYLE_SEVERITY_WARNING = "\x00severity:warning\x00"
STYLE_SEVERITY_INFO = "\x00severity:info\x00"
STYLE_SEVERITY_DEBUG = "\x00severity:debug\x00"
REFRESH_INTERVALS = (1.0, 5.0, 10.0)
ESCAPE_READ_TIMEOUT = 0.03
KEY_POLL_INTERVAL = 0.01
PANE_ORDER = ("cpu", "memory", "network", "disk", "logs")
PANE_TITLES = {
    "cpu": "CPU Usage",
    "memory": "Memory Usage",
    "network": "Network Usage",
    "disk": "Disk Usage",
    "logs": "Log Tail",
}
STYLE_MARKERS = (
    STYLE_SELECTED,
    STYLE_YANKED,
    STYLE_SEVERITY_FATAL,
    STYLE_SEVERITY_ERROR,
    STYLE_SEVERITY_WARNING,
    STYLE_SEVERITY_INFO,
    STYLE_SEVERITY_DEBUG,
)


class ProcessDiscoveryError(RuntimeError):
    """Raised when the monitor cannot enumerate local processes."""


@dataclass
class MongoProcessInfo:
    """Metadata for a discovered local MongoDB server process."""

    pid: int
    name: str
    port: int
    logpath: str
    dbpath: str
    cmdline: list


@dataclass
class MRunProcessSpec:
    """Expected process metadata loaded from a mongorun startup file."""

    port: int
    logpath: str
    dbpath: str
    cmdline: list


@dataclass
class MonitorAuthConfig:
    """Authentication metadata used by monitor network sampling."""

    enabled: bool = False
    username: str = ""
    password: str = ""
    auth_db: str = "admin"
    initial_user: bool = True

    def has_credentials(self):
        if not self.enabled or not self.initial_user or not self.username:
            return False
        return bool(self.password) or self.auth_db == "$external"

    def requires_credentials(self):
        return self.enabled and not self.has_credentials()

    def client_kwargs(self):
        if not self.has_credentials():
            return {}

        kwargs = {
            "username": self.username,
            "authSource": self.auth_db,
        }
        if self.auth_db != "$external":
            kwargs["password"] = self.password
        return kwargs

    def with_overrides(self, username=None, password=None, auth_db=None):
        if username is None and password is None and auth_db is None:
            return self

        return MonitorAuthConfig(
            enabled=True,
            username=username if username is not None else self.username,
            password=password if password is not None else self.password,
            auth_db=auth_db if auth_db is not None else self.auth_db,
            initial_user=True,
        )


@dataclass
class ProcessMetrics:
    """Live process resource metrics."""

    cpu_percent: float
    memory_rss: int
    status: str


@dataclass
class ThreadMetrics:
    """Live per-thread CPU timing for one MongoDB server process."""

    thread_id: int
    cpu_percent: float
    user_time: float
    system_time: float
    total_time: float


@dataclass
class ThreadSnapshot:
    """Thread samples and fallback metadata for one MongoDB server process."""

    metrics: list
    error: str = ""
    thread_count: int = None


@dataclass
class DashboardSnapshot:
    """One sampled dashboard state reused for fast cursor redraws."""

    processes: list
    process_metrics: dict
    network_metrics: dict
    disk_metrics: dict
    log_lines: list
    selected_ports: list
    thread_metrics: list
    thread_error: str = ""
    thread_count: int = None
    sampled_at: float = 0.0


@dataclass
class NetworkMetrics:
    """MongoDB serverStatus network rates."""

    available: bool
    bytes_in_per_sec: float = 0.0
    bytes_out_per_sec: float = 0.0
    requests_per_sec: float = 0.0
    error: str = ""


@dataclass
class DiskMetrics:
    """Disk consumption for a MongoDB dbpath and log file."""

    available: bool
    db_size: int = 0
    log_size: int = 0
    error: str = ""


def _normalize_process_name(name):
    name = os.path.basename(name or "").lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _get_cmdline_arg(cmdline, option):
    for index, arg in enumerate(cmdline):
        if arg == option and index + 1 < len(cmdline):
            return cmdline[index + 1].strip('"')
        if arg.startswith(option + "="):
            return arg.split("=", 1)[1].strip('"')
    return None


def _get_cmdline_int(cmdline, option, default=None):
    value = _get_cmdline_arg(cmdline, option)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def process_to_info(process):
    """Convert a psutil process into MongoProcessInfo, or None if unrelated."""
    try:
        name = _normalize_process_name(process.name())
        if name not in ("mongod", "mongos"):
            return None
        cmdline = process.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return None

    port = _get_cmdline_int(cmdline, "--port", default=27017)
    logpath = _get_cmdline_arg(cmdline, "--logpath") or ""
    dbpath = _get_cmdline_arg(cmdline, "--dbpath") or ""

    return MongoProcessInfo(
        pid=process.pid,
        name=name,
        port=port,
        logpath=logpath,
        dbpath=dbpath,
        cmdline=cmdline,
    )


def load_mrun_startup_config(data_dir):
    """Load datadir/.mrun_startup, returning an empty dict on failure."""
    startup_file = os.path.join(os.path.abspath(data_dir), ".mrun_startup")
    if not os.path.exists(startup_file):
        return {}

    try:
        with open(startup_file, "r") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return {}


def load_mrun_process_specs(data_dir):
    """Load expected mongorun server processes from datadir/.mrun_startup."""
    startup_config = load_mrun_startup_config(data_dir)
    startup_info = startup_config.get("startup_info", {})
    specs = {}
    for port_key, command_str in startup_info.items():
        try:
            cmdline = shlex.split(command_str)
        except ValueError:
            cmdline = str(command_str).split()

        port = _get_cmdline_int(cmdline, "--port")
        if port is None:
            try:
                port = int(port_key)
            except (TypeError, ValueError):
                continue

        specs[port] = MRunProcessSpec(
            port=port,
            logpath=_get_cmdline_arg(cmdline, "--logpath") or "",
            dbpath=_get_cmdline_arg(cmdline, "--dbpath") or "",
            cmdline=cmdline,
        )
    return specs


def load_monitor_auth_config(data_dir):
    """Load monitor auth metadata from datadir/.mrun_startup parsed args."""
    startup_config = load_mrun_startup_config(data_dir)
    parsed_args = startup_config.get("parsed_args", {})
    return MonitorAuthConfig(
        enabled=bool(parsed_args.get("auth")),
        username=parsed_args.get("username") or "",
        password=parsed_args.get("password") or "",
        auth_db=parsed_args.get("auth_db") or "admin",
        initial_user=parsed_args.get("initial-user", True),
    )


def build_monitor_tls_kwargs(parsed_args):
    """Build PyMongo TLS kwargs from stored mongorun parsed args."""
    opts = {}

    tls_server_keys = (
        "tlsMode",
        "tlsCertificateKeyFile",
        "tlsCertificateKeyFilePassword",
        "tlsClusterFile",
        "tlsClusterPassword",
        "tlsDisabledProtocols",
        "tlsAllowConnectionsWithoutCertificates",
        "tlsFIPSMode",
    )
    if any(parsed_args.get(key) for key in tls_server_keys):
        opts["tls"] = True

    tls_client_map = {
        "tlsClientCertificateKeyFile": "tlsCertificateKeyFile",
        "tlsClientCertificateKeyFilePassword": "tlsCertificateKeyFilePassword",
        "tlsCAFile": "tlsCAFile",
        "tlsCRLFile": "tlsCRLFile",
    }
    for source, target in tls_client_map.items():
        value = parsed_args.get(source)
        if value:
            opts["tls"] = True
            opts[target] = value
    if parsed_args.get("tlsAllowInvalidCertificates"):
        opts["tls"] = True
        opts["tlsAllowInvalidCertificates"] = True
    if parsed_args.get("tlsAllowInvalidHostnames"):
        opts["tls"] = True
        opts["tlsAllowInvalidHostnames"] = True

    ssl_server_keys = (
        "sslMode",
        "sslPEMKeyFile",
        "sslPEMKeyPassword",
        "sslClusterFile",
        "sslClusterPassword",
        "sslDisabledProtocols",
        "sslAllowConnectionsWithoutCertificates",
        "sslFIPSMode",
    )
    if any(parsed_args.get(key) for key in ssl_server_keys):
        opts["tls"] = True
        opts["tlsAllowInvalidCertificates"] = True

    ssl_client_map = {
        "sslClientCertificate": "tlsCertificateKeyFile",
        "sslClientPEMKeyFile": "tlsCertificateKeyFile",
        "sslClientPEMKeyPassword": "tlsCertificateKeyFilePassword",
        "sslCAFile": "tlsCAFile",
        "sslCRLFile": "tlsCRLFile",
    }
    for source, target in ssl_client_map.items():
        value = parsed_args.get(source)
        if value:
            opts["tls"] = True
            opts[target] = value
    if parsed_args.get("sslAllowInvalidCertificates"):
        opts["tls"] = True
        opts["tlsAllowInvalidCertificates"] = True
    if parsed_args.get("sslAllowInvalidHostnames"):
        opts["tls"] = True
        opts["tlsAllowInvalidHostnames"] = True

    return opts


def load_monitor_tls_kwargs(data_dir):
    """Load PyMongo TLS kwargs from datadir/.mrun_startup parsed args."""
    startup_config = load_mrun_startup_config(data_dir)
    return build_monitor_tls_kwargs(startup_config.get("parsed_args", {}))


def filter_mrun_processes(processes, specs):
    """Keep only discovered processes that are present in mrun startup specs."""
    filtered = []
    for process in processes:
        spec = specs.get(process.port)
        if spec is None:
            continue
        filtered.append(MongoProcessInfo(
            process.pid,
            process.name,
            process.port,
            process.logpath or spec.logpath,
            process.dbpath or spec.dbpath,
            process.cmdline,
        ))
    return sorted(filtered, key=lambda p: (p.port, p.name, p.pid))


def discover_mongo_processes(process_iter=None):
    """Discover local running mongod/mongos processes without .mrun_startup."""
    if process_iter is None:
        process_iter = psutil.process_iter

    processes = []
    try:
        for process in process_iter():
            info = process_to_info(process)
            if info is not None:
                processes.append(info)
    except PermissionError as exc:
        raise ProcessDiscoveryError(
            PROCESS_DISCOVERY_ERROR_MESSAGE % "permission denied") from exc
    except psutil.Error as exc:
        raise ProcessDiscoveryError(
            PROCESS_DISCOVERY_ERROR_MESSAGE % str(exc)) from exc

    return sorted(processes, key=lambda p: (p.port, p.name, p.pid))


def discover_mrun_processes(data_dir, process_iter=None):
    """Discover running mongod/mongos processes launched by this mrun data dir."""
    specs = load_mrun_process_specs(data_dir)
    if not specs:
        return []
    return filter_mrun_processes(discover_mongo_processes(process_iter), specs)


def read_process_metrics(process_info, process_factory=None):
    """Read CPU and memory metrics for a discovered process."""
    if process_factory is None:
        process_factory = psutil.Process

    try:
        process = process_factory(process_info.pid)
        memory_rss = process.memory_info().rss
        cpu_percent = process.cpu_percent(interval=None)
        status = process.status()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return ProcessMetrics(0.0, 0, "unavailable")

    return ProcessMetrics(cpu_percent, memory_rss, status)


def _thread_field(thread, name, index, default=0.0):
    if hasattr(thread, name):
        return getattr(thread, name)
    try:
        return thread[index]
    except (IndexError, TypeError):
        return default


class ThreadSampler:
    """Sample per-thread CPU deltas for one MongoDB server process."""

    def __init__(self, process_factory=None, clock=None):
        self.process_factory = process_factory or psutil.Process
        self.clock = clock or time.time
        self.previous = {}

    def sample(self, process_info):
        now = self.clock()
        try:
            process = self.process_factory(process_info.pid)
            thread_count = self._read_thread_count(process)
            threads = process.threads()
        except psutil.AccessDenied:
            return ThreadSnapshot(
                [],
                "thread details unavailable",
                self._read_thread_count_by_pid(process_info.pid),
            )
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return ThreadSnapshot([], "process no longer available")

        metrics = []
        live_keys = set()
        for thread in threads:
            thread_id = int(_thread_field(thread, "id", 0, 0))
            user_time = float(_thread_field(thread, "user_time", 1, 0.0))
            system_time = float(_thread_field(thread, "system_time", 2, 0.0))
            total_time = user_time + system_time
            key = (process_info.pid, thread_id)
            live_keys.add(key)
            previous = self.previous.get(key)
            cpu_percent = 0.0
            if previous is not None:
                previous_time, previous_total = previous
                elapsed = max(now - previous_time, 0.001)
                cpu_percent = max(
                    0.0, (total_time - previous_total) / elapsed * 100.0)
            self.previous[key] = (now, total_time)
            metrics.append(ThreadMetrics(
                thread_id=thread_id,
                cpu_percent=cpu_percent,
                user_time=user_time,
                system_time=system_time,
                total_time=total_time,
            ))

        stale_keys = [
            key for key in self.previous
            if key[0] == process_info.pid and key not in live_keys
        ]
        for key in stale_keys:
            del self.previous[key]

        return ThreadSnapshot(
            sorted(metrics, key=lambda item: (-item.cpu_percent, item.thread_id)),
            "",
            thread_count,
        )

    def _read_thread_count_by_pid(self, pid):
        try:
            return self._read_thread_count(self.process_factory(pid))
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            return None

    @staticmethod
    def _read_thread_count(process):
        if not hasattr(process, "num_threads"):
            return None
        return process.num_threads()


def calculate_path_size(path):
    """Calculate path size using only the standard library."""
    if not path:
        return 0, "missing path"
    if os.path.isfile(path):
        try:
            return os.path.getsize(path), ""
        except OSError as exc:
            return 0, str(exc)
    if not os.path.isdir(path):
        return 0, "path unavailable"

    total = 0
    errors = []
    for root, _, files in os.walk(path):
        for filename in files:
            filepath = os.path.join(root, filename)
            try:
                total += os.path.getsize(filepath)
            except OSError as exc:
                errors.append(str(exc))
    return total, "; ".join(errors[:2])


def read_disk_metrics(processes):
    """Read dbpath and logpath disk consumption for each process."""
    metrics = {}
    for process in processes:
        db_size, db_error = calculate_path_size(process.dbpath)
        log_size, log_error = calculate_path_size(process.logpath)
        errors = [error for error in (db_error, log_error) if error]
        metrics[process.port] = DiskMetrics(
            available=not errors,
            db_size=db_size,
            log_size=log_size,
            error="; ".join(errors),
        )
    return metrics


class NetworkSampler:
    """Sample MongoDB serverStatus network counters and expose per-second rates."""

    def __init__(self, client_factory=None, clock=None, client_kwargs=None,
                 auth_required=False):
        self.client_factory = client_factory or self._default_client_factory
        self.clock = clock or time.time
        self.client_kwargs = dict(client_kwargs or {})
        self.auth_required = auth_required
        self.previous = {}

    def sample(self, processes):
        now = self.clock()
        metrics = {}
        for process in processes:
            counters, error = self._read_counters(process.port)
            if counters is None:
                metrics[process.port] = NetworkMetrics(False, error=error)
                continue

            previous = self.previous.get(process.port)
            self.previous[process.port] = (now, counters)
            if previous is None:
                metrics[process.port] = NetworkMetrics(True)
                continue

            previous_time, previous_counters = previous
            elapsed = max(now - previous_time, 0.001)
            metrics[process.port] = NetworkMetrics(
                True,
                bytes_in_per_sec=max(
                    0.0, (counters["bytesIn"] - previous_counters["bytesIn"]) / elapsed),
                bytes_out_per_sec=max(
                    0.0, (counters["bytesOut"] - previous_counters["bytesOut"]) / elapsed),
                requests_per_sec=max(
                    0.0,
                    (counters["numRequests"] - previous_counters["numRequests"]) / elapsed,
                ),
            )
        return metrics

    def _read_counters(self, port):
        if self.auth_required:
            return None, AUTH_REQUIRED_STATUS

        client = None
        try:
            client_kwargs = {
                "directConnection": True,
                "serverSelectionTimeoutMS": 200,
            }
            client_kwargs.update(self.client_kwargs)
            client = self.client_factory(
                "localhost:%i" % port,
                **client_kwargs
            )
            status = client.admin.command("serverStatus")
            network = status.get("network", {})
            counters = {
                "bytesIn": int(network.get("bytesIn", 0)),
                "bytesOut": int(network.get("bytesOut", 0)),
                "numRequests": int(network.get("numRequests", 0)),
            }
            return counters, ""
        except Exception as exc:
            return None, str(exc)
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

    @staticmethod
    def _default_client_factory(host, **kwargs):
        from pymongo import MongoClient

        return MongoClient(host, **kwargs)


class LogTailer:
    """Tail selected log files and retain a bounded in-memory buffer."""

    def __init__(self, logpaths_by_port, max_lines=200):
        self.logpaths_by_port = dict(logpaths_by_port)
        self.max_lines = max_lines
        self.lines = deque(maxlen=max_lines)
        self.offsets = {}
        self.missing_paths = set()
        self._seed()

    def _seed(self):
        for port, path in self.logpaths_by_port.items():
            if not path:
                continue
            try:
                with open(path, "rb") as logfile:
                    recent = deque(logfile, maxlen=20)
                    self.offsets[port] = logfile.tell()
            except OSError:
                self.offsets[port] = 0
                self._append_missing(port, path)
                continue

            for line in recent:
                self._append_line(port, line)

    def poll(self):
        for port, path in self.logpaths_by_port.items():
            if not path:
                continue
            try:
                with open(path, "rb") as logfile:
                    logfile.seek(0, os.SEEK_END)
                    end = logfile.tell()
                    offset = self.offsets.get(port, 0)
                    if offset > end:
                        offset = 0
                    logfile.seek(offset)
                    for line in logfile:
                        self._append_line(port, line)
                    self.offsets[port] = logfile.tell()
            except OSError:
                self._append_missing(port, path)
        return list(self.lines)

    def _append_missing(self, port, path):
        marker = (port, path)
        if marker in self.missing_paths:
            return
        self.missing_paths.add(marker)
        self.lines.append("%s | log unavailable: %s" % (port, path))

    def _append_line(self, port, line):
        text = line.decode("utf-8", "replace").rstrip()
        self.lines.append("%s | %s" % (port, text))


def read_log_stream(tailer, stream_paused):
    """Return visible log lines, optionally without advancing file offsets."""
    if stream_paused:
        return list(tailer.lines)
    return tailer.poll()


def parse_log_selection(selection, candidates):
    """Return selected ports from a comma/space separated index or port list."""
    if selection is None or selection.strip() == "" or selection.strip().lower() == "all":
        return [candidate.port for candidate in candidates]

    selected_ports = []
    tokens = [token.strip() for token in selection.replace(",", " ").split()]
    for token in tokens:
        try:
            value = int(token)
        except ValueError:
            continue

        if 1 <= value <= len(candidates):
            port = candidates[value - 1].port
        else:
            port = value

        if port in [candidate.port for candidate in candidates] and port not in selected_ports:
            selected_ports.append(port)

    return selected_ports


def choose_logpaths(processes, input_func=input, stdout=None):
    """Prompt the user to choose log files to tail."""
    stdout = stdout or sys.stdout
    candidates = [process for process in processes if process.logpath]
    if not candidates:
        stdout.write("No --logpath values found; log tail quadrant will be empty.\n")
        stdout.flush()
        return {}

    stdout.write("\nSelect MongoDB logs to tail:\n")
    for index, process in enumerate(candidates, start=1):
        stdout.write("  [%i] %s port %s pid %s  %s\n" % (
            index, process.name, process.port, process.pid, process.logpath))
    stdout.write("Enter indexes or ports separated by commas, or press Enter for all: ")
    stdout.flush()

    selection = input_func()
    selected_ports = parse_log_selection(selection, candidates)
    return {
        process.port: process.logpath
        for process in candidates
        if process.port in selected_ports
    }


def format_bytes(value):
    value = float(value)
    units = ["B", "KB", "MB", "GB", "TB"]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            if unit == "B":
                return "%i%s" % (value, unit)
            return "%.1f%s" % (value, unit)
        value /= 1024.0


def format_rate(value):
    return format_bytes(value) + "/s"


def network_status_label(network):
    if network.error == AUTH_REQUIRED_STATUS:
        return AUTH_REQUIRED_STATUS
    return "unavailable"


def _truncate(text, width):
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "~"


def _styled_line(styles, text):
    if isinstance(styles, str):
        styles = [styles]
    return "".join(styles) + text


def _split_style(text):
    styles = []
    if not isinstance(text, str):
        return styles, text

    matched = True
    while matched:
        matched = False
        for marker in STYLE_MARKERS:
            if text.startswith(marker):
                styles.append(marker)
                text = text[len(marker):]
                matched = True
                break
    return styles, text


def _style_ansi(styles):
    if STYLE_YANKED in styles:
        return ANSI_GREEN + ANSI_INVERSE

    color = ""
    if STYLE_SEVERITY_FATAL in styles:
        color = ANSI_RED + ANSI_INVERSE
    elif STYLE_SEVERITY_ERROR in styles:
        color = ANSI_RED
    elif STYLE_SEVERITY_WARNING in styles:
        color = ANSI_YELLOW
    elif STYLE_SEVERITY_INFO in styles:
        color = ANSI_TEAL
    elif STYLE_SEVERITY_DEBUG in styles:
        color = ANSI_DIM

    if STYLE_SELECTED in styles:
        return color + ANSI_INVERSE
    return color


def clamp_log_cursor(log_lines, cursor, follow_tail):
    """Return a valid highlighted log-line index."""
    if not log_lines:
        return None
    if follow_tail or cursor is None:
        return len(log_lines) - 1
    return max(0, min(cursor, len(log_lines) - 1))


def clamp_optional_log_cursor(log_lines, cursor):
    """Return a valid optional log-line index without defaulting to tail."""
    if not log_lines or cursor is None:
        return None
    return max(0, min(cursor, len(log_lines) - 1))


def move_log_cursor(log_lines, cursor, delta):
    """Move the highlighted log-line index by delta."""
    if not log_lines:
        return None
    cursor = clamp_log_cursor(log_lines, cursor, follow_tail=False)
    return max(0, min(cursor + delta, len(log_lines) - 1))


def normalize_pane(pane):
    """Return a valid monitor pane name."""
    return pane if pane in PANE_ORDER else "logs"


def next_pane(current_pane, delta=1):
    """Move focus through dashboard panes."""
    current_pane = normalize_pane(current_pane)
    try:
        index = PANE_ORDER.index(current_pane)
    except ValueError:
        index = PANE_ORDER.index("logs")
    return PANE_ORDER[(index + delta) % len(PANE_ORDER)]


def clamp_process_cursor(processes, cursor):
    """Return a valid highlighted process index."""
    if not processes:
        return None
    if cursor is None:
        return 0
    return max(0, min(cursor, len(processes) - 1))


def move_process_cursor(processes, cursor, delta):
    """Move the highlighted process index by delta."""
    if not processes:
        return None
    cursor = clamp_process_cursor(processes, cursor)
    return max(0, min(cursor + delta, len(processes) - 1))


def selected_process(processes, cursor):
    """Return the selected process for a cursor index."""
    cursor = clamp_process_cursor(processes, cursor)
    if cursor is None:
        return None
    return processes[cursor]


def dashboard_snapshot_due(snapshot, now, next_sample_at, force_sample=False):
    """Return True when dashboard samplers should run again."""
    return force_sample or snapshot is None or now >= next_sample_at


def next_refresh_interval(current_interval):
    """Cycle the monitor refresh interval through supported values."""
    try:
        index = REFRESH_INTERVALS.index(float(current_interval))
    except ValueError:
        return REFRESH_INTERVALS[0]
    return REFRESH_INTERVALS[(index + 1) % len(REFRESH_INTERVALS)]


def format_seconds(seconds):
    """Format a refresh interval for display."""
    if float(seconds).is_integer():
        return "%is" % int(seconds)
    return "%.1fs" % seconds


def _visible_log_window(log_lines, cursor, height):
    height = max(height, 1)
    if not log_lines:
        return [], 0

    cursor = clamp_log_cursor(log_lines, cursor, follow_tail=False)
    start = max(0, cursor - height + 1)
    if cursor < start:
        start = cursor
    end = min(len(log_lines), start + height)
    return log_lines[start:end], start


def format_log_lines(log_lines, cursor, height, yanked_cursor=None):
    """Format log lines with a highlighted cursor marker."""
    visible, start = _visible_log_window(log_lines, cursor, height)
    formatted = []
    for offset, line in enumerate(visible):
        line_index = start + offset
        marker = ">" if line_index == cursor else " "
        text = "%s %s" % (marker, line)
        styles = []
        detected_style = severity_style(detect_log_severity(line))
        if detected_style:
            styles.append(detected_style)
        if line_index == yanked_cursor:
            styles.append(STYLE_YANKED)
        elif line_index == cursor:
            styles.append(STYLE_SELECTED)
        if styles:
            text = _styled_line(styles, text)
        formatted.append(text)
    return formatted


def strip_log_port_prefix(line):
    """Remove the monitor-added 'port | ' prefix from a log line."""
    prefix, separator, body = str(line).partition(" | ")
    if separator and prefix.strip().isdigit():
        return body
    return str(line)


def _json_log_candidates(text):
    text = str(text).strip()
    candidates = [text]
    object_start = text.find("{")
    if object_start > 0:
        candidates.append(text[object_start:])
    return candidates


def _severity_from_value(value):
    value = str(value or "").strip().lower()
    if value in ("f", "fatal", "critical"):
        return "fatal"
    if value in ("e", "error", "err"):
        return "error"
    if value in ("w", "warn", "warning"):
        return "warning"
    if value in ("i", "info", "information", "informational"):
        return "info"
    if value in ("d", "debug", "trace"):
        return "debug"
    return ""


def detect_log_severity(line):
    """Detect MongoDB log severity from JSON fields or plain text."""
    text = strip_log_port_prefix(line).strip()
    for candidate in _json_log_candidates(text):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            for key in ("s", "severity", "level"):
                severity = _severity_from_value(parsed.get(key))
                if severity:
                    return severity

    lowered = text.lower()
    if "fatal" in lowered or "critical" in lowered:
        return "fatal"
    if "error" in lowered or " err " in (" " + lowered + " "):
        return "error"
    if "warning" in lowered or " warn " in (" " + lowered + " "):
        return "warning"
    if "debug" in lowered or " trace " in (" " + lowered + " "):
        return "debug"
    if "information" in lowered or " info " in (" " + lowered + " "):
        return "info"
    return ""


def severity_style(severity):
    return {
        "fatal": STYLE_SEVERITY_FATAL,
        "error": STYLE_SEVERITY_ERROR,
        "warning": STYLE_SEVERITY_WARNING,
        "info": STYLE_SEVERITY_INFO,
        "debug": STYLE_SEVERITY_DEBUG,
    }.get(severity)


def prettify_log_line(line):
    """Return indented JSON lines for a raw log line, or None when invalid."""
    text = strip_log_port_prefix(line).strip()
    for candidate in _json_log_candidates(text):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        return json.dumps(parsed, indent=2).splitlines()

    return None


def format_pretty_log_lines(pretty_lines, height):
    """Format a fixed pretty JSON view for the log panel."""
    if not pretty_lines:
        return []
    return list(pretty_lines[:max(height, 1)])


def build_osc52_sequence(text):
    """Build an OSC 52 clipboard escape sequence for terminal clipboard yank."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\033]52;c;%s\a" % payload


def _footer(status_message, controls):
    return "%s | %s" % (status_message, controls) if status_message else controls


def make_panel(title, lines, width, height, focused=False):
    """Render one bordered panel with clipped content."""
    if width < 4 or height < 3:
        return [" " * max(width, 0) for _ in range(max(height, 0))]

    inner_width = width - 2
    inner_height = height - 2
    border_char = "=" if focused else "-"
    if focused:
        title = "[%s]" % title
    title = " %s " % title
    border = "+" + _truncate(title, inner_width).ljust(inner_width, border_char) + "+"
    rows = [border]

    for index in range(inner_height):
        text = lines[index] if index < len(lines) else ""
        styles, text = _split_style(text)
        row_text = _truncate(text, inner_width).ljust(inner_width)
        ansi = _style_ansi(styles)
        if ansi:
            rows.append(ansi + "|" + row_text + "|" + ANSI_RESET)
        else:
            rows.append("|" + row_text + "|")

    rows.append("+" + border_char * inner_width + "+")
    return rows


def format_cpu_lines(processes, process_metrics, cursor=None, show_cursor=False):
    """Format CPU process rows, optionally marking the selected process."""
    lines = ["  PORT   PID      PROCESS  CPU%   STATUS"]
    selected_index = clamp_process_cursor(processes, cursor)
    if not processes:
        lines.append("  No MongoDB processes found.")
        return lines

    for index, process in enumerate(processes):
        metrics = process_metrics.get(
            process.pid, ProcessMetrics(0.0, 0, "unavailable"))
        marker = ">" if show_cursor and index == selected_index else " "
        text = "%s %-6s %-8s %-8s %5.1f  %s" % (
            marker,
            process.port,
            process.pid,
            process.name,
            metrics.cpu_percent,
            metrics.status,
        )
        if show_cursor and index == selected_index:
            text = _styled_line(STYLE_SELECTED, text)
        lines.append(text)
    return lines


def format_memory_lines(processes, process_metrics):
    """Format memory rows for process RSS usage."""
    lines = ["PORT   PID      PROCESS  RSS"]
    if not processes:
        lines.append("No MongoDB processes found.")
        return lines

    for process in processes:
        metrics = process_metrics.get(
            process.pid, ProcessMetrics(0.0, 0, "unavailable"))
        lines.append("%-6s %-8s %-8s %s" % (
            process.port, process.pid, process.name,
            format_bytes(metrics.memory_rss)))
    return lines


def format_network_lines(processes, network_metrics):
    """Format MongoDB network counter rates."""
    lines = ["PORT   IN       OUT      REQ/s   STATUS"]
    if not processes:
        lines.append("No MongoDB processes found.")
        return lines

    for process in processes:
        network = network_metrics.get(process.port, NetworkMetrics(False))
        if network.available:
            lines.append("%-6s %-8s %-8s %-7.1f ok" % (
                process.port,
                format_rate(network.bytes_in_per_sec),
                format_rate(network.bytes_out_per_sec),
                network.requests_per_sec,
            ))
        else:
            lines.append("%-6s %-8s %-8s %-7s %s" % (
                process.port, "-", "-", "-", network_status_label(network)))
    return lines


def format_disk_lines(processes, disk_metrics):
    """Format dbpath and logpath disk consumption."""
    lines = ["PORT   DB SIZE   LOG SIZE  STATUS"]
    if not processes:
        lines.append("No MongoDB processes found.")
        return lines

    for process in processes:
        disk = disk_metrics.get(process.port, DiskMetrics(False))
        if disk.available:
            lines.append("%-6s %-9s %-9s ok" % (
                process.port,
                format_bytes(disk.db_size),
                format_bytes(disk.log_size),
            ))
        else:
            lines.append("%-6s %-9s %-9s unavailable" % (
                process.port,
                format_bytes(disk.db_size),
                format_bytes(disk.log_size),
            ))
    return lines


def format_thread_lines(process, thread_metrics, thread_error="",
                        thread_count=None):
    """Format per-thread timing rows for the selected process."""
    if process is None:
        return ["No MongoDB process selected."]

    lines = [
        "PROCESS port %s pid %s %s" % (
            process.port, process.pid, process.name),
    ]
    if thread_count is not None:
        lines.append("THREAD COUNT %s" % thread_count)
    if thread_error:
        lines.append(thread_error)
        return lines

    lines.append("TID        CPU%    USER     SYSTEM   TOTAL")
    if not thread_metrics:
        lines.append("No thread samples available yet.")
        return lines

    for thread in thread_metrics:
        lines.append("%-10s %5.1f   %-8.2f %-8.2f %-8.2f" % (
            thread.thread_id,
            thread.cpu_percent,
            thread.user_time,
            thread.system_time,
            thread.total_time,
        ))
    return lines


def _footer_controls(focused_pane, zoom_pane, refresh_interval, pretty_active,
                     stream_paused, process_scope, cpu_thread_view):
    focused_pane = normalize_pane(focused_pane)
    stream_control = (
        "space resume stream" if stream_paused else "space pause stream")
    scope_label = "scope %s" % process_scope
    scope_toggle = "a %s" % ("mrun only" if process_scope == "all" else "all")
    zoom_control = "z quadrants" if zoom_pane else "z zoom focus"

    controls = [
        "q/Ctrl+C quit",
        "Tab pane",
        zoom_control,
        "r reselect",
        scope_label,
        scope_toggle,
        "s refresh %s" % format_seconds(refresh_interval),
    ]

    if focused_pane == "cpu":
        controls.append("cpu j/k arrows select")
        controls.append("t %s threads" % (
            "process list" if cpu_thread_view else "thread view"))
    elif focused_pane == "logs":
        controls.extend([
            "logs j/k arrows move",
            "g latest",
            "p %s" % ("raw" if pretty_active else "pretty JSON"),
            "y yank",
            stream_control,
        ])
    else:
        controls.append("%s pane" % focused_pane)

    return " | ".join(controls)


def render_dashboard(processes, process_metrics, network_metrics, log_lines,
                     selected_ports=None, terminal_size=None, log_cursor=None,
                     status_message="", zoom_logs=False, yanked_cursor=None,
                     refresh_interval=1.0, pretty_lines=None,
                     stream_paused=False, disk_metrics=None,
                     process_scope="mrun", focused_pane="logs",
                     zoom_pane=None, cpu_cursor=None, cpu_thread_view=False,
                     thread_metrics=None, thread_error="",
                     thread_count=None):
    """Render the full four-quadrant monitor frame as a string."""
    if terminal_size is None:
        terminal_size = shutil.get_terminal_size((120, 40))

    columns = max(terminal_size.columns, 40)
    rows = max(terminal_size.lines - 1, 12)
    focused_pane = normalize_pane(focused_pane)
    if zoom_pane is None and zoom_logs:
        zoom_pane = "logs"
    zoom_pane = zoom_pane if zoom_pane in PANE_ORDER else None

    selected_ports = selected_ports or []
    if selected_ports:
        log_title = "Log Tail: " + ", ".join(str(port) for port in selected_ports)
    else:
        log_title = "Log Tail"

    pretty_active = pretty_lines is not None
    if pretty_active:
        log_title += " (Pretty JSON)"
    elif stream_paused:
        log_title += " (Paused)"

    disk_metrics = disk_metrics or {}
    thread_metrics = thread_metrics or []
    cpu_cursor = clamp_process_cursor(processes, cpu_cursor)
    selected_cpu = selected_process(processes, cpu_cursor)
    show_cpu_cursor = focused_pane == "cpu" or zoom_pane == "cpu"

    if cpu_thread_view:
        cpu_title = "CPU Threads"
        if selected_cpu is not None:
            cpu_title += ": port %s pid %s" % (
                selected_cpu.port, selected_cpu.pid)
        cpu_lines = format_thread_lines(
            selected_cpu, thread_metrics, thread_error, thread_count)
    else:
        cpu_title = "CPU Usage"
        cpu_lines = format_cpu_lines(
            processes, process_metrics, cpu_cursor, show_cpu_cursor)

    mem_lines = format_memory_lines(processes, process_metrics)
    net_lines = format_network_lines(processes, network_metrics)
    disk_lines = format_disk_lines(processes, disk_metrics)

    log_cursor = clamp_log_cursor(log_lines, log_cursor, follow_tail=False)
    yanked_cursor = clamp_optional_log_cursor(log_lines, yanked_cursor)
    full_log_height = rows - 2
    if pretty_active:
        log_lines_rendered = format_pretty_log_lines(
            pretty_lines, full_log_height)
    else:
        log_lines_rendered = (
            format_log_lines(log_lines, log_cursor, full_log_height,
                             yanked_cursor) if log_lines
            else ["No log selected."]
        )

    panels = {
        "cpu": (cpu_title, cpu_lines),
        "memory": ("Memory Usage", mem_lines),
        "network": ("Network Usage", net_lines),
        "disk": ("Disk Usage", disk_lines),
        "logs": (log_title, log_lines_rendered),
    }
    controls = _footer_controls(
        focused_pane,
        zoom_pane,
        refresh_interval,
        pretty_active,
        stream_paused,
        process_scope,
        cpu_thread_view,
    )

    if zoom_pane:
        zoom_title, zoom_lines = panels[zoom_pane]
        frame = make_panel(
            zoom_title, zoom_lines, columns, rows, focused=True)
        frame.append(_footer(status_message, controls))
        return "\n".join(frame)

    left_width = columns // 2
    right_width = columns - left_width
    top_height = rows // 2
    bottom_height = rows - top_height

    log_content_height = max(bottom_height - 2, 1)
    if pretty_active:
        log_content = format_pretty_log_lines(pretty_lines, log_content_height)
    else:
        log_content = (
            format_log_lines(log_lines, log_cursor, log_content_height,
                             yanked_cursor) if log_lines
            else ["No log selected."]
        )

    cpu_panel = make_panel(
        cpu_title, cpu_lines, left_width, top_height,
        focused=focused_pane == "cpu")
    mem_panel = make_panel(
        "Memory Usage", mem_lines, right_width, top_height,
        focused=focused_pane == "memory")
    net_height = max(bottom_height // 2, 3)
    disk_height = max(bottom_height - net_height, 3)
    if net_height + disk_height > bottom_height:
        disk_height = max(bottom_height - net_height, 0)
    net_panel = make_panel(
        "Network Usage", net_lines, left_width, net_height,
        focused=focused_pane == "network")
    disk_panel = make_panel(
        "Disk Usage", disk_lines, left_width, disk_height,
        focused=focused_pane == "disk")
    lower_left_panel = net_panel + disk_panel
    log_panel = make_panel(
        log_title, log_content, right_width, bottom_height,
        focused=focused_pane == "logs")

    frame = []
    frame.extend(left + right for left, right in zip(cpu_panel, mem_panel))
    frame.extend(left + right for left, right in zip(lower_left_panel, log_panel))
    frame.append(_footer(status_message, controls))
    return "\n".join(frame)


def parse_escape_sequence(sequence):
    """Translate terminal escape sequences into logical keys."""
    if sequence == "\x1b[Z":
        return "shift-tab"
    if sequence in ("\x1b[A", "\x1bOA"):
        return "up"
    if sequence in ("\x1b[B", "\x1bOB"):
        return "down"
    if sequence.startswith("\x1b[") and sequence[-1:] in ("A", "B"):
        return {"A": "up", "B": "down"}[sequence[-1]]
    return "escape"


def _escape_sequence_complete(sequence):
    if len(sequence) < 3:
        return False
    return sequence[-1].isalpha() or sequence[-1] == "~"


class TerminalController:
    """Minimal nonblocking terminal key reader with raw-mode cleanup."""

    def __init__(self, stdin=None, stdout=None):
        self.stdin = stdin or sys.stdin
        self.stdout = stdout or sys.stdout
        self.previous_settings = None

    def __enter__(self):
        if self._posix_raw_supported():
            self.previous_settings = termios.tcgetattr(self.stdin.fileno())
            tty.setcbreak(self.stdin.fileno())
        self.stdout.write("\033[?25l")
        self.stdout.flush()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.previous_settings is not None:
            termios.tcsetattr(self.stdin.fileno(), termios.TCSADRAIN, self.previous_settings)
        self.stdout.write("\033[?25h\n")
        self.stdout.flush()

    def read_key(self):
        if msvcrt is not None and msvcrt.kbhit():
            key = msvcrt.getwch()
            if key in ("\x00", "\xe0"):
                return {"H": "up", "P": "down"}.get(msvcrt.getwch(), key)
            if key == "\x03":
                return "ctrl-c"
            return key

        if not self._posix_raw_supported():
            return None

        readable, _, _ = select.select([self.stdin], [], [], 0)
        if readable:
            key = self.stdin.read(1)
            if key == "\x03":
                return "ctrl-c"
            if key == "\x1b":
                sequence = key
                deadline = time.time() + ESCAPE_READ_TIMEOUT
                while time.time() < deadline:
                    timeout = max(0.0, deadline - time.time())
                    if not select.select([self.stdin], [], [], timeout)[0]:
                        break
                    sequence += self.stdin.read(1)
                    if _escape_sequence_complete(sequence):
                        break
                return parse_escape_sequence(sequence)
            return key
        return None

    def _posix_raw_supported(self):
        return (
            termios is not None and
            tty is not None and
            hasattr(self.stdin, "isatty") and
            self.stdin.isatty() and
            hasattr(self.stdin, "fileno")
        )


class Monitor:
    """Run the interactive monitor command."""

    def __init__(self, client_factory=None, refresh_interval=1.0,
                 process_iter=None, stdout=None, stdin=None, input_func=None,
                 data_dir="./data", include_all=False,
                 monitor_username=None, monitor_password=None,
                 monitor_auth_db=None):
        self.client_factory = client_factory
        self.refresh_interval = (
            refresh_interval if refresh_interval in REFRESH_INTERVALS
            else REFRESH_INTERVALS[0])
        self.process_iter = process_iter
        self.data_dir = data_dir
        self.process_scope = "all" if include_all else "mrun"
        self.stdout = stdout or sys.stdout
        self.stdin = stdin or sys.stdin
        self.input_func = input_func or input
        self.auth_config = load_monitor_auth_config(data_dir).with_overrides(
            username=monitor_username,
            password=monitor_password,
            auth_db=monitor_auth_db,
        )
        network_client_kwargs = load_monitor_tls_kwargs(data_dir)
        network_client_kwargs.update(self.auth_config.client_kwargs())
        self.network_sampler = NetworkSampler(
            client_factory=client_factory,
            client_kwargs=network_client_kwargs,
            auth_required=self.auth_config.requires_credentials(),
        )
        self.thread_sampler = ThreadSampler()
        self.log_cursor = None
        self.follow_tail = True
        self.zoom_logs = False
        self.zoom_pane = None
        self.focused_pane = "logs"
        self.cpu_cursor = 0
        self.cpu_thread_view = False
        self.status_message = ""
        self.yanked_cursor = None
        self.pretty_lines = None
        self.pretty_previous_zoom = None
        self.stream_paused = False

    def run(self):
        processes = self._discover_processes_or_report()
        if processes is None:
            return 1
        if not processes:
            self.stdout.write(self._no_processes_message() + "\n")
            self.stdout.flush()
            return 1

        if not self._interactive_terminal():
            self.stdout.write("mrun --monitor requires an interactive terminal.\n")
            self.stdout.flush()
            return 1

        while True:
            try:
                logpaths = choose_logpaths(processes, self.input_func, self.stdout)
            except KeyboardInterrupt:
                self.stdout.write("\n")
                self.stdout.flush()
                return 0
            action = self._run_dashboard(logpaths)
            if action != "reselect":
                return 0
            processes = self._discover_processes_or_report()
            if processes is None:
                return 1
            if not processes:
                self.stdout.write(self._no_processes_message() + "\n")
                self.stdout.flush()
                return 1

    def _run_dashboard(self, logpaths):
        tailer = LogTailer(logpaths)
        self._prime_cpu()
        snapshot = None
        next_sample_at = 0.0
        force_sample = True

        with TerminalController(self.stdin, self.stdout) as terminal:
            try:
                while True:
                    now = time.time()
                    if dashboard_snapshot_due(
                            snapshot, now, next_sample_at, force_sample):
                        snapshot = self._read_dashboard_snapshot(tailer, logpaths)
                        if snapshot is None:
                            return "quit"
                        next_sample_at = snapshot.sampled_at + self.refresh_interval
                        force_sample = False

                    frame = render_dashboard(
                        snapshot.processes,
                        snapshot.process_metrics,
                        snapshot.network_metrics,
                        snapshot.log_lines,
                        snapshot.selected_ports,
                        log_cursor=self.log_cursor,
                        status_message=self.status_message,
                        zoom_logs=self.zoom_logs,
                        yanked_cursor=self.yanked_cursor,
                        refresh_interval=self.refresh_interval,
                        pretty_lines=self.pretty_lines,
                        stream_paused=self.stream_paused,
                        disk_metrics=snapshot.disk_metrics,
                        process_scope=self.process_scope,
                        focused_pane=self.focused_pane,
                        zoom_pane=self.zoom_pane,
                        cpu_cursor=self.cpu_cursor,
                        cpu_thread_view=self.cpu_thread_view,
                        thread_metrics=snapshot.thread_metrics,
                        thread_error=snapshot.thread_error,
                        thread_count=snapshot.thread_count,
                    )
                    self.stdout.write("\033[2J\033[H" + frame)
                    self.stdout.flush()

                    wait_start = time.time()
                    wait_timeout = max(0.0, next_sample_at - wait_start)
                    action = self._wait_for_action(
                        terminal, wait_start, snapshot.log_lines,
                        snapshot.processes, timeout=wait_timeout)
                    if action in ("quit", "reselect"):
                        return action
                    if action == "resample":
                        force_sample = True
            except KeyboardInterrupt:
                return "quit"

    def _read_dashboard_snapshot(self, tailer, logpaths):
        processes = self._discover_processes_or_report(clear_screen=True)
        if processes is None:
            return None
        if not processes:
            self.stdout.write(
                "\033[2J\033[H" + self._no_processes_message() + "\n")
            self.stdout.flush()
            return None

        process_metrics = {
            process.pid: read_process_metrics(process)
            for process in processes
        }
        self.cpu_cursor = clamp_process_cursor(processes, self.cpu_cursor)
        selected_cpu = selected_process(processes, self.cpu_cursor)
        thread_metrics = []
        thread_error = ""
        thread_count = None
        if self.cpu_thread_view and selected_cpu is not None:
            thread_snapshot = self.thread_sampler.sample(selected_cpu)
            thread_metrics = thread_snapshot.metrics
            thread_error = thread_snapshot.error
            thread_count = thread_snapshot.thread_count
        network_metrics = self.network_sampler.sample(processes)
        disk_metrics = read_disk_metrics(processes)
        log_lines = read_log_stream(tailer, self.stream_paused)
        self.log_cursor = clamp_log_cursor(
            log_lines, self.log_cursor, self.follow_tail)
        self.yanked_cursor = clamp_optional_log_cursor(
            log_lines, self.yanked_cursor)

        return DashboardSnapshot(
            processes=processes,
            process_metrics=process_metrics,
            network_metrics=network_metrics,
            disk_metrics=disk_metrics,
            log_lines=log_lines,
            selected_ports=sorted(logpaths),
            thread_metrics=thread_metrics,
            thread_error=thread_error,
            thread_count=thread_count,
            sampled_at=time.time(),
        )

    def _prime_cpu(self):
        try:
            processes = self._discover_processes()
        except ProcessDiscoveryError:
            return
        for process in processes:
            try:
                psutil.Process(process.pid).cpu_percent(interval=None)
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue

    def _discover_processes_or_report(self, clear_screen=False):
        try:
            return self._discover_processes()
        except ProcessDiscoveryError as exc:
            prefix = "\033[2J\033[H" if clear_screen else ""
            self.stdout.write(prefix + str(exc) + "\n")
            self.stdout.flush()
            return None

    def _discover_processes(self):
        if self.process_scope == "all":
            return discover_mongo_processes(self.process_iter)
        return discover_mrun_processes(self.data_dir, self.process_iter)

    def _no_processes_message(self):
        if self.process_scope == "all":
            return NO_PROCESSES_MESSAGE
        return NO_MRUN_PROCESSES_MESSAGE

    def _wait_for_action(self, terminal, start, log_lines, processes=None,
                         timeout=None):
        processes = processes or []
        if timeout is None:
            timeout = self.refresh_interval
        while time.time() - start < timeout:
            key = terminal.read_key()
            if key in ("q", "ctrl-c", "\x03"):
                return "quit"
            if key == "r":
                return "reselect"
            if key == "a":
                self._toggle_process_scope()
                return "reselect"
            if key in ("\t", "tab"):
                self._focus_next_pane(1)
                return "redraw"
            if key == "shift-tab":
                self._focus_next_pane(-1)
                return "redraw"
            if key == "z":
                self._toggle_focused_zoom()
                return "redraw"
            if key == "s":
                self._cycle_refresh_interval()
                return "redraw"

            if self.focused_pane == "cpu":
                if key in ("up", "k"):
                    self._move_cpu_cursor(processes, -1)
                    if self.cpu_thread_view:
                        return "resample"
                    return "redraw"
                if key in ("down", "j"):
                    self._move_cpu_cursor(processes, 1)
                    if self.cpu_thread_view:
                        return "resample"
                    return "redraw"
                if key in ("t", "T"):
                    self._toggle_cpu_thread_view(processes)
                    return "resample"
            elif self.focused_pane == "logs":
                if key in ("up", "k"):
                    self._move_log_cursor(log_lines, -1)
                    return "redraw"
                if key in ("down", "j"):
                    self._move_log_cursor(log_lines, 1)
                    return "redraw"
                if key == "g":
                    self._jump_to_latest(log_lines)
                    return "redraw"
                if key in ("p", "P"):
                    self._toggle_pretty_log_line(log_lines)
                    return "redraw"
                if key == "y":
                    self._yank_log_line(log_lines)
                    return "redraw"
                if key == " ":
                    self._toggle_streaming()
                    return "redraw"
            elif key == " ":
                self.status_message = "space applies to logs pane"
                return "redraw"
            time.sleep(KEY_POLL_INTERVAL)
        return None

    def _focus_next_pane(self, delta):
        previous_pane = self.focused_pane
        self.focused_pane = next_pane(self.focused_pane, delta)
        if previous_pane == "logs" and self.focused_pane != "logs":
            self._clear_pretty_log_line(restore_zoom=False)
        if self.zoom_pane is not None:
            self.zoom_pane = self.focused_pane
        self.zoom_logs = self.zoom_pane == "logs"
        self.status_message = "focus %s pane" % self.focused_pane

    def _toggle_focused_zoom(self):
        if self.focused_pane == "logs":
            self._clear_pretty_log_line(restore_zoom=False)
        if self.zoom_pane == self.focused_pane:
            self.zoom_pane = None
            self.zoom_logs = False
            self.status_message = "%s zoom off" % self.focused_pane
            return

        self.zoom_pane = self.focused_pane
        self.zoom_logs = self.zoom_pane == "logs"
        self.status_message = "%s zoom on" % self.focused_pane

    def _move_cpu_cursor(self, processes, delta):
        self.cpu_cursor = move_process_cursor(processes, self.cpu_cursor, delta)
        if self.cpu_cursor is None:
            self.cpu_thread_view = False
            self.status_message = "no MongoDB process selected"
            return

        process = selected_process(processes, self.cpu_cursor)
        self.status_message = "selected port %s pid %s" % (
            process.port, process.pid)

    def _toggle_cpu_thread_view(self, processes):
        self.cpu_cursor = clamp_process_cursor(processes, self.cpu_cursor)
        process = selected_process(processes, self.cpu_cursor)
        if process is None:
            self.cpu_thread_view = False
            self.status_message = "no MongoDB process selected"
            return

        self.cpu_thread_view = not self.cpu_thread_view
        if self.cpu_thread_view:
            self.status_message = "thread view for port %s pid %s" % (
                process.port, process.pid)
        else:
            self.status_message = "CPU process list"

    def _move_log_cursor(self, log_lines, delta):
        self._clear_pretty_log_line()
        self.log_cursor = move_log_cursor(log_lines, self.log_cursor, delta)
        if self.log_cursor is None:
            self.follow_tail = False
            self.status_message = "no log line selected"
        elif delta > 0 and self.log_cursor == len(log_lines) - 1:
            self.follow_tail = True
            self.status_message = "following latest log line"
        else:
            self.follow_tail = False
            self.status_message = "highlighted log line %i" % (self.log_cursor + 1)

    def _cycle_refresh_interval(self):
        self.refresh_interval = next_refresh_interval(self.refresh_interval)
        self.status_message = "refresh interval %s" % format_seconds(
            self.refresh_interval)

    def _toggle_streaming(self):
        self.stream_paused = not self.stream_paused
        if self.stream_paused:
            self.status_message = "log streaming paused"
        else:
            self.status_message = "log streaming resumed"

    def _toggle_process_scope(self):
        self.process_scope = "all" if self.process_scope == "mrun" else "mrun"
        self.log_cursor = None
        self.yanked_cursor = None
        self.cpu_cursor = 0
        self.cpu_thread_view = False
        self.zoom_pane = None
        self.zoom_logs = False
        self.follow_tail = True
        self.stream_paused = False
        self._clear_pretty_log_line(restore_zoom=False)
        self.status_message = (
            "showing all MongoDB processes" if self.process_scope == "all"
            else "showing mongorun-managed processes")

    def _jump_to_latest(self, log_lines):
        self._clear_pretty_log_line(restore_zoom=False)
        self.log_cursor = clamp_log_cursor(log_lines, self.log_cursor,
                                           follow_tail=True)
        if self.log_cursor is None:
            self.follow_tail = False
            self.status_message = "no log line selected"
            return

        self.follow_tail = True
        self.status_message = "following latest log line"

    def _toggle_pretty_log_line(self, log_lines):
        if self.pretty_lines is not None:
            self._clear_pretty_log_line()
            self.status_message = "raw log line view"
            return

        self.log_cursor = clamp_log_cursor(log_lines, self.log_cursor,
                                           self.follow_tail)
        if self.log_cursor is None:
            self.follow_tail = False
            self.status_message = "no log line selected"
            return

        self.follow_tail = False
        pretty_lines = prettify_log_line(log_lines[self.log_cursor])
        if pretty_lines is None:
            self.status_message = "selected line is not valid JSON"
            return

        self.pretty_lines = pretty_lines
        self.pretty_previous_zoom = self.zoom_logs
        self.zoom_logs = True
        self.zoom_pane = "logs"
        self.focused_pane = "logs"
        self.status_message = "prettified highlighted log line"

    def _clear_pretty_log_line(self, restore_zoom=True):
        if self.pretty_lines is None:
            return
        self.pretty_lines = None
        if restore_zoom and self.pretty_previous_zoom is not None:
            self.zoom_logs = self.pretty_previous_zoom
            self.zoom_pane = "logs" if self.zoom_logs else None
        self.pretty_previous_zoom = None

    def _yank_log_line(self, log_lines):
        self.log_cursor = clamp_log_cursor(log_lines, self.log_cursor,
                                           self.follow_tail)
        if self.log_cursor is None:
            self.status_message = "no log line selected"
            return

        line = log_lines[self.log_cursor]
        self.stdout.write(build_osc52_sequence(line))
        self.stdout.flush()
        self.follow_tail = False
        self.yanked_cursor = self.log_cursor
        self.status_message = "yanked highlighted log line"

    def _interactive_terminal(self):
        return (
            hasattr(self.stdin, "isatty") and self.stdin.isatty() and
            hasattr(self.stdout, "isatty") and self.stdout.isatty()
        )

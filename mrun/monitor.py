#!/usr/bin/env python3
"""Live terminal monitor for local MongoDB server processes."""

import base64
import json
import os
import re
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
ROLE_PASSWORD_REQUIRED = "Password Required"

ANSI_RESET = "\033[0m"
ANSI_YELLOW = "\033[33m"
ANSI_GREEN = "\033[32m"
ANSI_RED = "\033[31m"
ANSI_TEAL = "\033[38;5;44m"
ANSI_DIM = "\033[2m"
ANSI_INVERSE = "\033[7m"
ANSI_BOLD = "\033[1m"
ANSI_DEFAULT = "\033[39m"
PANE_HEADER_COLORS = {
    "cpu": ANSI_TEAL,
    "memory": ANSI_GREEN,
    "network": ANSI_YELLOW,
    "disk": ANSI_RED,
    "logs": ANSI_TEAL,
    "disk status": ANSI_RED,
    "network status": ANSI_YELLOW,
    "storage subsystem": ANSI_GREEN,
    "other subsystems": ANSI_TEAL,
}
ANSI_PRETTY_KEY_DARK = "\033[38;5;81m"
ANSI_PRETTY_STRING_DARK = "\033[38;5;114m"
ANSI_PRETTY_NUMBER_DARK = "\033[38;5;215m"
ANSI_PRETTY_KEYWORD_DARK = "\033[38;5;141m"
ANSI_PRETTY_PUNCT_DARK = "\033[38;5;245m"
ANSI_PRETTY_KEY_LIGHT = "\033[38;5;25m"
ANSI_PRETTY_STRING_LIGHT = "\033[38;5;28m"
ANSI_PRETTY_NUMBER_LIGHT = "\033[38;5;130m"
ANSI_PRETTY_KEYWORD_LIGHT = "\033[38;5;90m"
ANSI_PRETTY_PUNCT_LIGHT = "\033[38;5;240m"
ANSI_SEARCH_HIT = "\033[33m\033[7m"
STYLE_SELECTED = "\x00selected\x00"
STYLE_YANKED = "\x00yanked\x00"
STYLE_SEVERITY_FATAL = "\x00severity:fatal\x00"
STYLE_SEVERITY_ERROR = "\x00severity:error\x00"
STYLE_SEVERITY_WARNING = "\x00severity:warning\x00"
STYLE_SEVERITY_INFO = "\x00severity:info\x00"
STYLE_SEVERITY_DEBUG = "\x00severity:debug\x00"
STYLE_TABLE_HEADER = "\x00table-header\x00"
STYLE_ROLE_PRIMARY = "\x00role:primary\x00"
STYLE_ROLE_SECONDARY = "\x00role:secondary\x00"
STYLE_ROLE_WARNING = "\x00role:warning\x00"
STYLE_ROLE_DIM = "\x00role:dim\x00"
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
    STYLE_TABLE_HEADER,
    STYLE_ROLE_PRIMARY,
    STYLE_ROLE_SECONDARY,
    STYLE_ROLE_WARNING,
    STYLE_ROLE_DIM,
)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
JSON_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
FILTER_VALUE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*:.+")


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
    role_metrics: dict
    network_metrics: dict
    disk_metrics: dict
    log_lines: list
    selected_ports: list
    thread_metrics: list
    current_ops: "CurrentOpSnapshot" = None
    status_snapshot: "ServerStatusSnapshot" = None
    thread_error: str = ""
    thread_count: int = None
    sampled_at: float = 0.0


@dataclass
class RoleMetrics:
    """Replica-set role metadata for one MongoDB server process."""

    available: bool
    role: str = ""
    error: str = ""


@dataclass
class NetworkMetrics:
    """MongoDB serverStatus network rates."""

    available: bool
    bytes_in_per_sec: float = 0.0
    bytes_out_per_sec: float = 0.0
    requests_per_sec: float = 0.0
    error: str = ""


@dataclass
class LogFilterMatch:
    """Result of applying a filter query to a single log line."""

    matched: bool
    score: float = 0.0
    spans: list = None


@dataclass
class LogFilterView:
    """Filtered log lines plus their original raw-buffer indexes."""

    lines: list
    indexes: list
    spans_by_index: dict


@dataclass
class CurrentOpEntry:
    """One active currentOp entry normalized for compact terminal display."""

    port: int
    role: str
    secs_running: float
    op: str
    ns: str
    client: str
    desc: str
    opid: str = ""
    raw: dict = None


@dataclass
class CurrentOpSnapshot:
    """Top active currentOp entries across the visible MongoDB processes."""

    available: bool
    entries: list
    error: str = ""


@dataclass
class ServerStatusSnapshot:
    """Expanded MongoDB serverStatus results segregated by category."""

    available: bool
    port: int = 0
    # Disk (WiredTiger block manager, logging, flushing)
    disk: dict = None
    # Network (connections, opcounters, network rates)
    network: dict = None
    # Storage (WT cache, tickets, global lock, mem)
    storage: dict = None
    # Lightweight summaries for every top-level serverStatus subsystem.
    subsystems: dict = None
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


class ProcessSampler:
    """Sample process metrics while preserving psutil CPU history."""

    def __init__(self, process_factory=None):
        self.process_factory = process_factory or psutil.Process
        self.processes = {}

    def prime(self, processes):
        for process_info in processes:
            try:
                process = self._process(process_info.pid)
                process.cpu_percent(interval=None)
            except (psutil.AccessDenied, psutil.NoSuchProcess,
                    psutil.ZombieProcess):
                self.processes.pop(process_info.pid, None)

    def sample(self, processes):
        metrics = {}
        live_pids = set()
        for process_info in processes:
            live_pids.add(process_info.pid)
            metrics[process_info.pid] = self.sample_one(process_info)

        for pid in list(self.processes):
            if pid not in live_pids:
                del self.processes[pid]
        return metrics

    def sample_one(self, process_info):
        try:
            process = self._process(process_info.pid)
            memory_rss = process.memory_info().rss
            cpu_percent = process.cpu_percent(interval=None)
            status = process.status()
        except (psutil.AccessDenied, psutil.NoSuchProcess,
                psutil.ZombieProcess):
            self.processes.pop(process_info.pid, None)
            return ProcessMetrics(0.0, 0, "unavailable")

        return ProcessMetrics(cpu_percent, memory_rss, status)

    def _process(self, pid):
        process = self.processes.get(pid)
        if process is None:
            process = self.process_factory(pid)
            self.processes[pid] = process
        return process


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


class RoleSampler:
    """Sample replica-set role information from serverStatus for each process."""

    def __init__(self, client_factory=None, client_kwargs=None,
                 auth_required=False):
        self.client_factory = client_factory or self._default_client_factory
        self.client_kwargs = dict(client_kwargs or {})
        self.auth_required = auth_required

    def sample(self, processes):
        metrics = {}
        for process in processes:
            metrics[process.port] = self.sample_one(process)
        return metrics

    def sample_one(self, process):
        if self.auth_required:
            return RoleMetrics(
                False,
                role=ROLE_PASSWORD_REQUIRED,
                error=AUTH_REQUIRED_STATUS,
            )

        client = None
        try:
            client_kwargs = {
                "directConnection": True,
                "serverSelectionTimeoutMS": 200,
            }
            client_kwargs.update(self.client_kwargs)
            client = self.client_factory(
                "localhost:%i" % process.port,
                **client_kwargs
            )
            status = client.admin.command("serverStatus")
            return RoleMetrics(True, role=role_from_server_status(status))
        except Exception as exc:
            return RoleMetrics(False, role="unavailable", error=str(exc))
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

    @staticmethod
    def _default_client_factory(host, **kwargs):
        from pymongo import MongoClient

        return MongoClient(host, **kwargs)


class CurrentOpSampler:
    """Sample and rank active currentOp entries across visible processes."""

    def __init__(self, client_factory=None, client_kwargs=None,
                 auth_required=False):
        self.client_factory = client_factory or self._default_client_factory
        self.client_kwargs = dict(client_kwargs or {})
        self.auth_required = auth_required

    def sample(self, processes, role_metrics=None, limit=10, namespace=""):
        if self.auth_required:
            return CurrentOpSnapshot(
                False,
                [],
                error=ROLE_PASSWORD_REQUIRED,
            )

        role_metrics = role_metrics or {}
        entries = []
        errors = []
        for process in processes:
            process_entries, error = self._read_current_ops(
                process, role_metrics, namespace)
            entries.extend(process_entries)
            if error:
                errors.append("%s: %s" % (process.port, error))

        entries.sort(key=lambda entry: entry.secs_running, reverse=True)
        if entries:
            return CurrentOpSnapshot(True, entries[:limit])
        if errors:
            return CurrentOpSnapshot(False, [], error="; ".join(errors))
        return CurrentOpSnapshot(True, [])

    def _read_current_ops(self, process, role_metrics, namespace=""):
        client = None
        try:
            client_kwargs = {
                "directConnection": True,
                "serverSelectionTimeoutMS": 200,
            }
            client_kwargs.update(self.client_kwargs)
            client = self.client_factory(
                "localhost:%i" % process.port,
                **client_kwargs
            )
            command = {
                "currentOp": 1,
                "$all": True,
                "active": True,
            }
            if namespace:
                command["ns"] = namespace
            result = client.admin.command(command)
            role = role_display(role_metrics.get(process.port))
            entries = [
                current_op_entry(process.port, role, raw)
                for raw in result.get("inprog", [])
                if raw.get("active", True)
                and (not namespace or raw.get("ns") == namespace)
            ]
            return entries, ""
        except Exception as exc:
            return [], str(exc)
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

    @staticmethod
    def _default_client_factory(host, **kwargs):
        from pymongo import MongoClient

        return MongoClient(host, **kwargs)


class StatusSampler:
    """Sample detailed MongoDB serverStatus metrics on demand."""

    SUBSYSTEM_PRIORITY = [
        "host",
        "version",
        "process",
        "pid",
        "uptime",
        "localTime",
        "asserts",
        "connections",
        "network",
        "opcounters",
        "opcountersRepl",
        "metrics",
        "locks",
        "globalLock",
        "flowControl",
        "transactions",
        "repl",
        "electionMetrics",
        "sharding",
        "wiredTiger",
        "storageEngine",
        "mem",
        "tcmalloc",
        "security",
        "transportSecurity",
        "extra_info",
        "backgroundFlushing",
    ]

    def __init__(self, client_factory=None, clock=None, client_kwargs=None,
                 auth_required=False):
        self.client_factory = client_factory or self._default_client_factory
        self.clock = clock or time.time
        self.client_kwargs = dict(client_kwargs or {})
        self.auth_required = auth_required
        self.previous = {}

    def sample(self, process_info):
        """Execute serverStatus and segregate into Disk/Network/Storage."""
        if self.auth_required:
            return ServerStatusSnapshot(
                False,
                port=process_info.port,
                error=AUTH_REQUIRED_STATUS,
            )

        now = self.clock()
        client = None
        try:
            client_kwargs = {
                "directConnection": True,
                "serverSelectionTimeoutMS": 200,
            }
            client_kwargs.update(self.client_kwargs)
            client = self.client_factory(
                "localhost:%i" % process_info.port,
                **client_kwargs
            )
            status = client.admin.command("serverStatus")
            subsystems = self._extract_subsystems(status)

            disk = {
                "wt_block_manager": status.get(
                    "wiredTiger", {}).get("block-manager", {}),
                "wt_log": status.get("wiredTiger", {}).get("log", {}),
                "backgroundFlushing": status.get("backgroundFlushing", {}),
                "rates": {},
            }
            network_data = {
                "network": status.get("network", {}),
                "connections": status.get("connections", {}),
                "opcounters": status.get("opcounters", {}),
                "opcountersRepl": status.get("opcountersRepl", {}),
                "rates": {},
            }
            storage = {
                "wt_cache": status.get("wiredTiger", {}).get("cache", {}),
                "wt_tickets": status.get(
                    "wiredTiger", {}).get("concurrentTransactions", {}),
                "globalLock": status.get("globalLock", {}),
                "mem": status.get("mem", {}),
                "extra_info": status.get("extra_info", {}),
            }

            prev = self.previous.get(process_info.port)
            if prev:
                prev_time, prev_status = prev
                elapsed = max(now - prev_time, 0.001)
                disk["rates"] = self._calculate_disk_rates(
                    status, prev_status, elapsed)
                network_data["rates"] = self._calculate_network_rates(
                    status, prev_status, elapsed)

            self.previous[process_info.port] = (now, status)

            return ServerStatusSnapshot(
                available=True,
                port=process_info.port,
                disk=disk,
                network=network_data,
                storage=storage,
                subsystems=subsystems,
            )

        except Exception as exc:
            return ServerStatusSnapshot(
                False,
                port=process_info.port,
                error=str(exc),
            )
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()

    def _calculate_disk_rates(self, current, previous, elapsed):
        curr_wt = current.get("wiredTiger", {}).get("block-manager", {})
        prev_wt = previous.get("wiredTiger", {}).get("block-manager", {})
        return {
            "bytes_read_per_sec": (
                curr_wt.get("bytes read", 0) -
                prev_wt.get("bytes read", 0)
            ) / elapsed,
            "bytes_written_per_sec": (
                curr_wt.get("bytes written", 0) -
                prev_wt.get("bytes written", 0)
            ) / elapsed,
        }

    def _calculate_network_rates(self, current, previous, elapsed):
        curr_net = current.get("network", {})
        prev_net = previous.get("network", {})
        curr_ops = current.get("opcounters", {})
        prev_ops = previous.get("opcounters", {})

        rates = {
            "bytes_in_per_sec": (
                curr_net.get("bytesIn", 0) -
                prev_net.get("bytesIn", 0)
            ) / elapsed,
            "bytes_out_per_sec": (
                curr_net.get("bytesOut", 0) -
                prev_net.get("bytesOut", 0)
            ) / elapsed,
        }
        for op in ["insert", "query", "update", "delete", "getmore", "command"]:
            rates[op] = (curr_ops.get(op, 0) - prev_ops.get(op, 0)) / elapsed
        return rates

    @classmethod
    def _extract_subsystems(cls, status):
        """Summarize each top-level serverStatus subsystem for the UI."""
        priority = {name: index for index, name in enumerate(cls.SUBSYSTEM_PRIORITY)}
        subsystems = {}
        for name in sorted(
                status,
                key=lambda key: (priority.get(key, len(priority)), key)):
            subsystems[name] = cls._summarize_subsystem_value(status.get(name))
        return subsystems

    @staticmethod
    def _summarize_subsystem_value(value):
        if isinstance(value, dict):
            if not value:
                return "empty"
            return "%i fields" % len(value)
        if isinstance(value, (list, tuple)):
            return "%i items" % len(value)
        if value is None:
            return "null"
        return _truncate(str(value), 32)

    @staticmethod
    def _default_client_factory(host, **kwargs):
        from pymongo import MongoClient

        return MongoClient(host, **kwargs)


def role_from_server_status(status):
    """Extract a human-readable node role from serverStatus()."""
    repl = status.get("repl") or {}
    state = str(repl.get("stateStr") or "").strip()
    if state:
        normalized = state.upper()
        if normalized == "PRIMARY":
            return "Primary"
        if normalized == "SECONDARY":
            return "Secondary"
        return state.title()

    writable_primary = repl.get("isWritablePrimary")
    if writable_primary is None:
        writable_primary = status.get("isWritablePrimary")
    if writable_primary is True:
        return "Primary"
    if writable_primary is False and repl:
        return "Secondary"

    if status.get("process") == "mongos":
        return "Router"
    if repl:
        return "Replica Set"
    return "Standalone"


def role_display(role_metrics):
    """Return the role text shown in process and currentOp rows."""
    if role_metrics is None:
        return "unknown"
    if role_metrics.role:
        return role_metrics.role
    if role_metrics.error == AUTH_REQUIRED_STATUS:
        return ROLE_PASSWORD_REQUIRED
    return "unavailable"


def _current_op_secs(raw):
    if raw.get("secs_running") is not None:
        try:
            return float(raw.get("secs_running"))
        except (TypeError, ValueError):
            return 0.0
    if raw.get("microsecs_running") is not None:
        try:
            return float(raw.get("microsecs_running")) / 1000000.0
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _current_op_summary(raw):
    command = raw.get("command")
    if isinstance(command, dict) and command:
        keys = [key for key in command if not str(key).startswith("$")]
        key = keys[0] if keys else next(iter(command))
        value = command.get(key)
        if isinstance(value, str) and value:
            return "%s %s" % (key, value)
        return str(key)
    return raw.get("desc") or raw.get("msg") or ""


def current_op_entry(port, role, raw):
    """Normalize a raw currentOp document for terminal rendering."""
    return CurrentOpEntry(
        port=port,
        role=role,
        secs_running=_current_op_secs(raw),
        op=str(raw.get("op") or raw.get("type") or "unknown"),
        ns=str(raw.get("ns") or ""),
        client=str(raw.get("client") or raw.get("client_s") or ""),
        desc=str(_current_op_summary(raw) or ""),
        opid=str(raw.get("opid") or ""),
        raw=dict(raw or {}),
    )


def current_op_namespaces(snapshot):
    """Return sorted namespace names visible in a currentOp snapshot."""
    if not snapshot or not snapshot.entries:
        return []
    namespaces = {
        str(entry.ns)
        for entry in snapshot.entries
        if str(entry.ns or "").strip()
    }
    return sorted(namespaces)


def json_safe_value(value, depth=0):
    """Convert BSON/PyMongo values into dependency-free JSON-safe values."""
    if depth > 30:
        return str(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            if isinstance(key, str):
                safe_key = key
            else:
                safe_key = str(json_safe_value(key, depth + 1))
            safe[safe_key] = json_safe_value(item, depth + 1)
        return safe
    if isinstance(value, (list, tuple, set)):
        return [json_safe_value(item, depth + 1) for item in value]
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    return str(value)


def current_op_raw_json(raw):
    """Render a db.currentOp-style document as safe compact JSON text."""
    return json.dumps(json_safe_value(raw or {}))


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


def parse_current_op_namespace_selection(selection, namespaces):
    """Return a selected currentOp namespace, empty clear value, or None."""
    if selection is None:
        return None
    selection = str(selection).strip()
    if selection == "":
        return None
    if selection.lower() in ("all", "clear", "none", "*"):
        return ""

    try:
        index = int(selection)
    except ValueError:
        return selection

    if 1 <= index <= len(namespaces):
        return namespaces[index - 1]
    return None


def choose_current_op_namespace(namespaces, input_func=input, stdout=None):
    """Prompt for a currentOp namespace filter."""
    stdout = stdout or sys.stdout
    namespaces = list(namespaces or [])
    stdout.write("\nSelect currentOp namespace filter:\n")
    if namespaces:
        for index, namespace in enumerate(namespaces, start=1):
            stdout.write("  [%i] %s\n" % (index, namespace))
    else:
        stdout.write("  No active currentOp namespaces detected.\n")
    stdout.write(
        "Enter index or namespace, 'all' to clear, or press Enter to cancel: ")
    stdout.flush()
    return parse_current_op_namespace_selection(input_func(), namespaces)


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


def strip_ansi(text):
    """Remove terminal ANSI style sequences and internal style markers."""
    text = ANSI_ESCAPE_RE.sub("", str(text))
    for marker in STYLE_MARKERS:
        text = text.replace(marker, "")
    return text


def visible_width(text):
    """Return display width for ASCII text that may contain ANSI styles."""
    return len(strip_ansi(text))


def _truncate(text, width):
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[:width - 1] + "~"


def _truncate_ansi(text, width):
    """Truncate text by visible width while preserving ANSI sequences."""
    text = str(text)
    if visible_width(text) <= width:
        return text
    if width <= 0:
        return ""
    if width == 1:
        return "~"

    limit = width - 1
    output = []
    visible = 0
    index = 0
    while index < len(text) and visible < limit:
        match = ANSI_ESCAPE_RE.match(text, index)
        if match is not None:
            output.append(match.group(0))
            index = match.end()
            continue
        output.append(text[index])
        index += 1
        visible += 1

    truncated = "".join(output) + "~"
    if "\033[" in truncated and not truncated.endswith(ANSI_RESET):
        truncated += ANSI_RESET
    return truncated


def _pad_ansi(text, width, fillchar=" "):
    """Right-pad text by visible width while preserving ANSI sequences."""
    text = str(text)
    padding = max(width - visible_width(text), 0)
    return text + fillchar * padding


def _panel_content_padding(text, width):
    """Add one-cell left padding for non-empty panel content."""
    if not text or width <= 1:
        return text
    return " " + text


def _styled_line(styles, text):
    if isinstance(styles, str):
        styles = [styles]
    return "".join(styles) + text


def _table_header(text):
    return _styled_line(STYLE_TABLE_HEADER, text)


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


def _style_ansi(styles, header_color=None):
    prefix = ""
    if STYLE_TABLE_HEADER in styles:
        prefix = ANSI_BOLD + (header_color or "")

    if STYLE_YANKED in styles:
        return prefix + ANSI_GREEN + ANSI_INVERSE

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
        return prefix + color + ANSI_INVERSE
    return prefix + color


def role_style(role):
    """Return an inline style marker for a replica-set role."""
    normalized = str(role or "").strip().lower()
    if normalized == "primary":
        return STYLE_ROLE_PRIMARY
    if normalized == "secondary":
        return STYLE_ROLE_SECONDARY
    if normalized == ROLE_PASSWORD_REQUIRED.lower():
        return STYLE_ROLE_WARNING
    if normalized in ("unavailable", "unknown"):
        return STYLE_ROLE_DIM
    return ""


def colorize_role(role):
    """Colorize a role value without resetting selected-row inverse video."""
    text = str(role or "unknown")
    style = role_style(text)
    if style == STYLE_ROLE_PRIMARY:
        return ANSI_GREEN + text + ANSI_DEFAULT
    if style in (STYLE_ROLE_SECONDARY, STYLE_ROLE_WARNING):
        return ANSI_YELLOW + text + ANSI_DEFAULT
    if style == STYLE_ROLE_DIM:
        return ANSI_DIM + text + ANSI_RESET
    return text


def format_role(role_metrics, width=17):
    """Format a role column with stable visible width."""
    return _pad_ansi(_truncate_ansi(
        colorize_role(role_display(role_metrics)), width), width)


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


def _log_filter_active(query):
    return bool(str(query or "").strip())


def _find_case_insensitive_spans(text, needle):
    text = str(text)
    needle = str(needle or "")
    if not needle:
        return []

    spans = []
    lowered = text.lower()
    lowered_needle = needle.lower()
    start = 0
    while True:
        index = lowered.find(lowered_needle, start)
        if index < 0:
            return spans
        spans.append((index, index + len(needle)))
        start = index + max(len(needle), 1)


def _merge_spans(spans):
    normalized = []
    for start, end in spans or []:
        try:
            start = int(start)
            end = int(end)
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        normalized.append((start, end))
    normalized.sort()

    merged = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _ordered_fuzzy_spans(query, text):
    chars = [char for char in str(query or "").lower() if not char.isspace()]
    if not chars:
        return []

    spans = []
    position = 0
    lowered = str(text).lower()
    for char in chars:
        index = lowered.find(char, position)
        if index < 0:
            return []
        spans.append((index, index + 1))
        position = index + 1
    return spans


def score_log_filter(query, line):
    """Score how well a free-text query matches one log line.

    The matcher intentionally stays small and dependency-free. It first checks
    for a case-insensitive exact substring, then checks whether every query
    token appears somewhere in the line, and finally falls back to an ordered
    subsequence match. Ordered subsequence matching lets "slwop" match
    "Slow query operation" by finding those characters in order. The returned
    score describes match strength only; log rendering still preserves stream
    order instead of sorting by score.
    """
    query = str(query or "").strip()
    line = str(line)
    if not query:
        return LogFilterMatch(True, score=0.0, spans=[])

    exact_spans = _find_case_insensitive_spans(line, query)
    if exact_spans:
        return LogFilterMatch(True, score=300.0 + len(query), spans=exact_spans)

    tokens = [token for token in query.split() if token]
    if len(tokens) > 1:
        token_spans = []
        for token in tokens:
            spans = _find_case_insensitive_spans(line, token)
            if not spans:
                break
            token_spans.extend(spans)
        else:
            return LogFilterMatch(
                True,
                score=200.0 + sum(len(token) for token in tokens),
                spans=_merge_spans(token_spans),
            )

    fuzzy_spans = _ordered_fuzzy_spans(query, line)
    if fuzzy_spans:
        compactness = fuzzy_spans[-1][1] - fuzzy_spans[0][0]
        score = 100.0 + (len(fuzzy_spans) / max(compactness, 1))
        return LogFilterMatch(True, score=score, spans=fuzzy_spans)

    return LogFilterMatch(False, score=0.0, spans=[])


def _parse_json_log(line):
    text = strip_log_port_prefix(line).strip()
    for candidate in _json_log_candidates(text):
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _walk_json_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key), item
            for nested_key, nested_item in _walk_json_values(item):
                yield nested_key, nested_item
    elif isinstance(value, list):
        for item in value:
            for nested_key, nested_item in _walk_json_values(item):
                yield nested_key, nested_item


def _stringify_filter_value(value):
    if isinstance(value, dict):
        return " ".join(str(key) for key in value.keys())
    if isinstance(value, list):
        return " ".join(_stringify_filter_value(item) for item in value)
    if value is None:
        return ""
    return str(value)


def _log_port(line):
    prefix, separator, _ = str(line).partition(" | ")
    if separator and prefix.strip().isdigit():
        return prefix.strip()
    return ""


def _json_field_values(parsed, field):
    aliases = {
        "component": ("c", "component"),
        "severity": ("s", "severity", "level"),
        "msg": ("msg", "message"),
        "message": ("msg", "message"),
        "namespace": ("ns", "namespace"),
        "ns": ("ns", "namespace"),
    }.get(field, (field,))
    aliases = set(aliases)

    values = []
    for key, value in _walk_json_values(parsed):
        if key.lower() in aliases:
            values.append(_stringify_filter_value(value))
    return values


def _command_names_from_json(parsed):
    ignored = set([
        "$clusterTime",
        "$db",
        "apiVersion",
        "apiStrict",
        "apiDeprecationErrors",
        "autocommit",
        "lsid",
        "readConcern",
        "readPreference",
        "txnNumber",
        "writeConcern",
    ])
    names = []
    for key, value in _walk_json_values(parsed):
        lowered = key.lower()
        if lowered in ("commandname", "command_name", "cmd"):
            text = _stringify_filter_value(value).strip()
            if text:
                names.append(text)
        elif lowered == "command":
            if isinstance(value, dict):
                for command_key in value:
                    if command_key not in ignored:
                        names.append(str(command_key))
                        break
            else:
                text = _stringify_filter_value(value).strip()
                if text:
                    names.append(text)
    return names


def _filter_terms(query):
    try:
        return shlex.split(str(query or ""))
    except ValueError:
        return str(query or "").split()


def _structured_term(term):
    if not FILTER_VALUE_RE.match(term):
        return None, term
    key, value = term.split(":", 1)
    return key.strip().lower(), value.strip()


def _is_slowop_query(query):
    normalized = re.sub(r"[^a-z0-9]", "", str(query or "").lower())
    return normalized in ("slowop", "slowops", "slowquery", "slowoperation")


def _match_slowop_filter(line):
    lowered = str(line).lower()
    markers = [
        "slow query",
        "slow operation",
        "slowms",
        "durationmillis",
        "docsexamined",
        "keysexamined",
        "plansummary",
        "collscan",
    ]
    spans = []
    for marker in markers:
        spans.extend(_find_case_insensitive_spans(line, marker))
    if spans:
        return LogFilterMatch(True, score=260.0, spans=_merge_spans(spans))

    parsed = _parse_json_log(line)
    if parsed:
        msg = " ".join(_json_field_values(parsed, "msg")).lower()
        if "slow" in msg and ("query" in msg or "operation" in msg):
            return LogFilterMatch(True, score=260.0, spans=[])
    if "slow" in lowered and ("query" in lowered or "operation" in lowered):
        return LogFilterMatch(True, score=240.0, spans=[])
    return LogFilterMatch(False, score=0.0, spans=[])


def _match_structured_filter(key, value, line):
    parsed = _parse_json_log(line)
    key = str(key or "").lower()
    value = str(value or "").strip()
    if not value:
        return LogFilterMatch(False, score=0.0, spans=[])

    if key == "port":
        if _log_port(line) == value:
            return LogFilterMatch(
                True,
                score=280.0,
                spans=_find_case_insensitive_spans(line, value),
            )
        return LogFilterMatch(False, score=0.0, spans=[])

    if key == "severity":
        expected = _severity_from_value(value) or value.lower()
        actual = detect_log_severity(line)
        if actual == expected or value.lower() == actual:
            return LogFilterMatch(
                True,
                score=280.0,
                spans=_find_case_insensitive_spans(line, value),
            )
        return LogFilterMatch(False, score=0.0, spans=[])

    if parsed is None:
        return LogFilterMatch(False, score=0.0, spans=[])

    if key in ("cmd", "command"):
        values = _command_names_from_json(parsed)
    else:
        values = _json_field_values(parsed, key)

    if not values:
        return LogFilterMatch(False, score=0.0, spans=[])

    field_text = " ".join(values)
    match = score_log_filter(value, field_text)
    if not match.matched:
        return match

    spans = _find_case_insensitive_spans(line, value)
    if not spans:
        spans = score_log_filter(value, line).spans or []
    return LogFilterMatch(True, score=match.score + 80.0, spans=spans)


def match_log_filter(query, line):
    """Return whether a log line matches a free-text or structured filter.

    Structured terms use ``field:value`` syntax and all supplied terms must
    match. Supported fields include MongoDB log aliases such as ``cmd``,
    ``component``, ``severity``, ``port``, and ``msg``. Plain queries use the
    same dependency-free fuzzy scorer as search highlighting. The ``slowop``
    alias matches common MongoDB slow-operation log markers.
    """
    query = str(query or "").strip()
    if not query:
        return LogFilterMatch(True, score=0.0, spans=[])
    if _is_slowop_query(query):
        return _match_slowop_filter(line)

    terms = _filter_terms(query)
    structured = [_structured_term(term) for term in terms]
    if any(key for key, _ in structured):
        score = 0.0
        spans = []
        for key, value in structured:
            if key:
                match = _match_structured_filter(key, value, line)
            else:
                match = score_log_filter(value, line)
            if not match.matched:
                return LogFilterMatch(False, score=0.0, spans=[])
            score += match.score
            spans.extend(match.spans or [])
        return LogFilterMatch(True, score=score, spans=_merge_spans(spans))

    return score_log_filter(query, line)


def filter_log_lines(log_lines, query):
    """Return log lines matching query while preserving raw-buffer indexes."""
    if not _log_filter_active(query):
        indexes = list(range(len(log_lines or [])))
        return LogFilterView(list(log_lines or []), indexes, {})

    lines = []
    indexes = []
    spans_by_index = {}
    for index, line in enumerate(log_lines or []):
        match = match_log_filter(query, line)
        if not match.matched:
            continue
        lines.append(line)
        indexes.append(index)
        spans_by_index[index] = _merge_spans(match.spans or [])
    return LogFilterView(lines, indexes, spans_by_index)


def clamp_filtered_log_cursor(log_lines, cursor, follow_tail, query):
    """Clamp a raw log cursor against the active filtered view."""
    if not _log_filter_active(query):
        return clamp_log_cursor(log_lines, cursor, follow_tail)

    view = filter_log_lines(log_lines, query)
    if not view.indexes:
        return None
    if follow_tail or cursor is None:
        return view.indexes[-1]
    if cursor in view.indexes:
        return cursor
    for index in view.indexes:
        if index >= cursor:
            return index
    return view.indexes[-1]


def move_filtered_log_cursor(log_lines, cursor, delta, query):
    """Move the raw log cursor through the filtered view."""
    if not _log_filter_active(query):
        return move_log_cursor(log_lines, cursor, delta)

    view = filter_log_lines(log_lines, query)
    if not view.indexes:
        return None
    cursor = clamp_filtered_log_cursor(log_lines, cursor, False, query)
    try:
        position = view.indexes.index(cursor)
    except ValueError:
        position = len(view.indexes) - 1
    position = max(0, min(position + delta, len(view.indexes) - 1))
    return view.indexes[position]


def clamp_pretty_scroll(pretty_lines, offset, height):
    """Return a valid top-line offset for a pretty JSON viewport."""
    if not pretty_lines:
        return 0
    height = max(int(height or 1), 1)
    max_offset = max(0, len(pretty_lines) - height)
    try:
        offset = int(offset or 0)
    except (TypeError, ValueError):
        offset = 0
    return max(0, min(offset, max_offset))


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


def clamp_current_op_cursor(snapshot, cursor):
    """Return a valid highlighted currentOp index."""
    entries = snapshot.entries if snapshot and snapshot.entries else []
    if not entries:
        return None
    if cursor is None:
        return 0
    return max(0, min(cursor, len(entries) - 1))


def move_current_op_cursor(snapshot, cursor, delta):
    """Move the highlighted currentOp index by delta."""
    cursor = clamp_current_op_cursor(snapshot, cursor)
    if cursor is None:
        return None
    return max(0, min(cursor + delta, len(snapshot.entries) - 1))


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


def _visible_log_window(log_lines, cursor, height, view_start=None):
    height = max(height, 1)
    if not log_lines:
        return [], 0

    cursor = clamp_log_cursor(log_lines, cursor, follow_tail=False)
    if view_start is None:
        start = max(0, cursor - height + 1)
    else:
        max_start = max(0, len(log_lines) - height)
        start = max(0, min(int(view_start or 0), max_start))
        if cursor < start:
            start = cursor
        elif cursor >= start + height:
            start = cursor - height + 1
        start = max(0, min(start, max_start))
    end = min(len(log_lines), start + height)
    return log_lines[start:end], start


def clamp_log_view_start(log_lines, cursor, height, query="", view_start=0,
                         follow_tail=False):
    """Clamp the visible log-window start while preserving cursor independence."""
    view = filter_log_lines(log_lines, query)
    if not view.indexes:
        return 0

    height = max(int(height or 1), 1)
    display_cursor = _display_cursor_index(view.indexes, cursor)
    if display_cursor is None:
        display_cursor = len(view.indexes) - 1
    if follow_tail:
        return max(0, len(view.indexes) - height)

    max_start = max(0, len(view.indexes) - height)
    start = max(0, min(int(view_start or 0), max_start))
    if display_cursor < start:
        start = display_cursor
    elif display_cursor >= start + height:
        start = display_cursor - height + 1
    return max(0, min(start, max_start))


def _highlight_log_spans(line, spans, resume_ansi=""):
    spans = _merge_spans(spans)
    if not spans:
        return line

    line = str(line)
    output = []
    position = 0
    for start, end in spans:
        start = max(0, min(start, len(line)))
        end = max(start, min(end, len(line)))
        output.append(line[position:start])
        if end > start:
            output.append(
                ANSI_SEARCH_HIT + line[start:end] + ANSI_RESET + resume_ansi)
        position = end
    output.append(line[position:])
    return "".join(output)


def _display_cursor_index(line_indexes, cursor):
    if cursor is None:
        return None
    try:
        return line_indexes.index(cursor)
    except ValueError:
        return None


def format_log_lines(log_lines, cursor, height, yanked_cursor=None,
                     line_indexes=None, match_spans=None, view_start=None):
    """Format log lines with a highlighted cursor marker."""
    if line_indexes is None:
        line_indexes = list(range(len(log_lines or [])))
        display_cursor = cursor
    else:
        line_indexes = list(line_indexes)
        display_cursor = _display_cursor_index(line_indexes, cursor)
    match_spans = match_spans or {}

    visible, start = _visible_log_window(
        log_lines, display_cursor, height, view_start=view_start)
    formatted = []
    for offset, line in enumerate(visible):
        display_index = start + offset
        line_index = line_indexes[display_index]
        selected = line_index == cursor
        yanked = line_index == yanked_cursor
        marker = ">" if selected else " "
        styles = []
        detected_style = severity_style(detect_log_severity(line))
        if detected_style:
            styles.append(detected_style)
        if yanked:
            styles.append(STYLE_YANKED)
        elif selected:
            styles.append(STYLE_SELECTED)
        base_ansi = _style_ansi(styles)
        if not selected and not yanked:
            line = _highlight_log_spans(
                line, match_spans.get(line_index, []), resume_ansi=base_ansi)

        text = "%s %s" % (marker, line)
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


def detect_terminal_theme(environ=None):
    """Return dark or light based on monitor env overrides and COLORFGBG."""
    environ = environ if environ is not None else os.environ
    override = str(environ.get("MRUN_MONITOR_THEME", "")).strip().lower()
    if override in ("dark", "light"):
        return override

    colorfgbg = str(environ.get("COLORFGBG", "")).strip()
    if colorfgbg:
        try:
            background = int(colorfgbg.split(";")[-1])
        except (TypeError, ValueError):
            background = None
        if background is not None:
            if background in (0, 1, 2, 3, 4, 5, 6, 8):
                return "dark"
            return "light"

    return "dark"


def pretty_json_palette(theme=None, environ=None):
    """Return ANSI styles for pretty JSON tokens."""
    theme = theme or detect_terminal_theme(environ)
    if theme == "light":
        return {
            "key": ANSI_PRETTY_KEY_LIGHT,
            "string": ANSI_PRETTY_STRING_LIGHT,
            "number": ANSI_PRETTY_NUMBER_LIGHT,
            "keyword": ANSI_PRETTY_KEYWORD_LIGHT,
            "punctuation": ANSI_PRETTY_PUNCT_LIGHT,
        }
    return {
        "key": ANSI_PRETTY_KEY_DARK,
        "string": ANSI_PRETTY_STRING_DARK,
        "number": ANSI_PRETTY_NUMBER_DARK,
        "keyword": ANSI_PRETTY_KEYWORD_DARK,
        "punctuation": ANSI_PRETTY_PUNCT_DARK,
    }


def _ansi_wrap(style, text):
    return style + text + ANSI_RESET + ANSI_DEFAULT


def _style_json_value(value, palette):
    if JSON_STRING_RE.fullmatch(value):
        return _ansi_wrap(palette["string"], value)
    if value in ("true", "false", "null"):
        return _ansi_wrap(palette["keyword"], value)
    return _ansi_wrap(palette["number"], value)


def colorize_pretty_json_line(line, palette=None):
    """Apply token-level ANSI syntax highlighting to one pretty JSON line."""
    palette = palette or pretty_json_palette()
    line = str(line)
    output = []
    index = 0
    while index < len(line):
        match = JSON_STRING_RE.search(line, index)
        if match is None:
            output.append(_colorize_json_scalars(
                line[index:], palette))
            break

        prefix = line[index:match.start()]
        output.append(_colorize_json_scalars(prefix, palette))
        token = match.group(0)
        after = line[match.end():]
        if after.lstrip().startswith(":"):
            output.append(_ansi_wrap(palette["key"], token))
        else:
            output.append(_ansi_wrap(palette["string"], token))
        index = match.end()

    return "".join(output)


def _colorize_json_scalars(text, palette):
    output = []
    index = 0
    scalar_re = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|true|false|null")
    while index < len(text):
        match = scalar_re.search(text, index)
        if match is None:
            output.append(_colorize_json_punctuation(text[index:], palette))
            break
        output.append(_colorize_json_punctuation(
            text[index:match.start()], palette))
        output.append(_style_json_value(match.group(0), palette))
        index = match.end()
    return "".join(output)


def _colorize_json_punctuation(text, palette):
    output = []
    punctuation = set("{}[]:,")
    for char in text:
        if char in punctuation:
            output.append(_ansi_wrap(palette["punctuation"], char))
        else:
            output.append(char)
    return "".join(output)


def format_pretty_log_lines(pretty_lines, height, offset=0, colorize=True,
                            theme=None, environ=None):
    """Format a fixed pretty JSON view for the log panel."""
    if not pretty_lines:
        return []
    height = max(height, 1)
    offset = clamp_pretty_scroll(pretty_lines, offset, height)
    lines = list(pretty_lines[offset:offset + height])
    if not colorize:
        return lines
    palette = pretty_json_palette(theme, environ)
    return [colorize_pretty_json_line(line, palette) for line in lines]


def build_osc52_sequence(text):
    """Build an OSC 52 clipboard escape sequence for terminal clipboard yank."""
    payload = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return "\033]52;c;%s\a" % payload


def _footer(status_message, controls, width=None):
    text = "%s | %s" % (status_message, controls) if status_message else controls
    text = str(text).replace("\n", " ")
    if width is None:
        return text
    width = max(int(width or 0), 0)
    return _pad_ansi(_truncate_ansi(text, width), width)


def make_panel(title, lines, width, height, focused=False, header_color=None):
    """Render one bordered panel with clipped content."""
    if width < 4 or height < 3:
        return [" " * max(width, 0) for _ in range(max(height, 0))]

    inner_width = width - 2
    inner_height = height - 2
    border_char = "=" if focused else "-"
    title_text = "[%s]" % title if focused else title
    title_text = " %s " % title_text
    title_color = header_color or ""
    header_content = title_text
    if title_color:
        header_content = ANSI_BOLD + title_color + title_text + ANSI_RESET
    top_border_middle = _pad_ansi(
        _truncate_ansi(header_content, inner_width),
        inner_width,
        fillchar=border_char,
    )
    rows = ["+" + top_border_middle + "+"]

    for index in range(inner_height):
        text = lines[index] if index < len(lines) else ""
        styles, text = _split_style(text)
        text = _panel_content_padding(text, inner_width)
        row_text = _pad_ansi(_truncate_ansi(text, inner_width), inner_width)
        ansi = _style_ansi(styles, header_color=header_color)
        if ansi:
            rows.append("|" + ansi + row_text + ANSI_RESET + "|")
        else:
            rows.append("|" + row_text + "|")
    rows.append("+" + border_char * inner_width + "+")

    return rows


def format_cpu_lines(processes, process_metrics, cursor=None, show_cursor=False,
                     role_metrics=None):
    """Format CPU process rows, optionally marking the selected process."""
    role_metrics = role_metrics or {}
    lines = [_table_header(
        "  PORT   PID      ROLE              PROCESS  CPU%   STATUS")]
    selected_index = clamp_process_cursor(processes, cursor)
    if not processes:
        lines.append("  No MongoDB processes found.")
        return lines

    for index, process in enumerate(processes):
        metrics = process_metrics.get(
            process.pid, ProcessMetrics(0.0, 0, "unavailable"))
        role = format_role(role_metrics.get(process.port))
        marker = ">" if show_cursor and index == selected_index else " "
        text = "%s %-6s %-8s %s %-8s %5.1f  %s" % (
            marker,
            process.port,
            process.pid,
            role,
            process.name,
            metrics.cpu_percent,
            metrics.status,
        )
        if show_cursor and index == selected_index:
            text = _styled_line(STYLE_SELECTED, text)
        lines.append(text)
    return lines


def format_current_op_lines(snapshot, cursor=None, height=None, raw=False):
    """Format the top active currentOp entries for the CPU pane."""
    header = "RAW CURRENTOP DOCUMENTS" if raw else (
        "PORT   ROLE              SECS    OP        NS                 CLIENT        DESC")
    lines = [_table_header(header)]
    if snapshot is None:
        lines.append("currentOp not sampled yet.")
        return lines
    if not snapshot.available:
        lines.append("currentOp unavailable: %s" % (
            snapshot.error or "unavailable"))
        return lines
    if not snapshot.entries:
        lines.append("no active currentOp entries")
        return lines

    entries = snapshot.entries
    cursor = clamp_current_op_cursor(snapshot, cursor)
    start = 0
    if height is not None:
        content_height = max(int(height or 1) - 1, 1)
        start = max(0, min(
            cursor - content_height + 1,
            max(0, len(entries) - content_height),
        ))
        entries = entries[start:start + content_height]

    for offset, entry in enumerate(entries):
        index = start + offset
        marker = ">" if index == cursor else " "
        if raw:
            raw_text = current_op_raw_json(entry.raw)
            text = "%s %-6s %s %s" % (
                marker, entry.port, format_role(RoleMetrics(True, entry.role)),
                raw_text)
        else:
            text = "%s %-6s %s %7.1f %-9s %-18s %-13s %s" % (
                marker,
                entry.port,
                format_role(RoleMetrics(True, entry.role)),
                entry.secs_running,
                entry.op,
                entry.ns or "-",
                entry.client or "-",
                entry.desc or entry.opid or "-",
            )
        if index == cursor:
            text = _styled_line(STYLE_SELECTED, text)
        lines.append(text)
    return lines


def format_memory_lines(processes, process_metrics, role_metrics=None):
    """Format memory rows for process RSS usage."""
    role_metrics = role_metrics or {}
    lines = [_table_header("  PORT   ROLE              PID      PROCESS  RSS")]
    if not processes:
        lines.append("  No MongoDB processes found.")
        return lines

    for process in processes:
        metrics = process_metrics.get(
            process.pid, ProcessMetrics(0.0, 0, "unavailable"))
        lines.append("  %-6s %s %-8s %-8s %s" % (
            process.port,
            format_role(role_metrics.get(process.port)),
            process.pid,
            process.name,
            format_bytes(metrics.memory_rss)))
    return lines


def format_network_lines(processes, network_metrics, role_metrics=None):
    """Format MongoDB network counter rates."""
    role_metrics = role_metrics or {}
    lines = [_table_header(
        "  PORT   ROLE              IN       OUT      REQ/s   STATUS")]
    if not processes:
        lines.append("  No MongoDB processes found.")
        return lines

    for process in processes:
        network = network_metrics.get(process.port, NetworkMetrics(False))
        if network.available:
            lines.append("  %-6s %s %-8s %-8s %-7.1f ok" % (
                process.port,
                format_role(role_metrics.get(process.port)),
                format_rate(network.bytes_in_per_sec),
                format_rate(network.bytes_out_per_sec),
                network.requests_per_sec,
            ))
        else:
            lines.append("  %-6s %s %-8s %-8s %-7s %s" % (
                process.port,
                format_role(role_metrics.get(process.port)),
                "-", "-", "-", network_status_label(network)))
    return lines


def format_disk_lines(processes, disk_metrics, role_metrics=None):
    """Format dbpath and logpath disk consumption."""
    role_metrics = role_metrics or {}
    lines = [_table_header(
        "  PORT   ROLE              DB SIZE   LOG SIZE  STATUS")]
    if not processes:
        lines.append("  No MongoDB processes found.")
        return lines

    for process in processes:
        disk = disk_metrics.get(process.port, DiskMetrics(False))
        if disk.available:
            lines.append("  %-6s %s %-9s %-9s ok" % (
                process.port,
                format_role(role_metrics.get(process.port)),
                format_bytes(disk.db_size),
                format_bytes(disk.log_size),
            ))
        else:
            lines.append("  %-6s %s %-9s %-9s unavailable" % (
                process.port,
                format_role(role_metrics.get(process.port)),
                format_bytes(disk.db_size),
                format_bytes(disk.log_size),
            ))
    return lines


def _ascii_bar(value, total, width=10):
    """Render a simple ASCII progress bar."""
    if total <= 0:
        return "[" + "-" * width + "]"
    percent = min(1.0, max(0.0, value / total))
    filled = int(round(percent * width))
    return "[" + "#" * filled + "-" * (width - filled) + "] %i%%" % (percent * 100)


def _unavailable_status_lines(label, snapshot):
    lines = ["%s unavailable." % label]
    if snapshot and snapshot.error:
        lines.append(_truncate(snapshot.error, 78))
    return lines


def format_disk_status_lines(snapshot):
    """Format Disk section for expanded status view."""
    if not snapshot or not snapshot.available:
        return _unavailable_status_lines("Disk status", snapshot)

    lines = []
    disk = snapshot.disk or {}
    wt_bm = disk.get("wt_block_manager", {})
    rates = disk.get("rates", {})

    lines.append("WT Block Manager:")
    lines.append(" Read:    %s" % format_rate(rates.get("bytes_read_per_sec", 0)))
    lines.append(" Written: %s" % format_rate(rates.get("bytes_written_per_sec", 0)))
    lines.append(" Mapped Read: %s" % format_bytes(wt_bm.get("mapped bytes read", 0)))
    lines.append("")

    wt_log = disk.get("wt_log", {})
    lines.append("WT Logging:")
    lines.append(" Log Size:  %s" % format_bytes(wt_log.get("total log size activated", 0)))
    lines.append(" Log Write: %s" % format_bytes(wt_log.get("log bytes written", 0)))
    lines.append(" Log Ops:   %s" % wt_log.get("log write operations", 0))
    lines.append("")

    flushing = disk.get("backgroundFlushing", {})
    lines.append("Background Flushing:")
    lines.append(" Flushes: %s" % flushing.get("flushes", 0))
    lines.append(" Last ms: %sms" % flushing.get("last_ms", 0))
    lines.append(" Avg ms:  %sms" % flushing.get("average_ms", 0))

    return lines


def format_network_status_lines(snapshot):
    """Format Network section for expanded status view."""
    if not snapshot or not snapshot.available:
        return _unavailable_status_lines("Network status", snapshot)

    lines = []
    network = snapshot.network or {}
    conns = network.get("connections", {})
    rates = network.get("rates", {})

    lines.append("Connections:")
    lines.append(" Current:   %s" % conns.get("current", 0))
    lines.append(" Available: %s" % conns.get("available", 0))
    lines.append(" Created:   %s" % conns.get("totalCreated", 0))
    lines.append("")

    lines.append("Op Rates (ops/sec):")
    for op in ["insert", "query", "update", "delete", "getmore", "command"]:
        lines.append(" %-8s %7.1f" % (op.capitalize() + ":", rates.get(op, 0)))

    lines.append("")
    lines.append("Network Rates:")
    lines.append(" In:  %s" % format_rate(rates.get("bytes_in_per_sec", 0)))
    lines.append(" Out: %s" % format_rate(rates.get("bytes_out_per_sec", 0)))

    return lines


def format_storage_status_lines(snapshot):
    """Format Storage Subsystem section for expanded status view."""
    if not snapshot or not snapshot.available:
        return _unavailable_status_lines("Storage status", snapshot)

    lines = []
    storage = snapshot.storage or {}
    wt_cache = storage.get("wt_cache", {})
    wt_tickets = storage.get("wt_tickets", {})
    lock = storage.get("globalLock", {})
    mem = storage.get("mem", {})

    lines.append("WT Cache:")
    used = wt_cache.get("bytes currently in the cache", 0)
    total = wt_cache.get("maximum bytes configured", 0)
    lines.append(" Used:  %s" % _ascii_bar(used, total, width=12))
    lines.append(" Dirty: %s" % format_bytes(wt_cache.get("tracked dirty bytes in the cache", 0)))
    lines.append(" Max:   %s" % format_bytes(total))
    lines.append("")

    lines.append("WT Tickets (available):")
    lines.append(" Read:  %s" % wt_tickets.get("read", {}).get("available", 0))
    lines.append(" Write: %s" % wt_tickets.get("write", {}).get("available", 0))
    lines.append("")

    lines.append("Global Lock:")
    active = lock.get("activeClients", {})
    queue = lock.get("currentQueue", {})
    lines.append(" Active: R:%i / W:%i" % (active.get("readers", 0), active.get("writers", 0)))
    lines.append(" Queue:  R:%i / W:%i" % (queue.get("readers", 0), queue.get("writers", 0)))
    lines.append("")

    lines.append("Memory:")
    lines.append(" Resident: %s" % format_bytes(mem.get("resident", 0) * 1024 * 1024))
    lines.append(" Virtual:  %s" % format_bytes(mem.get("virtual", 0) * 1024 * 1024))

    return lines


def format_subsystem_status_lines(snapshot):
    """Format top-level serverStatus subsystem summaries."""
    if not snapshot or not snapshot.available:
        return _unavailable_status_lines("Subsystem status", snapshot)

    lines = [
        "Top-level serverStatus keys:",
        _table_header("SUBSYSTEM              SUMMARY"),
    ]
    subsystems = snapshot.subsystems or {}
    if not subsystems:
        lines.append("No subsystem fields returned.")
        return lines

    for name, summary in subsystems.items():
        lines.append("%-22s %s" % (
            _truncate(name + ":", 22),
            _truncate(summary, 36),
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

    lines.append(_table_header("TID        CPU%    USER     SYSTEM   TOTAL"))
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
                     stream_paused, process_scope, cpu_thread_view,
                     server_status_active=False, log_filter_query="",
                     log_filter_prompt=False, log_filter_input="",
                     log_filter_match_count=None, log_filter_total=None,
                     current_op_view=False, current_op_raw=False,
                     current_op_namespace=""):
    focused_pane = normalize_pane(focused_pane)
    stream_control = "space resume" if stream_paused else "space pause"
    scope_toggle = "a %s" % ("mrun only" if process_scope == "all" else "all")

    if server_status_active:
        return " | ".join([
            "q/Ctrl+C",
            "E exit",
            "s %s" % format_seconds(refresh_interval),
        ])

    controls = [
        "q",
        "Tab",
        "z quad" if zoom_pane else "z zoom",
        "r",
        "E",
        process_scope,
        scope_toggle,
        "s%s" % format_seconds(refresh_interval),
    ]

    if focused_pane == "cpu":
        controls.append("cpu j/k")
        controls.append("t %s threads" % (
            "list" if cpu_thread_view else "view"))
        controls.append("o %s" % (
            "logs" if current_op_view else "currentOps"))
    elif focused_pane == "logs":
        if current_op_view:
            controls.extend([
                "op j/k",
                "o logs",
                "O %s" % ("formatted" if current_op_raw else "raw"),
                "n ns",
            ])
            if current_op_namespace:
                controls.append("ns %s" % current_op_namespace)
                controls.append("c clear ns")
            return " | ".join(controls)
        elif log_filter_prompt:
            controls.extend([
                "filter: %s_" % log_filter_input,
                "Enter apply",
                "Esc cancel",
            ])
            return " | ".join(controls)

        if _log_filter_active(log_filter_query):
            if log_filter_match_count is not None and log_filter_total is not None:
                controls.append(
                    "%i/%i matches" % (
                        log_filter_match_count,
                        log_filter_total,
                    ))
            controls.append("c clear")

        if pretty_active:
            controls.extend([
                "pretty j/k",
                "p raw",
                "y",
                stream_control,
            ])
        else:
            controls.extend([
                "logs j/k",
                "g latest",
                "p pretty",
                "y",
                stream_control,
                "/ filter",
            ])
    else:
        controls.append("%s pane" % focused_pane)

    return " | ".join(controls)


def render_server_status_view(snapshot, columns, rows, status_message, controls):
    """Render the expanded server status view."""
    if not snapshot:
        snapshot = ServerStatusSnapshot(
            available=False,
            error="No status snapshot available.",
        )

    title_suffix = " (port %s)" % snapshot.port if snapshot.port else ""
    disk_lines = format_disk_status_lines(snapshot)
    network_lines = format_network_status_lines(snapshot)
    storage_lines = format_storage_status_lines(snapshot)
    subsystem_lines = format_subsystem_status_lines(snapshot)

    left_width = columns // 2
    right_width = columns - left_width
    top_height = max(3, rows // 2)
    bottom_height = max(3, rows - top_height)
    disk_panel = make_panel(
        "DISK STATUS" + title_suffix, disk_lines, left_width, top_height,
        header_color=PANE_HEADER_COLORS.get("disk status"))
    network_panel = make_panel(
        "NETWORK STATUS" + title_suffix, network_lines, right_width,
        top_height,
        header_color=PANE_HEADER_COLORS.get("network status"))
    storage_panel = make_panel(
        "STORAGE SUBSYSTEM" + title_suffix, storage_lines, left_width,
        bottom_height,
        header_color=PANE_HEADER_COLORS.get("storage subsystem"))
    subsystem_panel = make_panel(
        "OTHER SUBSYSTEMS" + title_suffix, subsystem_lines, right_width,
        bottom_height,
        header_color=PANE_HEADER_COLORS.get("other subsystems"))

    frame = []
    for left, right in zip(disk_panel, network_panel):
        frame.append(left + right)
    for left, right in zip(storage_panel, subsystem_panel):
        frame.append(left + right)

    frame.append(_footer(status_message, controls, columns))
    return "\n".join(frame)


def _split_heights(total, count):
    """Split a vertical region into stable pane heights."""
    total = max(int(total or 0), 0)
    count = max(int(count or 1), 1)
    base = total // count
    remainder = total % count
    return [base + (1 if index < remainder else 0) for index in range(count)]


def render_dashboard(processes, process_metrics, network_metrics, log_lines,
                     selected_ports=None, terminal_size=None, log_cursor=None,
                     status_message="", zoom_logs=False, yanked_cursor=None,
                     refresh_interval=1.0, pretty_lines=None,
                     stream_paused=False, disk_metrics=None,
                     process_scope="mrun", focused_pane="logs",
                     zoom_pane=None, cpu_cursor=None, cpu_thread_view=False,
                     thread_metrics=None, thread_error="",
                     thread_count=None, pretty_scroll=0,
                     server_status_active=False, status_snapshot=None,
                     log_filter_query="", log_filter_prompt=False,
                     log_filter_input="", role_metrics=None,
                     current_op_view=False, current_ops=None,
                     current_op_raw=False, current_op_cursor=None,
                     log_view_start=0, current_op_namespace=""):
    """Render the full monitor frame (quadrants or expanded status)."""
    if terminal_size is None:
        terminal_size = shutil.get_terminal_size((120, 40))

    columns = max(int(terminal_size.columns or 0), 4)
    rows = max(int(terminal_size.lines or 0) - 1, 1)
    focused_pane = normalize_pane(focused_pane)
    log_filter_view = filter_log_lines(log_lines, log_filter_query)
    filter_active = _log_filter_active(log_filter_query)
    filter_match_count = len(log_filter_view.lines) if filter_active else None
    filter_total = len(log_lines or []) if filter_active else None

    controls = _footer_controls(
        focused_pane,
        zoom_pane if not server_status_active else None,
        refresh_interval,
        pretty_lines is not None,
        stream_paused,
        process_scope,
        cpu_thread_view,
        server_status_active=server_status_active,
        log_filter_query=log_filter_query,
        log_filter_prompt=log_filter_prompt,
        log_filter_input=log_filter_input,
        log_filter_match_count=filter_match_count,
        log_filter_total=filter_total,
        current_op_view=current_op_view,
        current_op_raw=current_op_raw,
        current_op_namespace=current_op_namespace,
    )

    if server_status_active:
        return render_server_status_view(
            status_snapshot, columns, rows, status_message, controls)

    if zoom_pane is None and zoom_logs:
        zoom_pane = "logs"
    zoom_pane = zoom_pane if zoom_pane in PANE_ORDER else None

    selected_ports = selected_ports or []
    if selected_ports:
        log_title = "Log Tail: " + ", ".join(str(port) for port in selected_ports)
    else:
        log_title = "Log Tail"

    pretty_active = pretty_lines is not None
    title_states = []
    if pretty_active:
        title_states.append("Pretty JSON")
    elif stream_paused:
        title_states.append("Paused")
    if filter_active:
        title_states.append("filter: %s" % log_filter_query)
    if title_states:
        log_title += " (" + ", ".join(title_states) + ")"

    disk_metrics = disk_metrics or {}
    role_metrics = role_metrics or {}
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
            processes, process_metrics, cpu_cursor, show_cpu_cursor,
            role_metrics)

    mem_lines = format_memory_lines(processes, process_metrics, role_metrics)
    net_lines = format_network_lines(processes, network_metrics, role_metrics)
    disk_lines = format_disk_lines(processes, disk_metrics, role_metrics)

    log_cursor = clamp_filtered_log_cursor(
        log_lines, log_cursor, follow_tail=False, query=log_filter_query)
    yanked_cursor = clamp_optional_log_cursor(log_lines, yanked_cursor)
    full_log_height = rows - 2
    if pretty_active:
        log_lines_rendered = format_pretty_log_lines(
            pretty_lines, full_log_height, offset=pretty_scroll)
    else:
        if filter_active and not log_filter_view.lines:
            log_lines_rendered = [
                "No log lines match filter: %s" % log_filter_query]
        elif log_filter_view.lines:
            log_lines_rendered = format_log_lines(
                log_filter_view.lines,
                log_cursor,
                full_log_height,
                yanked_cursor,
                line_indexes=log_filter_view.indexes,
                match_spans=log_filter_view.spans_by_index,
                view_start=log_view_start,
            )
        else:
            log_lines_rendered = ["No log selected."]

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
        log_filter_query=log_filter_query,
        log_filter_prompt=log_filter_prompt,
        log_filter_input=log_filter_input,
        log_filter_match_count=filter_match_count,
        log_filter_total=filter_total,
        current_op_view=current_op_view,
        current_op_raw=current_op_raw,
        current_op_namespace=current_op_namespace,
    )

    if zoom_pane:
        if zoom_pane == "logs" and current_op_view:
            state = "Raw" if current_op_raw else "Formatted"
            zoom_title = "Current Ops (%s)" % state
            if current_op_namespace:
                zoom_title += " ns %s" % current_op_namespace
            zoom_lines = format_current_op_lines(
                current_ops,
                cursor=current_op_cursor,
                height=max(rows - 2, 1),
                raw=current_op_raw,
            )
        else:
            zoom_title, zoom_lines = panels[zoom_pane]
        frame = make_panel(
            zoom_title, zoom_lines, columns, rows, focused=True,
            header_color=PANE_HEADER_COLORS.get(zoom_pane))
        frame.append(_footer(status_message, controls, columns))
        return "\n".join(frame)

    left_width = columns // 2
    right_width = columns - left_width
    cpu_height, mem_height, net_height, disk_height = _split_heights(rows, 4)

    activity_content_height = max(rows - 2, 1)
    if current_op_view:
        state = "Raw" if current_op_raw else "Formatted"
        activity_title = "Current Ops (%s)" % state
        if current_op_namespace:
            activity_title += " ns %s" % current_op_namespace
        log_content = format_current_op_lines(
            current_ops,
            cursor=current_op_cursor,
            height=activity_content_height,
            raw=current_op_raw,
        )
    elif pretty_active:
        activity_title = log_title
        log_content = format_pretty_log_lines(
            pretty_lines, activity_content_height, offset=pretty_scroll)
    else:
        activity_title = log_title
        if filter_active and not log_filter_view.lines:
            log_content = [
                "No log lines match filter: %s" % log_filter_query]
        elif log_filter_view.lines:
            log_content = format_log_lines(
                log_filter_view.lines,
                log_cursor,
                activity_content_height,
                yanked_cursor,
                line_indexes=log_filter_view.indexes,
                match_spans=log_filter_view.spans_by_index,
                view_start=log_view_start,
            )
        else:
            log_content = ["No log selected."]

    cpu_panel = make_panel(
        cpu_title, cpu_lines, left_width, cpu_height,
        focused=focused_pane == "cpu",
        header_color=PANE_HEADER_COLORS.get("cpu"))
    mem_panel = make_panel(
        "Memory Usage", mem_lines, left_width, mem_height,
        focused=focused_pane == "memory",
        header_color=PANE_HEADER_COLORS.get("memory"))
    net_panel = make_panel(
        "Network Usage", net_lines, left_width, net_height,
        focused=focused_pane == "network",
        header_color=PANE_HEADER_COLORS.get("network"))
    disk_panel = make_panel(
        "Disk Usage", disk_lines, left_width, disk_height,
        focused=focused_pane == "disk",
        header_color=PANE_HEADER_COLORS.get("disk"))
    left_panel = cpu_panel + mem_panel + net_panel + disk_panel
    log_panel = make_panel(
        activity_title, log_content, right_width, rows,
        focused=focused_pane == "logs",
        header_color=PANE_HEADER_COLORS.get("logs"))

    frame = []
    frame.extend(left + right for left, right in zip(left_panel, log_panel))
    frame.append(_footer(status_message, controls, columns))
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

        stdin_fd = self.stdin.fileno()
        readable, _, _ = select.select([stdin_fd], [], [], 0)
        if readable:
            key = self._read_posix_char()
            if key == "\x03":
                return "ctrl-c"
            if key == "\x1b":
                sequence = key
                deadline = time.time() + ESCAPE_READ_TIMEOUT
                while time.time() < deadline:
                    timeout = max(0.0, deadline - time.time())
                    if not select.select([stdin_fd], [], [], timeout)[0]:
                        break
                    sequence += self._read_posix_char()
                    if _escape_sequence_complete(sequence):
                        break
                return parse_escape_sequence(sequence)
            return key
        return None

    def _read_posix_char(self):
        data = os.read(self.stdin.fileno(), 1)
        if not data:
            return ""
        return data.decode("latin-1")

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
        self.monitor_client_kwargs = dict(network_client_kwargs)
        self.network_sampler = NetworkSampler(
            client_factory=client_factory,
            client_kwargs=self.monitor_client_kwargs,
            auth_required=self.auth_config.requires_credentials(),
        )
        self.role_sampler = RoleSampler(
            client_factory=client_factory,
            client_kwargs=self.monitor_client_kwargs,
            auth_required=self.auth_config.requires_credentials(),
        )
        self.current_op_sampler = CurrentOpSampler(
            client_factory=client_factory,
            client_kwargs=self.monitor_client_kwargs,
            auth_required=self.auth_config.requires_credentials(),
        )
        self.thread_sampler = ThreadSampler()
        self.process_sampler = ProcessSampler()
        self.log_cursor = None
        self.follow_tail = True
        self.zoom_logs = False
        self.zoom_pane = None
        self.focused_pane = "logs"
        self.cpu_cursor = 0
        self.cpu_thread_view = False
        self.cpu_current_op_view = False
        self.current_op_cursor = None
        self.current_op_raw = False
        self.current_op_namespace = ""
        self.status_message = ""
        self.yanked_cursor = None
        self.log_view_start = 0
        self.pretty_lines = None
        self.pretty_scroll = 0
        self.pretty_previous_zoom = None
        self.stream_paused = False
        self.server_status_active = False
        self.log_filter_query = ""
        self.log_filter_prompt = False
        self.log_filter_input = ""

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
            if action == "select-currentop-namespace":
                self._select_current_op_namespace()
                continue
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
                        pretty_scroll=self.pretty_scroll,
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
                        server_status_active=self.server_status_active,
                        status_snapshot=snapshot.status_snapshot,
                        log_filter_query=self.log_filter_query,
                        log_filter_prompt=self.log_filter_prompt,
                        log_filter_input=self.log_filter_input,
                        role_metrics=snapshot.role_metrics,
                        current_op_view=self.cpu_current_op_view,
                        current_ops=snapshot.current_ops,
                        current_op_raw=self.current_op_raw,
                        current_op_cursor=self.current_op_cursor,
                        current_op_namespace=self.current_op_namespace,
                        log_view_start=self.log_view_start,
                    )
                    self.stdout.write("\033[2J\033[H" + frame)
                    self.stdout.flush()

                    wait_start = time.time()
                    wait_timeout = max(0.0, next_sample_at - wait_start)
                    action = self._wait_for_action(
                        terminal, wait_start, snapshot.log_lines,
                        snapshot.processes, current_ops=snapshot.current_ops,
                        timeout=wait_timeout)
                    if action in ("quit", "reselect"):
                        return action
                    if action == "select-currentop-namespace":
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

        process_metrics = self.process_sampler.sample(processes)
        role_metrics = self.role_sampler.sample(processes)
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
        current_ops = None
        if self.cpu_current_op_view:
            current_ops = self.current_op_sampler.sample(
                processes, role_metrics,
                namespace=self.current_op_namespace)
            self.current_op_cursor = clamp_current_op_cursor(
                current_ops, self.current_op_cursor)
        disk_metrics = read_disk_metrics(processes)
        log_lines = read_log_stream(tailer, self.stream_paused)
        self.log_cursor = clamp_filtered_log_cursor(
            log_lines,
            self.log_cursor,
            self.follow_tail,
            self.log_filter_query,
        )
        self.log_view_start = clamp_log_view_start(
            log_lines,
            self.log_cursor,
            self._activity_view_height(),
            self.log_filter_query,
            self.log_view_start,
            self.follow_tail,
        )
        self.yanked_cursor = clamp_optional_log_cursor(
            log_lines, self.yanked_cursor)

        status_snapshot = None
        if self.server_status_active and selected_cpu:
            if not hasattr(self, "status_sampler"):
                self.status_sampler = StatusSampler(
                    client_factory=self.client_factory,
                    client_kwargs=self.monitor_client_kwargs,
                    auth_required=self.auth_config.requires_credentials(),
                )
            status_snapshot = self.status_sampler.sample(selected_cpu)

        return DashboardSnapshot(
            processes=processes,
            process_metrics=process_metrics,
            role_metrics=role_metrics,
            network_metrics=network_metrics,
            disk_metrics=disk_metrics,
            log_lines=log_lines,
            selected_ports=sorted(logpaths),
            thread_metrics=thread_metrics,
            current_ops=current_ops,
            status_snapshot=status_snapshot,
            thread_error=thread_error,
            thread_count=thread_count,
            sampled_at=time.time(),
        )

    def _prime_cpu(self):
        try:
            processes = self._discover_processes()
        except ProcessDiscoveryError:
            return
        self.process_sampler.prime(processes)

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
                         current_ops=None, timeout=None):
        processes = processes or []
        if timeout is None:
            timeout = self.refresh_interval
        while time.time() - start < timeout:
            key = terminal.read_key()
            if self.log_filter_prompt:
                if key in ("ctrl-c", "\x03"):
                    return "quit"
                action = self._handle_log_filter_prompt_key(key, log_lines)
                if action:
                    return action
                time.sleep(KEY_POLL_INTERVAL)
                continue

            if key in ("q", "ctrl-c", "\x03"):
                return "quit"
            if key == "r":
                self._clear_pretty_log_line(restore_zoom=False)
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
            if key == "o":
                self._toggle_cpu_current_op_view()
                return "resample"
            if key == "O":
                self._toggle_current_op_raw()
                return "redraw"

            if key in ("e", "E"):
                self._toggle_server_status_view()
                return "resample"

            if key == "escape" and self.server_status_active:
                self._toggle_server_status_view()
                return "resample"

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
                    if self.cpu_current_op_view:
                        self._move_current_op_cursor(current_ops, -1)
                        return "redraw"
                    if self.pretty_lines is not None:
                        self._move_pretty_scroll(-1)
                        return "redraw"
                    self._move_log_cursor(log_lines, -1)
                    return "redraw"
                if key in ("down", "j"):
                    if self.cpu_current_op_view:
                        self._move_current_op_cursor(current_ops, 1)
                        return "redraw"
                    if self.pretty_lines is not None:
                        self._move_pretty_scroll(1)
                        return "redraw"
                    self._move_log_cursor(log_lines, 1)
                    return "redraw"
                if key == "g":
                    if self.cpu_current_op_view:
                        self.current_op_cursor = 0
                        self.status_message = "highlighted top currentOp"
                        return "redraw"
                    if self.pretty_lines is not None:
                        self.status_message = "press p before jumping latest"
                        return "redraw"
                    self._jump_to_latest(log_lines)
                    return "redraw"
                if self.cpu_current_op_view:
                    if key == "n":
                        return "select-currentop-namespace"
                    if key == "c":
                        self._clear_current_op_namespace()
                        return "resample"
                    if key in ("p", "P", "y", " ", "/", "c"):
                        self.status_message = "press o to return to logs"
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
                if key == "/":
                    self._start_log_filter_prompt()
                    return "redraw"
                if key == "c":
                    self._clear_log_filter(log_lines)
                    return "redraw"
            elif key == " ":
                self.status_message = "space applies to logs pane"
                return "redraw"
            time.sleep(KEY_POLL_INTERVAL)
        return None

    def _toggle_server_status_view(self):
        self.server_status_active = not self.server_status_active
        if self.server_status_active:
            self.status_message = "server status expanded view active"
        else:
            self.status_message = "dashboard view active"

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
            self.cpu_current_op_view = False
            self.status_message = "thread view for port %s pid %s" % (
                process.port, process.pid)
        else:
            self.status_message = "CPU process list"

    def _toggle_cpu_current_op_view(self):
        self.cpu_current_op_view = not self.cpu_current_op_view
        if self.cpu_current_op_view:
            self.cpu_thread_view = False
            self.focused_pane = "logs"
            self.current_op_cursor = None
            self.status_message = "currentOp top 10 view"
        else:
            self.status_message = "CPU process list"

    def _toggle_current_op_raw(self):
        if not self.cpu_current_op_view:
            self.status_message = "press o before toggling currentOp raw"
            return
        self.current_op_raw = not self.current_op_raw
        self.status_message = (
            "currentOp raw view" if self.current_op_raw
            else "currentOp formatted view")

    def _move_current_op_cursor(self, current_ops, delta):
        self.current_op_cursor = move_current_op_cursor(
            current_ops, self.current_op_cursor, delta)
        if self.current_op_cursor is None:
            self.status_message = "no currentOp entry selected"
            return
        self.status_message = "highlighted currentOp entry %i" % (
            self.current_op_cursor + 1)

    def _select_current_op_namespace(self):
        namespaces = []
        try:
            processes = self._discover_processes()
            role_metrics = self.role_sampler.sample(processes)
            snapshot = self.current_op_sampler.sample(
                processes, role_metrics, limit=200, namespace="")
            namespaces = current_op_namespaces(snapshot)
        except (KeyboardInterrupt, ProcessDiscoveryError):
            self.status_message = "currentOp namespace unchanged"
            return

        try:
            namespace = choose_current_op_namespace(
                namespaces, self.input_func, self.stdout)
        except KeyboardInterrupt:
            self.stdout.write("\n")
            self.stdout.flush()
            self.status_message = "currentOp namespace unchanged"
            return

        if namespace is None:
            self.status_message = "currentOp namespace unchanged"
            return

        self.current_op_namespace = namespace
        self.current_op_cursor = None
        self.cpu_current_op_view = True
        self.focused_pane = "logs"
        if namespace:
            self.status_message = "currentOp namespace %s" % namespace
        else:
            self.status_message = "currentOp namespace filter cleared"

    def _clear_current_op_namespace(self):
        if not self.current_op_namespace:
            self.status_message = "no currentOp namespace filter active"
            return
        self.current_op_namespace = ""
        self.current_op_cursor = None
        self.status_message = "currentOp namespace filter cleared"

    def _start_log_filter_prompt(self):
        if self.pretty_lines is not None:
            self.status_message = "press p before filtering logs"
            return
        self.log_filter_prompt = True
        self.log_filter_input = ""
        self.status_message = "type log filter"

    def _handle_log_filter_prompt_key(self, key, log_lines):
        if key is None:
            return None
        if key in ("\r", "\n", "enter"):
            self._apply_log_filter(log_lines)
            return "redraw"
        if key == "escape":
            self.log_filter_prompt = False
            self.log_filter_input = ""
            self.status_message = "log filter unchanged"
            return "redraw"
        if key in ("\x7f", "\b", "backspace"):
            self.log_filter_input = self.log_filter_input[:-1]
            self.status_message = "filter: %s_" % self.log_filter_input
            return "redraw"
        if key == "\x15":
            self.log_filter_input = ""
            self.status_message = "filter: _"
            return "redraw"
        if len(str(key)) == 1 and str(key).isprintable():
            self.log_filter_input += str(key)
            self.status_message = "filter: %s_" % self.log_filter_input
            return "redraw"
        return None

    def _apply_log_filter(self, log_lines):
        query = self.log_filter_input.strip()
        self.log_filter_prompt = False
        self.log_filter_input = ""
        self._clear_pretty_log_line(restore_zoom=False)
        if not query:
            self._clear_log_filter(log_lines)
            return

        self.log_filter_query = query
        self.follow_tail = True
        self.log_cursor = clamp_filtered_log_cursor(
            log_lines, self.log_cursor, True, self.log_filter_query)
        self.log_view_start = clamp_log_view_start(
            log_lines, self.log_cursor, self._activity_view_height(),
            self.log_filter_query, self.log_view_start, True)
        view = filter_log_lines(log_lines, self.log_filter_query)
        if self.log_cursor is None:
            self.status_message = "filter %s matched 0 log lines" % query
        else:
            self.status_message = "filter %s matched %i log lines" % (
                query, len(view.lines))

    def _clear_log_filter(self, log_lines):
        if not _log_filter_active(self.log_filter_query):
            self.log_filter_prompt = False
            self.log_filter_input = ""
            self.status_message = "no log filter active"
            return

        self.log_filter_query = ""
        self.log_filter_prompt = False
        self.log_filter_input = ""
        self.log_cursor = clamp_log_cursor(
            log_lines, self.log_cursor, self.follow_tail)
        self.log_view_start = clamp_log_view_start(
            log_lines, self.log_cursor, self._activity_view_height(),
            self.log_filter_query, self.log_view_start, self.follow_tail)
        self.status_message = "log filter cleared"

    def _move_log_cursor(self, log_lines, delta):
        self._clear_pretty_log_line()
        self.log_cursor = move_filtered_log_cursor(
            log_lines, self.log_cursor, delta, self.log_filter_query)
        self.log_view_start = clamp_log_view_start(
            log_lines, self.log_cursor, self._activity_view_height(),
            self.log_filter_query, self.log_view_start, False)
        view = filter_log_lines(log_lines, self.log_filter_query)
        latest_cursor = view.indexes[-1] if view.indexes else None
        if self.log_cursor is None:
            self.follow_tail = False
            self.status_message = "no log line selected"
        elif delta > 0 and self.log_cursor == latest_cursor:
            self.follow_tail = True
            self.status_message = "following latest log line"
        else:
            self.follow_tail = False
            if _log_filter_active(self.log_filter_query):
                position = view.indexes.index(self.log_cursor) + 1
                self.status_message = "highlighted filtered log line %i" % position
            else:
                self.status_message = (
                    "highlighted log line %i" % (self.log_cursor + 1))

    def _move_pretty_scroll(self, delta):
        if self.pretty_lines is None:
            return

        height = self._pretty_view_height()
        previous = clamp_pretty_scroll(
            self.pretty_lines, self.pretty_scroll, height)
        self.pretty_scroll = clamp_pretty_scroll(
            self.pretty_lines, previous + delta, height)
        if self.pretty_scroll == previous and delta < 0:
            self.status_message = "top of pretty JSON"
        elif self.pretty_scroll == previous and delta > 0:
            self.status_message = "bottom of pretty JSON"
        else:
            end_line = min(
                len(self.pretty_lines),
                self.pretty_scroll + height,
            )
            self.status_message = "pretty JSON lines %i-%i of %i" % (
                self.pretty_scroll + 1,
                end_line,
                len(self.pretty_lines),
            )

    @staticmethod
    def _pretty_view_height():
        terminal_size = shutil.get_terminal_size((120, 40))
        rows = max(terminal_size.lines - 1, 12)
        return max(rows - 2, 1)

    @staticmethod
    def _activity_view_height():
        terminal_size = shutil.get_terminal_size((120, 40))
        rows = max(int(terminal_size.lines or 0) - 1, 1)
        return max(rows - 2, 1)

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
        self.log_view_start = 0
        self.yanked_cursor = None
        self.log_filter_prompt = False
        self.log_filter_input = ""
        self.cpu_cursor = 0
        self.cpu_thread_view = False
        self.cpu_current_op_view = False
        self.current_op_cursor = None
        self.current_op_raw = False
        self.current_op_namespace = ""
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
        self.log_cursor = clamp_filtered_log_cursor(
            log_lines, self.log_cursor, True, self.log_filter_query)
        self.log_view_start = clamp_log_view_start(
            log_lines, self.log_cursor, self._activity_view_height(),
            self.log_filter_query, self.log_view_start, True)
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

        self.log_cursor = clamp_filtered_log_cursor(
            log_lines, self.log_cursor, self.follow_tail,
            self.log_filter_query)
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
        self.pretty_scroll = 0
        self.pretty_previous_zoom = self.zoom_logs
        self.zoom_logs = True
        self.zoom_pane = "logs"
        self.focused_pane = "logs"
        self.status_message = "prettified highlighted log line"

    def _clear_pretty_log_line(self, restore_zoom=True):
        if self.pretty_lines is None:
            self.pretty_scroll = 0
            return
        self.pretty_lines = None
        self.pretty_scroll = 0
        if restore_zoom and self.pretty_previous_zoom is not None:
            self.zoom_logs = self.pretty_previous_zoom
            self.zoom_pane = "logs" if self.zoom_logs else None
        self.pretty_previous_zoom = None

    def _yank_log_line(self, log_lines):
        self.log_cursor = clamp_filtered_log_cursor(
            log_lines, self.log_cursor, self.follow_tail,
            self.log_filter_query)
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

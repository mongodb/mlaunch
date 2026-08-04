#!/usr/bin/env python3
"""Live terminal monitor for local MongoDB server processes."""

import base64
from datetime import datetime
import json
import os
import re
import select
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

import psutil

from mrun.monitor_constants import (
    ANSI_BOLD,
    ANSI_DEFAULT,
    ANSI_DIM,
    ANSI_GREEN,
    ANSI_INVERSE,
    ANSI_KEY_HINT,
    ANSI_PRETTY_KEY_DARK,
    ANSI_PRETTY_KEY_LIGHT,
    ANSI_PRETTY_KEYWORD_DARK,
    ANSI_PRETTY_KEYWORD_LIGHT,
    ANSI_PRETTY_NUMBER_DARK,
    ANSI_PRETTY_NUMBER_LIGHT,
    ANSI_PRETTY_PUNCT_DARK,
    ANSI_PRETTY_PUNCT_LIGHT,
    ANSI_PRETTY_STRING_DARK,
    ANSI_PRETTY_STRING_LIGHT,
    ANSI_RED,
    ANSI_RESET,
    ANSI_ROLE_PRIMARY,
    ANSI_ROLE_SECONDARY,
    ANSI_ROLE_WARNING,
    ANSI_SEARCH_HIT,
    ANSI_TEAL,
    ANSI_YELLOW,
    AUTH_REQUIRED_STATUS,
    DEFAULT_CURRENT_OP_LIMIT,
    ESCAPE_READ_TIMEOUT,
    KEY_POLL_INTERVAL,
    LOG_POLL_INTERVAL,
    MAX_CURRENT_OP_LIMIT,
    NO_MRUN_PROCESSES_MESSAGE,
    NO_PROCESSES_MESSAGE,
    PANE_HEADER_COLORS,
    PANE_ORDER,
    PANE_TITLES,
    PROCESS_DISCOVERY_ERROR_MESSAGE,
    REFRESH_INTERVALS,
    ROLE_PASSWORD_REQUIRED,
    STATUS_DEFAULT_PANELS,
    STATUS_PANEL_COLORS,
    STATUS_PANEL_TITLES,
    STATUS_PROMOTION_SEQUENCE,
    STATUS_RAW_PANEL_PREFIX,
    STATUS_SLOT_LABELS,
    STYLE_MARKERS,
    STYLE_ROLE_DIM,
    STYLE_ROLE_PRIMARY,
    STYLE_ROLE_SECONDARY,
    STYLE_ROLE_WARNING,
    STYLE_SELECTED,
    STYLE_SEVERITY_DEBUG,
    STYLE_SEVERITY_ERROR,
    STYLE_SEVERITY_FATAL,
    STYLE_SEVERITY_INFO,
    STYLE_SEVERITY_WARNING,
    STYLE_TABLE_HEADER,
    STYLE_YANKED,
)

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


ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")
JSON_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
FILTER_VALUE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*:.+")
LOG_DATE_RE = re.compile(r'"t"\s*:\s*\{\s*"\$date"\s*:\s*"([^"]+)"')


class ProcessDiscoveryError(RuntimeError):
    """Raised when the monitor cannot enumerate local processes."""


class SelectionInputError(ValueError):
    """Raised when a monitor prompt receives an invalid selection."""


@dataclass
class MongoProcessInfo:
    """Metadata for a discovered local MongoDB server process."""

    pid: int
    name: str
    port: int
    logpath: str
    dbpath: str
    cmdline: list
    explicit_port: bool = True
    managed: bool = False
    group: str = ""
    group_order: int = 0


@dataclass
class MRunProcessSpec:
    """Expected process metadata loaded from a mongorun startup file."""

    port: int
    logpath: str
    dbpath: str
    cmdline: list
    name: str = ""
    replset: str = ""
    group: str = ""
    group_order: int = 0


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
    normalized_cpu_percent: float = None


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


class ConnectionManager:
    """Reuse MongoDB clients for monitor sampling."""

    def __init__(self, client_factory=None):
        self.client_factory = client_factory or self._default_client_factory
        self.clients = {}

    def get_client(self, host, **kwargs):
        key = self._cache_key(host, kwargs)
        if key not in self.clients:
            self.clients[key] = self.client_factory(host, **kwargs)
        return self.clients[key]

    def close_all(self):
        for client in list(self.clients.values()):
            if hasattr(client, "close"):
                client.close()
        self.clients.clear()

    @classmethod
    def _cache_key(cls, host, kwargs):
        return (
            cls._cache_value(host),
            cls._cache_value(kwargs),
        )

    @classmethod
    def _cache_value(cls, value):
        if isinstance(value, dict):
            return tuple(
                sorted(
                    (key, cls._cache_value(item))
                    for key, item in value.items()
                )
            )
        if isinstance(value, (list, tuple)):
            return tuple(cls._cache_value(item) for item in value)
        if isinstance(value, set):
            return tuple(sorted(cls._cache_value(item) for item in value))
        try:
            hash(value)
        except TypeError:
            return repr(value)
        return value

    @staticmethod
    def _default_client_factory(host, **kwargs):
        from pymongo import MongoClient

        return MongoClient(host, **kwargs)


@dataclass
class TableColumn:
    """Column definition for ANSI-aware monitor tables."""

    key: str
    header: str
    min_width: int = 0
    align: str = "left"


@dataclass
class MongoshTarget:
    """One selectable mongosh launch target."""

    label: str
    uri: str
    kind: str = ""


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
    raw: dict = None
    # Disk (WiredTiger block manager, logging, flushing)
    disk: dict = None
    # Network (connections, opcounters, network rates)
    network: dict = None
    # Storage (WT cache, tickets, global lock, mem)
    storage: dict = None
    # Lightweight summaries for every top-level serverStatus subsystem.
    subsystems: dict = None
    rate_ready: bool = False
    rate_elapsed: float = 0.0
    sampled_at: float = 0.0
    error: str = ""


@dataclass
class MongoCommandCapabilities:
    """Observed MongoDB command support for one monitor sampling path."""

    server_status: bool = True
    current_op: bool = True
    hello: bool = True
    version: str = ""
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


def _strip_config_comment(line):
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote:
            escaped = True
            continue
        if char in ("'", '"'):
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
            continue
        if char == "#" and not quote:
            return line[:index]
    return line


def _clean_config_value(value):
    value = _strip_config_comment(value).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_mongo_config_text(text):
    """Parse common MongoDB YAML-style config keys without a YAML dependency."""
    values = {}
    stack = []
    for raw_line in str(text or "").splitlines():
        line = _strip_config_comment(raw_line.expandtabs()).rstrip()
        if not line.strip() or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, value = line.strip().split(":", 1)
        key = key.strip()
        if not key:
            continue

        while stack and indent <= stack[-1][0]:
            stack.pop()

        value = _clean_config_value(value)
        path = [item[1] for item in stack] + [key]
        if value:
            values[".".join(path)] = value
        else:
            stack.append((indent, key))
    return values


def _resolve_process_path(process, path):
    path = os.path.expanduser(str(path or "").strip())
    if not path or os.path.isabs(path):
        return path
    try:
        cwd = process.cwd()
    except (AttributeError, psutil.AccessDenied, psutil.NoSuchProcess,
            psutil.ZombieProcess, OSError):
        cwd = os.getcwd()
    return os.path.abspath(os.path.join(cwd, path))


def _load_mongo_config_values(process, config_path):
    config_path = _resolve_process_path(process, config_path)
    if not config_path:
        return {}, ""
    try:
        with open(config_path, "r") as fp:
            return _parse_mongo_config_text(fp.read()), config_path
    except OSError:
        return {}, config_path


def _config_value(config_values, keys):
    for key in keys:
        value = config_values.get(key)
        if value not in (None, ""):
            return value
    return None


def _config_int(config_values, keys):
    value = _config_value(config_values, keys)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _config_path_value(config_values, keys, config_path):
    value = _config_value(config_values, keys)
    if not value:
        return ""
    value = os.path.expanduser(str(value))
    if os.path.isabs(value) or not config_path:
        return value
    return os.path.abspath(os.path.join(os.path.dirname(config_path), value))


def _mongo_binary_name_from_cmdline(cmdline):
    for arg in cmdline or []:
        name = _normalize_process_name(arg)
        if name in ("mongod", "mongos"):
            return name
    return ""


def _get_replset_name(cmdline):
    return (
        _get_cmdline_arg(cmdline, "--replSet") or
        _get_cmdline_arg(cmdline, "--replset") or
        ""
    )


def _cmdline_has_flag(cmdline, flag):
    return flag in (cmdline or [])


def _startup_shard_names(parsed_args):
    sharded = parsed_args.get("sharded") if parsed_args else None
    if not sharded:
        return []
    if isinstance(sharded, str):
        values = [sharded]
    else:
        values = list(sharded)
    if len(values) == 1:
        try:
            count = int(values[0])
        except (TypeError, ValueError):
            return values
        return ["shard%.2i" % (index + 1) for index in range(count)]
    return values


def _assign_process_spec_groups(specs, startup_config):
    parsed_args = startup_config.get("parsed_args", {}) if startup_config else {}
    if not parsed_args.get("sharded"):
        return specs

    shard_names = _startup_shard_names(parsed_args)
    discovered_shards = sorted({
        spec.replset for spec in specs.values()
        if spec.replset and _cmdline_has_flag(spec.cmdline, "--shardsvr")
    })
    for shard in discovered_shards:
        if shard not in shard_names:
            shard_names.append(shard)
    shard_order = {name: index for index, name in enumerate(shard_names)}

    for spec in specs.values():
        if spec.name == "mongos":
            spec.group = "mongos"
            spec.group_order = 0
        elif _cmdline_has_flag(spec.cmdline, "--configsvr"):
            spec.group = "config server"
            spec.group_order = 1
        elif (_cmdline_has_flag(spec.cmdline, "--shardsvr") or
              spec.replset in shard_order):
            spec.group = spec.replset or "shard"
            spec.group_order = 2 + shard_order.get(spec.group, len(shard_order))
    return specs


def _normalize_path(path):
    if not path:
        return ""
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _same_path(left, right):
    return bool(left and right and _normalize_path(left) == _normalize_path(right))


def process_to_info(process):
    """Convert a psutil process into MongoProcessInfo, or None if unrelated."""
    try:
        name = _normalize_process_name(process.name())
        if name not in ("mongod", "mongos"):
            return None
        cmdline = process.cmdline()
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return None

    config_values = {}
    config_path = ""
    configured = _get_cmdline_arg(cmdline, "-f") or _get_cmdline_arg(
        cmdline, "--config")
    if configured:
        config_values, config_path = _load_mongo_config_values(
            process, configured)

    cmdline_port = _get_cmdline_int(cmdline, "--port")
    config_port = _config_int(config_values, ("net.port", "port"))
    explicit_port = cmdline_port is not None or config_port is not None
    port = cmdline_port if cmdline_port is not None else (config_port or 27017)
    logpath = _get_cmdline_arg(cmdline, "--logpath") or _config_path_value(
        config_values, ("systemLog.path", "logpath"), config_path)
    dbpath = _get_cmdline_arg(cmdline, "--dbpath") or _config_path_value(
        config_values, ("storage.dbPath", "storage.dbpath", "dbpath"),
        config_path)

    return MongoProcessInfo(
        pid=process.pid,
        name=name,
        port=port,
        logpath=logpath,
        dbpath=dbpath,
        cmdline=cmdline,
        explicit_port=explicit_port,
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


def load_mrun_process_specs(data_dir, startup_config=None):
    """Load expected mongorun server processes from datadir/.mrun_startup."""
    if startup_config is None:
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
            name=_mongo_binary_name_from_cmdline(cmdline),
            replset=_get_replset_name(cmdline),
        )
    return _assign_process_spec_groups(specs, startup_config)


def load_monitor_auth_config(data_dir, startup_config=None):
    """Load monitor auth metadata from datadir/.mrun_startup parsed args."""
    if startup_config is None:
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


def load_monitor_tls_kwargs(data_dir, startup_config=None):
    """Load PyMongo TLS kwargs from datadir/.mrun_startup parsed args."""
    if startup_config is None:
        startup_config = load_mrun_startup_config(data_dir)
    return build_monitor_tls_kwargs(startup_config.get("parsed_args", {}))


def load_monitor_replset_name(data_dir, startup_config=None):
    """Load the configured replica-set name when available."""
    if startup_config is None:
        startup_config = load_mrun_startup_config(data_dir)
    parsed_args = startup_config.get("parsed_args", {})
    if parsed_args.get("replicaset"):
        return parsed_args.get("name") or "replset"
    return ""


def filter_mrun_processes(processes, specs):
    """Keep only discovered processes that are present in mrun startup specs."""
    filtered = []
    for process in processes:
        spec = specs.get(process.port)
        if spec is None or not process_matches_mrun_spec(process, spec):
            continue
        filtered.append(MongoProcessInfo(
            process.pid,
            process.name,
            process.port,
            process.logpath or spec.logpath,
            process.dbpath or spec.dbpath,
            process.cmdline,
            process.explicit_port,
            True,
            spec.group,
            spec.group_order,
        ))
    return sort_processes_for_monitor(filtered)


def process_matches_mrun_spec(process, spec):
    """Return True when a process matches the stored mrun startup command."""
    if spec.name and process.name != spec.name:
        return False
    if not process.explicit_port:
        return False
    if spec.replset and _get_replset_name(process.cmdline) != spec.replset:
        return False
    if spec.dbpath and _same_path(process.dbpath, spec.dbpath):
        return True
    if spec.logpath and _same_path(process.logpath, spec.logpath):
        return True
    return not (spec.dbpath or spec.logpath)


def process_sort_key(process):
    group = getattr(process, "group", "")
    if group:
        return (
            0,
            getattr(process, "group_order", 0),
            process.port,
            process.name,
            process.pid,
        )
    return (1, process.port, process.name, process.pid)


def sort_processes_for_monitor(processes):
    """Sort processes in sharded deployment order when group metadata exists."""
    return sorted(processes, key=process_sort_key)


def annotate_mrun_processes(processes, specs):
    """Mark discovered processes that belong to the selected mrun deployment."""
    annotated = []
    for process in processes:
        spec = specs.get(process.port)
        if spec is not None and process_matches_mrun_spec(process, spec):
            annotated.append(MongoProcessInfo(
                process.pid,
                process.name,
                process.port,
                process.logpath or spec.logpath,
                process.dbpath or spec.dbpath,
                process.cmdline,
                process.explicit_port,
                True,
                spec.group,
                spec.group_order,
            ))
        else:
            annotated.append(MongoProcessInfo(
                process.pid,
                process.name,
                process.port,
                process.logpath,
                process.dbpath,
                process.cmdline,
                process.explicit_port,
                False,
                getattr(process, "group", ""),
                getattr(process, "group_order", 0),
            ))
    return sort_processes_for_monitor(annotated)


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

    return sort_processes_for_monitor(processes)


def discover_mrun_processes(data_dir, process_iter=None, specs=None):
    """Discover running mongod/mongos processes launched by this mrun data dir."""
    if specs is None:
        specs = load_mrun_process_specs(data_dir)
    if not specs:
        return []
    return filter_mrun_processes(discover_mongo_processes(process_iter), specs)


def detected_cpu_count(cpu_count=None):
    """Return a safe logical CPU count for process CPU normalization."""
    if cpu_count is None:
        cpu_count = psutil.cpu_count() or os.cpu_count()
    try:
        cpu_count = int(cpu_count)
    except (TypeError, ValueError):
        cpu_count = 1
    return max(cpu_count, 1)


def normalize_cpu_percent(cpu_percent, cpu_count=None):
    """Normalize process CPU percent so 100% means all logical CPUs."""
    try:
        cpu_percent = float(cpu_percent)
    except (TypeError, ValueError):
        cpu_percent = 0.0
    return cpu_percent / detected_cpu_count(cpu_count)


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

    return ProcessMetrics(
        cpu_percent,
        memory_rss,
        status,
        normalize_cpu_percent(cpu_percent),
    )


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

    def __init__(self, process_factory=None, cpu_count=None):
        self.process_factory = process_factory or psutil.Process
        self.cpu_count = detected_cpu_count(cpu_count)
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

        return ProcessMetrics(
            cpu_percent,
            memory_rss,
            status,
            normalize_cpu_percent(cpu_percent, self.cpu_count),
        )

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


def _as_dict(value):
    return value if isinstance(value, dict) else {}


def _dict_at(document, *keys):
    current = _as_dict(document)
    for key in keys:
        current = _as_dict(current.get(key))
    return current


def _safe_number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _counter_delta(current, previous, key, elapsed):
    delta = (
        _safe_number(current.get(key)) -
        _safe_number(previous.get(key))
    )
    return max(0.0, delta) / elapsed


def _mongo_error_text(exc):
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _is_auth_error_text(text):
    lowered = str(text or "").lower()
    return any(fragment in lowered for fragment in (
        "auth required",
        "authentication",
        "not authorized",
        "unauthorized",
        "requires auth",
        "requires authentication",
    ))


def _is_unsupported_command_text(text):
    lowered = str(text or "").lower()
    return any(fragment in lowered for fragment in (
        "no such command",
        "unknown command",
        "unrecognized field",
        "unknown field",
        "unsupported",
        "invalid field",
        "badvalue",
    ))


def _compat_error_message(errors, command_name):
    errors = [error for error in errors if error]
    if not errors:
        return "%s unavailable" % command_name
    if any(_is_auth_error_text(error) for error in errors):
        return ROLE_PASSWORD_REQUIRED
    if all(_is_unsupported_command_text(error) for error in errors):
        return "unsupported %s command" % command_name
    return errors[-1]


class NetworkSampler:
    """Sample MongoDB serverStatus network counters and expose per-second rates."""

    def __init__(self, client_factory=None, clock=None, client_kwargs=None,
                 auth_required=False, connection_manager=None):
        self.connection_manager = (
            connection_manager or ConnectionManager(client_factory))
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

        try:
            client_kwargs = {
                "directConnection": True,
                "serverSelectionTimeoutMS": 200,
            }
            client_kwargs.update(self.client_kwargs)
            client = self.connection_manager.get_client(
                "localhost:%i" % port,
                **client_kwargs
            )
            status = client.admin.command("serverStatus")
            network = _dict_at(status, "network")
            counters = {
                "bytesIn": _safe_int(network.get("bytesIn", 0)),
                "bytesOut": _safe_int(network.get("bytesOut", 0)),
                "numRequests": _safe_int(network.get("numRequests", 0)),
            }
            return counters, ""
        except Exception as exc:
            return None, _compat_error_message(
                [_mongo_error_text(exc)], "serverStatus")


class RoleSampler:
    """Sample replica-set role information from serverStatus for each process."""

    def __init__(self, client_factory=None, client_kwargs=None,
                 auth_required=False, connection_manager=None):
        self.connection_manager = (
            connection_manager or ConnectionManager(client_factory))
        self.client_kwargs = dict(client_kwargs or {})
        self.auth_required = auth_required
        self.capabilities = {}

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

        try:
            client_kwargs = {
                "directConnection": True,
                "serverSelectionTimeoutMS": 200,
            }
            client_kwargs.update(self.client_kwargs)
            client = self.connection_manager.get_client(
                "localhost:%i" % process.port,
                **client_kwargs
            )
            errors = []
            fallback_role = ""
            try:
                status = client.admin.command("serverStatus")
                self.capabilities[process.port] = MongoCommandCapabilities(
                    server_status=True,
                    version=str(_as_dict(status).get("version") or ""),
                )
                role = role_from_server_status(status)
                if role not in ("Standalone", "Replica Set"):
                    return RoleMetrics(True, role=role)
                fallback_role = role
            except Exception as exc:
                errors.append(_mongo_error_text(exc))

            role, error = read_hello_role(client)
            if role:
                self.capabilities[process.port] = MongoCommandCapabilities(
                    server_status=not errors,
                    hello=True,
                    error="; ".join(errors),
                )
                return RoleMetrics(True, role=role)
            if error:
                errors.append(error)
            if fallback_role:
                self.capabilities[process.port] = MongoCommandCapabilities(
                    server_status=True,
                    hello=False,
                    error=error,
                )
                return RoleMetrics(True, role=fallback_role)
            self.capabilities[process.port] = MongoCommandCapabilities(
                server_status=not errors,
                hello=False,
                error=_compat_error_message(errors, "role"),
            )
            return RoleMetrics(
                False,
                role="unavailable",
                error=_compat_error_message(errors, "role"),
            )
        except Exception as exc:
            self.capabilities[process.port] = MongoCommandCapabilities(
                server_status=False,
                hello=False,
                error=_compat_error_message([_mongo_error_text(exc)], "role"),
            )
            return RoleMetrics(
                False,
                role="unavailable",
                error=_compat_error_message([_mongo_error_text(exc)], "role"),
            )


class CurrentOpSampler:
    """Sample and rank active currentOp entries across visible processes."""

    def __init__(self, client_factory=None, client_kwargs=None,
                 auth_required=False, connection_manager=None):
        self.connection_manager = (
            connection_manager or ConnectionManager(client_factory))
        self.client_kwargs = dict(client_kwargs or {})
        self.auth_required = auth_required
        self.capabilities = {}

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
        try:
            client_kwargs = {
                "directConnection": True,
                "serverSelectionTimeoutMS": 200,
            }
            client_kwargs.update(self.client_kwargs)
            client = self.connection_manager.get_client(
                "localhost:%i" % process.port,
                **client_kwargs
            )
            errors = []
            for command in current_op_command_candidates(namespace):
                try:
                    result = client.admin.command(command)
                    role = role_display(role_metrics.get(process.port))
                    entries = [
                        current_op_entry(process.port, role, raw)
                        for raw in current_op_documents(result)
                        if raw.get("active", True)
                        and (not namespace or raw.get("ns") == namespace)
                    ]
                    self.capabilities[process.port] = MongoCommandCapabilities(
                        current_op=True)
                    return entries, ""
                except Exception as exc:
                    errors.append(_mongo_error_text(exc))
            error = _compat_error_message(errors, "currentOp")
            self.capabilities[process.port] = MongoCommandCapabilities(
                current_op=False, error=error)
            return [], error
        except Exception as exc:
            self.capabilities[process.port] = MongoCommandCapabilities(
                current_op=False,
                error=_compat_error_message(
                    [_mongo_error_text(exc)], "currentOp"))
            return [], _compat_error_message(
                [_mongo_error_text(exc)], "currentOp")


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
                 auth_required=False, connection_manager=None):
        self.connection_manager = (
            connection_manager or ConnectionManager(client_factory))
        self.clock = clock or time.time
        self.client_kwargs = dict(client_kwargs or {})
        self.auth_required = auth_required
        self.previous = {}
        self.capabilities = {}

    def sample(self, process_info):
        """Execute serverStatus and segregate into Disk/Network/Storage."""
        if self.auth_required:
            return ServerStatusSnapshot(
                False,
                port=process_info.port,
                error=AUTH_REQUIRED_STATUS,
            )

        now = self.clock()
        try:
            client_kwargs = {
                "directConnection": True,
                "serverSelectionTimeoutMS": 200,
            }
            client_kwargs.update(self.client_kwargs)
            client = self.connection_manager.get_client(
                "localhost:%i" % process_info.port,
                **client_kwargs
            )
            status = _as_dict(client.admin.command("serverStatus"))
            self.capabilities[process_info.port] = MongoCommandCapabilities(
                server_status=True,
                version=str(status.get("version") or ""),
            )
            subsystems = self._extract_subsystems(status)

            disk = {
                "wt_block_manager": _dict_at(
                    status, "wiredTiger", "block-manager"),
                "wt_log": _dict_at(status, "wiredTiger", "log"),
                "backgroundFlushing": _dict_at(status, "backgroundFlushing"),
                "rates": {},
            }
            network_data = {
                "network": _dict_at(status, "network"),
                "connections": _dict_at(status, "connections"),
                "opcounters": _dict_at(status, "opcounters"),
                "opcountersRepl": _dict_at(status, "opcountersRepl"),
                "rates": {},
            }
            storage = {
                "wt_cache": _dict_at(status, "wiredTiger", "cache"),
                "wt_tickets": _dict_at(
                    status, "wiredTiger", "concurrentTransactions"),
                "globalLock": _dict_at(status, "globalLock"),
                "mem": _dict_at(status, "mem"),
                "extra_info": _dict_at(status, "extra_info"),
            }

            prev = self.previous.get(process_info.port)
            rate_ready = prev is not None
            rate_elapsed = 0.0
            if prev:
                prev_time, prev_status = prev
                rate_elapsed = max(now - prev_time, 0.001)
                disk["rates"] = self._calculate_disk_rates(
                    status, prev_status, rate_elapsed)
                network_data["rates"] = self._calculate_network_rates(
                    status, prev_status, rate_elapsed)

            self.previous[process_info.port] = (now, status)

            return ServerStatusSnapshot(
                available=True,
                port=process_info.port,
                raw=status,
                disk=disk,
                network=network_data,
                storage=storage,
                subsystems=subsystems,
                rate_ready=rate_ready,
                rate_elapsed=rate_elapsed,
                sampled_at=now,
            )

        except Exception as exc:
            self.capabilities[process_info.port] = MongoCommandCapabilities(
                server_status=False,
                error=_compat_error_message(
                    [_mongo_error_text(exc)], "serverStatus"),
            )
            return ServerStatusSnapshot(
                False,
                port=process_info.port,
                error=_compat_error_message(
                    [_mongo_error_text(exc)], "serverStatus"),
            )

    def _calculate_disk_rates(self, current, previous, elapsed):
        curr_wt = _dict_at(current, "wiredTiger", "block-manager")
        prev_wt = _dict_at(previous, "wiredTiger", "block-manager")
        return {
            "bytes_read_per_sec": _counter_delta(
                curr_wt, prev_wt, "bytes read", elapsed),
            "bytes_written_per_sec": _counter_delta(
                curr_wt, prev_wt, "bytes written", elapsed),
        }

    def _calculate_network_rates(self, current, previous, elapsed):
        curr_net = _dict_at(current, "network")
        prev_net = _dict_at(previous, "network")
        curr_ops = _dict_at(current, "opcounters")
        prev_ops = _dict_at(previous, "opcounters")

        rates = {
            "bytes_in_per_sec": _counter_delta(
                curr_net, prev_net, "bytesIn", elapsed),
            "bytes_out_per_sec": _counter_delta(
                curr_net, prev_net, "bytesOut", elapsed),
        }
        for op in ["insert", "query", "update", "delete", "getmore", "command"]:
            rates[op] = _counter_delta(curr_ops, prev_ops, op, elapsed)
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

def role_from_server_status(status):
    """Extract a human-readable node role from serverStatus()."""
    status = _as_dict(status)
    repl = _as_dict(status.get("repl"))
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


def role_from_hello(response):
    """Extract a human-readable node role from hello/isMaster output."""
    response = _as_dict(response)
    if response.get("msg") == "isdbgrid":
        return "Router"
    if response.get("isWritablePrimary") is True:
        return "Primary"
    if response.get("ismaster") is True:
        return "Primary"
    if response.get("secondary") is True:
        return "Secondary"
    if response.get("arbiterOnly") is True:
        return "Arbiter"
    if response.get("hidden") is True:
        return "Hidden"
    if response.get("setName") or response.get("hosts"):
        return "Replica Set"
    return ""


def read_hello_role(client):
    """Read role from hello or legacy isMaster without requiring serverStatus."""
    errors = []
    for command_name in ("hello", "isMaster"):
        try:
            role = role_from_hello(client.admin.command(command_name))
            if role:
                return role, ""
        except Exception as exc:
            errors.append(_mongo_error_text(exc))
    return "", _compat_error_message(errors, "hello")


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


def current_op_command_candidates(namespace=""):
    """Return preferred-to-compatible db.currentOp command shapes."""
    candidates = []
    full_command = {
        "currentOp": 1,
        "$all": True,
        "active": True,
    }
    if namespace:
        with_namespace = dict(full_command)
        with_namespace["ns"] = namespace
        candidates.append(with_namespace)
    candidates.append(full_command)
    candidates.append({"currentOp": 1, "$all": True})
    candidates.append({"currentOp": 1, "active": True})
    candidates.append({"currentOp": 1})

    unique = []
    seen = set()
    for candidate in candidates:
        marker = tuple(sorted(candidate.items()))
        if marker not in seen:
            unique.append(candidate)
            seen.add(marker)
    return unique


def current_op_documents(result):
    """Extract active-operation documents from common currentOp result shapes."""
    if isinstance(result, list):
        return [_as_dict(item) for item in result if isinstance(item, dict)]
    result = _as_dict(result)
    for key in ("inprog", "ops", "currentOps"):
        value = result.get(key)
        if isinstance(value, list):
            return [_as_dict(item) for item in value if isinstance(item, dict)]
    return []


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


def current_op_pretty_json_lines(raw):
    """Render a db.currentOp-style document as pretty JSON lines."""
    return json.dumps(json_safe_value(raw or {}), indent=2).splitlines()


def selected_current_op_entry(snapshot, cursor):
    """Return the selected currentOp entry, or None when no row is selected."""
    if not snapshot or not snapshot.entries:
        return None
    cursor = clamp_current_op_cursor(snapshot, cursor)
    if cursor is None:
        return None
    return snapshot.entries[cursor]


def current_op_summary_text(entry):
    """Return the compact formatted text for a currentOp row."""
    if entry is None:
        return ""
    return "%s %s %.1f %s %s %s %s" % (
        entry.port,
        entry.role or "unknown",
        entry.secs_running,
        entry.op,
        entry.ns or "-",
        entry.client or "-",
        entry.desc or entry.opid or "-",
    )


class LogTailer:
    """Tail selected log files and retain a bounded in-memory buffer."""

    def __init__(self, logpaths_by_port, max_lines=200):
        self.logpaths_by_port = dict(logpaths_by_port)
        self.max_lines = max_lines
        self.lines = deque(maxlen=max_lines)
        self.offsets = {}
        self.missing_paths = set()
        self.sequence = 0
        self._seed()

    def _seed(self):
        records = []
        for port, path in self.logpaths_by_port.items():
            if not path:
                continue
            try:
                with open(path, "rb") as logfile:
                    recent = _tail_file_lines(logfile, 20)
                    logfile.seek(0, os.SEEK_END)
                    self.offsets[port] = logfile.tell()
            except OSError:
                self.offsets[port] = 0
                self._append_missing(port, path)
                continue

            for line in recent:
                records.append(self._line_record(port, line))
        self._append_records(records)

    def poll(self):
        records = []
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
                        records.append(self._line_record(port, line))
                    self.offsets[port] = logfile.tell()
            except OSError:
                self._append_missing(port, path)
        self._append_records(records)
        return list(self.lines)

    def _append_missing(self, port, path):
        marker = (port, path)
        if marker in self.missing_paths:
            return
        self.missing_paths.add(marker)
        self.lines.append("%s | log unavailable: %s" % (port, path))

    def _append_line(self, port, line):
        _, text = self._line_record(port, line)
        self.lines.append(text)

    def _line_record(self, port, line):
        text = line.decode("utf-8", "replace").rstrip()
        timestamp = parse_log_timestamp(text)
        self.sequence += 1
        sort_key = (
            timestamp is None,
            timestamp if timestamp is not None else 0.0,
            self.sequence,
        )
        return sort_key, "%s | %s" % (port, text)

    def _append_records(self, records):
        for _, text in sorted(records, key=lambda record: record[0]):
            self.lines.append(text)


def _tail_file_lines(logfile, line_count, block_size=8192):
    """Return up to line_count trailing lines without scanning the whole file."""
    if line_count <= 0:
        return []

    logfile.seek(0, os.SEEK_END)
    position = logfile.tell()
    chunks = []
    newline_count = 0
    while position > 0 and newline_count <= line_count:
        read_size = min(block_size, position)
        position -= read_size
        logfile.seek(position)
        chunk = logfile.read(read_size)
        chunks.append(chunk)
        newline_count += chunk.count(b"\n")

    data = b"".join(reversed(chunks))
    lines = data.splitlines(keepends=True)
    if position > 0 and lines:
        lines = lines[1:]
    return lines[-line_count:]


def parse_log_timestamp(text):
    """Parse a MongoDB JSON log timestamp into epoch seconds when present."""
    match = LOG_DATE_RE.search(str(text))
    if match is None:
        return None
    value = match.group(1)
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def read_log_stream(tailer, stream_paused):
    """Return visible log lines, optionally without advancing file offsets."""
    if stream_paused:
        return list(tailer.lines)
    return tailer.poll()


def parse_log_selection(selection, candidates):
    """Return selected ports from a comma/space separated index or port list."""
    selection = "" if selection is None else str(selection).strip()
    if selection == "" or selection.lower() == "all":
        return [candidate.port for candidate in candidates]

    selected_ports = []
    candidates = list(candidates or [])
    candidate_ports = [candidate.port for candidate in candidates]
    tokens = [token.strip() for token in selection.replace(",", " ").split()]
    for token in tokens:
        try:
            value = int(token)
        except ValueError:
            raise SelectionInputError(
                "expected indexes or displayed ports, got: %s" % token)

        if 1 <= value <= len(candidates):
            port = candidates[value - 1].port
        elif value in candidate_ports:
            port = value
        else:
            raise SelectionInputError(
                "no displayed log entry matches port/index: %s" % token)

        if port not in selected_ports:
            selected_ports.append(port)

    return selected_ports


def _parse_log_process_selection(selection, candidates):
    """Return selected process objects from index or displayed port tokens."""
    selection = "" if selection is None else str(selection).strip()
    candidates = list(candidates or [])
    if selection == "" or selection.lower() == "all":
        return list(candidates)

    selected = []
    tokens = [token.strip() for token in selection.replace(",", " ").split()]
    for token in tokens:
        try:
            value = int(token)
        except ValueError:
            raise SelectionInputError(
                "expected indexes or displayed ports, got: %s" % token)

        if 1 <= value <= len(candidates):
            matches = [candidates[value - 1]]
        else:
            matches = [process for process in candidates if process.port == value]
            if not matches:
                raise SelectionInputError(
                    "no displayed log entry matches port/index: %s" % token)

        for process in matches:
            if process not in selected:
                selected.append(process)

    return selected


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


def parse_current_op_limit_selection(selection, current=DEFAULT_CURRENT_OP_LIMIT,
                                     maximum=MAX_CURRENT_OP_LIMIT):
    """Return a valid currentOp top-N limit, or None for invalid input."""
    if selection is None:
        return None
    selection = str(selection).strip()
    if selection == "":
        return int(current or DEFAULT_CURRENT_OP_LIMIT)
    try:
        value = int(selection)
    except (TypeError, ValueError):
        return None
    if value < 1:
        return None
    return min(value, int(maximum or MAX_CURRENT_OP_LIMIT))


def choose_current_op_limit(current=DEFAULT_CURRENT_OP_LIMIT,
                            input_func=input, stdout=None,
                            maximum=MAX_CURRENT_OP_LIMIT):
    """Prompt for the number of active currentOp rows to sample and display."""
    stdout = stdout or sys.stdout
    stdout.write(
        "\nEnter currentOp top-N limit (1-%i), or press Enter to keep %i: " % (
            maximum, current))
    stdout.flush()
    return parse_current_op_limit_selection(input_func(), current, maximum)


def _process_role_text(process, role_metrics=None):
    role_metrics = role_metrics or {}
    return role_display(role_metrics.get(process.port))


def _current_op_source_ports(processes, ports):
    """Return currentOp source processes filtered by selected ports."""
    processes = list(processes or [])
    if not ports:
        return processes
    wanted = {int(port) for port in ports}
    return [process for process in processes if process.port in wanted]


def current_op_source_label(processes, ports, role_metrics=None):
    """Return a compact label for the active currentOp source selection."""
    processes = list(processes or [])
    if not ports:
        return "all"
    selected = _current_op_source_ports(processes, ports)
    if not selected or len(selected) == len(processes):
        return "all"
    if len(selected) == 1:
        process = selected[0]
        role = _process_role_text(process, role_metrics)
        if role and role not in ("unknown", "unavailable"):
            return "%s %s" % (role.lower(), process.port)
        return "port %s" % process.port
    return "ports " + ",".join(str(process.port) for process in selected)


def _parse_current_op_source_selection(selection, processes, role_metrics=None):
    """Parse a currentOp source selector into ordered process ports.

    The selector accepts comma- or space-separated indexes, ports, and role
    names such as primary or secondary. Empty input and all select every visible
    process.
    """
    processes = list(processes or [])
    if not processes:
        return []
    role_metrics = role_metrics or {}
    selection = "" if selection is None else str(selection).strip()
    all_ports = [process.port for process in processes]
    if not selection or selection.lower() == "all":
        return all_ports

    tokens = [token for token in re.split(r"[\s,]+", selection) if token]
    selected = []
    by_port = {process.port: process for process in processes}
    for token in tokens:
        normalized = token.lower()
        token_ports = []
        if normalized in ("primary", "primaries"):
            token_ports = [
                process.port for process in processes
                if _process_role_text(process, role_metrics).lower() == "primary"
            ]
        elif normalized in ("secondary", "secondaries"):
            token_ports = [
                process.port for process in processes
                if _process_role_text(process, role_metrics).lower() == "secondary"
            ]
        else:
            try:
                value = int(token)
            except ValueError:
                raise SelectionInputError(
                    "expected indexes, displayed ports, primary, secondary, "
                    "or all; got: %s" % token)
            if 1 <= value <= len(processes):
                token_ports = [processes[value - 1].port]
            elif value in by_port:
                token_ports = [value]
            else:
                raise SelectionInputError(
                    "no currentOp source matches port/index: %s" % token)
        if not token_ports:
            raise SelectionInputError(
                "no currentOp source matches: %s" % token)
        for port in token_ports:
            if port not in selected:
                selected.append(port)
    return selected


def parse_current_op_source_selection(selection, processes, role_metrics=None):
    """Return selected currentOp source ports, or None for invalid input."""
    try:
        return _parse_current_op_source_selection(
            selection, processes, role_metrics)
    except SelectionInputError:
        return None


def require_current_op_source_selection(selection, processes, role_metrics=None):
    """Return selected currentOp source ports or raise with a prompt message."""
    return _parse_current_op_source_selection(
        selection, processes, role_metrics)


def choose_current_op_sources(processes, role_metrics=None, input_func=input,
                              stdout=None):
    """Prompt for currentOp source processes by index, port, or role."""
    stdout = stdout or sys.stdout
    processes = list(processes or [])
    role_metrics = role_metrics or {}
    stdout.write("\nSelect currentOp sources:\n")
    if processes:
        for index, process in enumerate(processes, start=1):
            role = _process_role_text(process, role_metrics)
            stdout.write(
                "  [%i] %-17s port %s pid %s %s\n" % (
                    index, role, process.port, process.pid, process.name))
    else:
        stdout.write("  No MongoDB processes detected.\n")
    prompt = (
        "Enter indexes, displayed ports, primary, secondary, all, "
        "or press Enter for all: ")
    while True:
        stdout.write(prompt)
        stdout.flush()
        try:
            return require_current_op_source_selection(
                input_func(), processes, role_metrics)
        except SelectionInputError as exc:
            stdout.write("Invalid currentOp source selection: %s\n" % exc)


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


def _mongo_uri(hosts, database="admin", replset_name=""):
    database = str(database or "admin").strip("/") or "admin"
    uri = "mongodb://%s/%s" % (hosts, database)
    if replset_name:
        uri += "?replicaSet=%s" % replset_name
    return uri


def _process_mongo_uri(process, database="admin"):
    return _mongo_uri("localhost:%i" % process.port, database)


def _append_mongosh_target(targets, label, uri, kind):
    if not uri:
        return
    if any(target.uri == uri and target.kind != "custom" for target in targets):
        return
    targets.append(MongoshTarget(label, uri, kind))


def mongosh_target_options(processes, role_metrics=None, selected=None,
                           replset_name=""):
    """Build selectable mongosh targets from visible MongoDB processes."""
    processes = sorted(processes or [], key=lambda p: (p.port, p.name, p.pid))
    role_metrics = role_metrics or {}
    targets = []
    primary = None
    for process in processes:
        role = role_display(role_metrics.get(process.port)).lower()
        if role == "primary":
            primary = process
            break
    if primary is not None:
        _append_mongosh_target(
            targets,
            "primary port %s" % primary.port,
            _process_mongo_uri(primary),
            "primary",
        )

    if selected is not None:
        _append_mongosh_target(
            targets,
            "selected port %s" % selected.port,
            _process_mongo_uri(selected),
            "selected",
        )

    if len(processes) > 1:
        hosts = ",".join("localhost:%i" % process.port for process in processes)
        label = "seed list"
        if replset_name:
            label += " replica set %s" % replset_name
        _append_mongosh_target(
            targets,
            label,
            _mongo_uri(hosts, replset_name=replset_name),
            "seed",
        )

    if processes:
        first = processes[0]
        _append_mongosh_target(
            targets,
            "first visible port %s" % first.port,
            _process_mongo_uri(first),
            "first",
        )

    targets.append(MongoshTarget("custom URI", "", "custom"))
    return targets


def parse_mongosh_target_selection(selection, targets):
    """Return a selected mongosh target, default target, custom marker, or None."""
    targets = list(targets or [])
    if not targets:
        return None
    selection = str(selection or "").strip()
    if selection == "":
        for target in targets:
            if target.kind != "custom":
                return target
        return targets[0]
    if selection.startswith("mongodb://") or selection.startswith("mongodb+srv://"):
        return MongoshTarget("typed URI", selection, "typed")
    try:
        index = int(selection)
    except (TypeError, ValueError):
        lowered = selection.lower()
        for target in targets:
            if lowered in (target.kind.lower(), target.label.lower()):
                return target
        return None
    if 1 <= index <= len(targets):
        return targets[index - 1]
    return None


def choose_mongosh_target(targets, input_func=input, stdout=None):
    """Prompt for the mongosh target to open."""
    stdout = stdout or sys.stdout
    targets = list(targets or [])
    stdout.write("\nLaunch mongosh:\n")
    for index, target in enumerate(targets, start=1):
        suffix = "  %s" % target.uri if target.uri else ""
        stdout.write("  [%i] %s%s\n" % (index, target.label, suffix))
    stdout.write(
        "Enter target index/name, URI, or press Enter for the first target: ")
    stdout.flush()
    target = parse_mongosh_target_selection(input_func(), targets)
    if target is None:
        return None
    if target.kind != "custom":
        return target

    stdout.write("Enter MongoDB URI for mongosh, or press Enter to cancel: ")
    stdout.flush()
    uri = str(input_func() or "").strip()
    if not uri:
        return None
    return MongoshTarget("custom URI", uri, "custom")


def mongosh_tls_args(tls_kwargs):
    """Translate monitor PyMongo TLS kwargs into mongosh command flags."""
    tls_kwargs = tls_kwargs or {}
    args = []
    if tls_kwargs.get("tls"):
        args.append("--tls")
    value_flags = {
        "tlsCertificateKeyFile": "--tlsCertificateKeyFile",
        "tlsCertificateKeyFilePassword": "--tlsCertificateKeyFilePassword",
        "tlsCAFile": "--tlsCAFile",
        "tlsCRLFile": "--tlsCRLFile",
    }
    for key, flag in value_flags.items():
        value = tls_kwargs.get(key)
        if value:
            args.extend([flag, str(value)])
    bool_flags = {
        "tlsAllowInvalidCertificates": "--tlsAllowInvalidCertificates",
        "tlsAllowInvalidHostnames": "--tlsAllowInvalidHostnames",
    }
    for key, flag in bool_flags.items():
        if tls_kwargs.get(key):
            args.append(flag)
    return args


def build_mongosh_command(uri, auth_config=None, tls_kwargs=None,
                          executable="mongosh"):
    """Build a shell-free mongosh argv list for an interactive handoff."""
    args = [executable, uri]
    args.extend(mongosh_tls_args(tls_kwargs))
    auth_config = auth_config or MonitorAuthConfig()
    if auth_config.has_credentials():
        args.extend(["--username", auth_config.username])
        args.extend(["--authenticationDatabase", auth_config.auth_db])
        if auth_config.auth_db == "$external":
            args.extend(["--authenticationMechanism", "MONGODB-X509"])
        elif auth_config.password:
            args.append("--password")
    return args


def choose_logpaths(processes, input_func=input, stdout=None):
    """Prompt the user to choose log files to tail."""
    stdout = stdout or sys.stdout
    candidates = list(processes or [])
    if not candidates:
        stdout.write("No MongoDB processes found; log tail quadrant will be empty.\n")
        stdout.flush()
        return {}

    stdout.write("\nSelect MongoDB logs to tail:\n")
    for index, process in enumerate(candidates, start=1):
        if process.managed and process.logpath:
            suffix = process.logpath
        elif process.managed:
            suffix = "managed process; log path unavailable"
        else:
            suffix = "external process; live tail log unavailable"
        stdout.write("  [%i] %s port %s pid %s  %s\n" % (
            index, process.name, process.port, process.pid, suffix))
    prompt = (
        "Enter indexes or displayed ports separated by commas, "
        "or press Enter for managed logs: ")
    while True:
        stdout.write(prompt)
        stdout.flush()
        selection = input_func()
        if str(selection or "").strip() == "":
            selected_processes = [
                process for process in candidates
                if process.managed and process.logpath
            ]
            break
        try:
            selected_processes = _parse_log_process_selection(selection, candidates)
            break
        except SelectionInputError as exc:
            stdout.write("Invalid log selection: %s\n" % exc)

    logpaths = {}
    for process in selected_processes:
        if process.managed and process.logpath:
            logpaths[process.port] = process.logpath
            continue

        stdout.write(
            "live tail log unavailable for port %s pid %s.\n" %
            (process.port, process.pid))
        stdout.write(
            "Enter log path for port %s pid %s, or press Enter to skip: " %
            (process.port, process.pid))
        stdout.flush()
        path = str(input_func() or "").strip()
        if not path:
            continue
        path = os.path.abspath(os.path.expanduser(path))
        if not os.path.isfile(path):
            stdout.write("Invalid log path; not tailing %s.\n" % path)
            continue
        logpaths[process.port] = path

    if not logpaths:
        stdout.write("No logs selected; log tail quadrant will be empty.\n")
        stdout.flush()
    return logpaths


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
        return ANSI_ROLE_PRIMARY + text + ANSI_DEFAULT
    if style == STYLE_ROLE_SECONDARY:
        return ANSI_ROLE_SECONDARY + text + ANSI_DEFAULT
    if style == STYLE_ROLE_WARNING:
        return ANSI_ROLE_WARNING + text + ANSI_DEFAULT
    if style == STYLE_ROLE_DIM:
        return ANSI_DIM + text + ANSI_RESET
    return text


def format_role(role_metrics, width=17):
    """Format a role column with stable visible width."""
    return _pad_ansi(_truncate_ansi(
        colorize_role(role_display(role_metrics)), width), width)


def process_display_name(process):
    """Mark processes outside the selected mrun deployment in all-process mode."""
    if getattr(process, "managed", False):
        return process.name
    return process.name + "*"


def process_group_display(process):
    """Return the sharded deployment group for a process row."""
    return getattr(process, "group", "") or "other processes"


def process_groups_visible(processes):
    """Return True when process rows should include group sections."""
    return any(getattr(process, "group", "") for process in processes or [])


def process_group_sections(processes):
    """Return sharded deployment section labels for process rows."""
    if not process_groups_visible(processes):
        return None
    return [process_group_display(process) for process in processes]


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


def normalize_visible_panes(visible_panes=None):
    """Return visible panes in canonical dashboard order."""
    if visible_panes is None:
        return PANE_ORDER
    visible = set(visible_panes)
    panes = tuple(pane for pane in PANE_ORDER if pane in visible)
    return panes or PANE_ORDER


def normalize_pane(pane, visible_panes=None):
    """Return a valid monitor pane name."""
    panes = normalize_visible_panes(visible_panes)
    if pane in panes:
        return pane
    return "logs" if "logs" in panes else panes[0]


def next_pane(current_pane, delta=1, visible_panes=None):
    """Move focus through dashboard panes."""
    panes = normalize_visible_panes(visible_panes)
    current_pane = normalize_pane(current_pane, panes)
    try:
        index = panes.index(current_pane)
    except ValueError:
        index = panes.index("logs") if "logs" in panes else 0
    return panes[(index + delta) % len(panes)]


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


def clamp_optional_current_op_cursor(snapshot, cursor):
    """Clamp an optional currentOp row index without selecting by default."""
    entries = snapshot.entries if snapshot and snapshot.entries else []
    if not entries or cursor is None:
        return None
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


def key_hint(key):
    """Return a colored footer key hint."""
    return ANSI_BOLD + ANSI_KEY_HINT + str(key) + ANSI_RESET


def control_hint(key, label=""):
    """Return one footer control with a highlighted key and plain label."""
    if label:
        return "%s %s" % (key_hint(key), label)
    return key_hint(key)


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


def _align_ansi(text, width, align="left"):
    """Pad and truncate one table cell by visible display width."""
    width = max(int(width or 0), 0)
    text = _truncate_ansi(str(text), width)
    padding = max(width - visible_width(text), 0)
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def _table_column_widths(columns, rows, max_width=None,
                         indent_width=0, prefix_width=0, gap="  "):
    """Return preferred column widths, shrinking wide cells when needed."""
    widths = []
    for column in columns:
        preferred = max(int(column.min_width or 0), visible_width(column.header))
        for row in rows:
            preferred = max(preferred, visible_width(row.get(column.key, "")))
        widths.append(preferred)

    if max_width is None:
        return widths

    gap_width = len(gap) * max(len(columns) - 1, 0)
    prefix_gap = 1 if prefix_width else 0
    available = max(int(max_width or 0) - indent_width - prefix_width -
                    prefix_gap - gap_width, 1)
    total = sum(widths)
    minimums = [
        min(width, max(1, int(column.min_width or 1),
                       visible_width(column.header)))
        for width, column in zip(widths, columns)
    ]
    index = len(widths) - 1
    while total > available and index >= 0:
        shrink = min(total - available, widths[index] - minimums[index])
        if shrink > 0:
            widths[index] -= shrink
            total -= shrink
        index -= 1
        if index < 0 and total > available:
            index = len(widths) - 1
            if all(width <= minimum for width, minimum in zip(widths, minimums)):
                break
    return widths


def format_table_lines(columns, rows, indent="  ", row_prefixes=None,
                       row_styles=None, max_width=None, section_labels=None):
    """Format an ANSI-aware table with stable column alignment."""
    rows = list(rows or [])
    columns = list(columns or [])
    row_prefixes = list(row_prefixes) if row_prefixes is not None else None
    row_styles = list(row_styles or [])
    section_labels = list(section_labels or [])
    gap = "  "
    prefix_width = (
        max([visible_width(prefix) for prefix in row_prefixes] or [0])
        if row_prefixes is not None else 0
    )
    widths = _table_column_widths(
        columns,
        rows,
        max_width=max_width,
        indent_width=visible_width(indent),
        prefix_width=prefix_width,
        gap=gap,
    )

    def build(cells, prefix=""):
        rendered = [
            _align_ansi(cells.get(column.key, ""), width, column.align)
            for column, width in zip(columns, widths)
        ]
        table = gap.join(rendered)
        if row_prefixes is None:
            return indent + table
        prefix = _align_ansi(prefix, prefix_width)
        return indent + prefix + " " + table

    header_cells = {
        column.key: column.header
        for column in columns
    }
    lines = [_table_header(build(header_cells, " " * prefix_width))]
    previous_section = None
    for index, row in enumerate(rows):
        section = section_labels[index] if index < len(section_labels) else ""
        if section and section != previous_section:
            section_indent = indent
            if row_prefixes is not None:
                section_indent += " " * (prefix_width + 1)
            lines.append(_table_header(section_indent + section))
            previous_section = section
        prefix = row_prefixes[index] if row_prefixes is not None else ""
        text = build(row, prefix)
        styles = row_styles[index] if index < len(row_styles) else []
        if styles:
            text = _styled_line(styles, text)
        lines.append(text)
    return lines


def _row_selection_styles(index, cursor=None, yanked_cursor=None):
    """Return row styles for selected/yanked rows with yank priority."""
    if yanked_cursor is not None and index == yanked_cursor:
        return [STYLE_YANKED]
    if cursor is not None and index == cursor:
        return [STYLE_SELECTED]
    return []


def process_cpu_percent_for_display(metrics, normalized=True):
    """Return raw or normalized process CPU percent for display."""
    if metrics is None:
        return 0.0
    if normalized:
        if metrics.normalized_cpu_percent is not None:
            return metrics.normalized_cpu_percent
        return metrics.cpu_percent
    return metrics.cpu_percent


def format_cpu_lines(processes, process_metrics, cursor=None, show_cursor=False,
                     role_metrics=None, width=None, normalized=True):
    """Format CPU process rows, optionally marking the selected process."""
    role_metrics = role_metrics or {}
    section_labels = process_group_sections(processes)
    selected_index = clamp_process_cursor(processes, cursor)
    if not processes:
        return [_table_header("  PORT   PID      ROLE              PROCESS  CPU%   STATUS"),
                "  No MongoDB processes found."]

    columns = [
        TableColumn("port", "PORT", 6),
        TableColumn("pid", "PID", 8),
        TableColumn("role", "ROLE", 17),
        TableColumn("process", "PROCESS", 8),
        TableColumn("cpu", "CPU%", 5, "right"),
        TableColumn("status", "STATUS", 8),
    ]
    rows = []
    prefixes = []
    styles = []
    for index, process in enumerate(processes):
        metrics = process_metrics.get(
            process.pid, ProcessMetrics(0.0, 0, "unavailable"))
        row = {
            "port": str(process.port),
            "pid": str(process.pid),
            "role": colorize_role(role_display(role_metrics.get(process.port))),
            "process": process_display_name(process),
            "cpu": "%.1f" % process_cpu_percent_for_display(
                metrics, normalized=normalized),
            "status": metrics.status,
        }
        rows.append(row)
        selected = show_cursor and index == selected_index
        prefixes.append(">" if selected else " ")
        styles.append([STYLE_SELECTED] if selected else [])
    return format_table_lines(
        columns, rows, indent="", row_prefixes=prefixes,
        row_styles=styles, max_width=width, section_labels=section_labels)


def format_current_op_lines(snapshot, cursor=None, height=None, raw=False,
                            yanked_cursor=None, width=None):
    """Format the top active currentOp entries for the log activity pane."""
    if snapshot is None:
        return [_table_header("RAW CURRENTOP DOCUMENTS" if raw else "PORT"),
                "currentOp not sampled yet."]
    if not snapshot.available:
        return [_table_header("RAW CURRENTOP DOCUMENTS" if raw else "PORT"),
                "currentOp unavailable: %s" % (
                    snapshot.error or "unavailable")]
    if not snapshot.entries:
        return [_table_header("RAW CURRENTOP DOCUMENTS" if raw else "PORT"),
                "no active currentOp entries"]

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

    if raw:
        columns = [
            TableColumn("port", "PORT", 5),
            TableColumn("role", "ROLE", 9),
            TableColumn("document", "RAW CURRENTOP DOCUMENT", 12),
        ]
    else:
        columns = [
            TableColumn("port", "PORT", 5),
            TableColumn("role", "ROLE", 9),
            TableColumn("secs", "SECS", 4, "right"),
            TableColumn("op", "OP", 4),
            TableColumn("ns", "NS", 9),
            TableColumn("client", "CLIENT", 6),
            TableColumn("desc", "DESC", 4),
        ]
    rows = []
    prefixes = []
    styles = []
    for offset, entry in enumerate(entries):
        index = start + offset
        if raw:
            rows.append({
                "port": str(entry.port),
                "role": colorize_role(entry.role or "unknown"),
                "document": current_op_raw_json(entry.raw),
            })
        else:
            rows.append({
                "port": str(entry.port),
                "role": colorize_role(entry.role or "unknown"),
                "secs": "%.1f" % entry.secs_running,
                "op": entry.op,
                "ns": entry.ns or "-",
                "client": entry.client or "-",
                "desc": entry.desc or entry.opid or "-",
            })
        prefixes.append(">" if index == cursor else " ")
        styles.append(_row_selection_styles(index, cursor, yanked_cursor))
    return format_table_lines(
        columns, rows, indent="", row_prefixes=prefixes,
        row_styles=styles, max_width=width)


def format_memory_lines(processes, process_metrics, role_metrics=None,
                        width=None):
    """Format memory rows for process RSS usage."""
    role_metrics = role_metrics or {}
    section_labels = process_group_sections(processes)
    if not processes:
        return [_table_header("  PORT    ROLE              PID       PROCESS   RSS"),
                "  No MongoDB processes found."]

    columns = [
        TableColumn("port", "PORT", 6),
        TableColumn("role", "ROLE", 17),
        TableColumn("pid", "PID", 8),
        TableColumn("process", "PROCESS", 8),
        TableColumn("rss", "RSS", 8, "right"),
    ]
    rows = []
    for process in processes:
        metrics = process_metrics.get(
            process.pid, ProcessMetrics(0.0, 0, "unavailable"))
        row = {
            "port": str(process.port),
            "role": colorize_role(role_display(role_metrics.get(process.port))),
            "pid": str(process.pid),
            "process": process_display_name(process),
            "rss": format_bytes(metrics.memory_rss),
        }
        rows.append(row)
    return format_table_lines(
        columns, rows, max_width=width, section_labels=section_labels)


def format_network_lines(processes, network_metrics, role_metrics=None,
                         width=None):
    """Format MongoDB network counter rates."""
    role_metrics = role_metrics or {}
    section_labels = process_group_sections(processes)
    if not processes:
        return [_table_header("  PORT    ROLE              IN        OUT       REQ/s   STATUS"),
                "  No MongoDB processes found."]

    columns = [
        TableColumn("port", "PORT", 6),
        TableColumn("role", "ROLE", 17),
        TableColumn("in", "IN", 8, "right"),
        TableColumn("out", "OUT", 8, "right"),
        TableColumn("req", "REQ/s", 7, "right"),
        TableColumn("status", "STATUS", 8),
    ]
    rows = []
    for process in processes:
        network = network_metrics.get(process.port, NetworkMetrics(False))
        if network.available:
            row = {
                "in": format_rate(network.bytes_in_per_sec),
                "out": format_rate(network.bytes_out_per_sec),
                "req": "%.1f" % network.requests_per_sec,
                "status": "ok",
            }
        else:
            row = {
                "in": "-",
                "out": "-",
                "req": "-",
                "status": network_status_label(network),
            }
        row.update({
            "port": str(process.port),
            "role": colorize_role(role_display(role_metrics.get(process.port))),
        })
        rows.append(row)
    return format_table_lines(
        columns, rows, max_width=width, section_labels=section_labels)


def format_disk_lines(processes, disk_metrics, role_metrics=None, width=None):
    """Format dbpath and logpath disk consumption."""
    role_metrics = role_metrics or {}
    section_labels = process_group_sections(processes)
    if not processes:
        return [_table_header("  PORT    ROLE              DB SIZE   LOG SIZE  STATUS"),
                "  No MongoDB processes found."]

    columns = [
        TableColumn("port", "PORT", 6),
        TableColumn("role", "ROLE", 17),
        TableColumn("db_size", "DB SIZE", 9, "right"),
        TableColumn("log_size", "LOG SIZE", 9, "right"),
        TableColumn("status", "STATUS", 8),
    ]
    rows = []
    for process in processes:
        disk = disk_metrics.get(process.port, DiskMetrics(False))
        if disk.available:
            status = "ok"
        else:
            status = "unavailable"
        row = {
            "port": str(process.port),
            "role": colorize_role(role_display(role_metrics.get(process.port))),
            "db_size": format_bytes(disk.db_size),
            "log_size": format_bytes(disk.log_size),
            "status": status,
        }
        rows.append(row)
    return format_table_lines(
        columns, rows, max_width=width, section_labels=section_labels)


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


def _status_rate_window(snapshot):
    if not snapshot or not snapshot.rate_ready:
        return "Rate Window: warming up"
    return "Rate Window: %.1fs" % snapshot.rate_elapsed


def _status_rate_value(snapshot, rates, key):
    if not snapshot or not snapshot.rate_ready:
        return "warming up"
    return format_rate(rates.get(key, 0))


def _status_op_rate_value(snapshot, rates, key):
    if not snapshot or not snapshot.rate_ready:
        return "warming up"
    return "%.1f" % rates.get(key, 0)


def format_disk_status_lines(snapshot):
    """Format Disk section for expanded status view."""
    if not snapshot or not snapshot.available:
        return _unavailable_status_lines("Disk status", snapshot)

    lines = []
    disk = snapshot.disk or {}
    wt_bm = disk.get("wt_block_manager", {})
    rates = disk.get("rates", {})

    lines.append("WT Block Manager:")
    lines.append(" %s" % _status_rate_window(snapshot))
    lines.append(" Read:    %s" % _status_rate_value(
        snapshot, rates, "bytes_read_per_sec"))
    lines.append(" Written: %s" % _status_rate_value(
        snapshot, rates, "bytes_written_per_sec"))
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
        lines.append(" %-8s %s" % (
            op.capitalize() + ":",
            _status_op_rate_value(snapshot, rates, op),
        ))

    lines.append("")
    lines.append("Network Rates:")
    lines.append(" %s" % _status_rate_window(snapshot))
    lines.append(" In:  %s" % _status_rate_value(
        snapshot, rates, "bytes_in_per_sec"))
    lines.append(" Out: %s" % _status_rate_value(
        snapshot, rates, "bytes_out_per_sec"))

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


def _status_raw_panel_key(raw_key):
    return STATUS_RAW_PANEL_PREFIX + str(raw_key)


def _status_raw_key(panel_key):
    if str(panel_key).startswith(STATUS_RAW_PANEL_PREFIX):
        return str(panel_key)[len(STATUS_RAW_PANEL_PREFIX):]
    return None


def _status_panel_label(panel_key):
    raw_key = _status_raw_key(panel_key)
    if raw_key is not None:
        return raw_key
    return STATUS_PANEL_TITLES.get(panel_key, str(panel_key))


def _status_panel_summary(snapshot, panel_key):
    raw_key = _status_raw_key(panel_key)
    if raw_key is not None:
        return (snapshot.subsystems or {}).get(raw_key, "raw section")
    return "panel"


def _status_raw_key_order(snapshot):
    if not snapshot or not snapshot.available:
        return []
    if snapshot.subsystems:
        return list(snapshot.subsystems)
    return list((snapshot.raw or {}).keys())


def _status_represented_raw_keys(panel_slots):
    represented = set()
    for panel_key in panel_slots or STATUS_DEFAULT_PANELS:
        raw_key = _status_raw_key(panel_key)
        if raw_key is not None:
            represented.add(raw_key)
    return represented


def status_selectable_panels(snapshot, panel_slots=None):
    """Return status panels or raw serverStatus keys not currently expanded."""
    if not snapshot or not snapshot.available:
        return []

    slots = tuple(panel_slots or STATUS_DEFAULT_PANELS)
    visible = set(slots)
    represented = _status_represented_raw_keys(slots)
    options = []

    for panel_key in STATUS_DEFAULT_PANELS:
        if panel_key == "subsystems":
            continue
        if panel_key not in visible:
            options.append(panel_key)

    for raw_key in _status_raw_key_order(snapshot):
        if raw_key in represented:
            continue
        options.append(_status_raw_panel_key(raw_key))

    return options


def _clamp_status_cursor(cursor, count):
    if count <= 0:
        return None
    if cursor is None:
        return 0
    return max(0, min(int(cursor), count - 1))


def clamp_status_scroll(count, cursor, scroll, height):
    if count <= 0 or cursor is None:
        return 0
    height = max(int(height or 1), 1)
    scroll = max(0, min(int(scroll or 0), max(count - height, 0)))
    if cursor < scroll:
        return cursor
    if cursor >= scroll + height:
        return max(0, cursor - height + 1)
    return scroll


def _server_status_scalar(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    return str(value)


def _flatten_server_status_value(value, prefix="", max_depth=3, limit=80):
    lines = []

    def add_line(line):
        if len(lines) < limit:
            lines.append(line)

    def walk(current, path, depth):
        if len(lines) >= limit:
            return
        if isinstance(current, dict):
            if not current:
                add_line("%s: {}" % path)
                return
            if depth >= max_depth:
                add_line("%s: %i fields" % (path, len(current)))
                return
            for key, value in current.items():
                next_path = "%s.%s" % (path, key) if path else str(key)
                walk(value, next_path, depth + 1)
                if len(lines) >= limit:
                    break
            return
        if isinstance(current, (list, tuple)):
            if not current:
                add_line("%s: []" % path)
                return
            if depth >= max_depth:
                add_line("%s: %i items" % (path, len(current)))
                return
            for index, value in enumerate(current[:limit]):
                next_path = "%s[%i]" % (path, index)
                walk(value, next_path, depth + 1)
                if len(lines) >= limit:
                    break
            return
        add_line("%s: %s" % (path, _server_status_scalar(current)))

    walk(value, prefix, 0)
    if len(lines) >= limit:
        lines.append("... truncated ...")
    return lines


def format_server_status_detail_lines(snapshot, raw_key):
    """Format one raw top-level serverStatus subsection for a promoted panel."""
    if not snapshot or not snapshot.available:
        return _unavailable_status_lines("serverStatus.%s" % raw_key, snapshot)

    raw = snapshot.raw or {}
    if raw_key not in raw:
        return ["serverStatus.%s unavailable." % raw_key]

    lines = ["serverStatus.%s" % raw_key]
    detail_lines = _flatten_server_status_value(raw.get(raw_key), raw_key)
    if not detail_lines:
        lines.append("No values returned.")
    else:
        lines.extend(detail_lines)
    return lines


def format_subsystem_status_lines(snapshot, cursor=None, scroll=0, height=None,
                                  panel_slots=None):
    """Format selectable serverStatus subsystem summaries."""
    if not snapshot or not snapshot.available:
        return _unavailable_status_lines("Subsystem status", snapshot)

    lines = [
        "Undisplayed serverStatus sections:",
        _table_header("  SECTION                SUMMARY"),
    ]
    options = status_selectable_panels(snapshot, panel_slots)
    if not options:
        lines.append("All known sections are expanded.")
        return lines

    visible_height = None
    if height is not None:
        visible_height = max(int(height) - len(lines), 1)
    cursor = _clamp_status_cursor(cursor, len(options))
    if visible_height is None:
        start = 0
        end = len(options)
    else:
        start = clamp_status_scroll(
            len(options), cursor, scroll, visible_height)
        end = min(len(options), start + visible_height)

    for index, panel_key in enumerate(options[start:end], start):
        selected = cursor == index
        label = _status_panel_label(panel_key)
        summary = _status_panel_summary(snapshot, panel_key)
        marker = ">" if selected else " "
        style = STYLE_SELECTED if selected else ""
        lines.append("%s%s %-22s %s" % (
            style,
            marker,
            _truncate(label + ":", 22),
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
                     current_op_namespace="",
                     current_op_pretty_active=False,
                     current_op_limit=DEFAULT_CURRENT_OP_LIMIT,
                     current_op_paused=False,
                     current_op_source_label_text="all",
                     cpu_normalized=True,
                     server_status_target_label="top-right"):
    focused_pane = normalize_pane(focused_pane)
    stream_control = control_hint(
        "Space", "resume" if stream_paused else "pause")
    scope_toggle = control_hint(
        "a", "mrun only" if process_scope == "all" else "show all")
    source_reselect = control_hint(
        "r", "op sources" if current_op_view else "logs")

    if server_status_active:
        return " | ".join([
            "%s/%s" % (key_hint("q"), key_hint("Ctrl+C")),
            control_hint("E", "exit"),
            "status %s" % key_hint("j/k"),
            control_hint("Enter", "promote"),
            control_hint("r", "reset"),
            "target:%s" % server_status_target_label,
            control_hint("s", format_seconds(refresh_interval)),
        ])

    controls = [
        key_hint("q"),
        key_hint("Tab"),
        control_hint("z", "quad" if zoom_pane else "zoom"),
        source_reselect,
    ]
    tail_controls = [
        "scope:%s" % process_scope,
        scope_toggle,
        control_hint("s", format_seconds(refresh_interval)),
        control_hint("E", "status"),
        control_hint("M", "shell"),
    ]

    if focused_pane == "cpu":
        controls.append("cpu %s" % key_hint("j/k"))
        controls.append(control_hint(
            "C", "%s cpu" % ("raw" if cpu_normalized else "norm")))
        controls.append(control_hint(
            "t", "%s threads" % (
                "list" if cpu_thread_view else "view")))
        controls.extend(tail_controls)
    elif focused_pane == "logs":
        if current_op_view:
            current_op_stream_control = (
                control_hint("Space", "resume ops") if current_op_paused
                else control_hint("Space", "pause ops"))
            controls.extend([
                "op pretty %s" % key_hint("j/k")
                if current_op_pretty_active else "op %s" % key_hint("j/k"),
                control_hint("o", "logs"),
                control_hint("O", "formatted" if current_op_raw else "raw"),
                control_hint("L", "top %i" % current_op_limit),
                control_hint(
                    "p", "list" if current_op_pretty_active else "pretty"),
                key_hint("y"),
                current_op_stream_control,
                control_hint("n", "ns"),
            ])
            if current_op_source_label_text and current_op_source_label_text != "all":
                controls.append("src %s" % current_op_source_label_text)
            if current_op_namespace:
                controls.append("ns %s" % current_op_namespace)
                controls.append(control_hint("c", "clear ns"))
            controls.extend(tail_controls)
            return " | ".join(controls)
        elif log_filter_prompt:
            controls.extend([
                "filter: %s_" % log_filter_input,
                control_hint("Enter", "apply"),
                control_hint("Esc", "cancel"),
            ])
            controls.extend(tail_controls)
            return " | ".join(controls)

        if _log_filter_active(log_filter_query):
            if log_filter_match_count is not None and log_filter_total is not None:
                controls.append(
                    "%i/%i matches" % (
                        log_filter_match_count,
                        log_filter_total,
                    ))
            controls.append(control_hint("c", "clear"))

        if pretty_active:
            controls.extend([
                "pretty %s" % key_hint("j/k"),
                control_hint("p", "raw"),
                key_hint("y"),
                stream_control,
            ])
        else:
            controls.append(control_hint("o", "currentOp"))
            controls.extend([
                "logs %s" % key_hint("j/k"),
                control_hint("g", "latest"),
                control_hint("p", "pretty"),
                stream_control,
                "scope:%s" % process_scope,
                scope_toggle,
            ])
            return " | ".join(controls)
        controls.extend(tail_controls)
    else:
        controls.append("%s pane" % focused_pane)
        controls.extend(tail_controls)

    return " | ".join(controls)


def _status_panel_title(panel_key, snapshot):
    raw_key = _status_raw_key(panel_key)
    if raw_key is not None:
        title = "SERVERSTATUS %s" % raw_key
    else:
        title = STATUS_PANEL_TITLES.get(panel_key, str(panel_key).upper())
    if snapshot and snapshot.port:
        title += " (port %s)" % snapshot.port
    return title


def _status_panel_header_color(panel_key):
    raw_key = _status_raw_key(panel_key)
    if raw_key is not None:
        return PANE_HEADER_COLORS.get("other subsystems")
    return PANE_HEADER_COLORS.get(
        STATUS_PANEL_COLORS.get(panel_key, "other subsystems"))


def _status_panel_lines(panel_key, snapshot, content_height,
                        subsystem_cursor, subsystem_scroll, panel_slots):
    raw_key = _status_raw_key(panel_key)
    if raw_key is not None:
        return format_server_status_detail_lines(snapshot, raw_key)
    if panel_key == "disk":
        return format_disk_status_lines(snapshot)
    if panel_key == "network":
        return format_network_status_lines(snapshot)
    if panel_key == "storage":
        return format_storage_status_lines(snapshot)
    if panel_key == "subsystems":
        return format_subsystem_status_lines(
            snapshot,
            cursor=subsystem_cursor,
            scroll=subsystem_scroll,
            height=content_height,
            panel_slots=panel_slots,
        )
    return ["Unknown status panel: %s" % panel_key]


def _normalize_status_panel_slots(panel_slots):
    slots = list(panel_slots or STATUS_DEFAULT_PANELS)
    while len(slots) < 4:
        slots.append(STATUS_DEFAULT_PANELS[len(slots)])
    return slots[:4]


def render_server_status_view(snapshot, columns, rows, status_message, controls,
                              panel_slots=None, subsystem_cursor=0,
                              subsystem_scroll=0, promotion_index=0):
    """Render the expanded server status view."""
    if not snapshot:
        snapshot = ServerStatusSnapshot(
            available=False,
            error="No status snapshot available.",
        )

    left_width = columns // 2
    right_width = columns - left_width
    top_height = max(3, rows // 2)
    bottom_height = max(3, rows - top_height)
    panel_slots = _normalize_status_panel_slots(panel_slots)
    target_slot = STATUS_PROMOTION_SEQUENCE[
        promotion_index % len(STATUS_PROMOTION_SEQUENCE)]

    panel_specs = [
        (panel_slots[0], left_width, top_height),
        (panel_slots[1], right_width, top_height),
        (panel_slots[2], left_width, bottom_height),
        (panel_slots[3], right_width, bottom_height),
    ]
    rendered_panels = []
    for slot_index, (panel_key, width, height) in enumerate(panel_specs):
        lines = _status_panel_lines(
            panel_key,
            snapshot,
            max(height - 2, 1),
            subsystem_cursor,
            subsystem_scroll,
            panel_slots,
        )
        focused = panel_key == "subsystems"
        if slot_index == target_slot and panel_key != "subsystems":
            lines = ["Next promotion target"] + lines
        rendered_panels.append(make_panel(
            _status_panel_title(panel_key, snapshot),
            lines,
            width,
            height,
            focused=focused,
            header_color=_status_panel_header_color(panel_key),
        ))

    frame = []
    for left, right in zip(rendered_panels[0], rendered_panels[1]):
        frame.append(left + right)
    for left, right in zip(rendered_panels[2], rendered_panels[3]):
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
                     log_view_start=0, current_op_namespace="",
                     current_op_pretty_lines=None,
                     current_op_pretty_scroll=0,
                     current_op_yanked_cursor=None,
                     current_op_limit=DEFAULT_CURRENT_OP_LIMIT,
                     current_op_paused=False,
                     current_op_source_ports=None,
                      cpu_normalized=True,
                      status_panel_slots=None,
                      status_subsystem_cursor=0,
                      status_subsystem_scroll=0,
                      status_promotion_index=0,
                      visible_panes=None):
    """Render the full monitor frame (quadrants or expanded status)."""
    if terminal_size is None:
        terminal_size = shutil.get_terminal_size((120, 40))

    columns = max(int(terminal_size.columns or 0), 4)
    rows = max(int(terminal_size.lines or 0) - 1, 1)
    active_panes = normalize_visible_panes(visible_panes)
    focused_pane = normalize_pane(focused_pane, active_panes)
    if zoom_pane is None and zoom_logs and "logs" in active_panes:
        zoom_pane = "logs"
    zoom_pane = zoom_pane if zoom_pane in active_panes else None
    log_filter_view = filter_log_lines(log_lines, log_filter_query)
    filter_active = _log_filter_active(log_filter_query)
    filter_match_count = len(log_filter_view.lines) if filter_active else None
    filter_total = len(log_lines or []) if filter_active else None
    current_op_pretty_active = (
        current_op_view and current_op_pretty_lines is not None)
    current_op_source_label_text = current_op_source_label(
        processes, current_op_source_ports, role_metrics)
    status_target_slot = STATUS_PROMOTION_SEQUENCE[
        status_promotion_index % len(STATUS_PROMOTION_SEQUENCE)]
    status_target_label = STATUS_SLOT_LABELS.get(
        status_target_slot, "top-right")

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
        current_op_pretty_active=current_op_pretty_active,
        current_op_limit=current_op_limit,
        current_op_paused=current_op_paused,
        current_op_source_label_text=current_op_source_label_text,
        cpu_normalized=cpu_normalized,
        server_status_target_label=status_target_label,
    )

    if server_status_active:
        return render_server_status_view(
            status_snapshot,
            columns,
            rows,
            status_message,
            controls,
            panel_slots=status_panel_slots,
            subsystem_cursor=status_subsystem_cursor,
            subsystem_scroll=status_subsystem_scroll,
            promotion_index=status_promotion_index,
        )

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
    logs_visible = "logs" in active_panes
    metric_panes = [
        pane for pane in ("cpu", "memory", "network", "disk")
        if pane in active_panes
    ]
    two_column = logs_visible and bool(metric_panes)
    left_width = columns // 2 if two_column else columns
    right_width = columns - left_width if two_column else columns
    metric_width = max(left_width - 3, 1)
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
        cpu_title = (
            "CPU Usage (normalized)" if cpu_normalized
            else "CPU Usage (raw)")
        cpu_lines = format_cpu_lines(
            processes, process_metrics, cpu_cursor, show_cpu_cursor,
            role_metrics, width=metric_width, normalized=cpu_normalized)

    mem_lines = format_memory_lines(
        processes, process_metrics, role_metrics, width=metric_width)
    net_lines = format_network_lines(
        processes, network_metrics, role_metrics, width=metric_width)
    disk_lines = format_disk_lines(
        processes, disk_metrics, role_metrics, width=metric_width)

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
        current_op_pretty_active=current_op_pretty_active,
        current_op_limit=current_op_limit,
        current_op_paused=current_op_paused,
        current_op_source_label_text=current_op_source_label_text,
        cpu_normalized=cpu_normalized,
    )

    if zoom_pane:
        if zoom_pane == "logs" and current_op_view:
            state = (
                "Pretty" if current_op_pretty_active
                else ("Raw" if current_op_raw else "Formatted"))
            zoom_title = "Current Ops (%s, top %i)" % (
                state, current_op_limit)
            title_states = []
            if current_op_source_label_text != "all":
                title_states.append(current_op_source_label_text)
            if current_op_paused:
                title_states.append("paused")
            if title_states:
                zoom_title += ", " + ", ".join(title_states)
            if current_op_namespace:
                zoom_title += " ns %s" % current_op_namespace
            if current_op_pretty_active:
                zoom_lines = format_pretty_log_lines(
                    current_op_pretty_lines,
                    max(rows - 2, 1),
                    offset=current_op_pretty_scroll)
            else:
                zoom_lines = format_current_op_lines(
                    current_ops,
                    cursor=current_op_cursor,
                    height=max(rows - 2, 1),
                    raw=current_op_raw,
                    yanked_cursor=current_op_yanked_cursor,
                    width=max(columns - 3, 1),
                )
        else:
            zoom_title, zoom_lines = panels[zoom_pane]
        frame = make_panel(
            zoom_title, zoom_lines, columns, rows, focused=True,
            header_color=PANE_HEADER_COLORS.get(zoom_pane))
        frame.append(_footer(status_message, controls, columns))
        return "\n".join(frame)

    activity_content_height = max(rows - 2, 1)
    if current_op_view:
        state = (
            "Pretty" if current_op_pretty_active
            else ("Raw" if current_op_raw else "Formatted"))
        activity_title = "Current Ops (%s, top %i)" % (
            state, current_op_limit)
        title_states = []
        if current_op_source_label_text != "all":
            title_states.append(current_op_source_label_text)
        if current_op_paused:
            title_states.append("paused")
        if title_states:
            activity_title += ", " + ", ".join(title_states)
        if current_op_namespace:
            activity_title += " ns %s" % current_op_namespace
        if current_op_pretty_active:
            log_content = format_pretty_log_lines(
                current_op_pretty_lines,
                activity_content_height,
                offset=current_op_pretty_scroll)
        else:
            log_content = format_current_op_lines(
                current_ops,
                cursor=current_op_cursor,
                height=activity_content_height,
                raw=current_op_raw,
                yanked_cursor=current_op_yanked_cursor,
                width=max(right_width - 3, 1),
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

    frame = []
    if metric_panes:
        metric_heights = _split_heights(rows, len(metric_panes))
        metric_panel = []
        for pane, height in zip(metric_panes, metric_heights):
            title, lines = panels[pane]
            metric_panel.extend(make_panel(
                title, lines, left_width, height,
                focused=focused_pane == pane,
                header_color=PANE_HEADER_COLORS.get(pane)))
    else:
        metric_panel = []

    if logs_visible and metric_panes:
        log_panel = make_panel(
            activity_title, log_content, right_width, rows,
            focused=focused_pane == "logs",
            header_color=PANE_HEADER_COLORS.get("logs"))
        frame.extend(left + right for left, right in zip(metric_panel, log_panel))
    elif logs_visible:
        frame.extend(make_panel(
            activity_title, log_content, columns, rows,
            focused=focused_pane == "logs",
            header_color=PANE_HEADER_COLORS.get("logs")))
    else:
        frame.extend(metric_panel)
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
    if sequence == "\x1b[5~":
        return "pageup"
    if sequence == "\x1b[6~":
        return "pagedown"
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
                 monitor_auth_db=None, mongosh_runner=None,
                 mongosh_executable="mongosh", which_func=None):
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
        self.startup_config = load_mrun_startup_config(data_dir)
        self.process_specs = load_mrun_process_specs(
            data_dir, self.startup_config)
        self.auth_config = load_monitor_auth_config(
            data_dir, self.startup_config).with_overrides(
            username=monitor_username,
            password=monitor_password,
            auth_db=monitor_auth_db,
        )
        network_client_kwargs = load_monitor_tls_kwargs(
            data_dir, self.startup_config)
        self.monitor_tls_kwargs = dict(network_client_kwargs)
        network_client_kwargs.update(self.auth_config.client_kwargs())
        self.monitor_client_kwargs = dict(network_client_kwargs)
        self.replset_name = load_monitor_replset_name(
            data_dir, self.startup_config)
        self.mongosh_runner = mongosh_runner or subprocess.call
        self.mongosh_executable = mongosh_executable
        self.which_func = which_func or shutil.which
        self.connection_manager = ConnectionManager(client_factory)
        self.network_sampler = NetworkSampler(
            connection_manager=self.connection_manager,
            client_kwargs=self.monitor_client_kwargs,
            auth_required=self.auth_config.requires_credentials(),
        )
        self.role_sampler = RoleSampler(
            connection_manager=self.connection_manager,
            client_kwargs=self.monitor_client_kwargs,
            auth_required=self.auth_config.requires_credentials(),
        )
        self.current_op_sampler = CurrentOpSampler(
            connection_manager=self.connection_manager,
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
        self.visible_panes = list(PANE_ORDER)
        self.cpu_cursor = 0
        self.cpu_normalized = True
        self.cpu_thread_view = False
        self.cpu_current_op_view = False
        self.current_op_cursor = None
        self.current_op_raw = False
        self.current_op_namespace = ""
        self.current_op_pretty_lines = None
        self.current_op_pretty_scroll = 0
        self.current_op_yanked_cursor = None
        self.current_op_limit = DEFAULT_CURRENT_OP_LIMIT
        self.current_op_paused = False
        self.current_op_paused_snapshot = None
        self.current_op_source_ports = []
        self.status_message = ""
        self.yanked_cursor = None
        self.log_view_start = 0
        self.pretty_lines = None
        self.pretty_scroll = 0
        self.pretty_previous_zoom = None
        self.stream_paused = False
        self.server_status_active = False
        self.status_panel_slots = list(STATUS_DEFAULT_PANELS)
        self.status_subsystem_cursor = 0
        self.status_subsystem_scroll = 0
        self.status_promotion_index = 0
        self.log_filter_query = ""
        self.log_filter_prompt = False
        self.log_filter_input = ""

    def run(self):
        try:
            return self._run()
        finally:
            self.connection_manager.close_all()

    def _run(self):
        processes = self._discover_processes_or_report()
        if processes is None:
            return 1
        if not processes:
            self.stdout.write(self._no_processes_message() + "\n")
            self.stdout.flush()
            return 1

        if not self._interactive_terminal():
            self.stdout.write("mrun monitor requires an interactive terminal.\n")
            self.stdout.flush()
            return 1

        logpaths = None
        while True:
            try:
                if logpaths is None:
                    logpaths = choose_logpaths(
                        processes, self.input_func, self.stdout)
            except KeyboardInterrupt:
                self.stdout.write("\n")
                self.stdout.flush()
                return 0
            action = self._run_dashboard(logpaths)
            if action == "select-currentop-namespace":
                self._select_current_op_namespace()
                continue
            if action == "select-currentop-limit":
                self._select_current_op_limit()
                continue
            if action == "select-currentop-sources":
                self._select_current_op_sources()
                continue
            if action == "launch-mongosh":
                self._launch_mongosh_admin_shell()
                continue
            if action != "reselect":
                return 0
            logpaths = None
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
                    else:
                        snapshot.log_lines = self._read_log_lines(tailer)

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
                        current_op_pretty_lines=self.current_op_pretty_lines,
                        current_op_pretty_scroll=self.current_op_pretty_scroll,
                        current_op_yanked_cursor=self.current_op_yanked_cursor,
                        current_op_limit=self.current_op_limit,
                        current_op_paused=self.current_op_paused,
                        current_op_source_ports=self.current_op_source_ports,
                        cpu_normalized=self.cpu_normalized,
                        status_panel_slots=self.status_panel_slots,
                        status_subsystem_cursor=self.status_subsystem_cursor,
                        status_subsystem_scroll=self.status_subsystem_scroll,
                        status_promotion_index=self.status_promotion_index,
                        log_view_start=self.log_view_start,
                        visible_panes=self.visible_panes,
                    )
                    self.stdout.write("\033[2J\033[H" + frame)
                    self.stdout.flush()

                    wait_start = time.time()
                    wait_timeout = min(
                        max(0.0, next_sample_at - wait_start),
                        LOG_POLL_INTERVAL,
                    )
                    action = self._wait_for_action(
                        terminal, wait_start, snapshot.log_lines,
                        snapshot.processes, current_ops=snapshot.current_ops,
                        status_snapshot=snapshot.status_snapshot,
                        timeout=wait_timeout)
                    if action in ("quit", "reselect"):
                        return action
                    if action == "select-currentop-namespace":
                        return action
                    if action == "select-currentop-limit":
                        return action
                    if action == "select-currentop-sources":
                        return action
                    if action == "launch-mongosh":
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
            current_op_processes = _current_op_source_ports(
                processes, self.current_op_source_ports)
            if self.current_op_paused and self.current_op_paused_snapshot is not None:
                current_ops = self.current_op_paused_snapshot
            elif not current_op_processes:
                current_ops = CurrentOpSnapshot(
                    False, [], error="selected currentOp sources unavailable")
                self.current_op_paused_snapshot = current_ops
            else:
                current_ops = self.current_op_sampler.sample(
                    current_op_processes, role_metrics,
                    limit=self.current_op_limit,
                    namespace=self.current_op_namespace)
                self.current_op_paused_snapshot = current_ops
            self.current_op_cursor = clamp_current_op_cursor(
                current_ops, self.current_op_cursor)
            self.current_op_yanked_cursor = clamp_optional_current_op_cursor(
                current_ops, self.current_op_yanked_cursor)
            if self.current_op_pretty_lines is not None:
                self.current_op_pretty_scroll = clamp_pretty_scroll(
                    self.current_op_pretty_lines,
                    self.current_op_pretty_scroll,
                    self._activity_view_height(),
                )
        disk_metrics = read_disk_metrics(processes)
        log_lines = self._read_log_lines(tailer)

        status_snapshot = None
        if self.server_status_active and selected_cpu:
            if not hasattr(self, "status_sampler"):
                self.status_sampler = StatusSampler(
                    connection_manager=self.connection_manager,
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

    def _read_log_lines(self, tailer):
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
        return log_lines

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
            return annotate_mrun_processes(
                discover_mongo_processes(self.process_iter), self.process_specs)
        return discover_mrun_processes(
            self.data_dir, self.process_iter, self.process_specs)

    def _no_processes_message(self):
        if self.process_scope == "all":
            return NO_PROCESSES_MESSAGE
        return NO_MRUN_PROCESSES_MESSAGE

    def _wait_for_action(self, terminal, start, log_lines, processes=None,
                         current_ops=None, status_snapshot=None,
                         timeout=None):
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
            if self.server_status_active:
                if key in ("e", "E", "escape"):
                    self._toggle_server_status_view()
                    return "resample"
                if key == "s":
                    self._cycle_refresh_interval()
                    return "redraw"
                if key == "a":
                    self._toggle_process_scope()
                    return "reselect"
                if key == "r":
                    self._reset_status_panels()
                    return "redraw"
                if key in ("up", "k"):
                    self._move_status_subsystem_cursor(status_snapshot, -1)
                    return "redraw"
                if key in ("down", "j"):
                    self._move_status_subsystem_cursor(status_snapshot, 1)
                    return "redraw"
                if key in ("pageup", "\x02"):
                    self._page_status_subsystem_cursor(status_snapshot, -1)
                    return "redraw"
                if key in ("pagedown", "\x06"):
                    self._page_status_subsystem_cursor(status_snapshot, 1)
                    return "redraw"
                if key in ("\r", "\n", "enter"):
                    self._promote_status_subsystem(status_snapshot)
                    return "redraw"
                time.sleep(KEY_POLL_INTERVAL)
                continue
            if key in ("1", "2", "3", "4", "5"):
                self._toggle_visible_pane(int(key) - 1)
                return "redraw"
            if key == "r":
                self._clear_pretty_log_line(restore_zoom=False)
                self._clear_current_op_pretty()
                if self.cpu_current_op_view:
                    return "select-currentop-sources"
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
            if key == "M":
                return "launch-mongosh"
            if key == "O":
                self._toggle_current_op_raw()
                return "redraw"
            if self.cpu_current_op_view and key == "n":
                return "select-currentop-namespace"
            if self.cpu_current_op_view and key == "L":
                return "select-currentop-limit"
            if self.cpu_current_op_view and key == "c":
                self._clear_current_op_namespace()
                return "resample"
            if self.cpu_current_op_view and key == " ":
                was_paused = self.current_op_paused
                self._toggle_current_op_sampling(current_ops)
                return "resample" if was_paused else "redraw"

            if key in ("e", "E"):
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
                if key == "C":
                    self._toggle_cpu_normalization()
                    return "redraw"
                if key in ("t", "T"):
                    self._toggle_cpu_thread_view(processes)
                    return "resample"
            elif self.focused_pane == "logs":
                if key == "o":
                    self._toggle_log_current_op_view()
                    return "resample"
                if key in ("up", "k"):
                    if self.cpu_current_op_view:
                        if self.current_op_pretty_lines is not None:
                            self._move_current_op_pretty_scroll(-1)
                            return "redraw"
                        self._move_current_op_cursor(current_ops, -1)
                        return "redraw"
                    if self.pretty_lines is not None:
                        self._move_pretty_scroll(-1)
                        return "redraw"
                    self._move_log_cursor(log_lines, -1)
                    return "redraw"
                if key in ("down", "j"):
                    if self.cpu_current_op_view:
                        if self.current_op_pretty_lines is not None:
                            self._move_current_op_pretty_scroll(1)
                            return "redraw"
                        self._move_current_op_cursor(current_ops, 1)
                        return "redraw"
                    if self.pretty_lines is not None:
                        self._move_pretty_scroll(1)
                        return "redraw"
                    self._move_log_cursor(log_lines, 1)
                    return "redraw"
                if key == "g":
                    if self.cpu_current_op_view:
                        if self.current_op_pretty_lines is not None:
                            self.status_message = (
                                "press p before selecting currentOp rows")
                            return "redraw"
                        self.current_op_cursor = 0
                        self.status_message = "highlighted top currentOp"
                        return "redraw"
                    if self.pretty_lines is not None:
                        self.status_message = "press p before jumping latest"
                        return "redraw"
                    self._jump_to_latest(log_lines)
                    return "redraw"
                if self.cpu_current_op_view:
                    if key in ("p", "P"):
                        self._toggle_pretty_current_op(current_ops)
                        return "redraw"
                    if key == "y":
                        self._yank_current_op(current_ops)
                        return "redraw"
                    if key == "/":
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

    @staticmethod
    def _status_selector_visible_rows():
        terminal_size = shutil.get_terminal_size((120, 40))
        rows = max(int(terminal_size.lines or 0) - 1, 1)
        top_height = max(3, rows // 2)
        bottom_height = max(3, rows - top_height)
        return max(bottom_height - 4, 1)

    def _status_options(self, snapshot):
        return status_selectable_panels(snapshot, self.status_panel_slots)

    def _move_status_subsystem_cursor(self, snapshot, delta):
        options = self._status_options(snapshot)
        if not options:
            self.status_subsystem_cursor = 0
            self.status_subsystem_scroll = 0
            self.status_message = "no undisplayed serverStatus sections"
            return

        cursor = _clamp_status_cursor(
            self.status_subsystem_cursor, len(options))
        cursor = max(0, min(cursor + delta, len(options) - 1))
        self.status_subsystem_cursor = cursor
        self.status_subsystem_scroll = clamp_status_scroll(
            len(options),
            cursor,
            self.status_subsystem_scroll,
            self._status_selector_visible_rows(),
        )
        self.status_message = "selected serverStatus %s" % _status_panel_label(
            options[cursor])

    def _page_status_subsystem_cursor(self, snapshot, direction):
        step = self._status_selector_visible_rows()
        self._move_status_subsystem_cursor(snapshot, direction * step)

    def _promote_status_subsystem(self, snapshot):
        options = self._status_options(snapshot)
        if not options:
            self.status_message = "no undisplayed serverStatus sections"
            return

        cursor = _clamp_status_cursor(
            self.status_subsystem_cursor, len(options))
        selected_panel = options[cursor]
        target_slot = STATUS_PROMOTION_SEQUENCE[
            self.status_promotion_index % len(STATUS_PROMOTION_SEQUENCE)]
        previous_panel = self.status_panel_slots[target_slot]
        self.status_panel_slots[target_slot] = selected_panel
        self.status_promotion_index = (
            self.status_promotion_index + 1) % len(STATUS_PROMOTION_SEQUENCE)

        remaining = self._status_options(snapshot)
        self.status_subsystem_cursor = (
            _clamp_status_cursor(cursor, len(remaining)) or 0)
        self.status_subsystem_scroll = clamp_status_scroll(
            len(remaining),
            self.status_subsystem_cursor,
            self.status_subsystem_scroll,
            self._status_selector_visible_rows(),
        )
        next_slot = STATUS_PROMOTION_SEQUENCE[
            self.status_promotion_index % len(STATUS_PROMOTION_SEQUENCE)]
        self.status_message = (
            "promoted %s to %s; minimized %s; next target %s" % (
                _status_panel_label(selected_panel),
                STATUS_SLOT_LABELS.get(target_slot, "pane"),
                _status_panel_label(previous_panel),
                STATUS_SLOT_LABELS.get(next_slot, "pane"),
            ))

    def _reset_status_panels(self):
        self.status_panel_slots = list(STATUS_DEFAULT_PANELS)
        self.status_subsystem_cursor = 0
        self.status_subsystem_scroll = 0
        self.status_promotion_index = 0
        self.status_message = "serverStatus layout reset"

    def _toggle_server_status_view(self):
        self.server_status_active = not self.server_status_active
        if self.server_status_active:
            self.status_message = "server status expanded view active"
        else:
            self.status_message = "dashboard view active"

    def _focus_next_pane(self, delta):
        previous_pane = self.focused_pane
        self.focused_pane = next_pane(
            self.focused_pane, delta, self.visible_panes)
        if previous_pane == "logs" and self.focused_pane != "logs":
            self._clear_pretty_log_line(restore_zoom=False)
        if self.zoom_pane is not None:
            self.zoom_pane = self.focused_pane
        self.zoom_logs = self.zoom_pane == "logs"
        self.status_message = "focus %s pane" % self.focused_pane

    def _toggle_visible_pane(self, pane_index):
        if pane_index < 0 or pane_index >= len(PANE_ORDER):
            return
        pane = PANE_ORDER[pane_index]
        if pane in self.visible_panes:
            if len(self.visible_panes) == 1:
                self.status_message = "at least one pane must remain visible"
                return
            self.visible_panes.remove(pane)
            if self.focused_pane == pane:
                self.focused_pane = normalize_pane(
                    self.focused_pane, self.visible_panes)
            if self.zoom_pane == pane:
                self.zoom_pane = None
            self.status_message = "%s pane hidden" % pane
        else:
            self.visible_panes.append(pane)
            self.visible_panes = list(normalize_visible_panes(self.visible_panes))
            self.status_message = "%s pane visible" % pane
        self.zoom_logs = self.zoom_pane == "logs"

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

    def _toggle_cpu_normalization(self):
        self.cpu_normalized = not self.cpu_normalized
        self.status_message = (
            "CPU normalized view" if self.cpu_normalized
            else "CPU raw view")

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
            self.current_op_paused = False
            self.current_op_paused_snapshot = None
            self.status_message = "thread view for port %s pid %s" % (
                process.port, process.pid)
        else:
            self.status_message = "CPU process list"

    def _toggle_log_current_op_view(self):
        self.cpu_current_op_view = not self.cpu_current_op_view
        if self.cpu_current_op_view:
            self.cpu_thread_view = False
            self.focused_pane = "logs"
            self.current_op_cursor = None
            self.current_op_paused = False
            self.current_op_paused_snapshot = None
            self.status_message = "log currentOp top %i view" % (
                self.current_op_limit)
        else:
            self._clear_current_op_pretty()
            self.current_op_paused = False
            self.current_op_paused_snapshot = None
            self.status_message = "log tail view"

    def _toggle_current_op_raw(self):
        if not self.cpu_current_op_view:
            self.status_message = "press o before toggling currentOp raw"
            return
        self._clear_current_op_pretty()
        self.current_op_raw = not self.current_op_raw
        self.status_message = (
            "currentOp raw view" if self.current_op_raw
            else "currentOp formatted view")

    def _move_current_op_cursor(self, current_ops, delta):
        self._clear_current_op_pretty()
        self.current_op_cursor = move_current_op_cursor(
            current_ops, self.current_op_cursor, delta)
        if self.current_op_cursor is None:
            self.status_message = "no currentOp entry selected"
            return
        self.status_message = "highlighted currentOp entry %i" % (
            self.current_op_cursor + 1)

    def _select_current_op_namespace(self):
        self._clear_current_op_pretty()
        namespaces = []
        try:
            processes = self._discover_processes()
            role_metrics = self.role_sampler.sample(processes)
            processes = _current_op_source_ports(
                processes, self.current_op_source_ports)
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
        self.current_op_yanked_cursor = None
        self.current_op_paused = False
        self.current_op_paused_snapshot = None
        self.cpu_current_op_view = True
        self.focused_pane = "logs"
        if namespace:
            self.status_message = "currentOp namespace %s" % namespace
        else:
            self.status_message = "currentOp namespace filter cleared"

    def _select_current_op_limit(self):
        self._clear_current_op_pretty()
        try:
            limit = choose_current_op_limit(
                self.current_op_limit, self.input_func, self.stdout)
        except KeyboardInterrupt:
            self.stdout.write("\n")
            self.stdout.flush()
            self.status_message = "currentOp limit unchanged"
            return

        if limit is None:
            self.status_message = "currentOp limit unchanged"
            return

        self.current_op_limit = limit
        self.current_op_cursor = None
        self.current_op_yanked_cursor = None
        self.current_op_paused = False
        self.current_op_paused_snapshot = None
        self.cpu_current_op_view = True
        self.focused_pane = "logs"
        self.status_message = "log currentOp top %i view" % limit

    def _select_current_op_sources(self):
        self._clear_current_op_pretty()
        try:
            processes = self._discover_processes()
        except ProcessDiscoveryError as exc:
            self.status_message = str(exc)
            return
        if not processes:
            self.status_message = "no MongoDB processes available for currentOp"
            return

        role_metrics = self.role_sampler.sample(processes)
        try:
            ports = choose_current_op_sources(
                processes, role_metrics, self.input_func, self.stdout)
        except KeyboardInterrupt:
            self.stdout.write("\n")
            self.stdout.flush()
            self.status_message = "currentOp source selection cancelled"
            return
        if ports is None:
            self.status_message = "invalid currentOp source selection"
            return

        self.current_op_source_ports = ports
        self.current_op_cursor = None
        self.current_op_yanked_cursor = None
        self.current_op_paused = False
        self.current_op_paused_snapshot = None
        self.cpu_current_op_view = True
        self.focused_pane = "logs"
        label = current_op_source_label(processes, ports, role_metrics)
        self.status_message = "currentOp sources: %s" % label

    def _toggle_current_op_sampling(self, current_ops):
        self.current_op_paused = not self.current_op_paused
        if self.current_op_paused:
            if current_ops is not None:
                self.current_op_paused_snapshot = current_ops
            self.status_message = "currentOp sampling paused"
        else:
            self.current_op_paused_snapshot = None
            self.status_message = "currentOp sampling resumed"

    def _clear_current_op_namespace(self):
        if not self.current_op_namespace:
            self.status_message = "no currentOp namespace filter active"
            return
        self.current_op_namespace = ""
        self.current_op_cursor = None
        self.current_op_yanked_cursor = None
        self.current_op_paused = False
        self.current_op_paused_snapshot = None
        self._clear_current_op_pretty()
        self.status_message = "currentOp namespace filter cleared"

    def _launch_mongosh_admin_shell(self):
        executable = self.which_func(self.mongosh_executable)
        if not executable:
            self.status_message = "mongosh not found in PATH"
            return
        if self.auth_config.requires_credentials():
            self.status_message = "mongosh requires credentials for this deployment"
            return

        try:
            processes = self._discover_processes()
        except ProcessDiscoveryError as exc:
            self.status_message = str(exc)
            return
        if not processes:
            self.status_message = "no MongoDB process available for mongosh"
            return

        role_metrics = self.role_sampler.sample(processes)
        selected = selected_process(processes, self.cpu_cursor)
        targets = mongosh_target_options(
            processes,
            role_metrics,
            selected=selected,
            replset_name=self.replset_name,
        )
        try:
            target = choose_mongosh_target(
                targets, self.input_func, self.stdout)
        except KeyboardInterrupt:
            self.stdout.write("\n")
            self.stdout.flush()
            self.status_message = "mongosh launch cancelled"
            return
        if target is None:
            self.status_message = "mongosh launch cancelled"
            return

        command = build_mongosh_command(
            target.uri,
            self.auth_config,
            self.monitor_tls_kwargs,
            executable,
        )
        self.stdout.write(
            "\nLaunching mongosh for %s. Exit mongosh to return to monitor.\n" %
            target.label)
        self.stdout.flush()
        try:
            result = self.mongosh_runner(command)
        except OSError as exc:
            self.status_message = "mongosh failed: %s" % exc
            return

        if result in (None, 0):
            self.status_message = "mongosh exited"
        else:
            self.status_message = "mongosh exited with status %s" % result

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

    def _move_current_op_pretty_scroll(self, delta):
        if self.current_op_pretty_lines is None:
            return

        height = self._activity_view_height()
        previous = clamp_pretty_scroll(
            self.current_op_pretty_lines,
            self.current_op_pretty_scroll,
            height)
        self.current_op_pretty_scroll = clamp_pretty_scroll(
            self.current_op_pretty_lines, previous + delta, height)
        if self.current_op_pretty_scroll == previous and delta < 0:
            self.status_message = "top of currentOp JSON"
        elif self.current_op_pretty_scroll == previous and delta > 0:
            self.status_message = "bottom of currentOp JSON"
        else:
            end_line = min(
                len(self.current_op_pretty_lines),
                self.current_op_pretty_scroll + height,
            )
            self.status_message = "currentOp JSON lines %i-%i of %i" % (
                self.current_op_pretty_scroll + 1,
                end_line,
                len(self.current_op_pretty_lines),
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
        self.current_op_yanked_cursor = None
        self.current_op_limit = DEFAULT_CURRENT_OP_LIMIT
        self.current_op_paused = False
        self.current_op_paused_snapshot = None
        self.current_op_source_ports = []
        self.zoom_pane = None
        self.zoom_logs = False
        self.follow_tail = True
        self.stream_paused = False
        self._clear_pretty_log_line(restore_zoom=False)
        self._clear_current_op_pretty()
        self.status_message = (
            "showing all MongoDB processes (* external)"
            if self.process_scope == "all"
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

    def _toggle_pretty_current_op(self, current_ops):
        if self.current_op_pretty_lines is not None:
            self._clear_current_op_pretty()
            self.status_message = "currentOp list view"
            return

        entry = selected_current_op_entry(current_ops, self.current_op_cursor)
        if entry is None:
            self.status_message = "no currentOp entry selected"
            return

        self.current_op_cursor = clamp_current_op_cursor(
            current_ops, self.current_op_cursor)
        self.current_op_pretty_lines = current_op_pretty_json_lines(entry.raw)
        self.current_op_pretty_scroll = 0
        self.focused_pane = "logs"
        self.status_message = "prettified highlighted currentOp"

    def _clear_current_op_pretty(self):
        self.current_op_pretty_lines = None
        self.current_op_pretty_scroll = 0

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

    def _yank_current_op(self, current_ops):
        entry = selected_current_op_entry(current_ops, self.current_op_cursor)
        if entry is None:
            self.status_message = "no currentOp entry selected"
            return

        self.current_op_cursor = clamp_current_op_cursor(
            current_ops, self.current_op_cursor)
        if self.current_op_pretty_lines is not None:
            text = "\n".join(self.current_op_pretty_lines)
        elif self.current_op_raw:
            text = current_op_raw_json(entry.raw)
        else:
            text = current_op_summary_text(entry)
        self.stdout.write(build_osc52_sequence(text))
        self.stdout.flush()
        self.current_op_yanked_cursor = self.current_op_cursor
        self.status_message = "yanked highlighted currentOp"

    def _interactive_terminal(self):
        return (
            hasattr(self.stdin, "isatty") and self.stdin.isatty() and
            hasattr(self.stdout, "isatty") and self.stdout.isatty()
        )

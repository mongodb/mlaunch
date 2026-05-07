import io
import json
import os
import time

import psutil
import pytest

from mrun.monitor import (
    ANSI_BOLD,
    ANSI_GREEN,
    ANSI_INVERSE,
    ANSI_RED,
    ANSI_DIM,
    ANSI_ROLE_PRIMARY,
    ANSI_ROLE_SECONDARY,
    ANSI_ROLE_WARNING,
    ANSI_SEARCH_HIT,
    ANSI_TEAL,
    ANSI_YELLOW,
    AUTH_REQUIRED_STATUS,
    build_monitor_tls_kwargs,
    build_mongosh_command,
    build_osc52_sequence,
    clamp_pretty_scroll,
    choose_current_op_limit,
    choose_current_op_namespace,
    choose_current_op_sources,
    choose_mongosh_target,
    colorize_pretty_json_line,
    CurrentOpEntry,
    current_op_namespaces,
    current_op_raw_json,
    current_op_pretty_json_lines,
    current_op_source_label,
    CurrentOpSampler,
    CurrentOpSnapshot,
    dashboard_snapshot_due,
    detect_terminal_theme,
    detect_log_severity,
    DiskMetrics,
    filter_log_lines,
    format_current_op_lines,
    format_cpu_lines,
    format_disk_lines,
    format_memory_lines,
    format_network_lines,
    format_pretty_log_lines,
    format_log_lines,
    filter_mrun_processes,
    LogTailer,
    load_monitor_auth_config,
    load_monitor_replset_name,
    load_monitor_tls_kwargs,
    load_mrun_process_specs,
    make_panel,
    MonitorAuthConfig,
    MongoshTarget,
    mongosh_target_options,
    mongosh_tls_args,
    Monitor,
    MongoProcessInfo,
    NetworkMetrics,
    NetworkSampler,
    NO_MRUN_PROCESSES_MESSAGE,
    NO_PROCESSES_MESSAGE,
    ProcessDiscoveryError,
    ProcessMetrics,
    ProcessSampler,
    read_disk_metrics,
    discover_mongo_processes,
    move_log_cursor,
    move_process_cursor,
    next_refresh_interval,
    next_pane,
    network_status_label,
    parse_escape_sequence,
    parse_current_op_namespace_selection,
    parse_current_op_limit_selection,
    parse_current_op_source_selection,
    parse_mongosh_target_selection,
    parse_log_selection,
    pretty_json_palette,
    process_to_info,
    prettify_log_line,
    read_log_stream,
    render_dashboard,
    render_server_status_view,
    ROLE_PASSWORD_REQUIRED,
    role_from_server_status,
    RoleMetrics,
    RoleSampler,
    score_log_filter,
    selected_process,
    strip_ansi,
    StatusSampler,
    ServerStatusSnapshot,
    ThreadMetrics,
    ThreadSampler,
    TerminalController,
    visible_width,
)
from mrun.mrun import MRunTool


class FakeProcess:
    def __init__(self, pid, name, cmdline):
        self.pid = pid
        self._name = name
        self._cmdline = cmdline

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmdline


class FakeThread:
    def __init__(self, thread_id, user_time, system_time):
        self.id = thread_id
        self.user_time = user_time
        self.system_time = system_time


class FakeThreadProcess:
    def __init__(self, threads):
        self._threads = threads

    def threads(self):
        return list(self._threads)


class FakeMemoryInfo:
    def __init__(self, rss):
        self.rss = rss


class FakeCpuProcess:
    def __init__(self, cpu_values):
        self.cpu_values = list(cpu_values)
        self.cpu_calls = 0

    def cpu_percent(self, interval=None):
        value = self.cpu_values[min(self.cpu_calls, len(self.cpu_values) - 1)]
        self.cpu_calls += 1
        return value

    def memory_info(self):
        return FakeMemoryInfo(4096)

    def status(self):
        return "running"


class FakeDeniedThreadProcess:
    def threads(self):
        raise psutil.AccessDenied(pid=10, name="mongod")

    def num_threads(self):
        return 113


def test_process_to_info_extracts_port_logpath_and_dbpath():
    process = FakeProcess(
        42,
        "mongod",
        [
            "mongod",
            "--replSet",
            "rs",
            "--dbpath",
            "/tmp/db",
            "--logpath",
            "/tmp/mongod.log",
            "--port",
            "27018",
        ],
    )

    info = process_to_info(process)

    assert info.pid == 42
    assert info.name == "mongod"
    assert info.port == 27018
    assert info.logpath == "/tmp/mongod.log"
    assert info.dbpath == "/tmp/db"


def test_process_to_info_supports_equals_style_port():
    process = FakeProcess(
        43,
        "mongos",
        ["mongos", "--port=27019", "--logpath=/tmp/mongos.log"],
    )

    info = process_to_info(process)

    assert info.port == 27019
    assert info.logpath == "/tmp/mongos.log"


def test_discover_mongo_processes_filters_and_sorts():
    processes = [
        FakeProcess(3, "python", ["python"]),
        FakeProcess(2, "mongod", ["mongod", "--port", "27018"]),
        FakeProcess(1, "mongos", ["mongos", "--port", "27017"]),
    ]

    discovered = discover_mongo_processes(lambda: processes)

    assert [(process.name, process.port) for process in discovered] == [
        ("mongos", 27017),
        ("mongod", 27018),
    ]


def test_discover_mongo_processes_reports_process_iter_permission_error():
    def denied_process_iter():
        raise PermissionError("denied")

    with pytest.raises(ProcessDiscoveryError) as exc:
        discover_mongo_processes(denied_process_iter)

    assert "could not list local processes: permission denied" in str(exc.value)


def test_load_mrun_process_specs_reads_startup_file(tmp_path):
    startup_file = tmp_path / ".mrun_startup"
    startup_file.write_text(json.dumps({
        "protocol_version": 2,
        "startup_info": {
            "27018": (
                "mongod --port 27018 --dbpath /tmp/db "
                "--logpath /tmp/mongod.log")
        },
    }))

    specs = load_mrun_process_specs(str(tmp_path))

    assert specs[27018].port == 27018
    assert specs[27018].dbpath == "/tmp/db"
    assert specs[27018].logpath == "/tmp/mongod.log"


def test_load_monitor_auth_config_reads_startup_credentials(tmp_path):
    startup_file = tmp_path / ".mrun_startup"
    startup_file.write_text(json.dumps({
        "protocol_version": 2,
        "parsed_args": {
            "auth": True,
            "username": "monitoruser",
            "password": "monitorpass",
            "auth_db": "admin",
            "initial-user": True,
        },
        "startup_info": {},
    }))

    auth_config = load_monitor_auth_config(str(tmp_path))

    assert auth_config.enabled is True
    assert auth_config.has_credentials() is True
    assert auth_config.client_kwargs() == {
        "username": "monitoruser",
        "password": "monitorpass",
        "authSource": "admin",
    }


def test_monitor_auth_config_reports_missing_initial_user_credentials():
    auth_config = MonitorAuthConfig(
        enabled=True,
        username="monitoruser",
        password="monitorpass",
        auth_db="admin",
        initial_user=False,
    )

    assert auth_config.has_credentials() is False
    assert auth_config.requires_credentials() is True
    assert auth_config.client_kwargs() == {}


def test_monitor_auth_config_credentials_override_startup_credentials():
    auth_config = MonitorAuthConfig(
        enabled=True,
        username="storeduser",
        password="storedpass",
        auth_db="admin",
        initial_user=True,
    ).with_overrides(
        username="overrideuser",
        password="overridepass",
        auth_db="admin2",
    )

    assert auth_config.client_kwargs() == {
        "username": "overrideuser",
        "password": "overridepass",
        "authSource": "admin2",
    }


def test_build_monitor_tls_kwargs_maps_tls_and_ssl_startup_args():
    tls_kwargs = build_monitor_tls_kwargs({
        "tlsMode": "requireTLS",
        "tlsCAFile": "/tmp/ca.pem",
        "tlsClientCertificateKeyFile": "/tmp/client.pem",
        "tlsClientCertificateKeyFilePassword": "secret",
        "tlsAllowInvalidHostnames": True,
    })

    assert tls_kwargs == {
        "tls": True,
        "tlsCAFile": "/tmp/ca.pem",
        "tlsCertificateKeyFile": "/tmp/client.pem",
        "tlsCertificateKeyFilePassword": "secret",
        "tlsAllowInvalidHostnames": True,
    }

    ssl_kwargs = build_monitor_tls_kwargs({
        "sslMode": "requireSSL",
        "sslCAFile": "/tmp/ca.pem",
        "sslClientPEMKeyFile": "/tmp/client.pem",
        "sslClientPEMKeyPassword": "secret",
        "sslAllowInvalidCertificates": True,
    })

    assert ssl_kwargs == {
        "tls": True,
        "tlsAllowInvalidCertificates": True,
        "tlsCAFile": "/tmp/ca.pem",
        "tlsCertificateKeyFile": "/tmp/client.pem",
        "tlsCertificateKeyFilePassword": "secret",
    }


def test_monitor_loads_tls_kwargs_from_startup_file(tmp_path):
    startup_file = tmp_path / ".mrun_startup"
    startup_file.write_text(json.dumps({
        "protocol_version": 2,
        "parsed_args": {
            "tlsMode": "requireTLS",
            "tlsCAFile": "/tmp/ca.pem",
            "tlsAllowInvalidCertificates": True,
        },
        "startup_info": {},
    }))

    assert load_monitor_tls_kwargs(str(tmp_path)) == {
        "tls": True,
        "tlsCAFile": "/tmp/ca.pem",
        "tlsAllowInvalidCertificates": True,
    }


def test_load_monitor_replset_name_reads_replicaset_name(tmp_path):
    startup_file = tmp_path / ".mrun_startup"
    startup_file.write_text(json.dumps({
        "protocol_version": 2,
        "parsed_args": {
            "replicaset": True,
            "name": "rs0",
        },
        "startup_info": {},
    }))

    assert load_monitor_replset_name(str(tmp_path)) == "rs0"


def test_monitor_network_sampler_combines_tls_and_auth_kwargs(tmp_path):
    startup_file = tmp_path / ".mrun_startup"
    startup_file.write_text(json.dumps({
        "protocol_version": 2,
        "parsed_args": {
            "auth": True,
            "username": "monitoruser",
            "password": "monitorpass",
            "auth_db": "admin",
            "initial-user": True,
            "tlsMode": "requireTLS",
            "tlsCAFile": "/tmp/ca.pem",
        },
        "startup_info": {},
    }))

    monitor = Monitor(stdout=io.StringIO(), data_dir=str(tmp_path))

    assert monitor.network_sampler.client_kwargs == {
        "tls": True,
        "tlsCAFile": "/tmp/ca.pem",
        "username": "monitoruser",
        "password": "monitorpass",
        "authSource": "admin",
    }


def test_filter_mrun_processes_keeps_only_startup_ports(tmp_path):
    startup_file = tmp_path / ".mrun_startup"
    startup_file.write_text(json.dumps({
        "protocol_version": 2,
        "startup_info": {
            "27018": (
                "mongod --port 27018 --dbpath /tmp/db "
                "--logpath /tmp/mongod.log")
        },
    }))
    discovered = [
        MongoProcessInfo(1, "mongod", 27017, "", "", []),
        MongoProcessInfo(2, "mongod", 27018, "", "", []),
    ]

    filtered = filter_mrun_processes(
        discovered, load_mrun_process_specs(str(tmp_path)))

    assert [(process.pid, process.port) for process in filtered] == [(2, 27018)]
    assert filtered[0].dbpath == "/tmp/db"
    assert filtered[0].logpath == "/tmp/mongod.log"


def test_mrun_monitor_flag_does_not_route_to_init(monkeypatch):
    called = {}

    def fake_monitor(self):
        called["monitor"] = True
        return 0

    monkeypatch.setattr(MRunTool, "monitor", fake_monitor)

    tool = MRunTool(test=True)
    result = tool.run("--monitor")

    assert result == 0
    assert called["monitor"] is True
    assert tool.args["command"] is None
    assert tool.args["monitor"] is True


def test_mrun_monitor_all_flag_is_parsed(monkeypatch):
    called = {}

    def fake_monitor(self):
        called["monitor"] = True
        called["all"] = self.args["all"]
        called["dir"] = self.args["dir"]
        return 0

    monkeypatch.setattr(MRunTool, "monitor", fake_monitor)

    tool = MRunTool(test=True)
    result = tool.run("--monitor --all --dir /tmp/mrun-data")

    assert result == 0
    assert called == {
        "monitor": True,
        "all": True,
        "dir": "/tmp/mrun-data",
    }


def test_mrun_monitor_flag_order_from_sys_argv(monkeypatch, capsys):
    calls = []

    def fake_monitor(self):
        calls.append((self.args["all"], self.args["dir"],
                      self.args["no_progressbar"]))
        return 0

    monkeypatch.setattr(MRunTool, "monitor", fake_monitor)

    cases = [
        ["mrun", "--all", "--monitor"],
        ["mrun", "--dir", "/tmp/mrun-data", "--monitor"],
        ["mrun", "--no-progressbar", "--monitor"],
    ]
    for argv in cases:
        monkeypatch.setattr("sys.argv", argv)
        tool = MRunTool(test=True)
        assert tool.run() == 0

    assert calls == [
        (True, os.path.abspath("./data"), False),
        (False, "/tmp/mrun-data", False),
        (False, os.path.abspath("./data"), True),
    ]
    assert "Detected mongod version" not in capsys.readouterr().out


def test_mrun_monitor_rejects_init_only_auth_flags(capsys):
    tool = MRunTool(test=True)

    with pytest.raises(SystemExit):
        tool.run("--monitor --auth-db admin")

    assert "unsupported monitor argument: --auth-db" in capsys.readouterr().err


def test_mrun_monitor_accepts_monitor_credential_flags(monkeypatch):
    called = {}

    def fake_monitor(self):
        called["username"] = self.args["monitor_username"]
        called["password"] = self.args["monitor_password"]
        called["auth_db"] = self.args["monitor_auth_db"]
        return 0

    monkeypatch.setattr(MRunTool, "monitor", fake_monitor)

    tool = MRunTool(test=True)
    result = tool.run(
        "--monitor --monitor-username monitoruser "
        "--monitor-password monitorpass --monitor-auth-db admin")

    assert result == 0
    assert called == {
        "username": "monitoruser",
        "password": "monitorpass",
        "auth_db": "admin",
    }


def test_mrun_monitor_flag_from_sys_argv_does_not_print_version(monkeypatch, capsys):
    called = {}

    def fake_monitor(self):
        called["monitor"] = True
        return 0

    monkeypatch.setattr(MRunTool, "monitor", fake_monitor)
    monkeypatch.setattr("sys.argv", ["mrun", "--monitor"])

    tool = MRunTool(test=True)
    result = tool.run()

    captured = capsys.readouterr()
    assert result == 0
    assert called["monitor"] is True
    assert "Detected mongod version" not in captured.out


def test_mrun_help_explains_monitor(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["mrun", "--help"])

    tool = MRunTool(test=True)
    try:
        tool.run()
    except SystemExit:
        pass

    output = capsys.readouterr().out
    flat_output = " ".join(output.split())
    assert "--monitor" in output
    assert "--all" in output
    assert "--monitor-username" in output
    assert "--monitor-password" in output
    assert "--monitor-auth-db" in output
    assert "CPU, memory, network, disk activity, and selectable log tail" in flat_output
    assert "Metric rows include a Role column" in flat_output
    assert "fatal/error/warning/info/debug severity colors" in flat_output
    assert "q or Ctrl+C quit" in flat_output
    assert "a toggle mrun/all processes" in flat_output
    assert "Tab switch panes" in flat_output
    assert "z zoom logs or focused pane" in flat_output
    assert "t toggles thread view" in flat_output
    assert "o toggles currentOp activity" in flat_output
    assert "O toggles currentOp raw/format" in flat_output
    assert "n selects currentOp namespace" in flat_output
    assert "L sets currentOp top-N limit" in flat_output
    assert "currentOp p pretty JSON" in flat_output
    assert "currentOp y yank selected op" in flat_output
    assert "g latest log line" in flat_output
    assert "p prettify highlighted log line as syntax-" in flat_output
    assert "colored JSON" in flat_output
    assert "y yank highlighted log line" in flat_output
    assert "/ filter logs, c clear filter/ns" in flat_output
    assert "space pause/resume log or currentOp streaming" in flat_output
    assert "s cycle refresh 1s/5s/10s" in flat_output
    assert "M launches mongosh admin shell" in flat_output


def test_monitor_reports_when_no_mongo_processes():
    stdout = io.StringIO()
    monitor = Monitor(process_iter=lambda: [], stdout=stdout, include_all=True)

    result = monitor.run()

    assert result == 1
    assert NO_PROCESSES_MESSAGE in stdout.getvalue()


def test_monitor_defaults_to_mrun_managed_processes(tmp_path):
    stdout = io.StringIO()
    monitor = Monitor(process_iter=lambda: [], stdout=stdout,
                      data_dir=str(tmp_path))

    result = monitor.run()

    assert result == 1
    assert NO_MRUN_PROCESSES_MESSAGE in stdout.getvalue()


def test_monitor_reports_process_discovery_permission_error():
    def denied_process_iter():
        raise PermissionError("denied")

    stdout = io.StringIO()
    monitor = Monitor(process_iter=denied_process_iter, stdout=stdout,
                      include_all=True)

    result = monitor.run()

    assert result == 1
    assert "could not list local processes: permission denied" in stdout.getvalue()


def test_parse_log_selection_accepts_indexes_ports_and_all():
    candidates = [
        MongoProcessInfo(10, "mongod", 27017, "/tmp/a.log", "", []),
        MongoProcessInfo(11, "mongod", 27018, "/tmp/b.log", "", []),
    ]

    assert parse_log_selection("", candidates) == [27017, 27018]
    assert parse_log_selection("all", candidates) == [27017, 27018]
    assert parse_log_selection("1,27018", candidates) == [27017, 27018]
    assert parse_log_selection("999", candidates) == []


def test_parse_current_op_namespace_selection_accepts_index_clear_and_name():
    namespaces = ["admin.$cmd", "test.orders"]

    assert parse_current_op_namespace_selection("", namespaces) is None
    assert parse_current_op_namespace_selection("1", namespaces) == "admin.$cmd"
    assert parse_current_op_namespace_selection("all", namespaces) == ""
    assert parse_current_op_namespace_selection("test.users", namespaces) == "test.users"
    assert parse_current_op_namespace_selection("9", namespaces) is None


def test_choose_current_op_namespace_prompts_with_detected_namespaces():
    stdout = io.StringIO()

    choice = choose_current_op_namespace(
        ["admin.$cmd", "test.orders"],
        input_func=lambda: "2",
        stdout=stdout,
    )

    assert choice == "test.orders"
    assert "[1] admin.$cmd" in stdout.getvalue()
    assert "[2] test.orders" in stdout.getvalue()


def test_parse_current_op_limit_selection_accepts_and_clamps_values():
    assert parse_current_op_limit_selection("", current=25) == 25
    assert parse_current_op_limit_selection("50") == 50
    assert parse_current_op_limit_selection("0") is None
    assert parse_current_op_limit_selection("-1") is None
    assert parse_current_op_limit_selection("abc") is None
    assert parse_current_op_limit_selection("999", maximum=500) == 500


def test_choose_current_op_limit_prompts_with_current_limit():
    stdout = io.StringIO()

    limit = choose_current_op_limit(
        25,
        input_func=lambda: "50",
        stdout=stdout,
    )

    assert limit == 50
    assert "press Enter to keep 25" in stdout.getvalue()


def test_parse_current_op_source_selection_accepts_index_port_and_role():
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "", "", []),
        MongoProcessInfo(11, "mongod", 27018, "", "", []),
        MongoProcessInfo(12, "mongod", 27019, "", "", []),
    ]
    roles = {
        27017: RoleMetrics(True, "Secondary"),
        27018: RoleMetrics(True, "Primary"),
        27019: RoleMetrics(True, "Secondary"),
    }

    assert parse_current_op_source_selection("", processes, roles) == [
        27017, 27018, 27019]
    assert parse_current_op_source_selection("1,27019", processes, roles) == [
        27017, 27019]
    assert parse_current_op_source_selection("primary", processes, roles) == [
        27018]
    assert parse_current_op_source_selection("secondary", processes, roles) == [
        27017, 27019]
    assert parse_current_op_source_selection("999", processes, roles) is None
    assert parse_current_op_source_selection("primary,27017", processes, roles) == [
        27018, 27017]


def test_choose_current_op_sources_prompts_with_roles():
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "", "", []),
        MongoProcessInfo(11, "mongod", 27018, "", "", []),
    ]
    roles = {
        27017: RoleMetrics(True, "Secondary"),
        27018: RoleMetrics(True, "Primary"),
    }
    stdout = io.StringIO()

    ports = choose_current_op_sources(
        processes, roles, input_func=lambda: "primary", stdout=stdout)

    assert ports == [27018]
    assert "[1] Secondary" in stdout.getvalue()
    assert "[2] Primary" in stdout.getvalue()


def test_current_op_source_label_describes_selected_source():
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "", "", []),
        MongoProcessInfo(11, "mongod", 27018, "", "", []),
    ]
    roles = {
        27017: RoleMetrics(True, "Secondary"),
        27018: RoleMetrics(True, "Primary"),
    }

    assert current_op_source_label(processes, [], roles) == "all"
    assert current_op_source_label(processes, [27018], roles) == "primary 27018"
    assert current_op_source_label(
        processes, [27017, 27018], roles) == "all"


def test_mongosh_target_options_prefers_primary_selected_and_seed():
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "/tmp/a.log", "", []),
        MongoProcessInfo(11, "mongod", 27018, "/tmp/b.log", "", []),
    ]
    roles = {
        27017: RoleMetrics(True, "Secondary"),
        27018: RoleMetrics(True, "Primary"),
    }

    targets = mongosh_target_options(
        processes,
        roles,
        selected=processes[0],
        replset_name="rs0",
    )

    assert targets[0] == MongoshTarget(
        "primary port 27018", "mongodb://localhost:27018/admin", "primary")
    assert targets[1] == MongoshTarget(
        "selected port 27017", "mongodb://localhost:27017/admin", "selected")
    assert targets[2] == MongoshTarget(
        "seed list replica set rs0",
        "mongodb://localhost:27017,localhost:27018/admin?replicaSet=rs0",
        "seed",
    )
    assert targets[-1].kind == "custom"


def test_parse_mongosh_target_selection_accepts_default_index_kind_and_uri():
    targets = [
        MongoshTarget("primary port 27018", "mongodb://localhost:27018/admin", "primary"),
        MongoshTarget("custom URI", "", "custom"),
    ]

    assert parse_mongosh_target_selection("", targets) == targets[0]
    assert parse_mongosh_target_selection("1", targets) == targets[0]
    assert parse_mongosh_target_selection("primary", targets) == targets[0]
    typed = parse_mongosh_target_selection(
        "mongodb://localhost:27017/admin", targets)
    assert typed.uri == "mongodb://localhost:27017/admin"
    assert typed.kind == "typed"
    assert parse_mongosh_target_selection("9", targets) is None


def test_choose_mongosh_target_prompts_for_custom_uri():
    stdout = io.StringIO()
    answers = iter(["2", "mongodb://localhost:27019/admin"])

    target = choose_mongosh_target(
        [
            MongoshTarget(
                "primary port 27018",
                "mongodb://localhost:27018/admin",
                "primary",
            ),
            MongoshTarget("custom URI", "", "custom"),
        ],
        input_func=lambda: next(answers),
        stdout=stdout,
    )

    assert target == MongoshTarget(
        "custom URI", "mongodb://localhost:27019/admin", "custom")
    assert "Launch mongosh" in stdout.getvalue()


def test_build_mongosh_command_includes_auth_and_tls_without_password_value():
    command = build_mongosh_command(
        "mongodb://localhost:27018/admin",
        MonitorAuthConfig(
            enabled=True,
            username="monitoruser",
            password="monitorpass",
            auth_db="admin",
            initial_user=True,
        ),
        {
            "tls": True,
            "tlsCAFile": "/tmp/ca.pem",
            "tlsAllowInvalidCertificates": True,
        },
        executable="/usr/local/bin/mongosh",
    )

    assert command == [
        "/usr/local/bin/mongosh",
        "mongodb://localhost:27018/admin",
        "--tls",
        "--tlsCAFile",
        "/tmp/ca.pem",
        "--tlsAllowInvalidCertificates",
        "--username",
        "monitoruser",
        "--authenticationDatabase",
        "admin",
        "--password",
    ]
    assert "monitorpass" not in command


def test_mongosh_tls_args_maps_bool_and_value_flags():
    assert mongosh_tls_args({
        "tls": True,
        "tlsCertificateKeyFile": "/tmp/client.pem",
        "tlsAllowInvalidHostnames": True,
    }) == [
        "--tls",
        "--tlsCertificateKeyFile",
        "/tmp/client.pem",
        "--tlsAllowInvalidHostnames",
    ]


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.admin = self
        self.closed = False

    def command(self, command_name):
        assert command_name == "serverStatus"
        return self.response

    def close(self):
        self.closed = True


def test_network_sampler_computes_rates_from_server_status_deltas():
    responses = [
        {"network": {"bytesIn": 100, "bytesOut": 200, "numRequests": 10}},
        {"network": {"bytesIn": 300, "bytesOut": 500, "numRequests": 16}},
    ]
    times = [10.0, 12.0]

    def client_factory(host, **kwargs):
        assert host == "localhost:27017"
        return FakeClient(responses.pop(0))

    def clock():
        return times.pop(0)

    sampler = NetworkSampler(client_factory=client_factory, clock=clock)
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])

    first = sampler.sample([process])[27017]
    second = sampler.sample([process])[27017]

    assert first.available is True
    assert second.bytes_in_per_sec == 100
    assert second.bytes_out_per_sec == 150
    assert second.requests_per_sec == 3


def test_network_sampler_passes_auth_kwargs_to_client_factory():
    captured = {}

    def client_factory(host, **kwargs):
        captured["host"] = host
        captured["kwargs"] = kwargs
        return FakeClient({"network": {}})

    sampler = NetworkSampler(
        client_factory=client_factory,
        client_kwargs={
            "username": "monitoruser",
            "password": "monitorpass",
            "authSource": "admin",
        },
    )
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])

    result = sampler.sample([process])[27017]

    assert result.available is True
    assert captured == {
        "host": "localhost:27017",
        "kwargs": {
            "directConnection": True,
            "serverSelectionTimeoutMS": 200,
            "username": "monitoruser",
            "password": "monitorpass",
            "authSource": "admin",
        },
    }


def test_network_sampler_reports_auth_required_without_connecting():
    called = {}

    def client_factory(host, **kwargs):
        called["client"] = True

    sampler = NetworkSampler(client_factory=client_factory, auth_required=True)
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])

    result = sampler.sample([process])[27017]

    assert result.available is False
    assert result.error == AUTH_REQUIRED_STATUS
    assert called == {}
    assert network_status_label(result) == AUTH_REQUIRED_STATUS


def test_role_from_server_status_prefers_repl_state():
    assert role_from_server_status({"repl": {"stateStr": "PRIMARY"}}) == "Primary"
    assert role_from_server_status({"repl": {"stateStr": "SECONDARY"}}) == "Secondary"
    assert role_from_server_status({"repl": {"stateStr": "RECOVERING"}}) == "Recovering"


def test_role_from_server_status_falls_back_to_writable_primary():
    assert role_from_server_status(
        {"repl": {"isWritablePrimary": True}}) == "Primary"
    assert role_from_server_status(
        {"repl": {"isWritablePrimary": False, "hosts": ["a", "b"]}}
    ) == "Secondary"
    assert role_from_server_status({"process": "mongos"}) == "Router"


def test_role_sampler_reads_roles_from_server_status():
    responses = {
        "localhost:27017": {"repl": {"stateStr": "PRIMARY"}},
        "localhost:27018": {"repl": {"stateStr": "SECONDARY"}},
    }

    def client_factory(host, **kwargs):
        return FakeClient(responses[host])

    sampler = RoleSampler(client_factory=client_factory)
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "", "", []),
        MongoProcessInfo(11, "mongod", 27018, "", "", []),
    ]

    result = sampler.sample(processes)

    assert result[27017].role == "Primary"
    assert result[27018].role == "Secondary"


def test_role_sampler_reports_password_required_without_connecting():
    called = {}

    def client_factory(host, **kwargs):
        called["client"] = True

    sampler = RoleSampler(client_factory=client_factory, auth_required=True)
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])

    result = sampler.sample([process])[27017]

    assert result.available is False
    assert result.role == ROLE_PASSWORD_REQUIRED
    assert result.error == AUTH_REQUIRED_STATUS
    assert called == {}


class FakeCurrentOpClient:
    def __init__(self, response, expected_command=None):
        self.response = response
        self.expected_command = expected_command
        self.admin = self
        self.commands = []
        self.closed = False

    def command(self, command):
        self.commands.append(command)
        expected = self.expected_command or {
            "currentOp": 1, "$all": True, "active": True}
        assert command == expected
        return self.response

    def close(self):
        self.closed = True


def test_current_op_sampler_sorts_and_limits_top_entries():
    responses = {
        "localhost:27017": {
            "inprog": [
                {
                    "active": True,
                    "secs_running": index,
                    "op": "query",
                    "ns": "test.coll",
                    "client": "127.0.0.1",
                    "command": {"find": "coll"},
                }
                for index in range(7)
            ],
        },
        "localhost:27018": {
            "inprog": [
                {
                    "active": True,
                    "secs_running": index,
                    "op": "command",
                    "ns": "admin.$cmd",
                    "client": "127.0.0.1",
                    "command": {"aggregate": "coll"},
                }
                for index in range(7, 14)
            ],
        },
    }

    def client_factory(host, **kwargs):
        return FakeCurrentOpClient(responses[host])

    sampler = CurrentOpSampler(client_factory=client_factory)
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "", "", []),
        MongoProcessInfo(11, "mongod", 27018, "", "", []),
    ]
    roles = {
        27017: RoleMetrics(True, "Primary"),
        27018: RoleMetrics(True, "Secondary"),
    }

    snapshot = sampler.sample(processes, roles)

    assert snapshot.available is True
    assert len(snapshot.entries) == 10
    assert snapshot.entries[0].secs_running == 13
    assert snapshot.entries[0].role == "Secondary"
    assert snapshot.entries[-1].secs_running == 4


def test_current_op_sampler_passes_namespace_filter_and_filters_results():
    response = {
        "inprog": [
            {
                "active": True,
                "secs_running": 4,
                "op": "query",
                "ns": "test.keep",
                "client": "127.0.0.1",
            },
            {
                "active": True,
                "secs_running": 9,
                "op": "query",
                "ns": "test.drop",
                "client": "127.0.0.1",
            },
        ],
    }
    clients = []

    def client_factory(host, **kwargs):
        client = FakeCurrentOpClient(
            response,
            expected_command={
                "currentOp": 1,
                "$all": True,
                "active": True,
                "ns": "test.keep",
            },
        )
        clients.append(client)
        return client

    sampler = CurrentOpSampler(client_factory=client_factory)
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])

    snapshot = sampler.sample([process], namespace="test.keep")

    assert snapshot.available is True
    assert [entry.ns for entry in snapshot.entries] == ["test.keep"]
    assert clients[0].commands == [{
        "currentOp": 1,
        "$all": True,
        "active": True,
        "ns": "test.keep",
    }]


def test_current_op_sampler_reports_password_required_without_connecting():
    called = {}

    def client_factory(host, **kwargs):
        called["client"] = True

    sampler = CurrentOpSampler(client_factory=client_factory, auth_required=True)

    snapshot = sampler.sample([MongoProcessInfo(10, "mongod", 27017, "", "", [])])

    assert snapshot.available is False
    assert snapshot.error == ROLE_PASSWORD_REQUIRED
    assert snapshot.entries == []
    assert called == {}


def test_log_tailer_seeds_and_polls_new_lines(tmp_path):
    logfile = tmp_path / "mongod.log"
    logfile.write_text("first\nsecond\n")
    tailer = LogTailer({27017: str(logfile)}, max_lines=5)

    with logfile.open("a") as fp:
        fp.write("third\n")

    lines = tailer.poll()

    assert "27017 | first" in lines
    assert "27017 | second" in lines
    assert "27017 | third" in lines


def test_read_log_stream_pauses_without_advancing_file_offsets(tmp_path):
    logfile = tmp_path / "mongod.log"
    logfile.write_text("first\n")
    tailer = LogTailer({27017: str(logfile)}, max_lines=5)

    with logfile.open("a") as fp:
        fp.write("second\n")

    paused_lines = read_log_stream(tailer, stream_paused=True)
    resumed_lines = read_log_stream(tailer, stream_paused=False)

    assert paused_lines == ["27017 | first"]
    assert "27017 | second" in resumed_lines


def test_read_disk_metrics_reports_dbpath_and_log_sizes(tmp_path):
    dbpath = tmp_path / "db"
    dbpath.mkdir()
    (dbpath / "collection.wt").write_bytes(b"abcd")
    logfile = tmp_path / "mongod.log"
    logfile.write_bytes(b"abcdef")
    process = MongoProcessInfo(
        10, "mongod", 27017, str(logfile), str(dbpath), [])

    metrics = read_disk_metrics([process])[27017]

    assert metrics.available is True
    assert metrics.db_size == 4
    assert metrics.log_size == 6


def test_thread_sampler_computes_thread_cpu_from_time_deltas():
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])
    samples = [
        [FakeThread(101, 1.0, 0.5), FakeThread(102, 0.5, 0.5)],
        [FakeThread(101, 2.0, 0.5), FakeThread(102, 0.5, 1.0)],
    ]
    times = [10.0, 12.0]

    def process_factory(pid):
        assert pid == 10
        return FakeThreadProcess(samples.pop(0))

    sampler = ThreadSampler(
        process_factory=process_factory,
        clock=lambda: times.pop(0),
    )

    first_snapshot = sampler.sample(process)
    second_snapshot = sampler.sample(process)

    assert first_snapshot.error == ""
    assert second_snapshot.error == ""
    assert first_snapshot.thread_count is None
    assert [thread.cpu_percent for thread in first_snapshot.metrics] == [0.0, 0.0]
    assert [(thread.thread_id, thread.cpu_percent) for thread in second_snapshot.metrics] == [
        (101, 50.0),
        (102, 25.0),
    ]


def test_thread_sampler_falls_back_to_thread_count_when_details_denied():
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])

    sampler = ThreadSampler(process_factory=lambda pid: FakeDeniedThreadProcess())

    snapshot = sampler.sample(process)

    assert snapshot.metrics == []
    assert snapshot.error == "thread details unavailable"
    assert snapshot.thread_count == 113


def test_process_sampler_reuses_process_objects_for_cpu_deltas():
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])
    created = []

    def process_factory(pid):
        assert pid == 10
        fake_process = FakeCpuProcess([0.0, 37.5])
        created.append(fake_process)
        return fake_process

    sampler = ProcessSampler(process_factory=process_factory)

    first_metrics = sampler.sample([process])
    second_metrics = sampler.sample([process])

    assert len(created) == 1
    assert first_metrics[10].cpu_percent == 0.0
    assert second_metrics[10].cpu_percent == 37.5
    assert second_metrics[10].memory_rss == 4096
    assert second_metrics[10].status == "running"


def test_process_sampler_prime_uses_cached_process_for_first_dashboard_sample():
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])
    created = []

    def process_factory(pid):
        fake_process = FakeCpuProcess([0.0, 22.0])
        created.append(fake_process)
        return fake_process

    sampler = ProcessSampler(process_factory=process_factory)

    sampler.prime([process])
    metrics = sampler.sample([process])

    assert len(created) == 1
    assert metrics[10].cpu_percent == 22.0


def test_render_dashboard_contains_left_metrics_and_activity_pane():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((100, 24)),
        disk_metrics={27017: DiskMetrics(True, 2048, 512)},
    )

    assert "CPU Usage" in rendered
    assert "Memory Usage" in rendered
    assert "Network Usage" in rendered
    assert "Disk Usage" in rendered
    assert "2.0KB" in rendered
    assert "Log Tail: 27017" in rendered
    assert "27017" in rendered


def test_render_dashboard_fits_terminal_without_footer_wrap():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    terminal_size = os.terminal_size((80, 18))

    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        selected_ports=[27017],
        terminal_size=terminal_size,
        focused_pane="logs",
    )

    lines = rendered.splitlines()
    assert len(lines) <= terminal_size.lines
    assert all(visible_width(line) <= terminal_size.columns for line in lines)
    assert "CPU Usage" in rendered
    assert "Log Tail: 27017" in rendered


def test_render_dashboard_marks_focused_cpu_process_selection():
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "/tmp/a.log", "", []),
        MongoProcessInfo(11, "mongod", 27018, "/tmp/b.log", "", []),
    ]

    rendered = render_dashboard(
        processes,
        {
            10: ProcessMetrics(12.5, 1024 * 1024, "running"),
            11: ProcessMetrics(3.0, 1024 * 1024, "sleeping"),
        },
        {},
        ["27017 | log line"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((100, 24)),
        focused_pane="cpu",
        cpu_cursor=1,
    )

    assert "[CPU Usage]" in rendered
    assert "> 27018" in rendered
    assert ANSI_INVERSE in rendered
    assert "t view threads" in rendered


def test_format_cpu_lines_includes_replica_role_column():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/a.log", "", [])

    lines = format_cpu_lines(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        cursor=0,
        show_cursor=True,
        role_metrics={27017: RoleMetrics(True, "Primary")},
    )

    assert "ROLE" in strip_ansi(lines[0])
    assert "Primary" in strip_ansi(lines[1])
    assert ANSI_ROLE_PRIMARY in lines[1]
    assert ANSI_BOLD not in lines[1]


def test_role_column_uses_muted_non_header_secondary_color():
    process = MongoProcessInfo(10, "mongod", 27018, "/tmp/a.log", "", [])

    lines = format_cpu_lines(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        role_metrics={27018: RoleMetrics(True, "Secondary")},
    )

    assert "Secondary" in strip_ansi(lines[1])
    assert ANSI_ROLE_SECONDARY in lines[1]
    assert ANSI_YELLOW not in lines[1]


def test_metric_formatters_include_role_column():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/a.log", "", [])
    roles = {27017: RoleMetrics(True, "Primary")}

    memory = format_memory_lines(
        [process], {10: ProcessMetrics(12.5, 1024 * 1024, "running")}, roles)
    network = format_network_lines(
        [process], {27017: NetworkMetrics(True, 1, 2, 3)}, roles)
    disk = format_disk_lines(
        [process], {27017: DiskMetrics(True, 2048, 512)}, roles)

    assert "ROLE" in strip_ansi(memory[0])
    assert "Primary" in strip_ansi(memory[1])
    assert "ROLE" in strip_ansi(network[0])
    assert "Primary" in strip_ansi(network[1])
    assert "ROLE" in strip_ansi(disk[0])
    assert "Primary" in strip_ansi(disk[1])


def test_metric_formatters_pad_left_like_cpu_rows():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/a.log", "", [])
    roles = {27017: RoleMetrics(True, "Primary")}

    cpu = format_cpu_lines(
        [process], {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        cursor=0, show_cursor=True, role_metrics=roles)
    memory = format_memory_lines(
        [process], {10: ProcessMetrics(12.5, 1024 * 1024, "running")}, roles)
    network = format_network_lines(
        [process], {27017: NetworkMetrics(True, 1, 2, 3)}, roles)
    disk = format_disk_lines(
        [process], {27017: DiskMetrics(True, 2048, 512)}, roles)

    assert strip_ansi(cpu[0]).startswith("  PORT")
    assert strip_ansi(cpu[1]).startswith("> 27017")
    assert strip_ansi(memory[0]).startswith("  PORT")
    assert strip_ansi(memory[1]).startswith("  27017")
    assert strip_ansi(network[0]).startswith("  PORT")
    assert strip_ansi(network[1]).startswith("  27017")
    assert strip_ansi(disk[0]).startswith("  PORT")
    assert strip_ansi(disk[1]).startswith("  27017")
    for lines in (cpu, memory, network, disk):
        header = strip_ansi(lines[0])
        row = strip_ansi(lines[1])
        assert header.index("PORT") == row.index("27017")
        assert header.index("ROLE") == row.index("Primary")


def test_format_cpu_lines_shows_password_required_role():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/a.log", "", [])

    lines = format_cpu_lines(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        role_metrics={
            27017: RoleMetrics(
                False,
                role=ROLE_PASSWORD_REQUIRED,
                error=AUTH_REQUIRED_STATUS,
            )
        },
    )

    assert ROLE_PASSWORD_REQUIRED in strip_ansi(lines[1])
    assert ANSI_ROLE_WARNING in lines[1]


def test_format_current_op_lines_shows_top_entries():
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017, "Primary", 12.4, "query", "test.coll",
                "127.0.0.1", "find coll", "op1"),
        ],
    )

    lines = format_current_op_lines(snapshot)

    assert "ROLE" in strip_ansi(lines[0])
    assert "Primary" in strip_ansi(lines[1])
    assert "test.coll" in strip_ansi(lines[1])


def test_format_current_op_lines_can_show_raw_documents():
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                12.4,
                "query",
                "test.coll",
                "127.0.0.1",
                "find coll",
                "op1",
                raw={"op": "query", "ns": "test.coll"},
            ),
        ],
    )

    lines = format_current_op_lines(snapshot, raw=True)

    assert "RAW CURRENTOP" in strip_ansi(lines[0])
    assert '"op": "query"' in strip_ansi(lines[1])


def test_format_current_op_lines_marks_yanked_entry_green():
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017, "Primary", 12.4, "query", "test.coll",
                "127.0.0.1", "find coll", "op1"),
        ],
    )

    lines = format_current_op_lines(snapshot, cursor=0, yanked_cursor=0)

    assert "test.coll" in strip_ansi(lines[1])
    assert lines[1].startswith("\x00yanked\x00")


def test_current_op_raw_json_stringifies_bson_like_values():
    class FakeObjectId:
        def __str__(self):
            return "507f1f77bcf86cd799439011"

    class FakeDate:
        def isoformat(self):
            return "2026-05-07T12:00:00"

    raw = {
        "_id": FakeObjectId(),
        "when": FakeDate(),
        "command": {"find": "coll", "lsid": {"id": FakeObjectId()}},
    }

    text = current_op_raw_json(raw)

    assert "507f1f77bcf86cd799439011" in text
    assert "2026-05-07T12:00:00" in text


def test_current_op_pretty_json_lines_stringifies_bson_like_values():
    class FakeObjectId:
        def __str__(self):
            return "507f1f77bcf86cd799439011"

    lines = current_op_pretty_json_lines({
        "op": "query",
        "objectId": FakeObjectId(),
    })

    assert '{' in lines[0]
    assert '  "op": "query",' in lines
    assert '  "objectId": "507f1f77bcf86cd799439011"' in lines


def test_format_current_op_lines_raw_mode_does_not_crash_on_bson_values():
    class FakeObjectId:
        def __str__(self):
            return "507f1f77bcf86cd799439011"

    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                12.4,
                "query",
                "test.coll",
                "127.0.0.1",
                "find coll",
                "op1",
                raw={"op": "query", "objectId": FakeObjectId()},
            ),
        ],
    )

    lines = format_current_op_lines(snapshot, raw=True)

    assert "507f1f77bcf86cd799439011" in strip_ansi(lines[1])


def test_current_op_namespaces_returns_sorted_unique_names():
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017, "Primary", 1.0, "query", "test.b", "", ""),
            CurrentOpEntry(
                27017, "Primary", 2.0, "query", "test.a", "", ""),
            CurrentOpEntry(
                27017, "Primary", 3.0, "query", "test.b", "", ""),
        ],
    )

    assert current_op_namespaces(snapshot) == ["test.a", "test.b"]


def test_render_dashboard_current_op_view_uses_right_activity_pane():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017, "Primary", 12.4, "query", "test.coll",
                "127.0.0.1", "find coll", "op1"),
        ],
    )

    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        terminal_size=os.terminal_size((120, 24)),
        focused_pane="cpu",
        cpu_cursor=0,
        current_op_view=True,
        current_ops=snapshot,
    )

    assert "Current Ops (Formatted, top 10)" in rendered
    assert "test.coll" in rendered
    assert "CPU Usage" in rendered
    assert "o logs" in rendered or "o currentOps" in rendered


def test_render_dashboard_current_op_raw_view_uses_right_activity_pane():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                12.4,
                "query",
                "test.coll",
                "127.0.0.1",
                "find coll",
                "op1",
                raw={"op": "query", "ns": "test.coll"},
            ),
        ],
    )

    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        terminal_size=os.terminal_size((120, 24)),
        current_op_view=True,
        current_op_raw=True,
        current_op_namespace="test.coll",
        current_ops=snapshot,
    )

    assert "Current Ops (Raw, top 10)" in rendered
    assert "ns test.coll" in rendered
    assert '"op": "query"' in rendered


def test_render_dashboard_current_op_view_shows_custom_limit():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017, "Primary", 12.4, "query", "test.coll",
                "127.0.0.1", "find coll", "op1"),
        ],
    )

    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        terminal_size=os.terminal_size((120, 24)),
        current_op_view=True,
        current_ops=snapshot,
        current_op_limit=50,
    )

    assert "Current Ops (Formatted, top 50)" in rendered
    assert "L top 50" in rendered


def test_render_dashboard_current_op_pretty_view_uses_activity_pane():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                12.4,
                "query",
                "test.coll",
                "127.0.0.1",
                "find coll",
                "op1",
                raw={"op": "query", "ns": "test.coll"},
            ),
        ],
    )

    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        terminal_size=os.terminal_size((120, 24)),
        current_op_view=True,
        current_ops=snapshot,
        current_op_pretty_lines=current_op_pretty_json_lines(
            snapshot.entries[0].raw),
    )

    assert "Current Ops (Pretty, top 10)" in rendered
    assert '"op"' in strip_ansi(rendered)
    assert "p list" in rendered


def test_render_dashboard_thread_view_is_toggle_only_not_default():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])

    default_rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        terminal_size=os.terminal_size((100, 24)),
        focused_pane="cpu",
        cpu_cursor=0,
    )
    thread_rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        terminal_size=os.terminal_size((100, 24)),
        focused_pane="cpu",
        cpu_cursor=0,
        cpu_thread_view=True,
        thread_metrics=[ThreadMetrics(101, 25.0, 1.0, 0.5, 1.5)],
    )

    assert "CPU Threads" not in default_rendered
    assert "CPU Threads: port 27017 pid 10" in thread_rendered
    assert "TID        CPU%" in thread_rendered
    assert "101" in thread_rendered
    assert "t list threads" in thread_rendered


def test_render_dashboard_thread_view_shows_count_when_details_denied():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])

    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        terminal_size=os.terminal_size((100, 24)),
        focused_pane="cpu",
        cpu_cursor=0,
        cpu_thread_view=True,
        thread_error="thread details unavailable",
        thread_count=113,
    )

    assert "THREAD COUNT 113" in rendered
    assert "thread details unavailable" in rendered
    assert "TID        CPU%" not in rendered


def test_render_dashboard_cpu_zoom_uses_current_cpu_mode():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | log line"],
        terminal_size=os.terminal_size((120, 18)),
        focused_pane="cpu",
        zoom_pane="cpu",
        cpu_cursor=0,
        cpu_thread_view=True,
        thread_metrics=[ThreadMetrics(101, 25.0, 1.0, 0.5, 1.5)],
    )

    assert "[CPU Threads: port 27017 pid 10]" in rendered
    assert "Memory Usage" not in rendered
    assert "Log Tail" not in rendered
    assert "z quad" in rendered


def test_render_dashboard_zoom_mode_focuses_log_tail():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | first", "27017 | second"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((120, 18)),
        log_cursor=1,
        zoom_logs=True,
    )

    assert "Log Tail: 27017" in rendered
    assert "> 27017 | second" in rendered
    assert ANSI_INVERSE in rendered
    assert "CPU Usage" not in rendered
    assert "z quad" in rendered
    assert "g latest" in rendered
    assert "p pretty" in rendered
    assert "space pause" in rendered
    assert "mrun" in rendered
    assert "a all" in rendered
    assert "s1s" in rendered


def test_render_dashboard_paused_stream_updates_title_and_footer():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | first"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((160, 18)),
        log_cursor=0,
        stream_paused=True,
    )

    assert "Log Tail: 27017 (Paused)" in rendered
    assert "space resume" in rendered


def test_render_dashboard_filters_log_stream_and_shows_match_count():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        [
            '27017 | {"s":"I","msg":"startup complete"}',
            '27017 | {"s":"I","msg":"Slow query","attr":{"durationMillis":42}}',
        ],
        selected_ports=[27017],
        terminal_size=os.terminal_size((120, 24)),
        log_cursor=1,
        zoom_logs=True,
        log_filter_query="slowop",
    )

    assert "filter: slowop" in rendered
    assert "1/2 matches" in rendered
    assert "Slow query" in strip_ansi(rendered)
    assert "startup complete" not in strip_ansi(rendered)


def test_render_dashboard_shows_empty_filter_result():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ['27017 | {"s":"I","msg":"startup complete"}'],
        selected_ports=[27017],
        terminal_size=os.terminal_size((100, 20)),
        log_cursor=0,
        zoom_logs=True,
        log_filter_query="slowop",
    )

    assert "No log lines match filter: slowop" in rendered
    assert "0/1 matches" in rendered


def test_render_dashboard_shows_auth_required_network_status():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {27017: NetworkSampler(auth_required=True).sample([process])[27017]},
        ["27017 | first"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((160, 24)),
        log_cursor=0,
    )

    assert AUTH_REQUIRED_STATUS in rendered


def test_render_dashboard_pretty_json_mode_replaces_raw_log_tail():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ['27017 | {"msg":"hello"}'],
        selected_ports=[27017],
        terminal_size=os.terminal_size((120, 18)),
        log_cursor=0,
        zoom_logs=True,
        pretty_lines=["{", '  "msg": "hello"', "}"],
    )

    assert "Log Tail: 27017 (Pretty JSON)" in rendered
    assert '  "msg": "hello"' in strip_ansi(rendered)
    assert "\033[" in rendered
    assert "> 27017" not in rendered
    assert "p raw" in rendered
    assert "pretty j/k" in rendered


def test_render_dashboard_pretty_json_uses_scroll_offset():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ['27017 | {"msg":"hello"}'],
        selected_ports=[27017],
        terminal_size=os.terminal_size((80, 8)),
        log_cursor=0,
        zoom_logs=True,
        pretty_lines=[
            "{",
            '  "line1": 1,',
            '  "line2": 2,',
            '  "line3": 3,',
            '  "line4": 4,',
            '  "line5": 5,',
            '  "line6": 6,',
            '  "line7": 7,',
            '  "line8": 8,',
            '  "line9": 9,',
            '  "line10": 10,',
            '  "line11": 11,',
            '  "line12": 12',
            "}",
        ],
        pretty_scroll=2,
    )

    stripped = strip_ansi(rendered)
    assert '  "line2": 2,' in stripped
    assert '  "line1": 1,' not in stripped


def test_pretty_json_colorizes_keys_strings_numbers_and_keywords():
    palette = pretty_json_palette("dark")

    rendered = colorize_pretty_json_line(
        '  "msg": "hello", "n": 12, "ok": true, "missing": null',
        palette,
    )

    assert palette["key"] + '"msg"' in rendered
    assert palette["string"] + '"hello"' in rendered
    assert palette["number"] + "12" in rendered
    assert palette["keyword"] + "true" in rendered
    assert palette["keyword"] + "null" in rendered
    assert strip_ansi(rendered) == (
        '  "msg": "hello", "n": 12, "ok": true, "missing": null')


def test_pretty_json_theme_detection_and_override():
    assert detect_terminal_theme({"MRUN_MONITOR_THEME": "light"}) == "light"
    assert detect_terminal_theme({"MRUN_MONITOR_THEME": "dark"}) == "dark"
    assert detect_terminal_theme({"COLORFGBG": "0;15"}) == "light"
    assert detect_terminal_theme({"COLORFGBG": "15;0"}) == "dark"
    assert pretty_json_palette("light")["number"] != pretty_json_palette("dark")["number"]


def test_format_pretty_log_lines_can_disable_color():
    lines = format_pretty_log_lines(['  "n": 12'], 1, colorize=False)

    assert lines == ['  "n": 12']


def test_format_pretty_log_lines_uses_scroll_offset():
    pretty_lines = ["{", '  "a": 1,', '  "b": 2', "}"]

    lines = format_pretty_log_lines(
        pretty_lines,
        2,
        offset=1,
        colorize=False,
    )

    assert lines == ['  "a": 1,', '  "b": 2']
    assert clamp_pretty_scroll(pretty_lines, 99, 2) == 2
    assert clamp_pretty_scroll(pretty_lines, -99, 2) == 0


def test_pretty_json_panel_uses_ansi_aware_widths():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ['27017 | {"msg":"hello"}'],
        selected_ports=[27017],
        terminal_size=os.terminal_size((50, 10)),
        log_cursor=0,
        zoom_logs=True,
        pretty_lines=[
            "{",
            '  "message": "this is a long string value for truncation",',
            '  "count": 12345,',
            '  "ok": true',
            "}",
        ],
    )

    panel_lines = rendered.splitlines()[:-1]
    assert panel_lines
    assert all(visible_width(line) == 50 for line in panel_lines)


def test_make_panel_uses_neutral_borders_and_colored_header_text():
    panel = make_panel(
        "Network Usage",
        ["content"],
        40,
        5,
        header_color=ANSI_YELLOW,
    )

    assert panel[0].startswith("+")
    assert not panel[0].startswith(ANSI_YELLOW + "+")
    assert ANSI_YELLOW in panel[0]
    assert "Network Usage" in panel[0]
    assert panel[1].startswith("|")
    assert all(visible_width(line) == 40 for line in panel)


def test_make_panel_bolds_title_and_pads_table_header():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    panel = make_panel(
        "CPU Usage",
        format_cpu_lines(
            [process],
            {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
            cursor=0,
            show_cursor=True,
        ),
        64,
        6,
        focused=True,
        header_color=ANSI_TEAL,
    )

    assert ANSI_BOLD in panel[0]
    assert ANSI_BOLD + ANSI_TEAL in panel[1]
    assert strip_ansi(panel[1]).startswith("|   PORT")
    assert strip_ansi(panel[2]).startswith("| > 27017")
    assert all(visible_width(line) == 64 for line in panel)


def test_render_dashboard_marks_yanked_log_line_green():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | first", "27017 | second"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((80, 18)),
        log_cursor=1,
        yanked_cursor=1,
        zoom_logs=True,
    )

    assert "27017 | second" in rendered
    assert ANSI_GREEN in rendered


def test_detect_log_severity_from_json_and_plain_text():
    assert detect_log_severity('27017 | {"s":"F","msg":"fatal"}') == "fatal"
    assert detect_log_severity('27017 | {"severity":"error"}') == "error"
    assert detect_log_severity('27017 | {"level":"warning"}') == "warning"
    assert detect_log_severity('27017 | {"s":"I","msg":"info"}') == "info"
    assert detect_log_severity('27017 | {"s":"D","msg":"debug"}') == "debug"
    assert detect_log_severity("27017 | fatal assertion") == "fatal"
    assert detect_log_severity("27017 | warning checkpoint slow") == "warning"
    assert detect_log_severity("27017 | info startup complete") == "info"
    assert detect_log_severity("27017 | debug checkpoint detail") == "debug"
    assert detect_log_severity("27017 | normal startup") == ""


def test_render_dashboard_colors_log_severity_as_traffic_lights():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        [
            '27017 | {"s":"E","msg":"error"}',
            '27017 | {"s":"F","msg":"fatal"}',
            '27017 | {"s":"W","msg":"warning"}',
            '27017 | {"s":"I","msg":"info"}',
            '27017 | {"s":"D","msg":"debug"}',
        ],
        selected_ports=[27017],
        terminal_size=os.terminal_size((100, 24)),
        log_cursor=3,
    )

    assert ANSI_RED in rendered
    assert ANSI_YELLOW in rendered
    assert ANSI_TEAL in rendered
    assert ANSI_DIM in rendered
    assert ANSI_INVERSE in rendered


def test_selected_log_line_keeps_info_color_with_inverse_highlight():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ['27017 | {"s":"I","msg":"info"}'],
        selected_ports=[27017],
        terminal_size=os.terminal_size((80, 18)),
        log_cursor=0,
        zoom_logs=True,
    )

    assert ANSI_TEAL in rendered
    assert ANSI_INVERSE in rendered


def test_yanked_log_line_style_overrides_severity_color():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ['27017 | {"s":"W","msg":"warning"}'],
        selected_ports=[27017],
        terminal_size=os.terminal_size((80, 18)),
        log_cursor=0,
        yanked_cursor=0,
        zoom_logs=True,
    )

    assert ANSI_GREEN in rendered
    assert ANSI_INVERSE in rendered
    assert ANSI_YELLOW not in rendered


def test_log_cursor_helpers_move_and_mark_lines():
    lines = ["first", "second", "third"]

    assert move_log_cursor(lines, None, -1) == 1
    assert move_log_cursor(lines, 1, 1) == 2
    formatted = format_log_lines(lines, 1, 3)
    assert formatted[0] == "  first"
    assert formatted[1].endswith("> second")
    assert formatted[2] == "  third"


def test_log_cursor_moves_inside_visible_window_before_scrolling():
    lines = ["first", "second", "third", "fourth", "fifth"]

    first_view = format_log_lines(lines, 1, 3, view_start=0)
    second_view = format_log_lines(lines, 2, 3, view_start=0)
    scrolled_view = format_log_lines(lines, 3, 3, view_start=0)

    assert [strip_ansi(line) for line in first_view] == [
        "  first",
        "> second",
        "  third",
    ]
    assert [strip_ansi(line) for line in second_view] == [
        "  first",
        "  second",
        "> third",
    ]
    assert [strip_ansi(line) for line in scrolled_view] == [
        "  second",
        "  third",
        "> fourth",
    ]


def test_filtered_log_cursor_uses_independent_view_start():
    lines = ["keep one", "drop", "keep two", "keep three"]
    view = filter_log_lines(lines, "keep")

    rendered = format_log_lines(
        view.lines,
        2,
        2,
        line_indexes=view.indexes,
        view_start=0,
    )

    assert [strip_ansi(line) for line in rendered] == [
        "  keep one",
        "> keep two",
    ]


def test_log_filter_scores_exact_token_and_fuzzy_matches():
    exact = score_log_filter("Slow query", "27017 | Slow query operation")
    token = score_log_filter("query slow", "27017 | Slow query operation")
    fuzzy = score_log_filter("slwop", "27017 | Slow query operation")
    missing = score_log_filter("rollback", "27017 | Slow query operation")

    assert exact.matched is True
    assert exact.score > token.score > fuzzy.score
    assert exact.spans == [(8, 18)]
    assert fuzzy.spans
    assert missing.matched is False


def test_filter_log_lines_supports_structured_mongodb_fields():
    lines = [
        (
            '27017 | {"s":"I","c":"COMMAND","msg":"Slow query",'
            '"attr":{"command":{"find":"users","filter":{}},"durationMillis":42}}'
        ),
        (
            '27018 | {"s":"I","c":"COMMAND","msg":"ok",'
            '"attr":{"command":{"aggregate":"orders","pipeline":[]}}}'
        ),
        '27017 | {"s":"E","c":"NETWORK","msg":"connection error"}',
    ]

    assert filter_log_lines(lines, "slowop").indexes == [0]
    assert filter_log_lines(lines, "cmd:find").indexes == [0]
    assert filter_log_lines(lines, "cmd:aggregate").indexes == [1]
    assert filter_log_lines(lines, "component:NETWORK").indexes == [2]
    assert filter_log_lines(lines, "severity:E").indexes == [2]
    assert filter_log_lines(lines, "port:27018").indexes == [1]
    assert filter_log_lines(lines, 'msg:"Slow query"').indexes == [0]


def test_format_log_lines_highlights_filter_hits_without_overriding_selection():
    lines = ["27017 | Slow query operation", "27017 | startup complete"]
    view = filter_log_lines(lines, "slowop")

    formatted = format_log_lines(
        view.lines,
        cursor=0,
        height=5,
        line_indexes=view.indexes,
        match_spans=view.spans_by_index,
    )

    assert ANSI_SEARCH_HIT not in formatted[0]

    formatted = format_log_lines(
        view.lines,
        cursor=1,
        height=5,
        line_indexes=view.indexes,
        match_spans=view.spans_by_index,
    )

    assert ANSI_SEARCH_HIT in formatted[0]


def test_dashboard_snapshot_due_skips_sampler_work_for_fast_redraws():
    snapshot = object()

    assert dashboard_snapshot_due(None, 10.0, 20.0) is True
    assert dashboard_snapshot_due(snapshot, 10.0, 20.0) is False
    assert dashboard_snapshot_due(snapshot, 20.0, 20.0) is True
    assert dashboard_snapshot_due(snapshot, 10.0, 20.0, force_sample=True) is True


def test_monitor_yanks_highlighted_log_line_to_terminal_clipboard():
    stdout = io.StringIO()
    monitor = Monitor(stdout=stdout)
    monitor.log_cursor = 1
    monitor.follow_tail = False

    monitor._yank_log_line(["first", "second"])

    assert build_osc52_sequence("second") in stdout.getvalue()
    assert ANSI_GREEN not in stdout.getvalue()
    assert ANSI_YELLOW not in stdout.getvalue()
    assert monitor.yanked_cursor == 1
    assert monitor.follow_tail is False
    assert monitor.status_message == "yanked highlighted log line"


def test_monitor_move_log_cursor_disables_follow_tail():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 2
    monitor.follow_tail = True

    monitor._move_log_cursor(["first", "second", "third"], -1)

    assert monitor.log_cursor == 1
    assert monitor.follow_tail is False
    assert monitor.status_message == "highlighted log line 2"


def test_monitor_resumes_follow_tail_at_latest_log_line():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 1
    monitor.follow_tail = False

    monitor._move_log_cursor(["first", "second", "third"], 1)

    assert monitor.log_cursor == 2
    assert monitor.follow_tail is True
    assert monitor.status_message == "following latest log line"


def test_parse_escape_sequence_accepts_common_arrow_variants():
    assert parse_escape_sequence("\x1b[A") == "up"
    assert parse_escape_sequence("\x1b[B") == "down"
    assert parse_escape_sequence("\x1bOA") == "up"
    assert parse_escape_sequence("\x1bOB") == "down"
    assert parse_escape_sequence("\x1b[1;2A") == "up"
    assert parse_escape_sequence("\x1b[1;5B") == "down"
    assert parse_escape_sequence("\x1b[Z") == "shift-tab"
    assert parse_escape_sequence("\x1b[C") == "escape"


def test_monitor_jump_to_latest_resumes_follow_tail():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 0
    monitor.follow_tail = False

    action = monitor._wait_for_action(
        FakeTerminal("g"), time.time(), ["first", "second", "third"])

    assert action == "redraw"
    assert monitor.log_cursor == 2
    assert monitor.follow_tail is True
    assert monitor.status_message == "following latest log line"


def test_monitor_log_filter_prompt_applies_and_clears_filter():
    lines = [
        '27017 | {"s":"I","msg":"startup complete"}',
        '27017 | {"s":"I","msg":"Slow query","attr":{"durationMillis":42}}',
    ]
    monitor = Monitor(stdout=io.StringIO())

    action = monitor._wait_for_action(FakeTerminal("/"), time.time(), lines)

    assert action == "redraw"
    assert monitor.log_filter_prompt is True

    for char in "slowop":
        action = monitor._handle_log_filter_prompt_key(char, lines)
        assert action == "redraw"

    action = monitor._handle_log_filter_prompt_key("\r", lines)

    assert action == "redraw"
    assert monitor.log_filter_prompt is False
    assert monitor.log_filter_query == "slowop"
    assert monitor.log_cursor == 1
    assert monitor.status_message == "filter slowop matched 1 log lines"

    action = monitor._wait_for_action(FakeTerminal("c"), time.time(), lines)

    assert action == "redraw"
    assert monitor.log_filter_query == ""
    assert monitor.status_message == "log filter cleared"


def test_monitor_log_filter_prompt_escape_keeps_existing_filter():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_filter_query = "slowop"
    monitor.log_filter_prompt = True
    monitor.log_filter_input = "cmd:find"

    action = monitor._handle_log_filter_prompt_key("escape", [])

    assert action == "redraw"
    assert monitor.log_filter_query == "slowop"
    assert monitor.log_filter_prompt is False
    assert monitor.log_filter_input == ""
    assert monitor.status_message == "log filter unchanged"


def test_monitor_filtered_navigation_yank_and_pretty_use_raw_line():
    stdout = io.StringIO()
    lines = [
        '27017 | {"s":"I","msg":"Slow query","attr":{"durationMillis":41}}',
        '27017 | {"s":"I","msg":"startup complete"}',
        '27017 | {"s":"I","msg":"Slow query","attr":{"durationMillis":42}}',
    ]
    monitor = Monitor(stdout=stdout)
    monitor.log_filter_query = "slowop"
    monitor.log_cursor = 0
    monitor.follow_tail = False

    monitor._move_log_cursor(lines, 1)

    assert monitor.log_cursor == 2
    assert monitor.follow_tail is True

    monitor._yank_log_line(lines)

    assert build_osc52_sequence(lines[2]) in stdout.getvalue()
    assert monitor.yanked_cursor == 2

    monitor._toggle_pretty_log_line(lines)

    assert monitor.pretty_lines is not None
    assert '  "msg": "Slow query",' in monitor.pretty_lines


def test_refresh_interval_cycle_and_key_handler():
    monitor = Monitor(stdout=io.StringIO())

    assert next_refresh_interval(1.0) == 5.0
    assert next_refresh_interval(5.0) == 10.0
    assert next_refresh_interval(10.0) == 1.0

    action = monitor._wait_for_action(FakeTerminal("s"), time.time(), [])

    assert action == "redraw"
    assert monitor.refresh_interval == 5.0
    assert monitor.status_message == "refresh interval 5s"


def test_monitor_space_toggles_log_streaming():
    monitor = Monitor(stdout=io.StringIO())

    action = monitor._wait_for_action(FakeTerminal(" "), time.time(), [])

    assert action == "redraw"
    assert monitor.stream_paused is True
    assert monitor.status_message == "log streaming paused"

    action = monitor._wait_for_action(FakeTerminal(" "), time.time(), [])

    assert action == "redraw"
    assert monitor.stream_paused is False
    assert monitor.status_message == "log streaming resumed"


def test_monitor_a_toggles_process_scope_and_reselects_logs():
    monitor = Monitor(stdout=io.StringIO())

    action = monitor._wait_for_action(FakeTerminal("a"), time.time(), [])

    assert action == "reselect"
    assert monitor.process_scope == "all"
    assert monitor.status_message == "showing all MongoDB processes"

    action = monitor._wait_for_action(FakeTerminal("a"), time.time(), [])

    assert action == "reselect"
    assert monitor.process_scope == "mrun"
    assert monitor.status_message == "showing mongorun-managed processes"


def test_prettify_log_line_parses_monitor_prefixed_json():
    pretty = prettify_log_line('27017 | {"msg":"hello","attr":{"port":27017}}')

    assert pretty == [
        "{",
        '  "msg": "hello",',
        '  "attr": {',
        '    "port": 27017',
        "  }",
        "}",
    ]


def test_prettify_log_line_parses_embedded_json_object():
    pretty = prettify_log_line('27017 | noise before {"msg":"hello"}')

    assert pretty == ["{", '  "msg": "hello"', "}"]


def test_monitor_pretty_toggle_pauses_and_zoom_renders_json():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 0
    monitor.follow_tail = True
    monitor.zoom_logs = False
    monitor.pretty_scroll = 7

    action = monitor._wait_for_action(
        FakeTerminal("p"), time.time(), ['27017 | {"msg":"hello"}'])

    assert action == "redraw"
    assert monitor.follow_tail is False
    assert monitor.zoom_logs is True
    assert monitor.zoom_pane == "logs"
    assert monitor.pretty_scroll == 0
    assert monitor.pretty_lines == ["{", '  "msg": "hello"', "}"]
    assert monitor.pretty_previous_zoom is False
    assert monitor.status_message == "prettified highlighted log line"


def test_monitor_pretty_mode_j_k_scrolls_without_moving_log_cursor(monkeypatch):
    monkeypatch.setattr(Monitor, "_pretty_view_height", staticmethod(lambda: 2))
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 1
    monitor.pretty_lines = ["{", '  "a": 1,', '  "b": 2', "}"]

    action = monitor._wait_for_action(
        FakeTerminal("j"), time.time(), ["first", "second", "third"])

    assert action == "redraw"
    assert monitor.pretty_scroll == 1
    assert monitor.log_cursor == 1
    assert monitor.pretty_lines is not None
    assert monitor.status_message == "pretty JSON lines 2-3 of 4"

    action = monitor._wait_for_action(
        FakeTerminal("k"), time.time(), ["first", "second", "third"])

    assert action == "redraw"
    assert monitor.pretty_scroll == 0
    assert monitor.log_cursor == 1
    assert monitor.status_message == "pretty JSON lines 1-2 of 4"


def test_monitor_pretty_mode_scroll_clamps_at_bottom(monkeypatch):
    monkeypatch.setattr(Monitor, "_pretty_view_height", staticmethod(lambda: 2))
    monitor = Monitor(stdout=io.StringIO())
    monitor.pretty_lines = ["{", '  "a": 1,', '  "b": 2', "}"]
    monitor.pretty_scroll = 2

    action = monitor._wait_for_action(
        FakeTerminal("down"), time.time(), ["first"])

    assert action == "redraw"
    assert monitor.pretty_scroll == 2
    assert monitor.status_message == "bottom of pretty JSON"


def test_monitor_pretty_mode_g_does_not_exit_pretty_view():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 0
    monitor.pretty_lines = ["{", "}"]

    action = monitor._wait_for_action(
        FakeTerminal("g"), time.time(), ["first", "second"])

    assert action == "redraw"
    assert monitor.log_cursor == 0
    assert monitor.pretty_lines == ["{", "}"]
    assert monitor.status_message == "press p before jumping latest"


def test_monitor_pretty_toggle_returns_to_previous_raw_view():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 0
    monitor.zoom_logs = False

    monitor._toggle_pretty_log_line(['27017 | {"msg":"hello"}'])
    monitor.pretty_scroll = 1
    monitor._toggle_pretty_log_line(['27017 | {"msg":"hello"}'])

    assert monitor.pretty_lines is None
    assert monitor.pretty_scroll == 0
    assert monitor.pretty_previous_zoom is None
    assert monitor.zoom_logs is False
    assert monitor.status_message == "raw log line view"


def test_monitor_pretty_toggle_reports_invalid_json():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 0
    monitor.follow_tail = True

    monitor._toggle_pretty_log_line(["27017 | not json"])

    assert monitor.pretty_lines is None
    assert monitor.follow_tail is False
    assert monitor.status_message == "selected line is not valid JSON"


class FakeTerminal:
    def __init__(self, key):
        self.key = key

    def read_key(self):
        return self.key


class FakeTerminalContext:
    def __init__(self, stdin=None, stdout=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read_key(self):
        return "q"


class FakeTTY:
    def __init__(self, fd):
        self.fd = fd

    def fileno(self):
        return self.fd

    def isatty(self):
        return True


def test_terminal_controller_reads_arrow_sequence_from_tty_fd():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x1b[A")
        terminal = TerminalController(stdin=FakeTTY(read_fd), stdout=io.StringIO())

        assert terminal.read_key() == "up"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_terminal_controller_reads_down_arrow_sequence_from_tty_fd():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x1b[B")
        terminal = TerminalController(stdin=FakeTTY(read_fd), stdout=io.StringIO())

        assert terminal.read_key() == "down"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_monitor_run_dashboard_renders_cached_snapshot_disk_metrics(monkeypatch):
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])
    monitor = Monitor(stdout=io.StringIO())
    monitor.network_sampler = NetworkSampler(
        client_factory=lambda host, **kwargs: FakeClient({"network": {}}))
    monitor.role_sampler = RoleSampler(
        client_factory=lambda host, **kwargs: FakeClient(
            {"repl": {"stateStr": "PRIMARY"}}))
    monkeypatch.setattr("mrun.monitor.TerminalController", FakeTerminalContext)
    monkeypatch.setattr(monitor, "_prime_cpu", lambda: None)
    monkeypatch.setattr(
        monitor,
        "_discover_processes_or_report",
        lambda clear_screen=False: [process],
    )

    assert monitor._run_dashboard({}) == "quit"


def test_pane_focus_helpers_cycle_forward_and_backward():
    assert next_pane("logs", 1) == "cpu"
    assert next_pane("cpu", 1) == "memory"
    assert next_pane("cpu", -1) == "logs"
    assert next_pane("unknown", 1) == "cpu"


def test_process_cursor_helpers_select_processes():
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "", "", []),
        MongoProcessInfo(11, "mongod", 27018, "", "", []),
    ]

    assert move_process_cursor(processes, None, 1) == 1
    assert move_process_cursor(processes, 1, 1) == 1
    assert selected_process(processes, 5).pid == 11
    assert selected_process([], 0) is None


def test_monitor_tab_cycles_focus_and_cpu_keys_select_process():
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "", "", []),
        MongoProcessInfo(11, "mongod", 27018, "", "", []),
    ]
    monitor = Monitor(stdout=io.StringIO())

    action = monitor._wait_for_action(
        FakeTerminal("\t"), time.time(), [], processes)

    assert action == "redraw"
    assert monitor.focused_pane == "cpu"
    assert monitor.status_message == "focus cpu pane"

    action = monitor._wait_for_action(
        FakeTerminal("down"), time.time(), [], processes)

    assert action == "redraw"
    assert monitor.cpu_cursor == 1
    assert monitor.status_message == "selected port 27018 pid 11"


def test_monitor_shift_tab_cycles_focus_backward():
    monitor = Monitor(stdout=io.StringIO())

    action = monitor._wait_for_action(
        FakeTerminal("shift-tab"), time.time(), [], [])

    assert action == "redraw"
    assert monitor.focused_pane == "disk"
    assert monitor.status_message == "focus disk pane"


def test_monitor_t_toggles_cpu_thread_view_only_when_cpu_focused():
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])
    monitor = Monitor(stdout=io.StringIO())
    monitor.refresh_interval = 0.01

    action = monitor._wait_for_action(
        FakeTerminal("t"), time.time(), [], [process])

    assert action is None
    assert monitor.cpu_thread_view is False

    monitor.focused_pane = "cpu"
    action = monitor._wait_for_action(
        FakeTerminal("t"), time.time(), [], [process])

    assert action == "resample"
    assert monitor.cpu_thread_view is True
    assert monitor.status_message == "thread view for port 27017 pid 10"

    action = monitor._wait_for_action(
        FakeTerminal("t"), time.time(), [], [process])

    assert action == "resample"
    assert monitor.cpu_thread_view is False
    assert monitor.status_message == "CPU process list"


def test_monitor_o_toggles_current_op_view_globally():
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])
    monitor = Monitor(stdout=io.StringIO())
    monitor.refresh_interval = 0.01

    action = monitor._wait_for_action(
        FakeTerminal("o"), time.time(), [], [process])

    assert action == "resample"
    assert monitor.cpu_current_op_view is True
    assert monitor.cpu_thread_view is False
    assert monitor.focused_pane == "logs"
    assert monitor.status_message == "currentOp top 10 view"

    action = monitor._wait_for_action(
        FakeTerminal("o"), time.time(), [], [process])

    assert action == "resample"
    assert monitor.cpu_current_op_view is False
    assert monitor.status_message == "CPU process list"


def test_monitor_upper_o_toggles_current_op_raw_mode():
    monitor = Monitor(stdout=io.StringIO())

    action = monitor._wait_for_action(FakeTerminal("O"), time.time(), [])

    assert action == "redraw"
    assert monitor.current_op_raw is False
    assert monitor.status_message == "press o before toggling currentOp raw"

    monitor.cpu_current_op_view = True
    action = monitor._wait_for_action(FakeTerminal("O"), time.time(), [])

    assert action == "redraw"
    assert monitor.current_op_raw is True
    assert monitor.status_message == "currentOp raw view"


def test_monitor_n_requests_current_op_namespace_selection():
    monitor = Monitor(stdout=io.StringIO())
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"

    action = monitor._wait_for_action(FakeTerminal("n"), time.time(), [])

    assert action == "select-currentop-namespace"


def test_monitor_l_requests_current_op_limit_selection():
    monitor = Monitor(stdout=io.StringIO())
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"

    action = monitor._wait_for_action(FakeTerminal("L"), time.time(), [])

    assert action == "select-currentop-limit"


def test_monitor_r_requests_current_op_source_selection_in_current_op_view():
    monitor = Monitor(stdout=io.StringIO())
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"

    action = monitor._wait_for_action(FakeTerminal("r"), time.time(), [])

    assert action == "select-currentop-sources"


def test_monitor_space_pauses_current_op_sampling():
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27018,
                "Primary",
                3.0,
                "query",
                "test.orders",
                "client",
                "desc",
                raw={"active": True},
            )
        ],
    )
    monitor = Monitor(stdout=io.StringIO())
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"

    action = monitor._wait_for_action(
        FakeTerminal(" "), time.time(), [], current_ops=snapshot)

    assert action == "redraw"
    assert monitor.current_op_paused is True
    assert monitor.current_op_paused_snapshot == snapshot
    assert monitor.status_message == "currentOp sampling paused"

    action = monitor._wait_for_action(
        FakeTerminal(" "), time.time(), [], current_ops=snapshot)

    assert action == "resample"
    assert monitor.current_op_paused is False
    assert monitor.current_op_paused_snapshot is None
    assert monitor.status_message == "currentOp sampling resumed"


def test_monitor_m_requests_mongosh_launch():
    monitor = Monitor(stdout=io.StringIO())

    action = monitor._wait_for_action(FakeTerminal("M"), time.time(), [])

    assert action == "launch-mongosh"


def test_monitor_select_current_op_limit_updates_limit():
    stdout = io.StringIO()
    monitor = Monitor(stdout=stdout, input_func=lambda: "50")
    monitor.current_op_limit = 10
    monitor.current_op_cursor = 3
    monitor.current_op_yanked_cursor = 3
    monitor.current_op_pretty_lines = ["{"]

    monitor._select_current_op_limit()

    assert monitor.current_op_limit == 50
    assert monitor.current_op_cursor is None
    assert monitor.current_op_yanked_cursor is None
    assert monitor.current_op_pretty_lines is None
    assert monitor.cpu_current_op_view is True
    assert monitor.focused_pane == "logs"
    assert monitor.status_message == "currentOp top 50 view"


def test_monitor_select_current_op_sources_updates_selected_ports():
    processes = [
        FakeProcess(10, "mongod", ["mongod", "--port", "27017"]),
        FakeProcess(11, "mongod", ["mongod", "--port", "27018"]),
    ]

    class FakeRoleSampler:
        def sample(self, processes):
            return {
                27017: RoleMetrics(True, "Secondary"),
                27018: RoleMetrics(True, "Primary"),
            }

    monitor = Monitor(
        process_iter=lambda: processes,
        include_all=True,
        stdout=io.StringIO(),
        input_func=lambda: "primary",
    )
    monitor.role_sampler = FakeRoleSampler()
    monitor.current_op_paused = True
    monitor.current_op_paused_snapshot = CurrentOpSnapshot(True, [])

    monitor._select_current_op_sources()

    assert monitor.current_op_source_ports == [27018]
    assert monitor.current_op_paused is False
    assert monitor.current_op_paused_snapshot is None
    assert monitor.cpu_current_op_view is True
    assert monitor.status_message == "currentOp sources: primary 27018"


def test_monitor_dashboard_snapshot_passes_current_op_limit():
    process = FakeProcess(
        10,
        "mongod",
        ["mongod", "--port", "27017"],
    )

    class FakeProcessSampler:
        def sample(self, processes):
            return {
                processes[0].pid: ProcessMetrics(1.0, 1024, "running"),
            }

    class FakeRoleSampler:
        def sample(self, processes):
            return {27017: RoleMetrics(True, "Primary")}

    class FakeNetworkSampler:
        def sample(self, processes):
            return {}

    class RecordingCurrentOpSampler:
        def __init__(self):
            self.limit = None
            self.namespace = None

        def sample(self, processes, role_metrics=None, limit=10, namespace=""):
            self.limit = limit
            self.namespace = namespace
            return CurrentOpSnapshot(True, [])

    current_ops = RecordingCurrentOpSampler()
    monitor = Monitor(
        process_iter=lambda: [process],
        include_all=True,
        stdout=io.StringIO(),
    )
    monitor.process_sampler = FakeProcessSampler()
    monitor.role_sampler = FakeRoleSampler()
    monitor.network_sampler = FakeNetworkSampler()
    monitor.current_op_sampler = current_ops
    monitor.cpu_current_op_view = True
    monitor.current_op_limit = 50
    monitor.current_op_namespace = "test.orders"

    monitor._read_dashboard_snapshot(LogTailer({}), {})

    assert current_ops.limit == 50
    assert current_ops.namespace == "test.orders"


def test_monitor_dashboard_snapshot_filters_current_op_sources():
    processes = [
        FakeProcess(10, "mongod", ["mongod", "--port", "27017"]),
        FakeProcess(11, "mongod", ["mongod", "--port", "27018"]),
    ]

    class FakeProcessSampler:
        def sample(self, processes):
            return {
                process.pid: ProcessMetrics(1.0, 1024, "running")
                for process in processes
            }

    class FakeRoleSampler:
        def sample(self, processes):
            return {
                27017: RoleMetrics(True, "Secondary"),
                27018: RoleMetrics(True, "Primary"),
            }

    class FakeNetworkSampler:
        def sample(self, processes):
            return {}

    class RecordingCurrentOpSampler:
        def __init__(self):
            self.ports = None

        def sample(self, processes, role_metrics=None, limit=10, namespace=""):
            self.ports = [process.port for process in processes]
            return CurrentOpSnapshot(True, [])

    current_ops = RecordingCurrentOpSampler()
    monitor = Monitor(
        process_iter=lambda: processes,
        include_all=True,
        stdout=io.StringIO(),
    )
    monitor.process_sampler = FakeProcessSampler()
    monitor.role_sampler = FakeRoleSampler()
    monitor.network_sampler = FakeNetworkSampler()
    monitor.current_op_sampler = current_ops
    monitor.cpu_current_op_view = True
    monitor.current_op_source_ports = [27018]

    monitor._read_dashboard_snapshot(LogTailer({}), {})

    assert current_ops.ports == [27018]


def test_monitor_dashboard_snapshot_reuses_paused_current_ops():
    process = FakeProcess(
        10,
        "mongod",
        ["mongod", "--port", "27017"],
    )

    class FakeProcessSampler:
        def sample(self, processes):
            return {
                processes[0].pid: ProcessMetrics(1.0, 1024, "running"),
            }

    class FakeRoleSampler:
        def sample(self, processes):
            return {27017: RoleMetrics(True, "Primary")}

    class FakeNetworkSampler:
        def sample(self, processes):
            return {}

    class FailingCurrentOpSampler:
        def sample(self, processes, role_metrics=None, limit=10, namespace=""):
            raise AssertionError("paused currentOp should not resample")

    paused_snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                1.0,
                "query",
                "test.orders",
                "client",
                "desc",
                raw={"active": True},
            )
        ],
    )
    monitor = Monitor(
        process_iter=lambda: [process],
        include_all=True,
        stdout=io.StringIO(),
    )
    monitor.process_sampler = FakeProcessSampler()
    monitor.role_sampler = FakeRoleSampler()
    monitor.network_sampler = FakeNetworkSampler()
    monitor.current_op_sampler = FailingCurrentOpSampler()
    monitor.cpu_current_op_view = True
    monitor.current_op_paused = True
    monitor.current_op_paused_snapshot = paused_snapshot

    snapshot = monitor._read_dashboard_snapshot(LogTailer({}), {})

    assert snapshot.current_ops == paused_snapshot


def test_monitor_launch_mongosh_runs_selected_target():
    process = FakeProcess(
        10,
        "mongod",
        ["mongod", "--port", "27017"],
    )
    calls = []

    class FakeRoleSampler:
        def sample(self, processes):
            return {27017: RoleMetrics(True, "Primary")}

    monitor = Monitor(
        process_iter=lambda: [process],
        include_all=True,
        stdout=io.StringIO(),
        input_func=lambda: "1",
        mongosh_runner=lambda command: calls.append(command) or 0,
        which_func=lambda executable: "/usr/bin/mongosh",
    )
    monitor.role_sampler = FakeRoleSampler()

    monitor._launch_mongosh_admin_shell()

    assert calls == [[
        "/usr/bin/mongosh",
        "mongodb://localhost:27017/admin",
    ]]
    assert monitor.status_message == "mongosh exited"


def test_monitor_launch_mongosh_reports_missing_executable():
    monitor = Monitor(
        stdout=io.StringIO(),
        which_func=lambda executable: None,
    )

    monitor._launch_mongosh_admin_shell()

    assert monitor.status_message == "mongosh not found in PATH"


def test_monitor_launch_mongosh_requires_credentials_when_missing():
    monitor = Monitor(
        stdout=io.StringIO(),
        which_func=lambda executable: "/usr/bin/mongosh",
    )
    monitor.auth_config = MonitorAuthConfig(
        enabled=True,
        username="monitoruser",
        password="",
        auth_db="admin",
        initial_user=True,
    )

    monitor._launch_mongosh_admin_shell()

    assert monitor.status_message == (
        "mongosh requires credentials for this deployment")


def test_monitor_c_clears_current_op_namespace_filter():
    monitor = Monitor(stdout=io.StringIO())
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"
    monitor.current_op_namespace = "test.orders"

    action = monitor._wait_for_action(FakeTerminal("c"), time.time(), [])

    assert action == "resample"
    assert monitor.current_op_namespace == ""
    assert monitor.status_message == "currentOp namespace filter cleared"


def test_monitor_p_toggles_current_op_pretty_view():
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                12.4,
                "query",
                "test.coll",
                "127.0.0.1",
                "find coll",
                "op1",
                raw={"op": "query", "ns": "test.coll"},
            ),
        ],
    )
    monitor = Monitor(stdout=io.StringIO())
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"

    action = monitor._wait_for_action(
        FakeTerminal("p"), time.time(), [], current_ops=snapshot)

    assert action == "redraw"
    assert monitor.current_op_pretty_lines is not None
    assert '  "op": "query",' in monitor.current_op_pretty_lines
    assert monitor.status_message == "prettified highlighted currentOp"

    action = monitor._wait_for_action(
        FakeTerminal("p"), time.time(), [], current_ops=snapshot)

    assert action == "redraw"
    assert monitor.current_op_pretty_lines is None
    assert monitor.status_message == "currentOp list view"


def test_monitor_yanks_current_op_to_terminal_clipboard():
    stdout = io.StringIO()
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                12.4,
                "query",
                "test.coll",
                "127.0.0.1",
                "find coll",
                "op1",
                raw={"op": "query", "ns": "test.coll"},
            ),
        ],
    )
    monitor = Monitor(stdout=stdout)
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"

    action = monitor._wait_for_action(
        FakeTerminal("y"), time.time(), [], current_ops=snapshot)

    assert action == "redraw"
    assert build_osc52_sequence(
        "27017 Primary 12.4 query test.coll 127.0.0.1 find coll"
    ) in stdout.getvalue()
    assert monitor.current_op_yanked_cursor == 0
    assert monitor.status_message == "yanked highlighted currentOp"


def test_monitor_yanks_current_op_pretty_json_when_active():
    stdout = io.StringIO()
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                12.4,
                "query",
                "test.coll",
                "127.0.0.1",
                "find coll",
                "op1",
                raw={"op": "query", "ns": "test.coll"},
            ),
        ],
    )
    monitor = Monitor(stdout=stdout)
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"
    monitor.current_op_pretty_lines = current_op_pretty_json_lines(
        snapshot.entries[0].raw)

    action = monitor._wait_for_action(
        FakeTerminal("y"), time.time(), [], current_ops=snapshot)

    assert action == "redraw"
    assert build_osc52_sequence(
        "\n".join(monitor.current_op_pretty_lines)) in stdout.getvalue()
    assert monitor.current_op_yanked_cursor == 0


def test_monitor_scrolls_current_op_pretty_with_jk():
    snapshot = CurrentOpSnapshot(
        True,
        [
            CurrentOpEntry(
                27017,
                "Primary",
                12.4,
                "query",
                "test.coll",
                "127.0.0.1",
                "find coll",
                "op1",
                raw={"op": "query", "ns": "test.coll"},
            ),
        ],
    )
    monitor = Monitor(stdout=io.StringIO())
    monitor.cpu_current_op_view = True
    monitor.focused_pane = "logs"
    monitor.current_op_pretty_lines = ["{"] + [
        '  "field%i": %i,' % (index, index)
        for index in range(80)
    ] + ["}"]

    action = monitor._wait_for_action(
        FakeTerminal("j"), time.time(), [], current_ops=snapshot)

    assert action == "redraw"
    assert monitor.current_op_pretty_scroll == 1
    assert monitor.current_op_cursor is None


def test_monitor_cpu_selection_resamples_when_thread_view_is_active():
    processes = [
        MongoProcessInfo(10, "mongod", 27017, "", "", []),
        MongoProcessInfo(11, "mongod", 27018, "", "", []),
    ]
    monitor = Monitor(stdout=io.StringIO())
    monitor.focused_pane = "cpu"
    monitor.cpu_thread_view = True

    action = monitor._wait_for_action(
        FakeTerminal("down"), time.time(), [], processes)

    assert action == "resample"
    assert monitor.cpu_cursor == 1
    assert monitor.status_message == "selected port 27018 pid 11"


def test_monitor_z_zooms_focused_pane():
    monitor = Monitor(stdout=io.StringIO())
    monitor.focused_pane = "cpu"

    action = monitor._wait_for_action(FakeTerminal("z"), time.time(), [], [])

    assert action == "redraw"
    assert monitor.zoom_pane == "cpu"
    assert monitor.zoom_logs is False
    assert monitor.status_message == "cpu zoom on"

    action = monitor._wait_for_action(FakeTerminal("z"), time.time(), [], [])

    assert action == "redraw"
    assert monitor.zoom_pane is None
    assert monitor.status_message == "cpu zoom off"


def test_monitor_log_controls_do_not_move_log_cursor_outside_logs_pane():
    monitor = Monitor(stdout=io.StringIO())
    monitor.focused_pane = "cpu"
    monitor.log_cursor = 1

    action = monitor._wait_for_action(
        FakeTerminal("up"), time.time(), ["first", "second"], [])

    assert action == "redraw"
    assert monitor.log_cursor == 1
    assert monitor.status_message == "no MongoDB process selected"


def test_ctrl_c_quits_monitor():
    monitor = Monitor(stdout=io.StringIO())

    action = monitor._wait_for_action(FakeTerminal("ctrl-c"), time.time(), [])

    assert action == "quit"


def test_status_sampler_segregates_metrics():
    # Exhaustive response containing Disk, Network, Storage, and other fields
    response = {
        "version": "8.0.0",
        "process": "mongod",
        "wiredTiger": {
            "block-manager": {"bytes read": 1000, "bytes written": 500},
            "log": {"total log size activated": 10000, "log bytes written": 200},
            "cache": {
                "bytes currently in the cache": 400,
                "maximum bytes configured": 1000,
                "tracked dirty bytes in the cache": 50
            },
            "concurrentTransactions": {
                "read": {"available": 128},
                "write": {"available": 127}
            }
        },
        "backgroundFlushing": {"flushes": 10},
        "network": {"bytesIn": 100, "bytesOut": 200},
        "connections": {"current": 5, "available": 95},
        "opcounters": {"insert": 1, "query": 2},
        "metrics": {"queryExecutor": {"scanned": 10}},
        "locks": {"Global": {"acquireCount": {"r": 1}}},
        "mem": {"resident": 100, "virtual": 200}
    }

    def client_factory(host, **kwargs):
        return FakeClient(response)

    sampler = StatusSampler(client_factory=client_factory)
    process = MongoProcessInfo(10, "mongod", 27017, "", "", [])

    snapshot = sampler.sample(process)

    assert snapshot.available is True
    # Verify Segregation
    assert snapshot.disk["wt_block_manager"]["bytes read"] == 1000
    assert snapshot.network["connections"]["current"] == 5
    assert snapshot.storage["wt_cache"]["maximum bytes configured"] == 1000
    assert snapshot.storage["wt_tickets"]["read"]["available"] == 128
    assert snapshot.subsystems["version"] == "8.0.0"
    assert snapshot.subsystems["metrics"] == "1 fields"
    assert snapshot.subsystems["locks"] == "1 fields"


def test_monitor_e_toggles_server_status_view():
    monitor = Monitor(stdout=io.StringIO())
    assert monitor.server_status_active is False

    action = monitor._wait_for_action(FakeTerminal("e"), time.time(), [])

    assert action == "resample"
    assert monitor.server_status_active is True
    assert "server status expanded view active" in monitor.status_message

    action = monitor._wait_for_action(FakeTerminal("E"), time.time(), [])

    assert action == "resample"
    assert monitor.server_status_active is False
    assert "dashboard view active" in monitor.status_message


def test_render_server_status_view_contains_all_sections():
    snapshot = ServerStatusSnapshot(
        available=True,
        port=27017,
        disk={"wt_block_manager": {}, "wt_log": {}, "backgroundFlushing": {}, "rates": {}},
        network={"network": {}, "connections": {}, "opcounters": {}, "rates": {}},
        storage={"wt_cache": {}, "wt_tickets": {}, "globalLock": {}, "mem": {}},
        subsystems={"version": "8.0.0", "metrics": "3 fields"}
    )

    rendered = render_server_status_view(
        snapshot, 120, 24, "test message", "controls")

    assert "DISK STATUS (port 27017)" in rendered
    assert "NETWORK STATUS (port 27017)" in rendered
    assert "STORAGE SUBSYSTEM (port 27017)" in rendered
    assert "OTHER SUBSYSTEMS (port 27017)" in rendered
    assert "metrics:" in rendered
    assert "test message" in rendered
    assert "controls" in rendered


def test_render_server_status_view_shows_unavailable_errors():
    snapshot = ServerStatusSnapshot(
        available=False,
        port=27017,
        error=AUTH_REQUIRED_STATUS,
    )

    rendered = render_server_status_view(snapshot, 100, 20, "", "controls")

    assert "Disk status unavailable." in rendered
    assert AUTH_REQUIRED_STATUS in rendered
    assert "controls" in rendered


def test_ascii_bar_rendering():
    from mrun.monitor import _ascii_bar
    assert _ascii_bar(50, 100, width=10) == "[#####-----] 50%"
    assert _ascii_bar(100, 100, width=10) == "[##########] 100%"
    assert _ascii_bar(0, 100, width=10) == "[----------] 0%"
    assert _ascii_bar(120, 100, width=10) == "[##########] 100%"


def test_monitor_review_doc_explains_invocation_path():
    with open("doc/monitor.md", "r") as fp:
        contents = fp.read()

    assert "mrun --monitor" in contents
    assert "MRunTool.run()" in contents
    assert "Monitor.run()" in contents
    assert "flowchart TD" in contents

import io
import json
import os
import time

import psutil
import pytest

from mrun.monitor import (
    ANSI_GREEN,
    ANSI_INVERSE,
    ANSI_RED,
    ANSI_DIM,
    ANSI_TEAL,
    ANSI_YELLOW,
    AUTH_REQUIRED_STATUS,
    build_monitor_tls_kwargs,
    build_osc52_sequence,
    dashboard_snapshot_due,
    detect_log_severity,
    DiskMetrics,
    format_log_lines,
    filter_mrun_processes,
    LogTailer,
    load_monitor_auth_config,
    load_monitor_tls_kwargs,
    load_mrun_process_specs,
    MonitorAuthConfig,
    Monitor,
    MongoProcessInfo,
    NetworkSampler,
    NO_MRUN_PROCESSES_MESSAGE,
    NO_PROCESSES_MESSAGE,
    ProcessDiscoveryError,
    ProcessMetrics,
    read_disk_metrics,
    discover_mongo_processes,
    move_log_cursor,
    move_process_cursor,
    next_refresh_interval,
    next_pane,
    network_status_label,
    parse_escape_sequence,
    parse_log_selection,
    process_to_info,
    prettify_log_line,
    read_log_stream,
    render_dashboard,
    selected_process,
    ThreadMetrics,
    ThreadSampler,
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
    assert "fatal/error/warning/info/debug severity colors" in flat_output
    assert "q or Ctrl+C quit" in flat_output
    assert "a toggle mrun/all processes" in flat_output
    assert "Tab switch panes" in flat_output
    assert "z zoom logs or focused pane" in flat_output
    assert "t toggles thread view" in flat_output
    assert "g latest log line" in flat_output
    assert "p prettify highlighted log line as JSON" in flat_output
    assert "y yank highlighted log line" in flat_output
    assert "space pause/resume log streaming" in flat_output
    assert "s cycle refresh 1s/5s/10s" in flat_output


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


def test_render_dashboard_contains_four_quadrants():
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
    assert "t thread view" in rendered


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
    assert "t process list" in thread_rendered


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
        terminal_size=os.terminal_size((80, 18)),
        focused_pane="cpu",
        zoom_pane="cpu",
        cpu_cursor=0,
        cpu_thread_view=True,
        thread_metrics=[ThreadMetrics(101, 25.0, 1.0, 0.5, 1.5)],
    )

    assert "[CPU Threads: port 27017 pid 10]" in rendered
    assert "Memory Usage" not in rendered
    assert "Log Tail" not in rendered
    assert "z quadrants" in rendered


def test_render_dashboard_zoom_mode_focuses_log_tail():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | first", "27017 | second"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((80, 18)),
        log_cursor=1,
        zoom_logs=True,
    )

    assert "Log Tail: 27017" in rendered
    assert "> 27017 | second" in rendered
    assert ANSI_INVERSE in rendered
    assert "CPU Usage" not in rendered
    assert "z quadrants" in rendered
    assert "g latest" in rendered
    assert "p pretty JSON" in rendered
    assert "space pause stream" in rendered
    assert "scope mrun" in rendered
    assert "a all" in rendered
    assert "s refresh 1s" in rendered


def test_render_dashboard_paused_stream_updates_title_and_footer():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {},
        ["27017 | first"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((90, 18)),
        log_cursor=0,
        stream_paused=True,
    )

    assert "Log Tail: 27017 (Paused)" in rendered
    assert "space resume stream" in rendered


def test_render_dashboard_shows_auth_required_network_status():
    process = MongoProcessInfo(10, "mongod", 27017, "/tmp/mongod.log", "", [])
    rendered = render_dashboard(
        [process],
        {10: ProcessMetrics(12.5, 1024 * 1024, "running")},
        {27017: NetworkSampler(auth_required=True).sample([process])[27017]},
        ["27017 | first"],
        selected_ports=[27017],
        terminal_size=os.terminal_size((100, 24)),
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
        terminal_size=os.terminal_size((80, 18)),
        log_cursor=0,
        zoom_logs=True,
        pretty_lines=["{", '  "msg": "hello"', "}"],
    )

    assert "Log Tail: 27017 (Pretty JSON)" in rendered
    assert '  "msg": "hello"' in rendered
    assert "> 27017" not in rendered
    assert "p raw" in rendered


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

    action = monitor._wait_for_action(
        FakeTerminal("p"), time.time(), ['27017 | {"msg":"hello"}'])

    assert action == "redraw"
    assert monitor.follow_tail is False
    assert monitor.zoom_logs is True
    assert monitor.pretty_lines == ["{", '  "msg": "hello"', "}"]
    assert monitor.pretty_previous_zoom is False
    assert monitor.status_message == "prettified highlighted log line"


def test_monitor_pretty_toggle_returns_to_previous_raw_view():
    monitor = Monitor(stdout=io.StringIO())
    monitor.log_cursor = 0
    monitor.zoom_logs = False

    monitor._toggle_pretty_log_line(['27017 | {"msg":"hello"}'])
    monitor._toggle_pretty_log_line(['27017 | {"msg":"hello"}'])

    assert monitor.pretty_lines is None
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


def test_monitor_review_doc_explains_invocation_path():
    with open("doc/monitor.md", "r") as fp:
        contents = fp.read()

    assert "mrun --monitor" in contents
    assert "MRunTool.run()" in contents
    assert "Monitor.run()" in contents
    assert "flowchart TD" in contents

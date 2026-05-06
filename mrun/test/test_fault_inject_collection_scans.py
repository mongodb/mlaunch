import io

import pytest

from mrun.fault_inject_collection_scans import (
    DEFAULT_URI,
    FaultInjectionConfig,
    build_scan_filter,
    main,
    parse_args,
    run_fault_injection,
    run_scan_once,
    validate_local_uri,
)


class FakeCursor:
    def __init__(self, documents=None):
        self.documents = documents or []
        self.comment_value = None

    def comment(self, value):
        self.comment_value = value
        return self

    def __iter__(self):
        return iter(self.documents)


class FakeCollection:
    def __init__(self, full_name="mrun_fault_injection.collection_scans",
                 count=0):
        self.full_name = full_name
        self.count = count
        self.find_calls = []
        self.inserted_documents = []
        self.dropped = False

    def count_documents(self, filter_doc):
        assert filter_doc == {}
        return self.count + len(self.inserted_documents)

    def insert_many(self, documents, ordered=False):
        self.inserted_documents.extend(documents)
        self.last_insert_ordered = ordered

    def find(self, filter_doc, projection=None, batch_size=None):
        cursor = FakeCursor()
        self.find_calls.append({
            "filter": filter_doc,
            "projection": projection,
            "batch_size": batch_size,
            "cursor": cursor,
        })
        return cursor

    def drop(self):
        self.dropped = True


class FakeAdmin:
    def __init__(self):
        self.commands = []

    def command(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        return {"ok": 1}


class FakeDatabase:
    def __init__(self, collection):
        self.collection = collection
        self.commands = []

    def __getitem__(self, name):
        return self.collection

    def command(self, *args, **kwargs):
        self.commands.append((args, kwargs))
        if args == ("profile", -1):
            return {"was": 1, "slowms": 100, "sampleRate": 0.5}
        return {"ok": 1}

    def with_options(self, **kwargs):
        self.with_options_kwargs = kwargs
        return self


class FakeClient:
    def __init__(self, database):
        self.admin = FakeAdmin()
        self.database = database
        self.closed = False

    def __getitem__(self, name):
        return self.database

    def close(self):
        self.closed = True


def test_parse_args_uses_local_replicaset_defaults_and_sleep_ms():
    config = parse_args(["--sleep-ms", "250"])

    assert config.uri == DEFAULT_URI
    assert config.db_name == "mrun_fault_injection"
    assert config.collection_name == "collection_scans"
    assert config.read_preference == "primary"
    assert config.sleep_seconds == 0.25


def test_validate_local_uri_rejects_nonlocal_hosts():
    validate_local_uri("mongodb://user:pass@localhost:27017,127.0.0.1:27018")

    with pytest.raises(ValueError) as exc:
        validate_local_uri("mongodb://example.com:27017/?replicaSet=rs0")

    assert "refusing non-local MongoDB URI hosts" in str(exc.value)


def test_dry_run_does_not_create_client():
    def fail_factory(uri):
        raise AssertionError("client factory should not be called")

    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(
        ["--dry-run", "--docs", "10"],
        client_factory=fail_factory,
        stdout=stdout,
        stderr=stderr,
    )

    assert code == 0
    assert "MongoDB collection-scan fault injection plan" in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_run_scan_once_adds_comment_and_unindexed_filter():
    collection = FakeCollection()

    seen = run_scan_once(
        collection,
        comment="mrun-monitor-fault-scan worker=3 iteration=7",
        batch_size=25,
        worker_id=3,
        iteration=7,
    )

    assert seen == 0
    assert collection.find_calls[0]["filter"] == build_scan_filter(3, 7)
    assert collection.find_calls[0]["projection"] == {"_id": 1}
    assert collection.find_calls[0]["batch_size"] == 25
    assert (
        collection.find_calls[0]["cursor"].comment_value ==
        "mrun-monitor-fault-scan worker=3 iteration=7"
    )


def test_run_fault_injection_restores_profiler_and_cleans_up():
    collection = FakeCollection(count=0)
    database = FakeDatabase(collection)
    client = FakeClient(database)
    config = FaultInjectionConfig(
        docs=3,
        payload_bytes=8,
        workers=1,
        duration_seconds=0.0,
        batch_size=2,
        enable_profiler=True,
        cleanup=True,
    )
    stdout = io.StringIO()

    summary = run_fault_injection(
        config,
        client_factory=lambda uri: client,
        out=stdout,
    )

    assert summary.scans == 0
    assert summary.errors == 0
    assert summary.profiler_enabled is True
    assert summary.cleaned_up is True
    assert client.admin.commands == [(("ping",), {})]
    assert database.commands == [
        (("profile", -1), {}),
        (("profile", 2), {"slowms": 0}),
        (("profile", 1), {"slowms": 100, "sampleRate": 0.5}),
    ]
    assert len(collection.inserted_documents) == 3
    assert collection.last_insert_ordered is False
    assert collection.dropped is True
    assert client.closed is True

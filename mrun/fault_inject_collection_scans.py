"""Generate local MongoDB collection scans for monitor testing.

This module is intentionally standalone: it does not add another console
entry point, and it only uses pymongo, which is already a mongorun dependency.
Run it with:

    uv run python mrun/fault_inject_collection_scans.py
"""

import argparse
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Iterable, List, Optional
from urllib.parse import urlparse


DEFAULT_URI = "mongodb://localhost:27017/?replicaSet=rs0"
DEFAULT_DB = "mrun_fault_injection"
DEFAULT_COLLECTION = "collection_scans"
DEFAULT_COMMENT = "mrun-monitor-fault-scan"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
READ_PREFERENCES = ("primary", "secondaryPreferred")


@dataclass
class FaultInjectionConfig:
    uri: str = DEFAULT_URI
    db_name: str = DEFAULT_DB
    collection_name: str = DEFAULT_COLLECTION
    docs: int = 5000
    payload_bytes: int = 1024
    workers: int = 2
    duration_seconds: float = 60.0
    batch_size: int = 100
    enable_profiler: bool = False
    cleanup: bool = False
    dry_run: bool = False
    comment: str = DEFAULT_COMMENT
    read_preference: str = "primary"
    allow_nonlocal: bool = False
    sleep_seconds: float = 0.0


@dataclass
class ProfilingState:
    level: int
    slowms: Optional[int]
    sample_rate: Optional[float]


@dataclass
class WorkerStats:
    scans: int = 0
    documents_seen: int = 0
    errors: int = 0
    last_error: Optional[str] = None


@dataclass
class RunSummary:
    scans: int
    documents_seen: int
    errors: int
    profiler_enabled: bool
    cleaned_up: bool


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate local MongoDB collection scans so mrun --monitor can "
            "show live log, CPU, memory, network, and disk activity."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--uri", default=DEFAULT_URI)
    parser.add_argument("--db", default=DEFAULT_DB, dest="db_name")
    parser.add_argument(
        "--collection",
        default=DEFAULT_COLLECTION,
        dest="collection_name",
    )
    parser.add_argument("--docs", type=positive_int, default=5000)
    parser.add_argument(
        "--payload-bytes",
        type=non_negative_int,
        default=1024,
        help="bytes of string payload stored in each seeded document",
    )
    parser.add_argument("--workers", type=positive_int, default=2)
    parser.add_argument(
        "--duration",
        type=positive_float,
        default=60.0,
        dest="duration_seconds",
        help="seconds to run collection-scan workers",
    )
    parser.add_argument("--batch-size", type=positive_int, default=100)
    parser.add_argument(
        "--profile",
        action="store_true",
        dest="enable_profiler",
        help=(
            "temporarily set profilingLevel=2 and slowms=0 on the test "
            "database, then restore the previous setting"
        ),
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="drop only the configured test collection after the run",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned workload without connecting to MongoDB",
    )
    parser.add_argument("--comment", default=DEFAULT_COMMENT)
    parser.add_argument(
        "--read-preference",
        choices=READ_PREFERENCES,
        default="primary",
        help="read preference used for the collection-scan workload",
    )
    parser.add_argument(
        "--allow-nonlocal",
        action="store_true",
        help="allow a URI that does not point only at localhost hosts",
    )
    parser.add_argument(
        "--sleep-ms",
        type=non_negative_float,
        default=0.0,
        help="optional sleep between scans in each worker",
    )
    return parser


def parse_args(argv: Optional[List[str]] = None) -> FaultInjectionConfig:
    args = build_parser().parse_args(argv)
    return FaultInjectionConfig(
        uri=args.uri,
        db_name=args.db_name,
        collection_name=args.collection_name,
        docs=args.docs,
        payload_bytes=args.payload_bytes,
        workers=args.workers,
        duration_seconds=args.duration_seconds,
        batch_size=args.batch_size,
        enable_profiler=args.enable_profiler,
        cleanup=args.cleanup,
        dry_run=args.dry_run,
        comment=args.comment,
        read_preference=args.read_preference,
        allow_nonlocal=args.allow_nonlocal,
        sleep_seconds=args.sleep_ms / 1000.0,
    )


def _uri_hosts(uri: str) -> List[str]:
    parsed = urlparse(uri)
    if parsed.scheme not in ("mongodb", "mongodb+srv"):
        raise ValueError("URI must start with mongodb:// or mongodb+srv://")

    netloc = parsed.netloc.rsplit("@", 1)[-1]
    hosts = []
    for hostport in netloc.split(","):
        hostport = hostport.strip()
        if not hostport:
            continue
        if hostport.startswith("["):
            host = hostport[1:].split("]", 1)[0]
        else:
            host = hostport.split(":", 1)[0]
        if host:
            hosts.append(host.lower())
    if not hosts:
        raise ValueError("URI does not contain any hosts")
    return hosts


def validate_local_uri(uri: str, allow_nonlocal: bool = False) -> None:
    if allow_nonlocal:
        return

    hosts = _uri_hosts(uri)
    nonlocal_hosts = [host for host in hosts if host not in LOCAL_HOSTS]
    if nonlocal_hosts:
        raise ValueError(
            "refusing non-local MongoDB URI hosts: %s; pass --allow-nonlocal "
            "only when you intentionally want to target them"
            % ", ".join(nonlocal_hosts)
        )


def make_pymongo_client(uri: str):
    from pymongo import MongoClient

    return MongoClient(uri, serverSelectionTimeoutMS=5000)


def apply_read_preference(database, read_preference: str):
    if read_preference == "primary":
        return database

    from pymongo import ReadPreference

    return database.with_options(
        read_preference=ReadPreference.SECONDARY_PREFERRED
    )


def build_seed_document(index: int, payload: str) -> dict:
    return {
        "source": "mrun-monitor-fault-injection",
        "seq": index,
        "bucket": index % 128,
        "payload": payload,
        "nested": {
            "even": index % 2 == 0,
            "created_at": datetime.utcnow(),
        },
    }


def ensure_seed_data(collection, docs: int, payload_bytes: int,
                     batch_size: int, out) -> None:
    current = collection.count_documents({})
    if current >= docs:
        print(
            "seed collection already has %d documents; target is %d"
            % (current, docs),
            file=out,
        )
        return

    payload = "x" * payload_bytes
    remaining = docs - current
    next_index = current
    print(
        "seeding %d documents into %s" % (remaining, collection.full_name),
        file=out,
    )
    while remaining > 0:
        count = min(batch_size, remaining)
        docs_to_insert = [
            build_seed_document(next_index + offset, payload)
            for offset in range(count)
        ]
        collection.insert_many(docs_to_insert, ordered=False)
        remaining -= count
        next_index += count


def read_profiling_state(database) -> ProfilingState:
    result = database.command("profile", -1)
    return ProfilingState(
        level=int(result.get("was", 0)),
        slowms=result.get("slowms"),
        sample_rate=result.get("sampleRate"),
    )


def enable_profiling(database) -> ProfilingState:
    previous = read_profiling_state(database)
    database.command("profile", 2, slowms=0)
    return previous


def restore_profiling(database, state: ProfilingState) -> None:
    kwargs = {}
    if state.slowms is not None:
        kwargs["slowms"] = state.slowms
    if state.sample_rate is not None:
        kwargs["sampleRate"] = state.sample_rate
    database.command("profile", state.level, **kwargs)


def build_scan_filter(worker_id: int, iteration: int) -> dict:
    return {
        "scan_probe": "missing-worker-%d-iteration-%d"
        % (worker_id, iteration)
    }


def build_scan_comment(base_comment: str, worker_id: int,
                       iteration: int) -> str:
    return "%s worker=%d iteration=%d" % (
        base_comment,
        worker_id,
        iteration,
    )


def run_scan_once(collection, comment: str, batch_size: int,
                  worker_id: int, iteration: int) -> int:
    cursor = collection.find(
        build_scan_filter(worker_id, iteration),
        projection={"_id": 1},
        batch_size=batch_size,
    ).comment(comment)

    seen = 0
    for _ in cursor:
        seen += 1
    return seen


def _scan_worker(collection, config: FaultInjectionConfig, worker_id: int,
                 stop_event: threading.Event, stats: WorkerStats) -> None:
    iteration = 0
    while not stop_event.is_set():
        comment = build_scan_comment(config.comment, worker_id, iteration)
        try:
            stats.documents_seen += run_scan_once(
                collection,
                comment,
                config.batch_size,
                worker_id,
                iteration,
            )
            stats.scans += 1
        except Exception as exc:
            stats.errors += 1
            stats.last_error = str(exc)
            stop_event.wait(1.0)
        iteration += 1
        if config.sleep_seconds:
            stop_event.wait(config.sleep_seconds)


def run_collection_scans(collection, config: FaultInjectionConfig
                         ) -> List[WorkerStats]:
    stats = [WorkerStats() for _ in range(config.workers)]
    if config.duration_seconds <= 0:
        return stats

    stop_event = threading.Event()
    threads = []
    for worker_id, worker_stats in enumerate(stats):
        thread = threading.Thread(
            target=_scan_worker,
            args=(collection, config, worker_id, stop_event, worker_stats),
        )
        thread.daemon = True
        thread.start()
        threads.append(thread)

    deadline = time.monotonic() + config.duration_seconds
    try:
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            time.sleep(min(0.25, max(0.0, remaining)))
    finally:
        stop_event.set()
        for thread in threads:
            thread.join()

    return stats


def summarize_worker_stats(stats: Iterable[WorkerStats]) -> WorkerStats:
    summary = WorkerStats()
    for worker_stats in stats:
        summary.scans += worker_stats.scans
        summary.documents_seen += worker_stats.documents_seen
        summary.errors += worker_stats.errors
        if worker_stats.last_error:
            summary.last_error = worker_stats.last_error
    return summary


def run_fault_injection(config: FaultInjectionConfig,
                        client_factory: Optional[Callable[[str], object]] = None,
                        out=None) -> RunSummary:
    validate_local_uri(config.uri, config.allow_nonlocal)
    out = out or sys.stdout
    client_factory = client_factory or make_pymongo_client

    client = client_factory(config.uri)
    primary_db = None
    profiler_state = None
    cleaned_up = False
    try:
        client.admin.command("ping")
        primary_db = client[config.db_name]
        seed_collection = primary_db[config.collection_name]
        ensure_seed_data(
            seed_collection,
            config.docs,
            config.payload_bytes,
            config.batch_size,
            out,
        )

        if config.enable_profiler:
            print(
                "enabling profiler level 2 slowms 0 on database %s"
                % config.db_name,
                file=out,
            )
            profiler_state = enable_profiling(primary_db)

        workload_db = apply_read_preference(
            primary_db,
            config.read_preference,
        )
        collection = workload_db[config.collection_name]
        print(
            "running %d worker(s) for %.1fs with comment prefix %r"
            % (config.workers, config.duration_seconds, config.comment),
            file=out,
        )
        stats = run_collection_scans(collection, config)
        totals = summarize_worker_stats(stats)
    finally:
        if profiler_state is not None and primary_db is not None:
            print("restoring previous profiler setting", file=out)
            restore_profiling(primary_db, profiler_state)
        if config.cleanup and primary_db is not None:
            primary_db[config.collection_name].drop()
            cleaned_up = True
            print(
                "dropped test collection %s.%s"
                % (config.db_name, config.collection_name),
                file=out,
            )
        if hasattr(client, "close"):
            client.close()

    print(
        "completed: scans=%d documents_seen=%d errors=%d"
        % (totals.scans, totals.documents_seen, totals.errors),
        file=out,
    )
    if totals.last_error:
        print("last worker error: %s" % totals.last_error, file=out)

    return RunSummary(
        scans=totals.scans,
        documents_seen=totals.documents_seen,
        errors=totals.errors,
        profiler_enabled=config.enable_profiler,
        cleaned_up=cleaned_up,
    )


def describe_run(config: FaultInjectionConfig) -> str:
    return "\n".join([
        "MongoDB collection-scan fault injection plan:",
        "  uri: %s" % config.uri,
        "  database: %s" % config.db_name,
        "  collection: %s" % config.collection_name,
        "  seed documents: %d" % config.docs,
        "  payload bytes per document: %d" % config.payload_bytes,
        "  workers: %d" % config.workers,
        "  duration seconds: %.1f" % config.duration_seconds,
        "  batch size: %d" % config.batch_size,
        "  profiler: %s" % ("enabled" if config.enable_profiler else "off"),
        "  cleanup: %s" % ("enabled" if config.cleanup else "off"),
        "  comment prefix: %s" % config.comment,
        "  read preference: %s" % config.read_preference,
    ])


def main(argv: Optional[List[str]] = None,
         client_factory: Optional[Callable[[str], object]] = None,
         stdout=None,
         stderr=None) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    config = parse_args(argv)
    try:
        validate_local_uri(config.uri, config.allow_nonlocal)
        if config.dry_run:
            print(describe_run(config), file=stdout)
            return 0

        summary = run_fault_injection(
            config,
            client_factory=client_factory,
            out=stdout,
        )
        return 1 if summary.errors else 0
    except KeyboardInterrupt:
        print("fault injection interrupted", file=stderr)
        return 130
    except Exception as exc:
        print("fault injection failed: %s" % exc, file=stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

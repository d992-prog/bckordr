from __future__ import annotations

import dataclasses
import importlib
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest


MODULE = "app.services.vpn_node_journal"
SECRET = "synthetic-secret-never-store-this"


@pytest.fixture
def journal():
    if importlib.util.find_spec(MODULE) is None:
        pytest.fail("The node journal contract has no implementation yet")
    return importlib.import_module(MODULE)


@pytest.fixture
def directory(tmp_path, journal):
    result = tmp_path / "node"
    result.mkdir(mode=0o700)
    journal.initialize_node_journal(result)
    return result


def operation(journal, *, key=1, generation=1, action="provision", identity=None):
    return journal.NodeOperation(identity or uuid4(), key, generation, action, "a" * 64)


def run(journal, directory, op, callback=lambda mark: mark(), **kwargs):
    return journal.execute_node_operation(directory, op, callback, **kwargs)


def lookup(journal, directory, op):
    return journal.lookup_node_operation_receipt(
        directory,
        operation_id=op.operation_id,
        request_digest=op.request_digest,
    )


def assert_error(journal, code, callback):
    with pytest.raises(journal.NodeJournalError) as error:
        callback()
    assert error.value.code == code
    assert str(error.value) == code
    assert SECRET not in str(error.value)
    assert error.value.__suppress_context__


def test_observed_replay_and_historical_replay_never_repeat_callback(
    journal, directory
):
    op = operation(journal)
    calls = []
    receipt = run(journal, directory, op, lambda mark: (mark(), calls.append(1)))
    assert (receipt.state, receipt.error_code) == ("observed", None)
    assert run(journal, directory, op, lambda mark: pytest.fail("replayed")) == receipt
    assert (
        run(journal, directory, operation(journal, generation=2, action="revoke")).state
        == "observed"
    )
    assert (
        run(journal, directory, op, lambda mark: pytest.fail("old provision"))
        == receipt
    )
    assert calls == [1]
    with pytest.raises(dataclasses.FrozenInstanceError):
        op.generation = 2
    assert not hasattr(op, "__dict__")
    assert not hasattr(receipt, "__dict__")


def test_exact_read_only_lookup_returns_receipt_without_changing_journal(
    monkeypatch, journal, directory
):
    op = operation(journal)
    receipt = run(journal, directory, op)
    before = {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    }
    uris = []
    real_connect = sqlite3.connect

    def connect(database, *args, **kwargs):
        uris.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(journal.sqlite3, "connect", connect)
    assert lookup(journal, directory, op) == receipt
    assert uris and all(uri.endswith("?mode=ro") for uri in uris)
    assert {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    } == before


def test_exact_read_only_lookup_distinguishes_missing_and_digest_conflict(
    journal, directory
):
    op = operation(journal)
    assert lookup(journal, directory, op) is None
    run(journal, directory, op)
    assert_error(
        journal,
        "vpn_node_operation_conflict",
        lambda: journal.lookup_node_operation_receipt(
            directory,
            operation_id=op.operation_id,
            request_digest="b" * 64,
        ),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("access_key_id", 2),
        ("generation", 2),
        ("action", "revoke"),
        ("request_digest", "b" * 64),
    ],
)
def test_identity_collision(journal, directory, field, value):
    op = operation(journal)
    run(journal, directory, op)
    assert_error(
        journal,
        "vpn_node_operation_conflict",
        lambda: run(journal, directory, dataclasses.replace(op, **{field: value})),
    )


def test_generation_and_permanent_revocation_even_failed_preflight(journal, directory):
    op = operation(journal, generation=3, action="revoke")

    def fail(mark):
        raise RuntimeError(SECRET)

    receipt = run(journal, directory, op, fail)
    assert (receipt.state, receipt.error_code) == (
        "failed",
        "vpn_node_preflight_failed",
    )
    assert run(journal, directory, op, lambda mark: pytest.fail("retry")) == receipt
    stale = run(journal, directory, operation(journal, generation=3, action="suspend"))
    assert (stale.state, stale.error_code) == ("stale", "vpn_node_operation_stale")
    blocked = run(journal, directory, operation(journal, generation=4))
    assert (blocked.state, blocked.error_code) == ("blocked", "vpn_node_key_revoked")
    assert (
        run(
            journal, directory, operation(journal, generation=4, action="suspend")
        ).state
        == "observed"
    )
    assert (
        run(journal, directory, operation(journal, generation=5, action="revoke")).state
        == "observed"
    )
    assert SECRET.encode() not in (directory / "operations.sqlite3").read_bytes()


def test_uncertainty_is_node_wide_and_never_expires(journal, directory):
    op = operation(journal)

    def fail(mark):
        mark()
        raise RuntimeError(SECRET)

    receipt = run(journal, directory, op, fail)
    assert (receipt.state, receipt.error_code) == (
        "uncertain",
        "vpn_node_mutation_uncertain",
    )
    assert run(journal, directory, op) == receipt
    for next_op in (
        operation(journal, key=2),
        operation(journal, generation=2, action="revoke"),
    ):
        blocked = run(journal, directory, next_op, lambda mark: pytest.fail("barrier"))
        assert (blocked.state, blocked.error_code) == (
            "blocked",
            "vpn_node_reconciliation_required",
        )


def test_other_key_allowed_after_success_and_hook_idempotent_then_expired(
    journal, directory
):
    hooks = []

    def perform(mark):
        mark()
        mark()
        hooks.append(mark)

    assert run(journal, directory, operation(journal), perform).state == "observed"
    before = (directory / "operations.sqlite3").read_bytes()
    assert_error(journal, "vpn_node_journal_invalid", hooks[0])
    assert (directory / "operations.sqlite3").read_bytes() == before
    assert run(journal, directory, operation(journal, key=2)).state == "observed"


@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
def test_base_exception_records_then_reraises_and_releases_gate(
    journal, directory, marked, exception
):
    op = operation(journal)

    def perform(mark):
        if marked:
            mark()
        raise exception()

    with pytest.raises(exception):
        run(journal, directory, op, perform)
    assert run(journal, directory, op).state == ("uncertain" if marked else "failed")


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_id", "bad"),
        ("access_key_id", True),
        ("access_key_id", 0),
        ("access_key_id", 2**63),
        ("generation", -1),
        ("generation", True),
        ("generation", 2**63),
        ("action", "enable"),
        ("request_digest", "A" * 64),
        ("request_digest", "a" * 63),
    ],
)
def test_invalid_operations_do_not_touch_directory(journal, tmp_path, field, value):
    op = dataclasses.replace(operation(journal), **{field: value})
    assert_error(
        journal,
        "vpn_node_journal_invalid",
        lambda: run(journal, tmp_path / "missing", op),
    )


@pytest.mark.parametrize("timeout", [0, -1, True, math.inf, math.nan, 31, "2"])
def test_invalid_timeout(journal, directory, timeout):
    assert_error(
        journal,
        "vpn_node_journal_invalid",
        lambda: run(
            journal, directory, operation(journal), lock_timeout_seconds=timeout
        ),
    )


def test_initialization_is_explicit_and_exclusive(journal, tmp_path):
    path = tmp_path / "private"
    path.mkdir(mode=0o700)
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, path, operation(journal)),
    )
    assert list(path.iterdir()) == []
    journal.initialize_node_journal(path)
    assert {item.name for item in path.iterdir()} == {
        "gate.sqlite3",
        "operations.sqlite3",
    }
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: journal.initialize_node_journal(path),
    )
    assert_error(
        journal,
        "vpn_node_journal_invalid",
        lambda: run(journal, Path("relative"), operation(journal)),
    )


@pytest.mark.parametrize("action", ["revoke", "provision"])
@pytest.mark.parametrize("damage", ["remove", "truncate"])
def test_lost_history_never_recreated(journal, directory, action, damage):
    def perform(mark):
        mark()
        if action == "provision":
            raise RuntimeError()

    run(journal, directory, operation(journal, action=action), perform)
    path = directory / "operations.sqlite3"
    if damage == "remove":
        path.unlink()
    else:
        path.write_bytes(b"")
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, directory, operation(journal, generation=2)),
    )
    assert not path.exists() if damage == "remove" else path.stat().st_size == 0


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE operations",
        "CREATE TABLE surprise(value TEXT)",
        "UPDATE operations SET phase='unknown'",
        "UPDATE operations SET error_code='synthetic-secret-never-store-this'",
        "DELETE FROM key_fences",
        "UPDATE key_fences SET latest_generation=0",
        "UPDATE key_fences SET permanently_revoked=0",
    ],
)
def test_schema_and_stored_value_corruption_fail_closed(journal, directory, sql):
    run(journal, directory, operation(journal, action="revoke"))
    with sqlite3.connect(directory / "operations.sqlite3") as connection:
        connection.execute(sql)
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, directory, operation(journal, key=2)),
    )


def child(directory, op, body):
    source = f"""
import os, sys, time
from pathlib import Path
from uuid import UUID
from app.services.vpn_node_journal import *
op = NodeOperation(UUID({str(op.operation_id)!r}), {op.access_key_id}, {op.generation}, {op.action!r}, {op.request_digest!r})
def perform(mark):
{body}
result = execute_node_operation(Path({str(directory)!r}), op, perform, lock_timeout_seconds=0.2)
print(result.state, result.error_code, flush=True)
"""
    return subprocess.Popen(
        [sys.executable, "-S", "-c", source],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


@pytest.mark.parametrize("marked", [False, True])
def test_real_process_death_recovers_persisted_phase(journal, directory, marked):
    op = operation(journal, action="revoke")
    process = child(
        directory, op, "    " + ("mark(); " if marked else "") + "os._exit(19)"
    )
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 19, (stdout, stderr)
    receipt = run(
        journal, directory, op, lambda mark: pytest.fail("dead callback retried")
    )
    assert (receipt.state, receipt.error_code) == (
        ("uncertain", "vpn_node_mutation_uncertain")
        if marked
        else ("failed", "vpn_node_interrupted_before_mutation")
    )
    blocked = run(
        journal,
        directory,
        operation(journal, generation=2),
        lambda mark: pytest.fail("fence"),
    )
    assert blocked.state == "blocked"
    assert blocked.error_code == (
        "vpn_node_reconciliation_required" if marked else "vpn_node_key_revoked"
    )


def test_independent_process_gate_is_held_through_callback(journal, directory):
    process = child(
        directory,
        operation(journal),
        "    print('inside', flush=True)\n    time.sleep(20)",
    )
    try:
        assert process.stdout.readline().strip() == "inside"
        started = time.monotonic()
        assert_error(
            journal,
            "vpn_node_journal_busy",
            lambda: run(
                journal, directory, operation(journal, key=2), lock_timeout_seconds=0.1
            ),
        )
        assert time.monotonic() - started < 2
    finally:
        process.kill()
        process.communicate(timeout=10)


def test_stdlib_import_under_python_s(journal):
    result = subprocess.run(
        [sys.executable, "-S", "-c", f"import {MODULE}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


class CommitFault:
    def __init__(self, connection, phase):
        self.connection = connection
        self.phase = phase

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        if self.connection.execute(
            "SELECT 1 FROM operations WHERE phase=?", (self.phase,)
        ).fetchone():
            raise sqlite3.OperationalError(SECRET)
        self.connection.commit()


@pytest.mark.parametrize("swallow", [False, True])
def test_failed_mark_commit_never_allows_normal_send_or_success(
    journal, directory, monkeypatch, swallow
):
    connect = journal._connect
    monkeypatch.setattr(
        journal,
        "_connect",
        lambda path, timeout: (
            CommitFault(connect(path, timeout), "mutating")
            if path.name == "operations.sqlite3"
            else connect(path, timeout)
        ),
    )
    sends = []
    op = operation(journal)

    def perform(mark):
        try:
            mark()
        except journal.NodeJournalError:
            if swallow:
                return
            raise
        sends.append(1)

    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, directory, op, perform),
    )
    assert sends == []
    monkeypatch.undo()
    assert (
        run(journal, directory, op).error_code == "vpn_node_interrupted_before_mutation"
    )


def test_failed_receipt_commit_after_send_never_success_and_reopen_blocks(
    journal, directory, monkeypatch
):
    connect = journal._connect
    monkeypatch.setattr(
        journal,
        "_connect",
        lambda path, timeout: (
            CommitFault(connect(path, timeout), "observed")
            if path.name == "operations.sqlite3"
            else connect(path, timeout)
        ),
    )
    sends = []
    op = operation(journal)
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, directory, op, lambda mark: (mark(), sends.append(1))),
    )
    assert sends == [1]
    monkeypatch.undo()
    process = child(
        directory, operation(journal, key=2), "    raise AssertionError('must not run')"
    )
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr
    assert stdout.strip() == "blocked vpn_node_reconciliation_required"
    assert run(journal, directory, op).state == "uncertain"


@pytest.mark.parametrize(
    "fault",
    ["mismatch", "uninitialized", "wrong_format", "missing_metadata", "empty_gate"],
)
def test_metadata_pair_and_partial_installation_fail_closed(journal, directory, fault):
    if fault == "empty_gate":
        (directory / "gate.sqlite3").write_bytes(b"")
    else:
        with sqlite3.connect(directory / "gate.sqlite3") as connection:
            if fault == "mismatch":
                connection.execute("UPDATE metadata SET journal_id=?", (str(uuid4()),))
            elif fault == "uninitialized":
                connection.execute("UPDATE metadata SET initialized=0")
            elif fault == "wrong_format":
                connection.execute("UPDATE metadata SET format=2")
            else:
                connection.execute("DELETE FROM metadata")
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, directory, operation(journal)),
    )
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: journal.initialize_node_journal(directory),
    )


def test_interrupted_install_does_not_mark_gate_ready(journal, tmp_path, monkeypatch):
    directory = tmp_path / "node"
    directory.mkdir(mode=0o700)
    connect = journal._connect

    class FailedInstall:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            raise sqlite3.OperationalError(SECRET)

    monkeypatch.setattr(
        journal,
        "_connect",
        lambda path, timeout: (
            FailedInstall(connect(path, timeout))
            if path.name == "operations.sqlite3"
            else connect(path, timeout)
        ),
    )
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: journal.initialize_node_journal(directory),
    )
    monkeypatch.undo()
    with sqlite3.connect(directory / "gate.sqlite3") as connection:
        assert connection.execute("SELECT initialized FROM metadata").fetchone() == (0,)
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, directory, operation(journal)),
    )


@pytest.mark.parametrize(
    "target", ["directory", "ancestor", "gate.sqlite3", "operations.sqlite3"]
)
def test_symlink_paths_refused(journal, directory, tmp_path, target):
    link = tmp_path / "link"
    actual_directory = directory
    try:
        if target in ("directory", "ancestor"):
            link.symlink_to(
                directory if target == "directory" else tmp_path,
                target_is_directory=True,
            )
            actual_directory = link if target == "directory" else link / "node"
        else:
            path = directory / target
            saved = tmp_path / "saved.sqlite3"
            path.replace(saved)
            path.symlink_to(saved)
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    assert_error(
        journal,
        "vpn_node_journal_invalid",
        lambda: run(journal, actual_directory, operation(journal)),
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and modes")
def test_private_modes_are_required_without_chmod_existing_files(journal, directory):
    assert directory.stat().st_mode & 0o077 == 0
    for filename in ("gate.sqlite3", "operations.sqlite3"):
        assert (directory / filename).stat().st_mode & 0o077 == 0
    path = directory / "operations.sqlite3"
    path.chmod(0o644)
    assert_error(
        journal,
        "vpn_node_journal_invalid",
        lambda: run(journal, directory, operation(journal)),
    )
    assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.skipif(os.name != "nt", reason="Windows directory reparse point")
@pytest.mark.parametrize("ancestor", [False, True])
def test_windows_junction_refused(journal, directory, tmp_path, ancestor):
    link = tmp_path / "junction"
    result = subprocess.run(
        [
            os.environ["COMSPEC"],
            "/d",
            "/c",
            "mklink",
            "/J",
            str(link),
            str(tmp_path if ancestor else directory),
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0
    path = link / "node" if ancestor else link
    assert_error(
        journal,
        "vpn_node_journal_invalid",
        lambda: run(journal, path, operation(journal)),
    )


def test_database_from_another_installation_is_not_replacement_history(
    journal, directory, tmp_path
):
    run(journal, directory, operation(journal, action="revoke"))
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    journal.initialize_node_journal(other)
    (directory / "operations.sqlite3").write_bytes(
        (other / "operations.sqlite3").read_bytes()
    )
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, directory, operation(journal, generation=2)),
    )


@pytest.mark.parametrize("filename", ["gate.sqlite3", "operations.sqlite3"])
def test_database_nonregular_file_is_refused(journal, directory, filename):
    path = directory / filename
    path.unlink()
    path.mkdir()
    assert_error(
        journal,
        "vpn_node_journal_invalid",
        lambda: run(journal, directory, operation(journal)),
    )


def test_abandoned_queue_on_other_key_is_safe_to_fail_and_continue(journal, directory):
    op = operation(journal)
    process = child(directory, op, "    os._exit(19)")
    stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 19, (stdout, stderr)
    assert run(journal, directory, operation(journal, key=2)).state == "observed"
    assert (
        run(journal, directory, op).error_code == "vpn_node_interrupted_before_mutation"
    )


def test_kill_immediately_after_queued_commit_preserves_revoke_fence(
    journal, directory
):
    op = operation(journal, action="revoke")
    source = f"""
import os
from pathlib import Path
from uuid import UUID
from app.services import vpn_node_journal as j
connect = j._connect
class KillAfterQueue:
    def __init__(self, connection): self.connection = connection
    def __getattr__(self, name): return getattr(self.connection, name)
    def commit(self):
        queued = self.connection.execute("SELECT 1 FROM operations WHERE phase='queued'").fetchone()
        self.connection.commit()
        if queued: os._exit(23)
j._connect = lambda path, timeout: KillAfterQueue(connect(path, timeout)) if path.name == 'operations.sqlite3' else connect(path, timeout)
op = j.NodeOperation(UUID({str(op.operation_id)!r}), 1, 1, 'revoke', 'a'*64)
j.execute_node_operation(Path({str(directory)!r}), op, lambda mark: os._exit(99))
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", source], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 23, result.stderr
    receipt = run(
        journal,
        directory,
        operation(journal, generation=2),
        lambda mark: pytest.fail("revoked"),
    )
    assert receipt.error_code == "vpn_node_key_revoked"
    assert (
        run(journal, directory, op).error_code == "vpn_node_interrupted_before_mutation"
    )


def test_actual_late_http_mutation_survives_executor_death_and_barrier(
    journal, directory
):
    accepted = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    mutations = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            accepted.set()
            if release.wait(10):
                mutations.append("late-enable")
                completed.set()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    op = operation(journal)
    process = child(
        directory,
        op,
        f"    import http.client\n    mark()\n    client = http.client.HTTPConnection('127.0.0.1', {server.server_port}, timeout=10)\n    client.request('POST', '/mutation')\n    client.getresponse()",
    )
    try:
        assert accepted.wait(5)
        process.kill()
        process.communicate(timeout=10)
        for late in (False, True):
            if late:
                release.set()
                assert completed.wait(5)
            for next_op in (
                operation(journal, generation=2, action="revoke"),
                operation(journal, key=2),
            ):
                receipt = run(
                    journal,
                    directory,
                    next_op,
                    lambda mark: pytest.fail("late barrier"),
                )
                assert (receipt.state, receipt.error_code) == (
                    "blocked",
                    "vpn_node_reconciliation_required",
                )
        assert mutations == ["late-enable"]
        assert run(journal, directory, op).state == "uncertain"
    finally:
        release.set()
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cleanup_error_is_static_and_gate_still_released(
    journal, directory, monkeypatch
):
    connect = journal._connect

    class CloseFault:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.connection.close()
            raise sqlite3.OperationalError(SECRET)

    monkeypatch.setattr(
        journal,
        "_connect",
        lambda path, timeout: (
            CloseFault(connect(path, timeout))
            if path.name == "operations.sqlite3"
            else connect(path, timeout)
        ),
    )
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(journal, directory, operation(journal)),
    )
    monkeypatch.undo()
    assert run(journal, directory, operation(journal, key=2)).state == "observed"


@pytest.mark.parametrize("filename", ["gate.sqlite3", "operations.sqlite3"])
@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE sqliteXsurprise(value TEXT)",
        "CREATE TRIGGER sqliteXsurprise AFTER INSERT ON metadata BEGIN SELECT 1; END",
        "CREATE INDEX sqliteXsurprise ON metadata(journal_id)",
    ],
)
def test_sqlite_like_prefix_does_not_hide_unknown_schema(
    journal, directory, filename, sql
):
    with sqlite3.connect(directory / filename) as connection:
        connection.execute(sql)
    called = []
    assert_error(
        journal,
        "vpn_node_journal_unavailable",
        lambda: run(
            journal, directory, operation(journal), lambda mark: called.append(1)
        ),
    )
    assert called == []


@pytest.mark.parametrize("exception", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("marked", [False, True])
@pytest.mark.parametrize("fault", ["receipt", "rollback", "close"])
def test_callback_interruption_survives_receipt_and_cleanup_faults(
    journal, directory, monkeypatch, exception, marked, fault
):
    connect = journal._connect
    attempts = []
    expected = exception("callback interrupted")
    op = operation(journal)
    phase = "uncertain" if marked else "failed"

    class Fault:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            if self.connection.execute(
                "SELECT 1 FROM operations WHERE phase=?", (phase,)
            ).fetchone():
                attempts.append("receipt")
                if fault == "receipt":
                    raise sqlite3.OperationalError(SECRET)
            self.connection.commit()

        def rollback(self):
            attempts.append("rollback")
            self.connection.rollback()
            if fault == "rollback":
                raise sqlite3.OperationalError(SECRET)

        def close(self):
            attempts.append("close")
            self.connection.close()
            if fault == "close":
                raise sqlite3.OperationalError(SECRET)

    monkeypatch.setattr(
        journal,
        "_connect",
        lambda path, timeout: (
            Fault(connect(path, timeout))
            if path.name == "operations.sqlite3"
            else connect(path, timeout)
        ),
    )

    def perform(mark):
        if marked:
            mark()
        raise expected

    with pytest.raises(exception) as error:
        run(journal, directory, op, perform)
    assert error.value is expected
    assert attempts == ["receipt", "rollback", "close"]
    monkeypatch.undo()
    assert run(journal, directory, op).state == phase
    assert run(journal, directory, operation(journal, key=2)).state == (
        "blocked" if marked else "observed"
    )


@pytest.mark.parametrize("initialize", [False, True])
def test_nul_path_rejected_with_static_invalid_error(journal, tmp_path, initialize):
    path = tmp_path / (SECRET + "\x00")
    call = (
        (lambda: journal.initialize_node_journal(path))
        if initialize
        else (lambda: run(journal, path, operation(journal)))
    )
    assert_error(journal, "vpn_node_journal_invalid", call)

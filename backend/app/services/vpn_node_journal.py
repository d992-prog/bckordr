"""Durable, node-local mutation fencing; no panel, network, or application imports.

The gate transaction spans the entire trusted callback. The independent operation
database commits intent before the callback and mutation intent before a send.
Uncertain writes require external reconciliation: this module has no reset or lease.
An observed receipt describes a historical callback observation, not runtime health.
Never restore ledger snapshots as recovery: matching IDs do not detect rollback
to older history from the same installation. External reconciliation is required.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from typing import Literal
from uuid import UUID, uuid4


@dataclass(frozen=True, slots=True)
class NodeOperation:
    operation_id: UUID
    access_key_id: int
    generation: int
    action: Literal["provision", "suspend", "revoke"]
    request_digest: str


@dataclass(frozen=True, slots=True)
class NodeOperationReceipt:
    state: Literal["observed", "failed", "uncertain", "stale", "blocked"]
    error_code: str | None


class NodeJournalError(ValueError):
    """Only static public error codes cross this boundary."""

    def __init__(self, code: str) -> None:
        if code not in {
            "vpn_node_journal_invalid",
            "vpn_node_journal_busy",
            "vpn_node_journal_unavailable",
            "vpn_node_operation_conflict",
        }:
            code = "vpn_node_journal_invalid"
        self.code = code
        super().__init__(code)


_METADATA = "CREATE TABLE metadata (journal_id TEXT NOT NULL, format INTEGER NOT NULL, initialized INTEGER NOT NULL)"
_OPERATIONS = "CREATE TABLE operations (operation_id TEXT PRIMARY KEY NOT NULL, access_key_id INTEGER NOT NULL, generation INTEGER NOT NULL, action TEXT NOT NULL, request_digest TEXT NOT NULL, phase TEXT NOT NULL, error_code TEXT, FOREIGN KEY (access_key_id) REFERENCES key_fences(access_key_id))"
_FENCES = "CREATE TABLE key_fences (access_key_id INTEGER PRIMARY KEY, latest_generation INTEGER NOT NULL, permanently_revoked INTEGER NOT NULL)"
_GATE_SCHEMA = {"metadata": _METADATA}
_JOURNAL_SCHEMA = {
    "metadata": _METADATA,
    "operations": _OPERATIONS,
    "key_fences": _FENCES,
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def _error(code: str = "vpn_node_journal_unavailable") -> None:
    raise NodeJournalError(code) from None


def _positive(value: object) -> bool:
    return type(value) is int and 0 < value <= 9223372036854775807


def _valid_operation(operation: object) -> bool:
    return (
        isinstance(operation, NodeOperation)
        and isinstance(operation.operation_id, UUID)
        and _positive(operation.access_key_id)
        and _positive(operation.generation)
        and operation.action in ("provision", "suspend", "revoke")
        and isinstance(operation.request_digest, str)
        and _DIGEST.fullmatch(operation.request_digest) is not None
    )


def _private(info: os.stat_result) -> bool:
    return os.name != "posix" or (
        info.st_uid == os.getuid() and info.st_mode & 0o077 == 0
    )


def _directory(directory: Path) -> None:
    if not isinstance(directory, Path) or not directory.is_absolute():
        _error("vpn_node_journal_invalid")
    try:
        for path in (directory, *directory.parents):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or bool(
                getattr(info, "st_file_attributes", 0) & 0x400
            ):
                _error("vpn_node_journal_invalid")
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or not _private(info):
            _error("vpn_node_journal_invalid")
    except (OSError, ValueError):
        _error("vpn_node_journal_invalid")


def _files(directory: Path) -> tuple[tuple[int, int], ...]:
    identities = []
    for name in ("gate.sqlite3", "operations.sqlite3"):
        path = directory / name
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or not _private(info)
            or bool(getattr(info, "st_file_attributes", 0) & 0x400)
        ):
            _error("vpn_node_journal_invalid")
        if info.st_size < 100:
            _error()
        identities.append((info.st_dev, info.st_ino))
        sidecar = directory / (name + "-journal")
        if sidecar.exists() or sidecar.is_symlink():
            extra = sidecar.lstat()
            if not stat.S_ISREG(extra.st_mode) or not _private(extra):
                _error("vpn_node_journal_invalid")
        for suffix in ("-wal", "-shm"):
            if (directory / (name + suffix)).exists() or (
                directory / (name + suffix)
            ).is_symlink():
                _error()
    return tuple(identities)


def _connect(path: Path, timeout: float) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path.as_uri() + "?mode=rw", uri=True, timeout=timeout, isolation_level=None
    )
    try:
        if connection.execute("PRAGMA journal_mode").fetchone() != ("delete",):
            _error()
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except BaseException:
        connection.close()
        raise


def _close(connection: sqlite3.Connection | None) -> None:
    if connection is not None:
        try:
            try:
                connection.rollback()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            _error()


def _sync_directory(directory: Path) -> None:
    if os.name == "posix":
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def initialize_node_journal(directory: Path) -> None:
    """Install once in an empty private directory; interrupted installs are refused."""
    _directory(directory)
    gate = journal = None
    try:
        if any(directory.iterdir()):
            _error()
        for name in ("gate.sqlite3", "operations.sqlite3"):
            descriptor = os.open(
                directory / name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            os.close(descriptor)
        _sync_directory(directory)
        journal_id = str(uuid4())
        gate = _connect(directory / "gate.sqlite3", 2.0)
        gate.execute("BEGIN IMMEDIATE")
        gate.execute(_METADATA)
        gate.execute("INSERT INTO metadata VALUES (?, 1, 0)", (journal_id,))
        gate.commit()
        journal = _connect(directory / "operations.sqlite3", 2.0)
        journal.execute("BEGIN IMMEDIATE")
        for schema in _JOURNAL_SCHEMA.values():
            journal.execute(schema)
        journal.execute("INSERT INTO metadata VALUES (?, 1, 1)", (journal_id,))
        journal.commit()
        gate.execute("BEGIN IMMEDIATE")
        gate.execute("UPDATE metadata SET initialized=1")
        gate.commit()
        _sync_directory(directory)
    except (OSError, sqlite3.Error, OverflowError, ValueError) as error:
        if isinstance(error, NodeJournalError):
            raise
        _error()
    finally:
        try:
            _close(journal)
        finally:
            _close(gate)


def _metadata(connection: sqlite3.Connection, schema: dict[str, str]) -> str:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name NOT GLOB 'sqlite_*'"
    ).fetchall()
    if len(rows) != len(schema) or any(
        kind != "table" or schema.get(name) != sql for kind, name, sql in rows
    ):
        _error()
    if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
        _error()
    rows = connection.execute(
        "SELECT journal_id, format, initialized FROM metadata"
    ).fetchall()
    if len(rows) != 1:
        _error()
    journal_id, version, initialized = rows[0]
    if (
        type(journal_id) is not str
        or str(UUID(journal_id)) != journal_id
        or type(version) is not int
        or version != 1
        or type(initialized) is not int
        or initialized != 1
    ):
        _error()
    return journal_id


def _validate_rows(connection: sqlite3.Connection) -> None:
    fences = {}
    for key, generation, revoked in connection.execute(
        "SELECT access_key_id, latest_generation, permanently_revoked FROM key_fences"
    ):
        if (
            not _positive(key)
            or not _positive(generation)
            or type(revoked) is not int
            or revoked not in (0, 1)
        ):
            _error()
        fences[key] = (generation, revoked)
    for identity, key, generation, action, digest, phase, error in connection.execute(
        "SELECT * FROM operations"
    ):
        if (
            type(identity) is not str
            or str(UUID(identity)) != identity
            or not _valid_operation(
                NodeOperation(UUID(identity), key, generation, action, digest)
            )
        ):
            _error()
        allowed = {
            "queued": (None,),
            "mutating": (None,),
            "observed": (None,),
            "failed": (
                "vpn_node_preflight_failed",
                "vpn_node_interrupted_before_mutation",
            ),
            "uncertain": ("vpn_node_mutation_uncertain",),
        }
        if (
            type(phase) is not str
            or phase not in allowed
            or error not in allowed[phase]
        ):
            _error()
        if (
            key not in fences
            or fences[key][0] < generation
            or (action == "revoke" and not fences[key][1])
        ):
            _error()


def _write_phase(
    connection: sqlite3.Connection, identity: str, phase: str, code: str | None
) -> None:
    connection.execute("BEGIN IMMEDIATE")
    cursor = connection.execute(
        "UPDATE operations SET phase=?, error_code=? WHERE operation_id=?",
        (phase, code, identity),
    )
    if cursor.rowcount != 1:
        _error()
    connection.commit()


def execute_node_operation(
    directory: Path,
    operation: NodeOperation,
    perform: Callable[[Callable[[], None]], None],
    *,
    lock_timeout_seconds: float = 2.0,
) -> NodeOperationReceipt:
    """Execute at most once; the callback must mark immediately before any send."""
    if (
        not _valid_operation(operation)
        or not callable(perform)
        or type(lock_timeout_seconds) not in (int, float)
        or not 0 < lock_timeout_seconds <= 30
        or not math.isfinite(lock_timeout_seconds)
    ):
        _error("vpn_node_journal_invalid")
    _directory(directory)
    gate = journal = None
    callback_interruption: BaseException | None = None
    try:
        identities = _files(directory)
        try:
            gate = _connect(directory / "gate.sqlite3", float(lock_timeout_seconds))
            gate.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", 0) & 255 in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                _error("vpn_node_journal_busy")
            _error()
        if _files(directory) != identities:
            _error()
        journal = _connect(
            directory / "operations.sqlite3", float(lock_timeout_seconds)
        )
        if _metadata(gate, _GATE_SCHEMA) != _metadata(journal, _JOURNAL_SCHEMA):
            _error()
        _validate_rows(journal)
        identity = str(operation.operation_id)
        fields = (
            operation.access_key_id,
            operation.generation,
            operation.action,
            operation.request_digest,
        )
        existing = journal.execute(
            "SELECT access_key_id, generation, action, request_digest, phase, error_code FROM operations WHERE operation_id=?",
            (identity,),
        ).fetchone()
        if existing is not None and existing[:4] != fields:
            _error("vpn_node_operation_conflict")
        # The gate proves all surviving in-progress entries were abandoned.
        journal.execute("BEGIN IMMEDIATE")
        journal.execute(
            "UPDATE operations SET phase='failed', error_code='vpn_node_interrupted_before_mutation' WHERE phase='queued'"
        )
        journal.execute(
            "UPDATE operations SET phase='uncertain', error_code='vpn_node_mutation_uncertain' WHERE phase='mutating'"
        )
        journal.commit()
        if existing is not None:
            phase, code = journal.execute(
                "SELECT phase, error_code FROM operations WHERE operation_id=?",
                (identity,),
            ).fetchone()
            return NodeOperationReceipt(phase, code)
        if journal.execute(
            "SELECT 1 FROM operations WHERE phase IN ('mutating', 'uncertain') LIMIT 1"
        ).fetchone():
            return NodeOperationReceipt("blocked", "vpn_node_reconciliation_required")
        fence = journal.execute(
            "SELECT latest_generation, permanently_revoked FROM key_fences WHERE access_key_id=?",
            (operation.access_key_id,),
        ).fetchone()
        if fence and operation.generation <= fence[0]:
            return NodeOperationReceipt("stale", "vpn_node_operation_stale")
        if fence and fence[1] and operation.action == "provision":
            return NodeOperationReceipt("blocked", "vpn_node_key_revoked")
        journal.execute("BEGIN IMMEDIATE")
        journal.execute(
            "INSERT INTO key_fences VALUES (?, ?, ?) ON CONFLICT(access_key_id) DO UPDATE SET latest_generation=excluded.latest_generation, permanently_revoked=MAX(key_fences.permanently_revoked, excluded.permanently_revoked)",
            (
                operation.access_key_id,
                operation.generation,
                int(operation.action == "revoke"),
            ),
        )
        journal.execute(
            "INSERT INTO operations VALUES (?, ?, ?, ?, ?, 'queued', NULL)",
            (identity, *fields),
        )
        journal.commit()
        active = True
        marked = False
        mark_failed = False

        def mark_mutating() -> None:
            nonlocal marked, mark_failed
            if not active:
                _error("vpn_node_journal_invalid")
            if mark_failed:
                _error()
            if marked:
                return
            try:
                _write_phase(journal, identity, "mutating", None)
                marked = True
            except BaseException as error:
                mark_failed = True
                if isinstance(error, Exception):
                    _error()
                raise

        raised = None
        try:
            perform(mark_mutating)
        except BaseException as error:
            raised = error
            if not isinstance(error, Exception):
                callback_interruption = error
        finally:
            active = False
        if mark_failed:
            if raised is not None and not isinstance(raised, Exception):
                raise raised
            _error()
        if raised is None:
            receipt = NodeOperationReceipt("observed", None)
        elif marked:
            receipt = NodeOperationReceipt("uncertain", "vpn_node_mutation_uncertain")
        else:
            receipt = NodeOperationReceipt("failed", "vpn_node_preflight_failed")
        if _files(directory) != identities:
            _error()
        _write_phase(journal, identity, receipt.state, receipt.error_code)
        if raised is not None and not isinstance(raised, Exception):
            raise raised
        return receipt
    except (OSError, sqlite3.Error, OverflowError, ValueError, TypeError) as error:
        # NodeJournalError is a ValueError, so retain only its allowlisted code.
        if isinstance(error, NodeJournalError):
            raise
        _error()
    finally:
        try:
            try:
                _close(journal)
            finally:
                _close(gate)
        finally:
            # Persistence and both cleanup attempts must run, but their failures
            # must not replace the callback's original process interruption.
            if callback_interruption is not None:
                raise callback_interruption from None

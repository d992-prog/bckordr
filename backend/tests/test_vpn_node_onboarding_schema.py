from datetime import UTC, datetime

from app.db import models
from app.db.migrations import MIGRATIONS
from app.schemas.control import (
    WorkerNodeBase,
    WorkerNodeCreateRequest,
    WorkerNodeResponse,
    WorkerNodeUpdateRequest,
)
from sqlalchemy import String, Text, inspect

HOST_KEY_MIGRATIONS = (
    "ALTER TABLE worker_nodes ADD COLUMN IF NOT EXISTS ssh_host_key TEXT NULL",
    "ALTER TABLE worker_nodes ADD COLUMN IF NOT EXISTS ssh_host_key_fingerprint VARCHAR(64) NULL",
)


def test_worker_node_persists_nullable_ssh_host_identity() -> None:
    columns = inspect(models.WorkerNode).columns

    host_key = columns["ssh_host_key"]
    assert isinstance(host_key.type, Text)
    assert host_key.nullable is True

    fingerprint = columns["ssh_host_key_fingerprint"]
    assert isinstance(fingerprint.type, String)
    assert fingerprint.type.length == 64
    assert fingerprint.nullable is True


def test_worker_node_response_exposes_only_the_host_key_fingerprint() -> None:
    now = datetime(2026, 10, 15, tzinfo=UTC)
    response = WorkerNodeResponse.model_validate(
        {
            "id": 1,
            "name": "vpn-node-1",
            "ssh_host_key": "ssh-ed25519 raw-public-key",
            "ssh_host_key_fingerprint": "SHA256:trusted-fingerprint",
            "last_seen_at": None,
            "last_heartbeat_at": None,
            "created_at": now,
            "updated_at": now,
        }
    ).model_dump()

    assert response["ssh_host_key_fingerprint"] == "SHA256:trusted-fingerprint"
    assert "ssh_host_key" not in response


def test_worker_node_write_schemas_do_not_accept_or_expose_host_key_fields() -> None:
    forbidden_fields = {"ssh_host_key", "ssh_host_key_fingerprint"}

    assert forbidden_fields.isdisjoint(WorkerNodeBase.model_fields)
    assert forbidden_fields.isdisjoint(WorkerNodeCreateRequest.model_fields)
    assert forbidden_fields.isdisjoint(WorkerNodeUpdateRequest.model_fields)

    create = WorkerNodeCreateRequest.model_validate(
        {
            "name": "vpn-node-1",
            "ssh_host_key": "ssh-ed25519 raw-public-key",
            "ssh_host_key_fingerprint": "SHA256:trusted-fingerprint",
        }
    )
    update = WorkerNodeUpdateRequest.model_validate(
        {
            "ssh_host_key": "ssh-ed25519 raw-public-key",
            "ssh_host_key_fingerprint": "SHA256:trusted-fingerprint",
        }
    )

    assert forbidden_fields.isdisjoint(create.model_dump())
    assert forbidden_fields.isdisjoint(update.model_dump(exclude_unset=True))


def test_host_key_migrations_are_registered_as_idempotent_startup_migrations() -> None:
    for statement in HOST_KEY_MIGRATIONS:
        assert MIGRATIONS.count(statement) == 1
        assert "ADD COLUMN IF NOT EXISTS" in statement

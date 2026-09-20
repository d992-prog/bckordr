from __future__ import annotations

import importlib
import importlib.util
import inspect
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import require_admin
from app.api.routes import control
from app.db import session as db_session
from app.db.base import Base
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription, WorkerNode
from app.schemas.control import VpnAccessKeyCreateRequest
from app.services import vpn_provisioning


def _initial_display_name():
    spec = importlib.util.find_spec("app.services.vpn_profile_names")
    assert spec is not None, "VPN profile naming service is missing"
    return importlib.import_module("app.services.vpn_profile_names").initial_display_name


def _profile_name_service(name: str):
    module = importlib.import_module("app.services.vpn_profile_names")
    function = getattr(module, name, None)
    assert callable(function), f"{name} is missing"
    return function


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'profile-names.db').as_posix()}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def issuance_env(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'issuance.db').as_posix()}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as session:
        customer = VpnCustomer(telegram_user_id="issue-primary")
        other_customer = VpnCustomer(telegram_user_id="issue-other")
        worker = WorkerNode(
            name="profile-name-node",
            status="ready",
            is_enabled=True,
            vpn_role="vpn_node",
            vpn_enabled=True,
            vpn_runtime_status="ready",
            vpn_public_host="vpn.example.test",
            vpn_inbound_id=1,
            ssh_host="192.0.2.10",
            ssh_password="test-password",
        )
        session.add_all([customer, other_customer, worker])
        await session.flush()
        first_subscription = VpnSubscription(
            customer_id=customer.id, status="active", max_devices=10
        )
        second_subscription = VpnSubscription(
            customer_id=customer.id, status="active", max_devices=10
        )
        session.add_all([first_subscription, second_subscription])
        await session.flush()
        session.add_all(
            [
                VpnAccessKey(
                    subscription_id=first_subscription.id,
                    public_name="test1",
                    status="active",
                ),
                VpnAccessKey(
                    subscription_id=second_subscription.id,
                    public_name="dropcatch-retired",
                    status="revoked",
                ),
            ]
        )
        await session.commit()
        ids = SimpleNamespace(
            customer=customer.id,
            other_customer=other_customer.id,
            first_subscription=first_subscription.id,
            second_subscription=second_subscription.id,
            worker=worker.id,
        )

    app = FastAPI()
    app.include_router(control.router)

    async def database():
        async with factory() as session:
            yield session

    async def admin():
        return SimpleNamespace(id=1, role="owner")

    async def provision(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        access_key.worker_id = worker.id
        access_key.status = "active"
        access_key.config_uri = f"vless://issued-{access_key.id}"
        return access_key

    app.dependency_overrides[db_session.get_db] = database
    app.dependency_overrides[require_admin] = admin
    monkeypatch.setattr(control, "provision_vpn_access_key", provision)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield SimpleNamespace(
            client=client, factory=factory, engine=engine, ids=ids
        )
    await engine.dispose()


@pytest.mark.parametrize(
    ("public_name", "ordinal", "expected"),
    [
        (None, 1, "Профиль 1"),
        ("   ", 2, "Профиль 2"),
        ("test", 3, "Профиль 3"),
        ("TeSt42", 4, "Профиль 4"),
        ("dropcatch-phone", 5, "Профиль 5"),
        ("DROPCATCH-", 6, "Профиль 6"),
        ("  Телефон Ивана  ", 7, "Телефон Ивана"),
        ("\x00Тел\u200bефон\ud800", 8, "Телефон"),
        ("я" * 63 + " \tignored", 9, "я" * 63),
        ("я" * 70, 10, "я" * 64),
        ("\u200b " + "x" * 64, 11, "x" * 63),
    ],
)
def test_initial_display_name_cleans_legacy_names_and_uses_customer_ordinal(
    public_name: str | None,
    ordinal: int,
    expected: str,
) -> None:
    assert _initial_display_name()(public_name, ordinal) == expected


def test_initial_display_name_does_not_mutate_meaningful_legacy_value() -> None:
    public_name = "  Рабочий ноутбук  "

    assert _initial_display_name()(public_name, 3) == "Рабочий ноутбук"
    assert public_name == "  Рабочий ноутбук  "


@pytest.mark.asyncio
async def test_initialize_customer_names_numbers_all_keys_per_customer_and_preserves_identity(
    session_factory,
) -> None:
    async with session_factory() as session:
        first_customer = VpnCustomer(telegram_user_id="first")
        second_customer = VpnCustomer(telegram_user_id="second")
        session.add_all([first_customer, second_customer])
        await session.flush()
        first_active = VpnSubscription(
            customer_id=first_customer.id, status="active", max_devices=10
        )
        second_active = VpnSubscription(
            customer_id=second_customer.id, status="active", max_devices=10
        )
        first_disabled = VpnSubscription(
            customer_id=first_customer.id, status="disabled", max_devices=10
        )
        session.add_all([first_active, second_active, first_disabled])
        await session.flush()

        keys = [
            VpnAccessKey(
                subscription_id=second_active.id,
                public_name=None,
                external_uuid=str(uuid4()),
                config_uri="vless://second-one",
                status="revoked",
            ),
            VpnAccessKey(
                subscription_id=first_active.id,
                public_name="test1",
                external_uuid=str(uuid4()),
                config_uri="vless://first-one",
                status="active",
            ),
            VpnAccessKey(
                subscription_id=second_active.id,
                public_name="test",
                external_uuid=str(uuid4()),
                config_uri="vless://second-two",
                status="suspended",
            ),
            VpnAccessKey(
                subscription_id=first_disabled.id,
                public_name="  Ноутбук  ",
                external_uuid=str(uuid4()),
                config_uri="vless://first-two",
                status="revoked",
            ),
            VpnAccessKey(
                subscription_id=first_active.id,
                public_name="legacy-name",
                display_name="Выбрано раньше",
                external_uuid=str(uuid4()),
                config_uri="vless://first-three",
                status="pending_revoke",
            ),
            VpnAccessKey(
                subscription_id=first_disabled.id,
                public_name="dropcatch-old",
                external_uuid=str(uuid4()),
                config_uri="vless://first-four",
                status="failed",
            ),
            VpnAccessKey(
                subscription_id=first_disabled.id,
                public_name="я" * 63 + " \x00ignored",
                external_uuid=str(uuid4()),
                config_uri="vless://first-five",
                status="pending_suspend",
            ),
        ]
        session.add_all(keys)
        await session.flush()
        first_customer_id = first_customer.id
        second_customer_id = second_customer.id
        identity_before = {
            key.id: (key.public_name, key.external_uuid, key.config_uri, key.status)
            for key in keys
        }

        next_first = await _profile_name_service("initialize_customer_names")(
            session, first_customer_id
        )
        next_second = await _profile_name_service("initialize_customer_names")(
            session, second_customer_id
        )
        await session.commit()

    async with session_factory() as session:
        stored = list(
            (await session.scalars(select(VpnAccessKey).order_by(VpnAccessKey.id))).all()
        )
        assert next_first == 6
        assert next_second == 3
        assert {key.id: key.display_name for key in stored} == {
            keys[0].id: "Профиль 1",
            keys[1].id: "Профиль 1",
            keys[2].id: "Профиль 2",
            keys[3].id: "Ноутбук",
            keys[4].id: "Выбрано раньше",
            keys[5].id: "Профиль 4",
            keys[6].id: "я" * 63,
        }
        assert {
            key.id: (key.public_name, key.external_uuid, key.config_uri, key.status)
            for key in stored
        } == identity_before


@pytest.mark.asyncio
async def test_initialize_customer_names_is_idempotent_and_never_replaces_non_null_names(
    session_factory,
) -> None:
    async with session_factory() as session:
        customer = VpnCustomer(telegram_user_id="stable")
        session.add(customer)
        await session.flush()
        subscription = VpnSubscription(customer_id=customer.id, max_devices=3)
        session.add(subscription)
        await session.flush()
        session.add_all(
            [
                VpnAccessKey(subscription_id=subscription.id, public_name="test1"),
                VpnAccessKey(
                    subscription_id=subscription.id,
                    public_name="phone",
                    display_name="Permanent choice",
                ),
            ]
        )
        await session.commit()
        customer_id = customer.id

    async with session_factory() as session:
        assert await _profile_name_service("initialize_customer_names")(
            session, customer_id
        ) == 3
        await session.commit()
    async with session_factory() as session:
        keys = list(
            (await session.scalars(select(VpnAccessKey).order_by(VpnAccessKey.id))).all()
        )
        keys[0].public_name = "renamed legacy identity"
        await session.commit()
    async with session_factory() as session:
        assert await _profile_name_service("initialize_customer_names")(
            session, customer_id
        ) == 3
        await session.commit()
    async with session_factory() as session:
        names = list(
            (
                await session.scalars(
                    select(VpnAccessKey.display_name).order_by(VpnAccessKey.id)
                )
            ).all()
        )
        assert names == ["Профиль 1", "Permanent choice"]


@pytest.mark.asyncio
async def test_backfill_profile_names_returns_without_customers(session_factory) -> None:
    await _profile_name_service("backfill_profile_names")(session_factory)


@pytest.mark.asyncio
async def test_backfill_profile_names_commits_batches_and_resumes_after_interruption(
    session_factory, monkeypatch
) -> None:
    async with session_factory() as session:
        for ordinal in range(1, 102):
            customer = VpnCustomer(telegram_user_id=f"batch-{ordinal}")
            session.add(customer)
            await session.flush()
            subscription = VpnSubscription(customer_id=customer.id)
            session.add(subscription)
            await session.flush()
            session.add(
                VpnAccessKey(subscription_id=subscription.id, public_name="test")
            )
        await session.commit()

    module = importlib.import_module("app.services.vpn_profile_names")
    original_initialize = _profile_name_service("initialize_customer_names")

    async def interrupt_last_batch(session, customer_id):
        if customer_id == 101:
            raise RuntimeError("simulated interruption")
        return await original_initialize(session, customer_id)

    monkeypatch.setattr(module, "initialize_customer_names", interrupt_last_batch)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        await _profile_name_service("backfill_profile_names")(session_factory)

    async with session_factory() as session:
        names = list(
            (
                await session.scalars(
                    select(VpnAccessKey.display_name).order_by(VpnAccessKey.id)
                )
            ).all()
        )
        assert names[:100] == ["Профиль 1"] * 100
        assert names[100] is None

    monkeypatch.setattr(module, "initialize_customer_names", original_initialize)
    await _profile_name_service("backfill_profile_names")(session_factory)

    async with session_factory() as session:
        names = list(
            (
                await session.scalars(
                    select(VpnAccessKey.display_name).order_by(VpnAccessKey.id)
                )
            ).all()
        )
        assert names == ["Профиль 1"] * 101


def test_access_key_create_request_validates_optional_display_name() -> None:
    request = VpnAccessKeyCreateRequest(
        subscription_id=1,
        public_name="legacy",
        display_name="  Рабочий ноутбук  ",
    )

    assert request.public_name == "legacy"
    assert getattr(request, "display_name", None) == "Рабочий ноутбук"
    assert VpnAccessKeyCreateRequest(subscription_id=1).public_name is None


@pytest.mark.parametrize("display_name", ["", "   ", "bad\x00name", "x" * 65])
def test_access_key_create_request_rejects_invalid_display_name(
    display_name: str,
) -> None:
    with pytest.raises(ValidationError) as error:
        VpnAccessKeyCreateRequest(subscription_id=1, display_name=display_name)

    assert "invalid_display_name" in str(error.value)
    assert error.value.errors()[0]["loc"] == ("display_name",)


def test_startup_backfills_profile_names_immediately_after_migrations() -> None:
    from app import main

    source = inspect.getsource(main.lifespan)
    assert "await backfill_profile_names(AsyncSessionLocal)" in source
    assert source.index("await run_startup_migrations(engine)") < source.index(
        "await backfill_profile_names(AsyncSessionLocal)"
    ) < source.index("await ensure_owner_account")


@pytest.mark.asyncio
async def test_key_creation_uses_explicit_name_then_legacy_fallback_across_subscriptions(
    issuance_env,
) -> None:
    explicit = await issuance_env.client.post(
        "/control/vpn/access-keys",
        json={
            "subscription_id": issuance_env.ids.first_subscription,
            "worker_id": issuance_env.ids.worker,
            "public_name": "test99",
            "display_name": "  My laptop  ",
        },
    )
    legacy_name = await issuance_env.client.post(
        "/control/vpn/access-keys",
        json={
            "subscription_id": issuance_env.ids.second_subscription,
            "worker_id": issuance_env.ids.worker,
            "public_name": "  Планшет  ",
        },
    )
    legacy_placeholder = await issuance_env.client.post(
        "/control/vpn/access-keys",
        json={
            "subscription_id": issuance_env.ids.second_subscription,
            "worker_id": issuance_env.ids.worker,
            "public_name": "test",
        },
    )

    assert [
        explicit.status_code,
        legacy_name.status_code,
        legacy_placeholder.status_code,
    ] == [201, 201, 201]
    async with issuance_env.factory() as session:
        keys = list(
            (await session.scalars(select(VpnAccessKey).order_by(VpnAccessKey.id))).all()
        )
        assert [key.display_name for key in keys] == [
            "Профиль 1",
            "Профиль 2",
            "My laptop",
            "Планшет",
            "Профиль 5",
        ]


@pytest.mark.asyncio
async def test_key_creation_queries_customer_before_subscription_and_worker_locks(
    issuance_env,
) -> None:
    statements: list[str] = []

    def capture_sql(connection, clauseelement, multiparams, params, execution_options):
        del connection, multiparams, params, execution_options
        statement = str(clauseelement.compile(dialect=postgresql.dialect()))
        statements.append(" ".join(statement.lower().split()))

    event.listen(issuance_env.engine.sync_engine, "before_execute", capture_sql)
    try:
        response = await issuance_env.client.post(
            "/control/vpn/access-keys",
            json={
                "subscription_id": issuance_env.ids.first_subscription,
                "worker_id": issuance_env.ids.worker,
                "public_name": "phone",
            },
        )
    finally:
        event.remove(issuance_env.engine.sync_engine, "before_execute", capture_sql)

    assert response.status_code == 201
    customer_id_lookup = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("select vpn_subscriptions.customer_id")
    )
    customer_lock_shape = next(
        index
        for index, statement in enumerate(statements)
        if " from vpn_customers " in statement and statement.endswith(" for update")
    )
    subscription_lock_shape = next(
        index
        for index, statement in enumerate(statements)
        if statement.startswith("select vpn_subscriptions.id")
        and statement.endswith(" for update")
    )
    worker_lock_shape = next(
        index
        for index, statement in enumerate(statements)
        if " from worker_nodes " in statement and statement.endswith(" for update")
    )
    assert (
        customer_id_lookup
        < customer_lock_shape
        < subscription_lock_shape
        < worker_lock_shape
    )


@pytest.mark.asyncio
async def test_key_creation_rejects_subscription_that_changed_customer(
    issuance_env, monkeypatch
) -> None:
    original_lock = control.lock_vpn_subscription

    async def changed_lock(session, subscription_id):
        subscription = await original_lock(session, subscription_id)
        assert subscription is not None
        subscription.customer_id = issuance_env.ids.other_customer
        return subscription

    monkeypatch.setattr(control, "lock_vpn_subscription", changed_lock)
    response = await issuance_env.client.post(
        "/control/vpn/access-keys",
        json={
            "subscription_id": issuance_env.ids.first_subscription,
            "worker_id": issuance_env.ids.worker,
            "public_name": "phone",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "VPN subscription customer changed"


def _embedded_json_payload(command: str) -> dict[str, object]:
    export_line = next(
        line
        for line in command.splitlines()
        if "DROPCATCH_VPN_CLIENT_" in line and "PAYLOAD=" in line
    )
    payload_start = export_line.index("{")
    payload_end = export_line.rindex("}") + 1
    return json.loads(export_line[payload_start:payload_end])


@pytest.mark.asyncio
async def test_display_name_edits_do_not_change_remote_vpn_identity_or_reset_counters(
    session_factory, monkeypatch
) -> None:
    commands: list[str] = []
    original_uuid = "11111111-1111-4111-8111-111111111111"
    original_uri = f"vless://{original_uuid}@vpn.example.test:443"

    async def ssh(worker, incoming):
        del worker
        commands.extend(incoming)
        command = incoming[0]
        if "DROPCATCH_VPN_CLIENT_SUSPEND_PAYLOAD=" in command:
            return "DROPCATCH_VPN_CLIENT_SUSPEND_STATUS=suspended\n"
        if "DROPCATCH_VPN_CLIENT_REVOKE_PAYLOAD=" in command:
            return "DROPCATCH_VPN_CLIENT_REVOKE_STATUS=revoked\n"
        return (
            "DROPCATCH_VPN_CLIENT_STATUS=provisioned\n"
            f"DROPCATCH_VPN_CLIENT_URL={original_uri}\n"
        )

    monkeypatch.setattr(vpn_provisioning, "execute_worker_ssh_commands", ssh)
    async with session_factory() as session:
        customer = VpnCustomer(telegram_user_id="remote-identity")
        worker = WorkerNode(
            name="identity-node",
            status="ready",
            is_enabled=True,
            vpn_role="vpn_node",
            vpn_enabled=True,
            vpn_runtime_status="ready",
            vpn_public_host="vpn.example.test",
            vpn_inbound_id=1,
            ssh_host="192.0.2.20",
            ssh_password="test-password",
        )
        session.add_all([customer, worker])
        await session.flush()
        subscription = VpnSubscription(
            customer_id=customer.id,
            status="active",
            max_devices=3,
            traffic_limit_gb=25,
        )
        session.add(subscription)
        await session.flush()
        access_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=worker.id,
            public_name="Legacy Phone",
            display_name="Телефон",
            status="active",
            external_uuid=original_uuid,
            config_uri=original_uri,
        )
        session.add(access_key)
        await session.flush()
        expected_email = f"dropcatch-{access_key.id}-legacy-phone"

        await vpn_provisioning.provision_vpn_access_key(
            session, access_key, subscription=subscription, worker=worker
        )
        access_key.display_name = "Рабочий ноутбук"
        await vpn_provisioning.provision_vpn_access_key(
            session, access_key, subscription=subscription, worker=worker
        )
        access_key.display_name = "Новый ярлык"
        await vpn_provisioning.suspend_vpn_access_key(
            session, access_key, worker=worker
        )
        await vpn_provisioning.revoke_vpn_access_key(
            session, access_key, worker=worker
        )

        payloads = [_embedded_json_payload(command) for command in commands]
        assert len(payloads) == 4
        assert {payload["client_email"] for payload in payloads} == {expected_email}
        assert {payload["client_uuid"] for payload in payloads} == {original_uuid}
        assert access_key.external_uuid == original_uuid
        assert access_key.config_uri == original_uri
        assert access_key.public_name == "Legacy Phone"
        assert access_key.display_name == "Новый ярлык"
        assert all("resetClientTraffic" not in command for command in commands)
        assert all("reset_client_traffic" not in command for command in commands)

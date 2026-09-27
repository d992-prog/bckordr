from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

import asyncssh
import pytest

from app.services.vpn_node_transport import VpnNodeTransportSnapshot
from app.services.vpn_reality_endpoint_installer import (
    EndpointInstallRequest,
    encode_install_receipt,
    encode_install_request,
    make_endpoint_receipt,
    parse_install_request,
)


BACKEND = Path(__file__).parents[1]
PASSWORD = "synthetic-ssh-password"
PUBLIC_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"


def _request(action="inspect", **overrides):
    value = {
        "version": 1,
        "action": action,
        "worker_id": 15,
        "public_host": "vpn.example.test",
        "server_name": "front.example.test",
        "short_id": "0123456789abcdef",
    }
    value.update(overrides)
    return parse_install_request(value)


def _worker_2_request(action="inspect"):
    return EndpointInstallRequest(
        action=action,
        worker_id=2,
        public_host="vpn.example.test",
        server_name="front.example.test",
        short_id="0123456789abcdef",
    )


def _receipt(state="observed", *, worker_id=15):
    return make_endpoint_receipt(
        state=state,
        worker_id=worker_id,
        inbound_id=27,
        public_host="vpn.example.test",
        server_name="front.example.test",
        public_key=PUBLIC_KEY,
        short_id="0123456789abcdef",
    )


def test_endpoint_candidate_bundle_is_deterministic_minimal_and_importable(tmp_path: Path) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    first = tmp_path / "first.pyz"
    second = tmp_path / "second.pyz"

    first_digest = module.build_endpoint_installer_bundle(BACKEND, first, worker_id=15)
    second_digest = module.build_endpoint_installer_bundle(BACKEND, second, worker_id=15)

    assert first_digest == second_digest == hashlib.sha256(first.read_bytes()).hexdigest()
    assert first.read_bytes() == second.read_bytes()
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == {"__main__.py", *module.ENDPOINT_BUNDLE_MEMBERS}
        assert "app/services/vpn_node_entrypoint.py" not in archive.namelist()
        assert "app/services/vpn_node_bundle.py" not in archive.namelist()
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(first), "--import-probe"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        module.ENDPOINT_IMPORT_PROBE_SENTINEL,
        b"",
    )


def _run_candidate_admin(module, parent: Path, request: bytes):
    source = module._candidate_admin_source(parent, Path(sys.executable))
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", source],
        input=request,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_endpoint_candidate_bundle_requires_worker_id(tmp_path: Path) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    target = tmp_path / "missing-worker.pyz"

    with pytest.raises(TypeError):
        module.build_endpoint_installer_bundle(BACKEND, target)

    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("worker_id", [True, 0, -1, 2**63])
def test_endpoint_candidate_bundle_rejects_invalid_worker_id(
    tmp_path: Path, worker_id: object
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    target = tmp_path / "invalid-worker.pyz"

    with pytest.raises(module.EndpointInstallerDeploymentError) as caught:
        module.build_endpoint_installer_bundle(BACKEND, target, worker_id=worker_id)

    assert str(caught.value) == "vpn_endpoint_installer_transport_failed"
    assert not list(tmp_path.iterdir())


def test_endpoint_candidate_bundle_is_bound_to_worker_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = importlib.import_module("app.services.vpn_reality_endpoint_installer")
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    worker_15 = tmp_path / "worker-15.pyz"
    worker_2 = tmp_path / "worker-2.pyz"

    digest_15 = module.build_endpoint_installer_bundle(BACKEND, worker_15, worker_id=15)
    digest_2 = module.build_endpoint_installer_bundle(BACKEND, worker_2, worker_id=2)

    assert digest_15 != digest_2
    with zipfile.ZipFile(worker_2) as archive:
        main = archive.read("__main__.py")
    assert b"installer.CONTROLLED_WORKER_ID = 2\n" in main
    assert b"installer.CONTROLLED_WORKER_ID = 15\n" not in main

    observed = []

    def probe_main() -> int:
        assert installer.CONTROLLED_WORKER_ID == 2
        worker_2_request = {
            "version": 1,
            "action": "inspect",
            "worker_id": 2,
            "public_host": "vpn.example.test",
            "server_name": "front.example.test",
            "short_id": "0123456789abcdef",
        }
        assert installer.parse_install_request(worker_2_request).worker_id == 2
        with pytest.raises(installer.EndpointInstallError) as caught:
            installer.parse_install_request({**worker_2_request, "worker_id": 15})
        assert caught.value.code == "vpn_endpoint_install_request_invalid"
        observed.append(True)
        return 0

    monkeypatch.setattr(installer, "CONTROLLED_WORKER_ID", installer.CONTROLLED_WORKER_ID)
    monkeypatch.setattr(installer, "main", probe_main)
    monkeypatch.setattr(sys, "argv", [str(worker_2)])
    with pytest.raises(SystemExit) as caught:
        exec(compile(main, "__main__.py", "exec"), {"__name__": "__main__"})

    assert caught.value.code == 0
    assert observed == [True]


@pytest.fixture
def secure_candidate_parent(tmp_path: Path):
    if os.name == "posix":
        if os.geteuid() != 0:
            pytest.skip("root-only candidate administrator")
        with tempfile.TemporaryDirectory(
            prefix="veltrix-endpoint-test-", dir="/root"
        ) as raw:
            parent = Path(raw) / "remote"
            parent.mkdir(mode=0o700)
            yield parent
        return
    parent = tmp_path / "remote"
    parent.mkdir(mode=0o700)
    yield parent


def test_candidate_admin_installs_idempotently_and_removes_only_exact_hash(
    tmp_path: Path,
    secure_candidate_parent: Path,
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    bundle_path = tmp_path / "built.pyz"
    digest = module.build_endpoint_installer_bundle(BACKEND, bundle_path, worker_id=15)
    bundle = bundle_path.read_bytes()
    parent = secure_candidate_parent
    target = parent / module.ENDPOINT_CANDIDATE_NAME
    install_request = module._encode_candidate_admin_request(
        "install", digest, bundle
    )

    first = _run_candidate_admin(module, parent, install_request)
    second = _run_candidate_admin(module, parent, install_request)

    assert (first.returncode, first.stderr) == (0, b"")
    assert json.loads(first.stdout) == {
        "version": 1,
        "state": "installed",
        "sha256": digest,
    }
    assert json.loads(second.stdout)["state"] == "already_present"
    assert target.read_bytes() == bundle
    if os.name == "posix":
        assert os.stat(target).st_mode & 0o777 == 0o600
    assert not list(parent.glob(".endpoint-installer-*.tmp"))

    wrong_digest = "f" * 64 if digest != "f" * 64 else "e" * 64
    refused = _run_candidate_admin(
        module,
        parent,
        module._encode_candidate_admin_request("remove", wrong_digest),
    )
    removed = _run_candidate_admin(
        module,
        parent,
        module._encode_candidate_admin_request("remove", digest),
    )

    assert (refused.returncode, refused.stdout, refused.stderr) == (1, b"", b"")
    assert json.loads(removed.stdout) == {
        "version": 1,
        "state": "removed",
        "sha256": digest,
    }
    assert not target.exists()


def test_candidate_admin_never_overwrites_a_foreign_target(tmp_path: Path) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    bundle_path = tmp_path / "built.pyz"
    digest = module.build_endpoint_installer_bundle(BACKEND, bundle_path, worker_id=15)
    bundle = bundle_path.read_bytes()
    parent = tmp_path / "remote"
    parent.mkdir(mode=0o700)
    target = parent / module.ENDPOINT_CANDIDATE_NAME
    target.write_bytes(b"foreign-candidate")

    refused = _run_candidate_admin(
        module,
        parent,
        module._encode_candidate_admin_request("install", digest, bundle),
    )

    assert (refused.returncode, refused.stdout, refused.stderr) == (1, b"", b"")
    assert target.read_bytes() == b"foreign-candidate"
    assert not list(parent.glob(".endpoint-installer-*.tmp"))


class PasswordServer(asyncssh.SSHServer):
    def begin_auth(self, _username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "root" and password == PASSWORD


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_id", [15, 2])
async def test_strict_runner_uses_explicit_worker_pin_fixed_command_and_exact_io(
    worker_id: int,
) -> None:
    installer = importlib.import_module("app.services.vpn_reality_endpoint_installer")
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    assert installer.CONTROLLED_WORKER_ID == 15
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    invocations = []
    observed_globals = []

    async def boundary_connector(*args, **kwargs):
        observed_globals.append(installer.CONTROLLED_WORKER_ID)
        return await asyncssh.connect(*args, **kwargs)

    async def process_factory(process):
        raw = await process.stdin.read()
        invocations.append((process.command, process.term_type, raw))
        process.stdout.write(encode_install_receipt(_receipt(worker_id=worker_id)))
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=f"[127.0.0.1]:{port} ".encode() + host_key.export_public_key("openssh"),
            password=PASSWORD,
        )
        request = _request() if worker_id == 15 else _worker_2_request()

        receipt = await module.execute_endpoint_installer_over_ssh(
            snapshot,
            request,
            controlled_worker_id=worker_id,
            connector=boundary_connector,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert receipt == _receipt(worker_id=worker_id)
    assert observed_globals == [15]
    assert installer.CONTROLLED_WORKER_ID == 15
    assert invocations == [
        (
            module.FIXED_ENDPOINT_INSTALLER_COMMAND,
            None,
            encode_install_request(request, controlled_worker_id=worker_id),
        )
    ]


@pytest.mark.asyncio
async def test_strict_runner_rejects_worker_mismatch_before_connector() -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    snapshot = VpnNodeTransportSnapshot(
        host="127.0.0.1",
        port=2222,
        username="root",
        known_hosts=b"[127.0.0.1]:2222 " + host_key.export_public_key("openssh"),
        password=PASSWORD,
    )
    assert module._connection_options(snapshot)["password"] == PASSWORD
    connected = False

    async def connector(*_args, **_kwargs):
        nonlocal connected
        connected = True
        pytest.fail("worker mismatch attempted a connection")

    with pytest.raises(
        module.EndpointInstallerDeploymentError,
        match="^vpn_endpoint_installer_transport_failed$",
    ):
        await module.execute_endpoint_installer_over_ssh(
            snapshot,
            _worker_2_request(),
            controlled_worker_id=15,
            connector=connector,
        )

    assert connected is False


@pytest.mark.asyncio
async def test_candidate_install_and_post_acceptance_cleanup_use_only_fixed_commands(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    bundle_path = tmp_path / "candidate.pyz"
    digest = module.build_endpoint_installer_bundle(BACKEND, bundle_path, worker_id=15)
    bundle = bundle_path.read_bytes()
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    invocations = []

    async def process_factory(process):
        raw = await process.stdin.read()
        invocations.append((process.command, process.term_type, raw))
        if process.command == module.FIXED_ENDPOINT_INSTALLER_COMMAND:
            process.stdout.write(encode_install_receipt(_receipt()))
        elif len(invocations) == 1:
            process.stdout.write(module._encode_candidate_admin_receipt("installed", digest))
        else:
            process.stdout.write(module._encode_candidate_admin_receipt("removed", digest))
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=f"[127.0.0.1]:{port} ".encode()
            + host_key.export_public_key("openssh"),
            password=PASSWORD,
        )
        installed_digest = await module.install_endpoint_installer_over_ssh(
            snapshot,
            bundle,
            expected_sha256=digest,
        )
        observed = await module.remove_endpoint_installer_over_ssh(
            snapshot,
            digest,
            inspect_request=_request("inspect"),
            controlled_worker_id=15,
        )
    finally:
        server.close()
        await server.wait_closed()

    assert installed_digest == digest
    assert observed == _receipt()
    assert [item[0] for item in invocations] == [
        module.FIXED_ENDPOINT_CANDIDATE_ADMIN_COMMAND,
        module.FIXED_ENDPOINT_INSTALLER_COMMAND,
        module.FIXED_ENDPOINT_CANDIDATE_ADMIN_COMMAND,
    ]
    assert all(item[1] is None for item in invocations)
    assert invocations[0][2] == module._encode_candidate_admin_request(
        "install", digest, bundle
    )
    assert invocations[2][2] == module._encode_candidate_admin_request(
        "remove", digest
    )


@pytest.mark.asyncio
async def test_candidate_install_requires_the_precomputed_reviewed_digest() -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    bundle = b"synthetic-bundle"
    wrong_digest = "f" * 64

    with pytest.raises(
        module.EndpointInstallerDeploymentError,
        match="^vpn_endpoint_installer_transport_failed$",
    ):
        await module.install_endpoint_installer_over_ssh(
            None,
            bundle,
            expected_sha256=wrong_digest,
            connector=lambda *_args, **_kwargs: pytest.fail(
                "digest mismatch attempted a connection"
            ),
        )


@pytest.mark.asyncio
async def test_mutating_runner_failure_after_stdin_is_classified_uncertain_without_leak() -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    secret = "remote-private-material"

    async def process_factory(process):
        await process.stdin.read()
        process.stderr.write(secret.encode())
        process.exit(1)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=f"[127.0.0.1]:{port} ".encode() + host_key.export_public_key("openssh"),
            password=PASSWORD,
        )
        with pytest.raises(module.EndpointInstallerDeploymentError) as caught:
            await module.execute_endpoint_installer_over_ssh(
                snapshot,
                _request("ensure"),
                controlled_worker_id=15,
            )
    finally:
        server.close()
        await server.wait_closed()

    assert caught.value.mutation_uncertain is True
    assert str(caught.value) == "vpn_endpoint_installer_mutation_uncertain"
    assert secret not in repr(caught.value)


@pytest.mark.asyncio
async def test_runner_timeout_is_bounded(monkeypatch) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    host_key = asyncssh.generate_private_key("ssh-ed25519")

    async def process_factory(process):
        await process.stdin.read()
        await asyncio.sleep(1)
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=f"[127.0.0.1]:{port} ".encode() + host_key.export_public_key("openssh"),
            password=PASSWORD,
        )
        monkeypatch.setattr(module, "ENDPOINT_OPERATION_TIMEOUT", 0.01)
        with pytest.raises(module.EndpointInstallerDeploymentError) as caught:
            await module.execute_endpoint_installer_over_ssh(
                snapshot,
                _request("ensure"),
                controlled_worker_id=15,
            )
        assert caught.value.mutation_uncertain is True
    finally:
        server.close()
        await server.wait_closed()

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import asyncssh
import pytest

from app.services.vpn_node_transport import VpnNodeTransportSnapshot
from app.services.vpn_reality_endpoint_installer import (
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


def _receipt(state="observed"):
    return make_endpoint_receipt(
        state=state,
        worker_id=15,
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

    first_digest = module.build_endpoint_installer_bundle(BACKEND, first)
    second_digest = module.build_endpoint_installer_bundle(BACKEND, second)

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


def test_candidate_admin_installs_idempotently_and_removes_only_exact_hash(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    bundle_path = tmp_path / "built.pyz"
    digest = module.build_endpoint_installer_bundle(BACKEND, bundle_path)
    bundle = bundle_path.read_bytes()
    parent = tmp_path / "remote"
    parent.mkdir(mode=0o700)
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
    digest = module.build_endpoint_installer_bundle(BACKEND, bundle_path)
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
async def test_strict_runner_uses_pin_fixed_command_and_exact_stdin_stdout() -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    invocations = []

    async def process_factory(process):
        raw = await process.stdin.read()
        invocations.append((process.command, process.term_type, raw))
        process.stdout.write(encode_install_receipt(_receipt()))
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
        request = _request()

        receipt = await module.execute_endpoint_installer_over_ssh(snapshot, request)
    finally:
        server.close()
        await server.wait_closed()

    assert receipt == _receipt()
    assert invocations == [
        (module.FIXED_ENDPOINT_INSTALLER_COMMAND, None, encode_install_request(request))
    ]


@pytest.mark.asyncio
async def test_candidate_install_and_post_acceptance_cleanup_use_only_fixed_commands(
    tmp_path: Path,
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    bundle_path = tmp_path / "candidate.pyz"
    digest = module.build_endpoint_installer_bundle(BACKEND, bundle_path)
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
            await module.execute_endpoint_installer_over_ssh(snapshot, _request("ensure"))
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
            await module.execute_endpoint_installer_over_ssh(snapshot, _request("ensure"))
        assert caught.value.mutation_uncertain is True
    finally:
        server.close()
        await server.wait_closed()

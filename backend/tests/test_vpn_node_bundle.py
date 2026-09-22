from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from app.services.vpn_node_bundle import BUNDLE_MEMBERS, build_node_bundle


BACKEND = Path(__file__).resolve().parents[1]


def archive_members(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return set(archive.namelist())


def test_bundle_has_exact_manifest_and_stable_hash(tmp_path):
    first = tmp_path / "first.pyz"
    second = tmp_path / "second.pyz"
    first_hash = build_node_bundle(BACKEND, first)
    second_hash = build_node_bundle(BACKEND, second)

    expected = set(BUNDLE_MEMBERS) | {"__main__.py"}
    assert archive_members(first) == expected
    assert first.read_bytes() == second.read_bytes()
    assert first_hash == second_hash == hashlib.sha256(first.read_bytes()).hexdigest()


def test_bundle_executes_under_isolated_python_without_site_packages(tmp_path):
    bundle = tmp_path / "node.pyz"
    build_node_bundle(BACKEND, bundle)
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(bundle)],
        input=b"not-json",
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 2
    assert result.stdout == result.stderr == b""


def test_bundle_import_probe_emits_unambiguous_sentinel(tmp_path):
    from app.services.vpn_node_bundle import IMPORT_PROBE_SENTINEL

    bundle = tmp_path / "node.pyz"
    build_node_bundle(BACKEND, bundle)
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(bundle), "--import-probe"],
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout == IMPORT_PROBE_SENTINEL
    assert result.stderr == b""


def test_bundle_modules_use_only_stdlib_and_bundled_imports():
    bundled = {
        member[:-3].replace("/", ".")
        for member in BUNDLE_MEMBERS
        if member.endswith(".py")
    }
    bundled_roots = {name.split(".")[0] for name in bundled}
    for member in BUNDLE_MEMBERS:
        if not member.endswith(".py"):
            continue
        tree = ast.parse((BACKEND / member).read_text(encoding="utf-8"), member)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots = {node.module.split(".")[0]}
            else:
                continue
            assert roots <= sys.stdlib_module_names | bundled_roots | {"app"}


def test_failed_source_compile_does_not_replace_existing_bundle(tmp_path):
    source = tmp_path / "backend"
    for member in BUNDLE_MEMBERS:
        destination = source / member
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((BACKEND / member).read_bytes())
    target = tmp_path / "node.pyz"
    target.write_bytes(b"existing-bundle")
    broken = source / "app/services/vpn_node_entrypoint.py"
    broken.write_text("this is not valid Python !!!", encoding="utf-8")

    with pytest.raises(Exception):
        build_node_bundle(source, target)
    assert target.read_bytes() == b"existing-bundle"
    assert not list(tmp_path.glob(".node.pyz.*.tmp"))


def test_failed_import_probe_does_not_replace_existing_bundle(tmp_path):
    source = tmp_path / "backend"
    for member in BUNDLE_MEMBERS:
        destination = source / member
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((BACKEND / member).read_bytes())
    target = tmp_path / "node.pyz"
    target.write_bytes(b"existing-bundle")
    entrypoint = source / "app/services/vpn_node_entrypoint.py"
    entrypoint.write_text("import module_which_does_not_exist\n", encoding="utf-8")

    with pytest.raises(Exception):
        build_node_bundle(source, target)
    assert target.read_bytes() == b"existing-bundle"
    assert not list(tmp_path.glob(".node.pyz.*.tmp"))


def test_system_exit_during_import_does_not_replace_existing_bundle(tmp_path):
    source = tmp_path / "backend"
    for member in BUNDLE_MEMBERS:
        destination = source / member
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((BACKEND / member).read_bytes())
    target = tmp_path / "node.pyz"
    target.write_bytes(b"existing-bundle")
    endpoint_types = source / "app/services/vpn_endpoint_types.py"
    endpoint_types.write_text("raise SystemExit(2)\n", encoding="utf-8")

    with pytest.raises(Exception):
        build_node_bundle(source, target)
    assert target.read_bytes() == b"existing-bundle"
    assert not list(tmp_path.glob(".node.pyz.*.tmp"))


def test_missing_source_does_not_replace_existing_bundle(tmp_path):
    target = tmp_path / "node.pyz"
    target.write_bytes(b"existing-bundle")
    with pytest.raises(Exception):
        build_node_bundle(tmp_path / "missing", target)
    assert target.read_bytes() == b"existing-bundle"

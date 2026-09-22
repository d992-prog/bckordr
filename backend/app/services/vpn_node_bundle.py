"""Build the exact dependency-free node runner zipapp."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import zipfile


BUNDLE_MEMBERS = (
    "app/__init__.py",
    "app/services/__init__.py",
    "app/services/vpn_endpoint_types.py",
    "app/services/vpn_node_request.py",
    "app/services/vpn_node_journal.py",
    "app/services/vpn_xui_node_http.py",
    "app/services/vpn_xui_node_observation.py",
    "app/services/vpn_xui_identity.py",
    "app/services/vpn_xray_runtime.py",
    "app/services/vpn_xui_node_executor.py",
    "app/services/vpn_node_entrypoint.py",
)
IMPORT_PROBE_SENTINEL = b"veltrix-vpn-node-import-ok-v1\n"
_MAIN = (
    b"import sys\n"
    b"import app.services.vpn_endpoint_types\n"
    b"import app.services.vpn_node_request\n"
    b"import app.services.vpn_node_journal\n"
    b"import app.services.vpn_xui_node_http\n"
    b"import app.services.vpn_xui_node_observation\n"
    b"import app.services.vpn_xui_identity\n"
    b"import app.services.vpn_xray_runtime\n"
    b"import app.services.vpn_xui_node_executor\n"
    b"from app.services.vpn_node_entrypoint import main\n"
    b"if sys.argv[1:] == ['--import-probe']:\n"
    b"    sys.stdout.buffer.write(" + repr(IMPORT_PROBE_SENTINEL).encode("ascii") + b")\n"
    b"    raise SystemExit(0)\n"
    b"raise SystemExit(main())\n"
)
_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class NodeBundleError(ValueError):
    def __init__(self) -> None:
        super().__init__("vpn_node_bundle_failed")


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_node_bundle(source_root: Path, target: Path) -> str:
    if (
        not isinstance(source_root, Path)
        or not isinstance(target, Path)
        or not source_root.is_dir()
        or not target.is_absolute()
        or not target.parent.is_dir()
    ):
        raise NodeBundleError from None
    sources: list[tuple[str, bytes]] = []
    try:
        for member in BUNDLE_MEMBERS:
            raw = (source_root / member).read_bytes()
            compile(raw, member, "exec", dont_inherit=True)
            sources.append((member, raw))
    except (OSError, SyntaxError, ValueError, TypeError):
        raise NodeBundleError from None

    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(temporary, "x") as archive:
            archive.writestr(_entry("__main__.py"), _MAIN)
            for member, raw in sources:
                archive.writestr(_entry(member), raw)
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(temporary), "--import-probe"],
            input=b"",
            capture_output=True,
            timeout=10,
            check=False,
        )
        if (
            result.returncode != 0
            or result.stdout != IMPORT_PROBE_SENTINEL
            or result.stderr
        ):
            raise NodeBundleError from None
        raw_bundle = temporary.read_bytes()
        digest = hashlib.sha256(raw_bundle).hexdigest()
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return digest
    except (OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        raise NodeBundleError from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

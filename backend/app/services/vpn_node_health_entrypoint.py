"""Bounded read-only health entrypoint for the node bundle."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from app.services.vpn_endpoint_types import VpnEndpointError, VpnEndpointTarget
from app.services.vpn_node_entrypoint import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_INVALID_REQUEST,
    MAX_CONFIG_BYTES,
    MAX_TOKEN_BYTES,
    NODE_CONFIG_PATH,
    NODE_TOKEN_PATH,
    NodeEntrypointError,
    _bounded_read,
    _effective_uid,
    _node_config,
    _read_private_file,
    _validate_private_regular_file,
)
from app.services.vpn_node_health import (
    MAX_HEALTH_PAYLOAD_BYTES,
    VpnNodeHealthReceipt,
    parse_node_health_request,
    serialize_node_health_receipt,
)
from app.services.vpn_xui_node_http import NodePanelError, NodePanelSession
from app.services.vpn_xui_node_observation import (
    NodeEndpointObservation,
    observe_node_endpoint,
    xray_endpoint_is_listening,
)


def _receipt(observation: NodeEndpointObservation) -> VpnNodeHealthReceipt:
    if not (
        isinstance(observation, NodeEndpointObservation)
        and observation.state in {"matched", "not_observed"}
        and observation.transport in {"matched", "mismatch", "unsupported", None}
        and observation.runtime in {"running", "stop", "error"}
        and type(observation.listening) is bool
    ):
        raise TypeError from None
    if observation.runtime == "error":
        return VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_runtime_unavailable", "error"
        )
    runtime = "stopped" if observation.runtime == "stop" else "running"
    if runtime == "stopped":
        return VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_runtime_stopped", runtime
        )
    if observation.state == "not_observed":
        return VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_endpoint_missing", runtime
        )
    if observation.transport != "matched":
        return VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_endpoint_mismatch", runtime
        )
    if not observation.listening:
        return VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_listener_unavailable", runtime
        )
    return VpnNodeHealthReceipt("healthy", None, runtime)


def _failure_receipt(error: Exception) -> VpnNodeHealthReceipt:
    if isinstance(error, NodePanelError):
        return VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_panel_unavailable", None
        )
    if (
        isinstance(error, VpnEndpointError)
        and error.code == "vpn_xui_inventory_incomplete"
    ):
        return VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_panel_unavailable", None
        )
    if isinstance(error, VpnEndpointError) and error.code in {
        "vpn_xui_status_invalid",
        "vpn_xui_version_unsupported",
    }:
        return VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_runtime_unavailable", "error"
        )
    return VpnNodeHealthReceipt("unhealthy", "vpn_node_health_internal", None)


def run_node_health_entrypoint(
    stdin: BinaryIO,
    stdout: BinaryIO,
    _stderr: BinaryIO,
    *,
    config_path: Path = NODE_CONFIG_PATH,
    token_path: Path = NODE_TOKEN_PATH,
    effective_uid: Callable[[], int] = _effective_uid,
    listener_probe: Callable[[dict, VpnEndpointTarget], bool] = (
        xray_endpoint_is_listening
    ),
    observer: Callable[..., NodeEndpointObservation] = observe_node_endpoint,
) -> int:
    try:
        request = parse_node_health_request(
            _bounded_read(stdin, MAX_HEALTH_PAYLOAD_BYTES)
        )
    except BaseException as error:  # noqa: BLE001 - contain process interruption
        return (
            EXIT_INVALID_REQUEST if isinstance(error, Exception) else EXIT_INTERRUPTED
        )
    try:
        if effective_uid() != 0:
            raise NodeEntrypointError from None
        panel_url, database_path = _node_config(
            _read_private_file(config_path, limit=MAX_CONFIG_BYTES)
        )
        _validate_private_regular_file(database_path)
        token = _read_private_file(token_path, limit=MAX_TOKEN_BYTES).decode(
            "ascii", errors="strict"
        )
        panel_session = NodePanelSession(
            panel_url,
            username=None,
            password=None,
            api_token=token,
        )
    except BaseException as error:  # noqa: BLE001 - contain process interruption
        if not isinstance(error, Exception):
            return EXIT_INTERRUPTED
        receipt = VpnNodeHealthReceipt(
            "unhealthy", "vpn_node_health_internal", None
        )
    else:
        try:
            with panel_session as panel:
                receipt = _receipt(
                    observer(
                        panel,
                        target=request.target,
                        database_path=database_path,
                        listener_probe=listener_probe,
                    )
                )
        except BaseException as error:  # noqa: BLE001 - contain process interruption
            if not isinstance(error, Exception):
                return EXIT_INTERRUPTED
            receipt = _failure_receipt(error)

    try:
        encoded = serialize_node_health_receipt(receipt)
        if stdout.write(encoded) != len(encoded):
            raise NodeEntrypointError from None
        stdout.flush()
        return 0
    except BaseException as error:  # noqa: BLE001 - contain process interruption
        return EXIT_FAILURE if isinstance(error, Exception) else EXIT_INTERRUPTED


def main() -> int:
    return run_node_health_entrypoint(
        sys.stdin.buffer,
        sys.stdout.buffer,
        sys.stderr.buffer,
    )


if __name__ == "__main__":
    raise SystemExit(main())

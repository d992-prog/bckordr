from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.services.vpn_endpoint_types import VpnEndpointError, VpnEndpointTarget


@dataclass(frozen=True, slots=True)
class XuiClientObservation:
    state: Literal["matched", "not_observed"]
    record_id: int | None = None
    enabled: bool | None = None


def inspect_xui_client_identity(
    *,
    panel_version: str,
    target: VpnEndpointTarget,
    client_uuid: UUID,
    client_email: str,
    inbound_response: object,
    client_response: object,
) -> XuiClientObservation:
    """Compare identity observations in 3x-UI 3.8.5 list responses only.

    Neither observation authorizes mutation or proves full inventory coverage,
    transport correctness, authentication/authorization, runtime application,
    revocation, or readiness. Inbound lists are user-scoped, inventories may be
    partial, and the two reads are non-atomic; ``not_observed`` is not proof of
    absence or permission to create or revoke. Transport must be validated
    separately.
    """
    if panel_version != "3.8.5":
        raise VpnEndpointError("vpn_xui_version_unsupported") from None
    if not isinstance(client_uuid, UUID) or target.protocol != "vless":
        raise VpnEndpointError("vpn_xui_identity_invalid") from None
    target_id = _positive_int(target.inbound_id)
    expected_email = _email(client_email)
    inbound_rows = _inventory_rows(inbound_response)
    client_rows = _inventory_rows(client_response)

    inbound_matches = _matching_inbound_clients(
        inbound_rows, target_id, client_uuid, expected_email
    )
    global_matches = _matching_global_clients(client_rows, client_uuid, expected_email)
    if not inbound_matches and not global_matches:
        return XuiClientObservation("not_observed")
    if len(inbound_matches) != 1 or len(global_matches) != 1:
        raise VpnEndpointError("vpn_xui_identity_conflict") from None

    record_id, record_uuid, record_email, enabled, attachments = global_matches[0]
    if (
        record_uuid != client_uuid
        or record_email != expected_email
        or attachments != (target_id,)
        or inbound_matches[0] != (target_id, client_uuid, expected_email, enabled)
    ):
        raise VpnEndpointError("vpn_xui_identity_conflict") from None
    return XuiClientObservation("matched", record_id=record_id, enabled=enabled)


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    return value


def _email(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    try:
        return UUID(value)
    except ValueError:
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None


def _enabled(value: object) -> bool:
    if not isinstance(value, bool):
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    return value


def _inventory_rows(response: object) -> list[dict]:
    if (
        not isinstance(response, dict)
        or response.get("success") is not True
        or response.get("nodePending", False) is not False
    ):
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    rows = response.get("obj")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    return rows


def _matching_inbound_clients(
    rows: list[dict], target_id: int, client_uuid: UUID, client_email: str
) -> list[tuple[int, UUID, str, bool]]:
    seen_ids: set[int] = set()
    matches: list[tuple[int, UUID, str, bool]] = []
    target_row = None
    for row in rows:
        inbound_id = _positive_int(row.get("id"))
        protocol = row.get("protocol")
        if inbound_id in seen_ids or not isinstance(protocol, str) or not protocol:
            raise VpnEndpointError("vpn_xui_inventory_invalid") from None
        seen_ids.add(inbound_id)
        if inbound_id == target_id:
            target_row = row
        if protocol != "vless":
            continue
        settings = row.get("settings")
        if not isinstance(settings, dict) or not isinstance(
            settings.get("clients"), list
        ):
            raise VpnEndpointError("vpn_xui_inventory_invalid") from None
        for client in settings["clients"]:
            if not isinstance(client, dict):
                raise VpnEndpointError("vpn_xui_inventory_invalid") from None
            observed_uuid = _uuid(client.get("id"))
            observed_email = _email(client.get("email"))
            enabled = _enabled(client.get("enable"))
            if observed_uuid == client_uuid or observed_email == client_email:
                matches.append((inbound_id, observed_uuid, observed_email, enabled))
    if target_row is None:
        raise VpnEndpointError("vpn_xui_inbound_missing") from None
    if (
        target_row["protocol"] != "vless"
        or target_row.get("nodeId") is not None
        or target_row.get("fallbackParent") is not None
    ):
        raise VpnEndpointError("vpn_xui_inbound_unsupported") from None
    return matches


def _attachments(row: dict) -> tuple[int, ...]:
    if "inboundIds" not in row:
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    values = row["inboundIds"]
    if values is None:
        return ()
    if not isinstance(values, list):
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    attachments = tuple(_positive_int(value) for value in values)
    if len(attachments) != len(set(attachments)):
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None
    return attachments


def _matching_global_clients(
    rows: list[dict], client_uuid: UUID, client_email: str
) -> list[tuple[int, UUID | None, str, bool, tuple[int, ...]]]:
    seen_ids: set[int] = set()
    seen_emails: set[str] = set()
    seen_uuids: set[UUID] = set()
    matches: list[tuple[int, UUID | None, str, bool, tuple[int, ...]]] = []
    for row in rows:
        record_id = _positive_int(row.get("id"))
        observed_email = _email(row.get("email"))
        raw_uuid = row.get("uuid")
        observed_uuid = None if raw_uuid == "" else _uuid(raw_uuid)
        enabled = _enabled(row.get("enable"))
        attachments = _attachments(row)
        if (
            record_id in seen_ids
            or observed_email in seen_emails
            or (observed_uuid is not None and observed_uuid in seen_uuids)
        ):
            raise VpnEndpointError("vpn_xui_identity_conflict") from None
        seen_ids.add(record_id)
        seen_emails.add(observed_email)
        if observed_uuid is not None:
            seen_uuids.add(observed_uuid)
        if observed_uuid == client_uuid or observed_email == client_email:
            matches.append(
                (record_id, observed_uuid, observed_email, enabled, attachments)
            )
    return matches

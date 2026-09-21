from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, fields, replace
from traceback import format_exception
from typing import get_args, get_type_hints
from uuid import UUID

import pytest

from app.services.vpn_endpoints import VpnEndpointError, VpnEndpointTarget
from app.services.vpn_xui_identity import (
    XuiClientObservation,
    inspect_xui_client_identity,
)


CLIENT_UUID = UUID("11111111-2222-4333-8444-555555555555")
OTHER_UUID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
CLIENT_EMAIL = "synthetic-profile"
INVALID_IDS = [None, True, False, 0, -1, 2.0, "2"]
INVALID_EMAILS = [
    None,
    "",
    " leading",
    "trailing ",
    "inner space",
    "tab\there",
    "non\u00a0breaking",
    "nul\x00here",
]
SECRET = "synthetic_secret_must_not_escape"


@pytest.fixture
def inventory():
    return {
        "panel_version": "3.8.5",
        "target": VpnEndpointTarget(
            endpoint_id=2,
            worker_id=15,
            inbound_id=2,
            public_host="vpn.example.test",
            port=443,
            protocol="vless",
            transport="tcp",
            security="reality",
            server_name="example.test",
            public_key="synthetic-public-key",
            short_id="abcd",
            fingerprint="chrome",
            flow="xtls-rprx-vision",
        ),
        "client_uuid": CLIENT_UUID,
        "client_email": CLIENT_EMAIL,
        "inbound_response": {
            "success": True,
            "obj": [
                {
                    "id": 2,
                    "protocol": "vless",
                    "nodeId": None,
                    "settings": {
                        "clients": [
                            {
                                "id": str(CLIENT_UUID),
                                "email": CLIENT_EMAIL,
                                "enable": True,
                            }
                        ]
                    },
                }
            ],
        },
        "client_response": {
            "success": True,
            "obj": [
                {
                    "id": 19,
                    "uuid": str(CLIENT_UUID),
                    "email": CLIENT_EMAIL,
                    "enable": True,
                    "inboundIds": [2],
                }
            ],
        },
    }


def _target_row(inventory):
    return inventory["inbound_response"]["obj"][0]


def _inbound_client(inventory):
    return _target_row(inventory)["settings"]["clients"][0]


def _global_client(inventory):
    return inventory["client_response"]["obj"][0]


def _assert_error(inventory, code):
    original = deepcopy(inventory)
    with pytest.raises(VpnEndpointError) as exc_info:
        inspect_xui_client_identity(**inventory)
    assert exc_info.value.code == code
    assert str(exc_info.value) == code
    assert repr(exc_info.value) == f"VpnEndpointError('{code}')"
    assert inventory == original
    return exc_info.value


@pytest.mark.parametrize("enabled", [True, False])
def test_exact_identity_match_observes_enabled_or_disabled_without_mutation(
    inventory, enabled
):
    _inbound_client(inventory)["enable"] = enabled
    _global_client(inventory)["enable"] = enabled
    original = deepcopy(inventory)

    result = inspect_xui_client_identity(**inventory)

    assert result == XuiClientObservation("matched", record_id=19, enabled=enabled)
    assert inventory == original


def test_observation_is_frozen_and_contains_only_identity_summary(inventory):
    result = inspect_xui_client_identity(**inventory)

    assert [field.name for field in fields(result)] == ["state", "record_id", "enabled"]
    assert get_args(get_type_hints(XuiClientObservation)["state"]) == (
        "matched",
        "not_observed",
    )
    assert asdict(result) == {"state": "matched", "record_id": 19, "enabled": True}
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.enabled = False


@pytest.mark.parametrize(
    "wire_uuid", [str(OTHER_UUID).upper(), OTHER_UUID.hex, "{" + str(OTHER_UUID) + "}"]
)
def test_uuid_comparison_uses_parsed_identity(inventory, wire_uuid):
    inventory["client_uuid"] = OTHER_UUID
    _inbound_client(inventory)["id"] = wire_uuid
    _global_client(inventory)["uuid"] = str(OTHER_UUID)

    assert inspect_xui_client_identity(**inventory) == XuiClientObservation(
        "matched", 19, True
    )


def test_exact_inbound_selected_after_unrelated_rows_and_nonvless_records(inventory):
    inventory["inbound_response"]["obj"].insert(
        0,
        {
            "id": 999,
            "protocol": "vless",
            "settings": {
                "clients": [
                    {"id": str(OTHER_UUID), "email": "unrelated", "enable": False}
                ]
            },
        },
    )
    inventory["inbound_response"]["obj"].insert(
        0, {"id": 15, "protocol": "trojan", "settings": "not-vless-settings"}
    )
    inventory["client_response"]["obj"][:0] = [
        {
            "id": 1,
            "uuid": "",
            "email": "trojan-one",
            "enable": True,
            "inboundIds": None,
        },
        {
            "id": 2,
            "uuid": "",
            "email": "trojan-two",
            "enable": False,
            "inboundIds": [15],
        },
    ]

    assert inspect_xui_client_identity(**inventory) == XuiClientObservation(
        "matched", 19, True
    )


def test_unknown_fields_and_messages_cannot_escape_observation(inventory):
    for response in (inventory["inbound_response"], inventory["client_response"]):
        response.update(msg=SECRET, password=SECRET, nodePending=False)
    _target_row(inventory).update(privateKey=SECRET, password=SECRET)
    _inbound_client(inventory).update(password=SECRET, subscription=SECRET)
    _global_client(inventory).update(password=SECRET, subscription=SECRET)
    original = deepcopy(inventory)

    result = inspect_xui_client_identity(**inventory)

    assert result == XuiClientObservation("matched", 19, True)
    assert SECRET not in str(result)
    assert SECRET not in repr(result)
    assert inventory == original


def test_transport_security_ports_and_flow_are_not_verified_by_identity_observation(
    inventory,
):
    inventory["target"] = replace(
        inventory["target"],
        transport="ws",
        security="none",
        port=9443,
        flow="another-flow",
    )
    _target_row(inventory).update(
        port=1, streamSettings={"security": "tls", "network": "grpc"}
    )
    _inbound_client(inventory)["flow"] = "inbound-flow"
    _global_client(inventory)["flow"] = "global-flow"

    assert inspect_xui_client_identity(**inventory) == XuiClientObservation(
        "matched", 19, True
    )


@pytest.mark.parametrize("version", ["3.8.4", "3.8.5 ", "v3.8.5", None, 3.85])
def test_only_pinned_panel_version_is_supported(inventory, version):
    inventory["panel_version"] = version
    _assert_error(inventory, "vpn_xui_version_unsupported")


@pytest.mark.parametrize("uuid", [None, str(CLIENT_UUID), "", 1])
def test_expected_uuid_must_already_be_uuid_object(inventory, uuid):
    inventory["client_uuid"] = uuid
    _assert_error(inventory, "vpn_xui_identity_invalid")


@pytest.mark.parametrize("protocol", ["vmess", "VLESS", "", None])
def test_target_protocol_must_be_vless(inventory, protocol):
    inventory["target"] = replace(inventory["target"], protocol=protocol)
    _assert_error(inventory, "vpn_xui_identity_invalid")


@pytest.mark.parametrize("inbound_id", INVALID_IDS)
def test_recorded_inbound_id_must_be_positive_integer(inventory, inbound_id):
    inventory["target"] = replace(inventory["target"], inbound_id=inbound_id)
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("location", ["expected", "inbound", "global"])
@pytest.mark.parametrize("email", INVALID_EMAILS)
def test_every_email_is_validated(inventory, location, email):
    if location == "expected":
        inventory["client_email"] = email
    elif location == "inbound":
        _inbound_client(inventory)["email"] = email
    else:
        _global_client(inventory)["email"] = email
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("response_key", ["inbound_response", "client_response"])
@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        "response",
        {},
        {"success": True},
        {"success": False, "obj": []},
        {"success": 1, "obj": []},
        {"success": "true", "obj": []},
        {"success": True, "obj": None},
        {"success": True, "obj": {}},
        {"success": True, "obj": "[]"},
        {"success": True, "obj": [None]},
        {"success": True, "obj": [[]]},
        {"success": True, "obj": ["row"]},
        {"success": True, "obj": {"nodePending": True}},
    ],
)
def test_both_inventory_envelopes_must_be_explicit_successful_lists(
    inventory, response_key, response
):
    inventory[response_key] = response
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("response_key", ["inbound_response", "client_response"])
@pytest.mark.parametrize("pending", [True, None, 0, 1, "false"])
def test_node_pending_is_accepted_only_when_absent_or_false(
    inventory, response_key, pending
):
    inventory[response_key]["nodePending"] = pending
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("response_key", ["inbound_response", "client_response"])
@pytest.mark.parametrize("record_id", INVALID_IDS)
def test_all_inventory_record_ids_must_be_positive_integers(
    inventory, response_key, record_id
):
    inventory[response_key]["obj"][0]["id"] = record_id
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("response_key", ["inbound_response", "client_response"])
def test_slim_rows_without_required_fields_are_invalid(inventory, response_key):
    inventory[response_key]["obj"] = [{"id": 2}]
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("protocol", [None, "", 7])
def test_every_inbound_requires_protocol_string(inventory, protocol):
    inventory["inbound_response"]["obj"].append({"id": 3, "protocol": protocol})
    _assert_error(inventory, "vpn_xui_inventory_invalid")


def test_duplicate_inbound_ids_are_invalid_even_when_rows_match(inventory):
    inventory["inbound_response"]["obj"].append(deepcopy(_target_row(inventory)))
    _assert_error(inventory, "vpn_xui_inventory_invalid")


def test_missing_exact_target_is_not_an_absence_observation(inventory):
    _target_row(inventory)["id"] = 15
    _assert_error(inventory, "vpn_xui_inbound_missing")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("protocol", "trojan"),
        ("nodeId", 9),
        ("nodeId", 0),
        ("nodeId", False),
        ("fallbackParent", 3),
        ("fallbackParent", 0),
        ("fallbackParent", False),
    ],
)
def test_target_must_be_local_vless_without_fallback_parent(inventory, field, value):
    _target_row(inventory)[field] = value
    _assert_error(inventory, "vpn_xui_inbound_unsupported")


@pytest.mark.parametrize(
    "settings",
    [
        None,
        '{"clients": []}',
        [],
        {},
        {"clients": None},
        {"clients": {}},
        {"clients": "[]"},
    ],
)
@pytest.mark.parametrize("unrelated", [False, True])
def test_all_vless_inbounds_require_object_settings_and_client_lists(
    inventory, settings, unrelated
):
    row = _target_row(inventory)
    if unrelated:
        row = {"id": 3, "protocol": "vless"}
        inventory["inbound_response"]["obj"].append(row)
    row["settings"] = settings
    _assert_error(inventory, "vpn_xui_inventory_invalid")


def test_missing_vless_settings_are_invalid(inventory):
    del _target_row(inventory)["settings"]
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("client", [None, [], "client", {}, {"id": str(CLIENT_UUID)}])
def test_inbound_clients_must_be_complete_objects(inventory, client):
    _target_row(inventory)["settings"]["clients"] = [client]
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("location", ["inbound", "global"])
@pytest.mark.parametrize("uuid", [None, 7, "not-a-uuid", CLIENT_UUID])
def test_wire_uuid_must_be_a_valid_string(inventory, location, uuid):
    if location == "inbound":
        _inbound_client(inventory)["id"] = uuid
    else:
        _global_client(inventory)["uuid"] = uuid
    _assert_error(inventory, "vpn_xui_inventory_invalid")


def test_vless_inbound_client_cannot_have_empty_uuid(inventory):
    _inbound_client(inventory)["id"] = ""
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("field", ["uuid", "email", "enable", "inboundIds"])
def test_global_client_requires_all_identity_fields(inventory, field):
    del _global_client(inventory)[field]
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("location", ["inbound", "global"])
@pytest.mark.parametrize("enable", [None, 0, 1, "true"])
def test_client_enable_must_be_boolean(inventory, location, enable):
    client = (
        _inbound_client(inventory)
        if location == "inbound"
        else _global_client(inventory)
    )
    client["enable"] = enable
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize(
    "attachments",
    [2, "2", {}, [None], [True], [False], [0], [-1], [2.0], ["2"], [2, 2]],
)
def test_attachments_require_distinct_positive_integer_list(inventory, attachments):
    _global_client(inventory)["inboundIds"] = attachments
    _assert_error(inventory, "vpn_xui_inventory_invalid")


@pytest.mark.parametrize("attachments", [None, [], [3], [2, 3], [3, 2]])
def test_orphan_foreign_and_multiattached_identity_is_conflicting(
    inventory, attachments
):
    _global_client(inventory)["inboundIds"] = attachments
    _assert_error(inventory, "vpn_xui_identity_conflict")


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("inbound", "email", "stale-email"),
        ("global", "email", "stale-email"),
        ("inbound", "id", str(OTHER_UUID)),
        ("global", "uuid", str(OTHER_UUID)),
        ("global", "uuid", ""),
    ],
)
def test_matching_one_identity_field_does_not_hide_other_field_conflict(
    inventory, location, field, value
):
    client = (
        _inbound_client(inventory)
        if location == "inbound"
        else _global_client(inventory)
    )
    client[field] = value
    _assert_error(inventory, "vpn_xui_identity_conflict")


@pytest.mark.parametrize("location", ["inbound", "global"])
def test_uuid_and_email_split_between_distinct_records_are_ambiguous(
    inventory, location
):
    if location == "inbound":
        _inbound_client(inventory)["email"] = "other-email"
        _target_row(inventory)["settings"]["clients"].append(
            {"id": str(OTHER_UUID), "email": CLIENT_EMAIL, "enable": True}
        )
    else:
        _global_client(inventory)["email"] = "other-email"
        inventory["client_response"]["obj"].append(
            {
                "id": 20,
                "uuid": str(OTHER_UUID),
                "email": CLIENT_EMAIL,
                "enable": True,
                "inboundIds": [2],
            }
        )
    _assert_error(inventory, "vpn_xui_identity_conflict")


@pytest.mark.parametrize("field", ["id", "uuid", "email"])
@pytest.mark.parametrize("unrelated", [False, True])
def test_duplicate_global_identity_fields_are_conflicts_everywhere(
    inventory, field, unrelated
):
    first = _global_client(inventory)
    if unrelated:
        first = {
            "id": 20,
            "uuid": str(OTHER_UUID),
            "email": "unrelated",
            "enable": True,
            "inboundIds": [],
        }
        inventory["client_response"]["obj"].append(first)
    second = {
        "id": 21,
        "uuid": "99999999-8888-4777-8666-555555555555",
        "email": "different",
        "enable": True,
        "inboundIds": [],
    }
    second[field] = first[field]
    if field == "uuid":
        second[field] = "{" + first[field].upper() + "}"
    inventory["client_response"]["obj"].append(second)
    _assert_error(inventory, "vpn_xui_identity_conflict")


@pytest.mark.parametrize("same_inbound", [True, False])
def test_repeated_inbound_identity_is_conflicting(inventory, same_inbound):
    duplicate = deepcopy(_inbound_client(inventory))
    if same_inbound:
        _target_row(inventory)["settings"]["clients"].append(duplicate)
    else:
        inventory["inbound_response"]["obj"].append(
            {"id": 3, "protocol": "vless", "settings": {"clients": [duplicate]}}
        )
    _assert_error(inventory, "vpn_xui_identity_conflict")


def test_sole_matching_inbound_must_be_exact_target(inventory):
    client = _target_row(inventory)["settings"]["clients"].pop()
    inventory["inbound_response"]["obj"].append(
        {"id": 3, "protocol": "vless", "settings": {"clients": [client]}}
    )
    _assert_error(inventory, "vpn_xui_identity_conflict")


@pytest.mark.parametrize("missing_from", ["inbound", "global"])
def test_identity_observed_in_only_one_inventory_is_conflicting(
    inventory, missing_from
):
    if missing_from == "inbound":
        _target_row(inventory)["settings"]["clients"] = []
    else:
        inventory["client_response"]["obj"] = []
    _assert_error(inventory, "vpn_xui_identity_conflict")


@pytest.mark.parametrize("location", ["inbound", "global"])
def test_enable_state_must_agree(inventory, location):
    client = (
        _inbound_client(inventory)
        if location == "inbound"
        else _global_client(inventory)
    )
    client["enable"] = False
    _assert_error(inventory, "vpn_xui_identity_conflict")


def test_no_identity_observed_is_only_not_observed(inventory):
    _target_row(inventory)["settings"]["clients"] = []
    inventory["client_response"]["obj"] = []
    original = deepcopy(inventory)

    assert inspect_xui_client_identity(**inventory) == XuiClientObservation(
        "not_observed"
    )
    assert inventory == original


def test_unrelated_stale_pair_does_not_count_as_expected_identity(inventory):
    _inbound_client(inventory).update(
        id=str(OTHER_UUID), email="stale-unrelated", enable=False
    )
    _global_client(inventory).update(
        uuid=str(OTHER_UUID), email="current-unrelated", inboundIds=[999]
    )

    assert inspect_xui_client_identity(**inventory) == XuiClientObservation(
        "not_observed"
    )


@pytest.mark.parametrize(
    ("location", "field", "value", "code"),
    [
        (
            "inbound",
            "id",
            SECRET.ljust(32, "x"),
            "vpn_xui_inventory_invalid",
        ),
        (
            "global",
            "uuid",
            SECRET.ljust(32, "x"),
            "vpn_xui_inventory_invalid",
        ),
        ("global", "email", SECRET, "vpn_xui_identity_conflict"),
        ("inbound", "email", " " + SECRET, "vpn_xui_inventory_invalid"),
    ],
)
def test_secret_sentinels_never_escape_errors_or_formatted_exception_chains(
    inventory, location, field, value, code
):
    inventory["inbound_response"]["msg"] = SECRET
    inventory["client_response"]["password"] = SECRET
    client = (
        _inbound_client(inventory)
        if location == "inbound"
        else _global_client(inventory)
    )
    client[field] = value

    error = _assert_error(inventory, code)

    assert SECRET not in str(error)
    assert SECRET not in repr(error)
    assert SECRET not in "".join(format_exception(error))
    assert error.__cause__ is None
    if field in ("id", "uuid"):
        assert error.__suppress_context__ is True
        assert isinstance(error.__context__, ValueError)
        assert SECRET in str(error.__context__)


def test_documentation_limits_both_observations(inventory):
    inspect_xui_client_identity(**inventory)
    docstring = (inspect_xui_client_identity.__doc__ or "").lower()

    for term in (
        "neither",
        "mutation",
        "full inventory",
        "transport",
        "auth",
        "runtime",
        "revocation",
        "readiness",
        "user-scoped",
        "non-atomic",
        "not_observed",
        "absen",
        "create",
        "revoke",
    ):
        assert term in docstring

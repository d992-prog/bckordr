from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
from copy import deepcopy
import importlib
import io
import json
from pathlib import Path
from uuid import UUID

import pytest


PUBLIC_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
PRIVATE_KEY = "Hx4dHBsaGRgXFhUUExIREA8ODQwLCgkIBwYFBAMCAQA"
CLIENT_UUID = "11111111-2222-4333-8444-555555555555"
CLIENT_EMAIL = "acceptance-20260923@example.test"


def _module():
    return importlib.import_module("app.services.vpn_reality_endpoint_installer")


def _request(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "version": 1,
        "action": "ensure",
        "worker_id": 15,
        "public_host": "vpn.example.test",
        "server_name": "front.example.test",
        "short_id": "0123456789abcdef",
    }
    value.update(overrides)
    return value


def test_request_contract_is_frozen_and_policy_is_not_caller_controlled() -> None:
    module = _module()

    request = module.parse_install_request(_request())

    assert asdict(request) == {
        "action": "ensure",
        "worker_id": 15,
        "public_host": "vpn.example.test",
        "server_name": "front.example.test",
        "short_id": "0123456789abcdef",
        "inbound_id": None,
        "receipt_digest": None,
        "acceptance_uuid": None,
        "acceptance_email": None,
    }
    assert request.__class__.__dataclass_params__.frozen is True
    assert "__slots__" in request.__class__.__dict__
    with pytest.raises(FrozenInstanceError):
        request.worker_id = 1
    for forbidden in ("port", "protocol", "transport", "security", "fingerprint", "flow"):
        assert not hasattr(request, forbidden)


@pytest.mark.parametrize(
    "value",
    [
        _request(version=2),
        _request(extra=True),
        _request(worker_id=True),
        _request(worker_id=0),
        _request(worker_id=14),
        _request(public_host="https://vpn.example.test"),
        _request(public_host="vpn.example.test/path"),
        _request(public_host="vpn.example.test."),
        _request(server_name=" front.example.test"),
        _request(short_id="ABCDEF"),
        _request(short_id="abc"),
        _request(action="remove"),
        _request(action="ensure", inbound_id=7),
    ],
)
def test_request_parser_rejects_noncanonical_or_action_inconsistent_values(value) -> None:
    module = _module()

    with pytest.raises(module.EndpointInstallError, match="^vpn_endpoint_install_request_invalid$"):
        module.parse_install_request(value)


def test_remove_requires_exact_inbound_and_receipt_digest() -> None:
    module = _module()
    value = _request(
        action="remove",
        inbound_id=27,
        receipt_digest="a" * 64,
    )

    request = module.parse_install_request(value)

    assert request.action == "remove"
    assert request.inbound_id == 27
    assert request.receipt_digest == "a" * 64


def test_request_encoding_is_canonical_and_round_trips_exactly() -> None:
    module = _module()
    request = module.parse_install_request(_request(action="inspect"))

    encoded = module.encode_install_request(request)

    assert encoded == (
        b'{"version":1,"action":"inspect","worker_id":15,'
        b'"public_host":"vpn.example.test","server_name":"front.example.test",'
        b'"short_id":"0123456789abcdef"}\n'
    )
    assert module.parse_install_request(module._json_object(encoded)) == request


@pytest.mark.parametrize("action", ["add_acceptance_client", "remove_acceptance_client"])
def test_acceptance_client_actions_require_exact_private_input_but_do_not_make_it_policy(action) -> None:
    module = _module()
    request = module.parse_install_request(
        _request(
            action=action,
            inbound_id=27,
            receipt_digest="a" * 64,
            acceptance_uuid=CLIENT_UUID,
            acceptance_email=CLIENT_EMAIL,
        )
    )

    assert request.acceptance_uuid == UUID(CLIENT_UUID)
    assert request.acceptance_email == CLIENT_EMAIL


@pytest.mark.parametrize(
    "raw",
    [
        b'{"version":1,"version":1}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'[]',
        b'null',
        b'{',
        b'{"value":"\xff"}',
    ],
)
def test_strict_json_rejects_duplicates_constants_and_nonobjects(raw: bytes) -> None:
    module = _module()

    with pytest.raises(module.EndpointInstallError, match="^vpn_endpoint_install_request_invalid$"):
        module._json_object(raw)


def test_public_receipt_is_canonical_bounded_and_self_authenticating() -> None:
    module = _module()

    receipt = module.make_endpoint_receipt(
        state="staged",
        worker_id=15,
        inbound_id=27,
        public_host="vpn.example.test",
        server_name="front.example.test",
        public_key=PUBLIC_KEY,
        short_id="0123456789abcdef",
    )
    encoded = module.encode_install_receipt(receipt)
    decoded = json.loads(encoded)

    assert decoded == asdict(receipt)
    assert decoded["port"] == 443
    assert decoded["protocol"] == "vless"
    assert decoded["transport"] == "raw"
    assert decoded["security"] == "reality"
    assert decoded["fingerprint"] == "chrome"
    assert decoded["flow"] == "xtls-rprx-vision"
    assert len(decoded["receipt_digest"]) == 64
    assert len(encoded) <= module.MAX_RECEIPT_BYTES
    assert encoded.endswith(b"\n")
    assert module.parse_install_receipt(decoded) == receipt
    with pytest.raises(FrozenInstanceError):
        receipt.public_key = "changed"


def test_entrypoint_writes_nothing_for_invalid_input() -> None:
    module = _module()
    stdout = io.BytesIO()
    stderr = io.BytesIO()

    result = module.run_endpoint_installer(io.BytesIO(b"{}"), stdout, stderr)

    assert result == module.EXIT_INVALID_REQUEST
    assert stdout.getvalue() == b""
    assert stderr.getvalue() == b""


class FakePanel:
    def __init__(self, rows: list[dict] | None = None, clients: list[dict] | None = None):
        self.rows = deepcopy(rows or [])
        self.clients = deepcopy(clients or [])
        self.events: list[tuple[str, str, dict | None, bool]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def request(self, method, route, *, body=None, mutation=False):
        self.events.append((method, route, deepcopy(body), mutation))
        if route == "panel/api/server/status":
            return {"success": True, "obj": {"panelVersion": "3.8.5", "xray": {"state": "running", "errorMsg": ""}}}
        if route == "panel/api/inbounds/list":
            return {"success": True, "obj": deepcopy(self.rows)}
        if route == "panel/api/clients/list":
            return {"success": True, "obj": deepcopy(self.clients)}
        if route == "panel/api/inbounds/add":
            row = deepcopy(body)
            row["id"] = 27
            self.rows.append(row)
            return {"success": True, "obj": deepcopy(row)}
        if route == "panel/api/inbounds/del/27":
            self.rows = [row for row in self.rows if row["id"] != 27]
            return {"success": True, "obj": 27}
        if route == "panel/api/clients/add":
            client = deepcopy(body["client"])
            self.rows[0]["settings"]["clients"].append(client)
            self.clients.append({
                "id": 91,
                "uuid": client["id"],
                "email": client["email"],
                "enable": True,
                "inboundIds": [27],
            })
            return {"success": True, "obj": None}
        if route == "panel/api/clients/del/acceptance-20260923%40example.test":
            self.rows[0]["settings"]["clients"] = []
            self.clients = []
            return {"success": True, "obj": None}
        raise AssertionError((method, route, body, mutation))


def _exact_row(module, *, clients: list[dict] | None = None) -> dict:
    return module._inbound_payload(
        server_name="front.example.test",
        short_id="0123456789abcdef",
        private_key=PRIVATE_KEY,
        public_key=PUBLIC_KEY,
    ) | {"id": 27, "settings": {"clients": clients or [], "decryption": "none", "fallbacks": []}}


def _mock_complete_inventory(module, monkeypatch, panel: FakePanel) -> None:
    monkeypatch.setattr(module, "_local_inbound_ids", lambda _path: tuple(sorted(row["id"] for row in panel.rows)))


def test_ensure_creates_exact_empty_reality_inbound_and_never_exports_private_key(monkeypatch) -> None:
    module = _module()
    panel = FakePanel()
    _mock_complete_inventory(module, monkeypatch, panel)

    receipt = module.execute_endpoint_action(
        module.parse_install_request(_request()),
        database_path=Path("C:/synthetic/panel.db"),
        panel_factory=lambda: panel,
        key_generator=lambda: (PRIVATE_KEY, PUBLIC_KEY),
    )

    assert receipt.state == "staged"
    assert receipt.inbound_id == 27
    assert PRIVATE_KEY not in repr(receipt)
    assert PRIVATE_KEY not in module.encode_install_receipt(receipt).decode()
    mutation = next(event for event in panel.events if event[1] == "panel/api/inbounds/add")
    assert mutation[0::3] == ("POST", True)
    assert mutation[2] == module._inbound_payload(
        server_name="front.example.test",
        short_id="0123456789abcdef",
        private_key=PRIVATE_KEY,
        public_key=PUBLIC_KEY,
    )
    assert mutation[2]["port"] == 443
    assert mutation[2]["settings"]["clients"] == []
    assert mutation[2]["streamSettings"]["network"] == "raw"
    assert mutation[2]["streamSettings"]["security"] == "reality"
    assert mutation[2]["streamSettings"]["realitySettings"]["target"] == "front.example.test:443"


def test_ensure_is_idempotent_only_for_exact_empty_endpoint(monkeypatch) -> None:
    module = _module()
    panel = FakePanel([_exact_row(module)])
    _mock_complete_inventory(module, monkeypatch, panel)

    receipt = module.execute_endpoint_action(
        module.parse_install_request(_request()),
        database_path=Path("C:/synthetic/panel.db"),
        panel_factory=lambda: panel,
        key_generator=lambda: pytest.fail("idempotent inspection generated a key"),
    )

    assert receipt.state == "already_present"
    assert all(not mutation for _, _, _, mutation in panel.events)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("trafficReset",), "monthly"),
        (("streamSettings", "realitySettings", "maxTimediff"), 5000),
        (("sniffing", "enabled"), False),
        (("shareAddrStrategy",), "all"),
    ],
)
def test_idempotent_inspection_rejects_security_relevant_drift(
    monkeypatch, path, value
) -> None:
    module = _module()
    conflicting = _exact_row(module)
    target = conflicting
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    panel = FakePanel([conflicting])
    _mock_complete_inventory(module, monkeypatch, panel)

    with pytest.raises(module.EndpointInstallError, match="^vpn_endpoint_install_conflict$"):
        module.execute_endpoint_action(
            module.parse_install_request(_request()),
            database_path=Path("C:/synthetic/panel.db"),
            panel_factory=lambda: panel,
            key_generator=lambda: pytest.fail("drift generated a key"),
        )

    assert all(not mutation for _, _, _, mutation in panel.events)


def test_port_443_conflict_fails_before_key_generation_or_mutation(monkeypatch) -> None:
    module = _module()
    conflicting = _exact_row(module)
    conflicting["protocol"] = "trojan"
    panel = FakePanel([conflicting])
    _mock_complete_inventory(module, monkeypatch, panel)

    with pytest.raises(module.EndpointInstallError, match="^vpn_endpoint_install_conflict$"):
        module.execute_endpoint_action(
            module.parse_install_request(_request()),
            database_path=Path("C:/synthetic/panel.db"),
            panel_factory=lambda: panel,
            key_generator=lambda: pytest.fail("conflict generated a key"),
        )

    assert all(not mutation for _, _, _, mutation in panel.events)


def test_guarded_inverse_refuses_any_client_and_preserves_inbound(monkeypatch) -> None:
    module = _module()
    embedded = {"id": CLIENT_UUID, "email": CLIENT_EMAIL, "enable": True, "flow": "xtls-rprx-vision"}
    panel = FakePanel([_exact_row(module, clients=[embedded])], [{"id": 91, "uuid": CLIENT_UUID, "email": CLIENT_EMAIL, "enable": True, "inboundIds": [27]}])
    _mock_complete_inventory(module, monkeypatch, panel)
    observed = module.make_endpoint_receipt(
        state="observed", worker_id=15, inbound_id=27, public_host="vpn.example.test",
        server_name="front.example.test", public_key=PUBLIC_KEY, short_id="0123456789abcdef",
    )
    request = module.parse_install_request(_request(action="remove", inbound_id=27, receipt_digest=observed.receipt_digest))

    with pytest.raises(module.EndpointInstallError, match="^vpn_endpoint_install_clients_present$"):
        module.execute_endpoint_action(request, database_path=Path("C:/synthetic/panel.db"), panel_factory=lambda: panel)

    assert any(row["id"] == 27 for row in panel.rows)
    assert all(route != "panel/api/inbounds/del/27" for _, route, _, _ in panel.events)


def test_guarded_inverse_deletes_only_exact_empty_443_and_preserves_owner_8443(monkeypatch) -> None:
    module = _module()
    owner = {"id": 1, "port": 8443, "protocol": "vless"}
    panel = FakePanel([owner, _exact_row(module)])
    _mock_complete_inventory(module, monkeypatch, panel)
    observed = module.make_endpoint_receipt(
        state="observed", worker_id=15, inbound_id=27, public_host="vpn.example.test",
        server_name="front.example.test", public_key=PUBLIC_KEY, short_id="0123456789abcdef",
    )

    removed = module.execute_endpoint_action(
        module.parse_install_request(
            _request(action="remove", inbound_id=27, receipt_digest=observed.receipt_digest)
        ),
        database_path=Path("C:/synthetic/panel.db"),
        panel_factory=lambda: panel,
    )

    assert removed.state == "removed"
    assert [row["id"] for row in panel.rows] == [1]
    assert [(method, route) for method, route, _, mutation in panel.events if mutation] == [
        ("POST", "panel/api/inbounds/del/27")
    ]


def test_disposable_acceptance_client_add_and_exact_cleanup_never_echo_identity(monkeypatch) -> None:
    module = _module()
    panel = FakePanel([_exact_row(module)])
    _mock_complete_inventory(module, monkeypatch, panel)
    endpoint = module.make_endpoint_receipt(
        state="observed", worker_id=15, inbound_id=27, public_host="vpn.example.test",
        server_name="front.example.test", public_key=PUBLIC_KEY, short_id="0123456789abcdef",
    )
    common = {
        "inbound_id": 27,
        "receipt_digest": endpoint.receipt_digest,
        "acceptance_uuid": CLIENT_UUID,
        "acceptance_email": CLIENT_EMAIL,
    }

    added = module.execute_endpoint_action(
        module.parse_install_request(_request(action="add_acceptance_client", **common)),
        database_path=Path("C:/synthetic/panel.db"), panel_factory=lambda: panel,
    )
    removed = module.execute_endpoint_action(
        module.parse_install_request(_request(action="remove_acceptance_client", **common)),
        database_path=Path("C:/synthetic/panel.db"), panel_factory=lambda: panel,
    )

    assert added.state == "acceptance_client_present"
    assert removed.state == "acceptance_client_removed"
    assert added.receipt_digest == removed.receipt_digest == endpoint.receipt_digest
    public_output = module.encode_install_receipt(added) + module.encode_install_receipt(removed)
    assert CLIENT_UUID.encode() not in public_output
    assert CLIENT_EMAIL.encode() not in public_output
    add_body = next(body for _, route, body, _ in panel.events if route == "panel/api/clients/add")
    assert add_body == {
        "client": module._acceptance_client_payload(
            module.parse_install_request(_request(action="add_acceptance_client", **common))
        ),
        "inboundIds": [27],
    }
    assert add_body["client"]["subId"] == ""
    assert add_body["client"]["trafficReset"] == "never"
    assert add_body["client"]["trafficResetDay"] == 1
    assert panel.rows[0]["settings"]["clients"] == []
    assert panel.clients == []


def test_pinned_xray_key_output_is_parsed_exactly_without_repr_leak() -> None:
    module = _module()
    output = (
        f"PrivateKey: {PUBLIC_KEY}\n"
        f"Password (PublicKey): {PUBLIC_KEY}\n"
        f"Hash32: {PUBLIC_KEY}\n"
    ).encode()

    private_key, public_key = module._parse_x25519_output(output)

    assert (private_key, public_key) == (PUBLIC_KEY, PUBLIC_KEY)
    with pytest.raises(module.EndpointInstallError, match="^vpn_endpoint_install_keygen_failed$") as caught:
        module._parse_x25519_output(output + b"extra\n")
    assert PUBLIC_KEY not in repr(caught.value)


def test_admin_panel_adapter_is_token_only_and_allows_only_exact_one_shot_routes(monkeypatch) -> None:
    module = _module()
    calls = []

    def exchange(self, method, route, **kwargs):
        calls.append((method, route, kwargs))
        return {"success": True, "obj": {"panelVersion": "3.8.5", "xray": {"state": "running", "errorMsg": ""}}}

    monkeypatch.setattr(module.EndpointAdminPanelSession, "_exchange", exchange)
    with module.EndpointAdminPanelSession("http://panel.example/base/", api_token="test-token") as panel:
        panel.request("POST", "panel/api/inbounds/add", body={"fixed": True}, mutation=True)
        panel.request("POST", "panel/api/inbounds/del/27", body={}, mutation=True)
        panel.request("POST", "panel/api/clients/del/acceptance%40example.test", body={}, mutation=True)
        with pytest.raises(module.NodePanelError, match="^vpn_xui_request_invalid$"):
            panel.request("POST", "panel/api/inbounds/update/27", body={}, mutation=True)

    assert [item[:2] for item in calls] == [
        ("GET", "/base/panel/api/server/status"),
        ("POST", "/base/panel/api/inbounds/add"),
        ("POST", "/base/panel/api/inbounds/del/27"),
        ("POST", "/base/panel/api/clients/del/acceptance%40example.test"),
    ]
    assert all(call[2]["api_token"] == "test-token" for call in calls)
    with pytest.raises(TypeError):
        module.EndpointAdminPanelSession("http://panel.example/", username="admin", password="secret")


def test_temporary_installer_is_not_in_permanent_dispatcher_bundle() -> None:
    from app.services.vpn_node_bundle import BUNDLE_MEMBERS

    assert "app/services/vpn_reality_endpoint_installer.py" not in BUNDLE_MEMBERS


def test_default_node_execution_requires_root_and_constructs_token_only_panel(monkeypatch) -> None:
    module = _module()
    request = module.parse_install_request(_request(action="inspect"))
    with pytest.raises(module.EndpointInstallError, match="^vpn_endpoint_install_privilege_required$"):
        module._execute_node_request(request, effective_uid=lambda: 1000)

    seen = {}

    class Panel(FakePanel):
        def __init__(self, panel_url, *, api_token):
            super().__init__([_exact_row(module)])
            seen.update(panel_url=panel_url, api_token=api_token)

    monkeypatch.setattr(module, "_read_private_file", lambda path, *, limit: b"token-only" if path == module.NODE_TOKEN_PATH else b"{}")
    monkeypatch.setattr(module, "_node_config", lambda _raw: ("http://panel.example/base/", Path("C:/synthetic/panel.db")))
    monkeypatch.setattr(module, "_validate_private_regular_file", lambda _path: None)
    monkeypatch.setattr(module, "EndpointAdminPanelSession", Panel)
    monkeypatch.setattr(module, "_local_inbound_ids", lambda _path: (27,))

    receipt = module._execute_node_request(request, effective_uid=lambda: 0)

    assert receipt.state == "observed"
    assert seen == {"panel_url": "http://panel.example/base/", "api_token": "token-only"}

from __future__ import annotations

import dataclasses
import importlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import traceback
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest


CLIENT = UUID("12345678-1234-4234-9234-123456789abc")
ALIAS = UUID("12345678-1234-ffff-9234-123456789abc")
EMAIL = "customer-1@example.test"
FLOW = "xtls-rprx-vision"


@pytest.fixture
def runtime():
    return importlib.import_module("app.services.vpn_xray_runtime")


def inputs(**changes):
    return dict(
        port=443, client_uuid=CLIENT, client_email=EMAIL, flow=FLOW,
        executable_path=Path.cwd() / "xray", config_path=Path.cwd() / "config.json",
    ) | changes


def client(**changes):
    return dict(id=str(CLIENT), email=EMAIL, flow=FLOW) | changes


def account(**changes):
    return {
        "email": EMAIL,
        "account": {
            "_TypedMessage_": "xray.proxy.vless.Account",
            "id": str(CLIENT),
            "flow": FLOW,
        },
    } | changes


def config():
    return {
        "api": {"tag": "api", "services": ["HandlerService"]},
        "inbounds": [
            {"tag": "api", "listen": "127.0.0.1", "port": 62789},
            {"tag": "public", "protocol": "vless", "port": 443,
             "settings": {"clients": [client()]}},
            {"tag": "other", "protocol": "vless", "port": 444,
             "settings": {"clients": []}},
        ],
    }


def test_observation_has_only_nonsecret_fields(runtime):
    observed = runtime.XrayRuntimeObservation("matched", 123, 456)
    assert dataclasses.asdict(observed) == {
        "state": "matched", "process_id": 123, "process_start_ticks": 456,
    }
    assert not hasattr(observed, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        observed.state = "not_observed"


@pytest.mark.parametrize("changes", [
    {"port": True}, {"port": 0}, {"port": 65536}, {"port": "443"},
    {"client_uuid": str(CLIENT)}, {"client_email": ""},
    {"client_email": "x" * 65}, {"client_email": "hello world"},
    {"client_email": "x\n"}, {"client_email": "x/y"},
    {"client_email": "x?y"}, {"client_email": "x#y"},
    {"client_email": "юзер"}, {"flow": None}, {"flow": "other"},
    {"timeout_seconds": True}, {"timeout_seconds": 0},
    {"timeout_seconds": 31}, {"timeout_seconds": float("nan")},
    {"timeout_seconds": float("inf")}, {"timeout_seconds": "8"},
    {"executable_path": Path("relative")}, {"config_path": Path("relative")},
    {"executable_path": "/usr/bin/xray"},
])
def test_invalid_input_precedes_all_io(runtime, monkeypatch, changes):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid input reached IO")
    monkeypatch.setattr(runtime, "_observe", forbidden)
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        runtime.observe_xray_client(**inputs(**changes))
    assert caught.value.code == "vpn_xray_runtime_invalid"


def test_public_errors_remove_sensitive_exception_context(runtime, monkeypatch):
    secret = f"/private/{CLIENT}/{EMAIL}"
    def fail(*args, **kwargs):
        raise OSError(secret)
    monkeypatch.setattr(runtime, "_observe", fail)
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        runtime.observe_xray_client(**inputs())
    exc = caught.value
    assert str(exc) == "vpn_xray_runtime_unavailable"
    assert secret not in repr(exc)
    assert secret not in "".join(traceback.format_exception(exc))
    assert exc.__cause__ is None and exc.__context__ is None


@pytest.mark.parametrize("invalid", [False, True])
def test_public_errors_clear_callers_active_exception(runtime, monkeypatch, invalid):
    secret = f"/private/{CLIENT}/{EMAIL}"
    def fail(*args, **kwargs):
        raise OSError(secret)
    monkeypatch.setattr(runtime, "_observe", fail)
    try:
        raise OSError(secret)
    except OSError:
        with pytest.raises(runtime.XrayRuntimeError) as caught:
            runtime.observe_xray_client(**inputs(port=0 if invalid else 443))
    exc = caught.value
    assert exc.code == ("vpn_xray_runtime_invalid" if invalid else "vpn_xray_runtime_unavailable")
    assert exc.__cause__ is None and exc.__context__ is None
    assert secret not in repr(exc)
    assert secret not in "".join(traceback.format_exception(exc))


@pytest.mark.parametrize("raw", [
    b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
    b'{"x":-Infinity}', b'{"x":1e999}', b'{"x":-1e999}',
    b'{"x":"\xff"}', b'[]', b'null', b'{',
])
def test_strict_json_rejects_untrusted_shapes(runtime, raw):
    with pytest.raises((runtime.XrayRuntimeError, ValueError)):
        runtime._json_object(raw)


def test_exact_config_and_typed_runtime_shape(runtime):
    api_port, tags, target = runtime._configuration(config(), 443, CLIENT, EMAIL, FLOW)
    assert (api_port, tags, target) == (62789, ["public", "other"], "public")
    assert runtime._runtime_users(
        {"users": [account()]}, "public", "public", CLIENT, EMAIL, FLOW
    )


@pytest.mark.parametrize("payload", [{}, {"users": []}])
def test_empty_marshaled_users_are_not_observed(runtime, payload):
    assert not runtime._runtime_users(payload, "public", "public", CLIENT, EMAIL, FLOW)


@pytest.mark.parametrize("row,tag", [
    (account(email=EMAIL.upper()), "public"),
    (account(account=account()["account"] | {"id": str(ALIAS)}), "public"),
    (account(account=account()["account"] | {"flow": ""}), "public"),
    (account(), "other"),
    (account(email="different"), "public"),
])
def test_runtime_aliases_wrong_flow_and_other_inbound_conflict(runtime, row, tag):
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        runtime._runtime_users({"users": [row]}, tag, "public", CLIENT, EMAIL, FLOW)
    assert caught.value.code == "vpn_xray_runtime_conflict"


@pytest.mark.parametrize("row", [
    {}, account(email=""), account(email="bad/email"), account(account={}),
    account(account=account()["account"] | {"_TypedMessage_": "other.Account"}),
    account(account=account()["account"] | {"id": "invalid"}),
    account(account=account()["account"] | {"flow": None}),
])
def test_runtime_rejects_anonymous_and_unsupported_accounts(runtime, row):
    with pytest.raises((runtime.XrayRuntimeError, ValueError)):
        runtime._runtime_users({"users": [row]}, "public", "public", CLIENT, EMAIL, FLOW)


def test_runtime_default_empty_flow(runtime):
    row = account()
    del row["account"]["flow"]
    assert runtime._runtime_users({"users": [row]}, "public", "public", CLIENT, EMAIL, "")


def test_unrecognized_response_is_not_empty_inventory(runtime):
    with pytest.raises(runtime.XrayRuntimeError):
        runtime._runtime_users({"error": "unavailable"}, "public", "public", CLIENT, EMAIL, FLOW)


@pytest.mark.parametrize("typed", [False, True])
def test_unrelated_unsupported_flow_fails_closed(runtime, typed):
    row = client(id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", email="other", flow="unsupported")
    if typed:
        row = account(email=row["email"], account={
            "_TypedMessage_": "xray.proxy.vless.Account", "id": row["id"], "flow": row["flow"]
        })
    with pytest.raises(runtime.XrayRuntimeError):
        runtime._clients([row], "other", "public", CLIENT, EMAIL, FLOW, typed=typed)


@pytest.mark.parametrize("alias", [client(id=str(ALIAS)), client(email=EMAIL.upper())])
def test_configuration_normalized_alias_conflicts(runtime, alias):
    value = config()
    value["inbounds"][2]["settings"]["clients"] = [alias]
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        runtime._configuration(value, 443, CLIENT, EMAIL, FLOW)
    assert caught.value.code == "vpn_xray_runtime_conflict"


@pytest.mark.parametrize("change", [
    lambda c: c["api"].update(services=[]),
    lambda c: c["inbounds"][0].update(listen="0.0.0.0"),
    lambda c: c["inbounds"][0].update(port=True),
    lambda c: c["inbounds"][1].update(tag="bad/tag"),
    lambda c: c["inbounds"][2].update(tag="public"),
    lambda c: c["inbounds"][2].update(port=443),
    lambda c: c["inbounds"][1].update(port=444),
    lambda c: c["inbounds"][1]["settings"].update(clients=[client(email="")]),
])
def test_configuration_requires_exact_target_and_loopback_api(runtime, change):
    value = config()
    change(value)
    with pytest.raises(runtime.XrayRuntimeError):
        runtime._configuration(value, 443, CLIENT, EMAIL, FLOW)


@pytest.mark.parametrize("is_runtime", [False, True])
def test_unrelated_per_inbound_normalized_duplicates_fail(runtime, is_runtime):
    first = client(id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa", email="other")
    second = client(id="aaaaaaaa-aaaa-bbbb-aaaa-aaaaaaaaaaaa", email="second")
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        if is_runtime:
            rows = [account(email=row["email"], account={
                "_TypedMessage_": "xray.proxy.vless.Account", "id": row["id"]
            }) for row in (first, second)]
            runtime._runtime_users({"users": rows}, "other", "public", CLIENT, EMAIL, FLOW)
        else:
            value = config()
            value["inbounds"][2]["settings"]["clients"] = [first, second]
            runtime._configuration(value, 443, CLIENT, EMAIL, FLOW)
    assert caught.value.code == "vpn_xray_runtime_conflict"


@pytest.fixture
def node(runtime, monkeypatch):
    root = Path.cwd() / "proc-fixture"
    exe, cfg = Path.cwd() / "xray", Path.cwd() / "config.json"
    pid = root / "123"
    data = {
        cfg: json.dumps(config()).encode(),
        pid / "stat": b"123 (name with ) space) S " + b"0 " * 18 + b"456 0\n",
        pid / "cmdline": f"{exe}\0run\0-config\0{cfg}\0".encode(),
        pid / "net/tcp": b"header\n 0: 0100007F:F545 00000000:0000 0A 0:0 0:0 0 0 0 789\n",
    }
    links = {pid / "exe": str(exe), pid / "cwd": str(Path.cwd()), pid / "fd/3": "socket:[789]"}
    links[pid / "ns/net"] = "net:[12345]"
    links[root / "thread-self/ns/net"] = "net:[12345]"
    dirs = {root: [pid], pid / "fd": [pid / "fd/3"]}
    calls = []
    responses = [b"Xray 26.9.9 (Xray, Penetrates Everything.)\n", json.dumps({"users": [account()]}).encode(), b"{}"]
    def read(path, *args):
        return data[path]
    def readlink(path, *args):
        if path not in links:
            raise FileNotFoundError()
        return links[path]
    def run(argv, deadline):
        calls.append((argv, deadline))
        return responses.pop(0)
    monkeypatch.setattr(runtime, "_PROC", root, raising=False)
    monkeypatch.setattr(runtime, "_require_linux", lambda: None, raising=False)
    monkeypatch.setattr(runtime, "_read", read, raising=False)
    monkeypatch.setattr(runtime, "_readlink", readlink, raising=False)
    monkeypatch.setattr(runtime, "_entries", lambda path, *args: dirs[path], raising=False)
    monkeypatch.setattr(runtime, "_trusted_file", lambda path, *args: (str(path), 1), raising=False)
    monkeypatch.setattr(runtime, "_run", run, raising=False)
    return dict(root=root, exe=exe, cfg=cfg, pid=pid, data=data, links=links,
                dirs=dirs, calls=calls, responses=responses)


def test_observe_pins_process_and_reads_every_configured_vless_inbound(runtime, node):
    assert runtime.observe_xray_client(**inputs()) == runtime.XrayRuntimeObservation("matched", 123, 456)
    argv = [call[0] for call in node["calls"]]
    assert argv[0] == [str(node["exe"]), "version"]
    assert len(argv) == 3
    assert argv[1][:4] == [str(node["exe"]), "api", "inbounduser", "--server=127.0.0.1:62789"]
    assert argv[1][-1] == "-tag=public"
    assert argv[2][-1] == "-tag=other"
    assert argv[1][4].startswith("--timeout=")
    assert re.fullmatch(r"--timeout=[1-9][0-9]*", argv[1][4])
    assert len({call[1] for call in node["calls"]}) == 1


def test_observe_empty_users(runtime, node):
    node["responses"][1] = b"{}"
    assert runtime.observe_xray_client(**inputs()).state == "not_observed"


@pytest.mark.parametrize("version", [b"Xray 26.9.8\n", b"Xray 26.9.90\n", b"secret"])
def test_unsupported_binary_version(runtime, node, version):
    node["responses"][0] = version
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        runtime.observe_xray_client(**inputs())
    assert caught.value.code == "vpn_xray_runtime_unavailable"
    assert len(node["calls"]) == 1


@pytest.mark.parametrize("count", [0, 2])
def test_exactly_one_process_required(runtime, node, count):
    node["dirs"][node["root"]] = [node["root"] / str(123 + i) for i in range(count)]
    if count == 2:
        node["links"][node["root"] / "124/exe"] = str(node["exe"])
    with pytest.raises(runtime.XrayRuntimeError):
        runtime.observe_xray_client(**inputs())
    assert not node["calls"]


@pytest.mark.parametrize("arguments", [
    "run", "run -c wrong.json", "run -c config.json -config config.json",
    "run -c config.json -confdir configs", "run --config=config.json",
    "run -c config.json --config-dir=configs", "run -c config.json -confdir=configs",
])
def test_untrusted_config_arguments(runtime, node, arguments):
    node["data"][node["pid"] / "cmdline"] = (str(node["exe"]) + "\0" + arguments.replace(" ", "\0") + "\0").encode()
    with pytest.raises(runtime.XrayRuntimeError):
        runtime.observe_xray_client(**inputs())
    assert not node["calls"]


@pytest.mark.parametrize("flag", ["-c", "-config", "--config"])
def test_relative_config_resolves_from_process_cwd(runtime, node, flag):
    node["data"][node["pid"] / "cmdline"] = f"xray\0run\0{flag}\0config.json\0".encode()
    assert runtime.observe_xray_client(**inputs()).state == "matched"


@pytest.mark.parametrize("bad", ["address", "port", "state", "owner"])
def test_api_listener_must_belong_to_the_selected_process(runtime, node, bad):
    path = node["pid"] / "net/tcp"
    if bad == "owner":
        node["links"][node["pid"] / "fd/3"] = "socket:[999]"
    else:
        old, new = {"address": (b"0100007F", b"00000000"), "port": (b"F545", b"FFFF"), "state": (b"0A", b"01")}[bad]
        node["data"][path] = node["data"][path].replace(old, new)
    with pytest.raises(runtime.XrayRuntimeError):
        runtime.observe_xray_client(**inputs())
    assert not node["calls"]


def test_api_listener_must_share_the_calling_threads_network_namespace(runtime, node):
    node["links"][node["root"] / "thread-self/ns/net"] = "net:[54321]"
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        runtime.observe_xray_client(**inputs())
    assert caught.value.code == "vpn_xray_runtime_unavailable"
    assert not node["calls"]


@pytest.mark.parametrize("changed", ["process", "thread", "both"])
def test_network_namespace_drift_after_cli_fails_closed(runtime, node, monkeypatch, changed):
    original = runtime._run
    def mutate(argv, deadline):
        result = original(argv, deadline)
        if changed in ("process", "both"):
            node["links"][node["pid"] / "ns/net"] = "net:[54321]"
        if changed in ("thread", "both"):
            node["links"][node["root"] / "thread-self/ns/net"] = "net:[54321]"
        return result
    monkeypatch.setattr(runtime, "_run", mutate)
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        runtime.observe_xray_client(**inputs())
    assert caught.value.code == "vpn_xray_runtime_unavailable"


@pytest.mark.parametrize("drift", ["pid", "exe", "config", "config_identity", "executable_identity", "listener"])
def test_drift_after_cli_fails_closed(runtime, node, monkeypatch, drift):
    original = runtime._run
    def mutate(argv, deadline):
        result = original(argv, deadline)
        if drift == "pid":
            path = node["pid"] / "stat"
            node["data"][path] = node["data"][path].replace(b"456", b"457")
        elif drift == "exe":
            node["links"][node["pid"] / "exe"] = str(node["exe"]) + "-other"
        elif drift == "config":
            node["data"][node["cfg"]] += b" "
        elif drift.endswith("identity"):
            changed = node["cfg"] if drift == "config_identity" else node["exe"]
            monkeypatch.setattr(runtime, "_trusted_file", lambda p, *args: (str(p), 2 if p == changed else 1))
        else:
            node["links"][node["pid"] / "fd/3"] = "socket:[999]"
        return result
    monkeypatch.setattr(runtime, "_run", mutate)
    with pytest.raises(runtime.XrayRuntimeError):
        runtime.observe_xray_client(**inputs())


def test_nonzero_cli_is_never_absence(runtime, node, monkeypatch):
    def fail(argv, deadline):
        raise subprocess.CalledProcessError(1, argv, output=str(CLIENT), stderr=EMAIL)
    monkeypatch.setattr(runtime, "_run", fail)
    with pytest.raises(runtime.XrayRuntimeError) as caught:
        runtime.observe_xray_client(**inputs())
    assert caught.value.code == "vpn_xray_runtime_unavailable"
    assert caught.value.__context__ is None


def test_deadline_includes_file_reads(runtime, node, monkeypatch):
    original = runtime._read
    def slow_read(*args):
        time.sleep(0.03)
        return original(*args)
    monkeypatch.setattr(runtime, "_read", slow_read)
    with pytest.raises(runtime.XrayRuntimeError):
        runtime.observe_xray_client(**inputs(timeout_seconds=0.01))
    assert not node["calls"]


@pytest.mark.parametrize("unsafe", ["owner", "writable", "symlink", "reparse", "directory"])
@pytest.mark.parametrize("ancestor", [False, True])
def test_trusted_file_rejects_unsafe_file_and_ancestors(runtime, monkeypatch, unsafe, ancestor):
    target = Path.cwd() / "xray"
    changed = target.parent if ancestor else target
    def metadata(path):
        directory = Path(path) != target
        mode = (stat.S_IFDIR if directory else stat.S_IFREG) | 0o755
        result = SimpleNamespace(st_mode=mode, st_uid=0, st_gid=0, st_dev=1,
                                 st_ino=1, st_size=1, st_mtime_ns=1, st_ctime_ns=1,
                                 st_file_attributes=0)
        if Path(path) == changed:
            if unsafe == "owner":
                result.st_uid = 1000
            elif unsafe == "writable":
                result.st_mode |= 0o002
            elif unsafe == "symlink":
                result.st_mode = stat.S_IFLNK | 0o777
            elif unsafe == "reparse":
                result.st_file_attributes = 1024
            else:
                result.st_mode = (stat.S_IFREG if ancestor else stat.S_IFDIR) | 0o755
        return result
    monkeypatch.setattr(os, "lstat", metadata)
    with pytest.raises(runtime.XrayRuntimeError):
        runtime._trusted_file(target, time.monotonic() + 1)


def test_read_is_bounded_and_closes_file(runtime, tmp_path):
    path = tmp_path / "large"
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(runtime.XrayRuntimeError):
        runtime._read(path, time.monotonic() + 1)
    path.unlink()


@pytest.mark.parametrize("script", [
    "import time; time.sleep(60)",
    "import sys,time; sys.stdout.buffer.write(b'x' * (3*1024*1024)); sys.stdout.flush(); time.sleep(60)",
    "import sys,time; sys.stderr.buffer.write(b'x' * (3*1024*1024)); sys.stderr.flush(); time.sleep(60)",
])
def test_real_child_timeout_and_output_overflow_cleanup(runtime, monkeypatch, script):
    children = []
    original = subprocess.Popen
    def track(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(runtime.subprocess, "Popen", track)
    started = time.monotonic()
    with pytest.raises(runtime.XrayRuntimeError):
        runtime._run([sys.executable, "-c", script], time.monotonic() + 0.4)
    assert time.monotonic() - started < 4
    assert len(children) == 1
    assert children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


def test_real_child_success_and_nonzero_exit(runtime):
    assert runtime._run([sys.executable, "-c", "print('hello')"], time.monotonic() + 3).strip() == b"hello"
    with pytest.raises(runtime.XrayRuntimeError):
        runtime._run([sys.executable, "-c", "raise SystemExit(1)"], time.monotonic() + 3)


def test_cleanup_does_not_wait_for_inherited_pipes(runtime):
    script = (
        "import subprocess,sys,time; "
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(1.2)']); "
        "time.sleep(60)"
    )
    started = time.monotonic()
    with pytest.raises(runtime.XrayRuntimeError):
        runtime._run([sys.executable, "-c", script], started + 0.2)
    assert time.monotonic() - started < 0.8


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_owned_child_cleanup_preserves_interruption(runtime, monkeypatch, interruption):
    children = []
    original = subprocess.Popen
    def track(*args, **kwargs):
        child = original(*args, **kwargs)
        wait = child.wait
        interrupted = False
        def interrupt_once(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise interruption()
            return wait(*args, **kwargs)
        child.wait = interrupt_once
        children.append(child)
        return child
    monkeypatch.setattr(subprocess, "Popen", track)
    with pytest.raises(interruption):
        runtime._run([sys.executable, "-c", "import time; time.sleep(60)"], time.monotonic() + 3)
    assert len(children) == 1 and children[0].poll() is not None
    assert children[0].stdout.closed and children[0].stderr.closed


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("interrupt_body", [False, True])
def test_interrupted_cleanup_reaps_child_and_closes_pipes(
    runtime, monkeypatch, interruption, interrupt_body
):
    children = []
    waits = []
    original = subprocess.Popen
    body_interrupt = interruption("body")
    cleanup_interrupt = interruption("cleanup")
    def track(*args, **kwargs):
        child = original(*args, **kwargs)
        wait = child.wait
        def interrupt_once(*args, **kwargs):
            cleanup = not args and not kwargs
            waits.append(cleanup)
            if cleanup and waits.count(True) == 1:
                raise cleanup_interrupt
            if not cleanup and interrupt_body:
                raise body_interrupt
            return wait(*args, **kwargs)
        child.wait = interrupt_once
        children.append((child, wait))
        return child
    monkeypatch.setattr(subprocess, "Popen", track)
    try:
        with pytest.raises(interruption) as caught:
            runtime._run([sys.executable, "-c", "import time; time.sleep(60)"], time.monotonic() + 0.1)
        assert caught.value is (body_interrupt if interrupt_body else cleanup_interrupt)
        assert waits.count(True) >= 2
        assert children[0][0].poll() is not None
        assert children[0][0].stdout.closed and children[0][0].stderr.closed
    finally:
        for child, wait in children:
            if child.poll() is None:
                child.kill()
            wait()
            child.stdout.close()
            child.stderr.close()


def test_import_without_site_packages_has_no_io(tmp_path):
    module = Path(__file__).resolve().parents[1] / "app/services/vpn_xray_runtime.py"
    script = (
        "import builtins, importlib.util, pathlib, sys; "
        f"spec=importlib.util.spec_from_file_location('runtime', {str(module)!r}); "
        "m=importlib.util.module_from_spec(spec); sys.modules['runtime']=m; "
        "spec.loader.exec_module(m); print('ok')"
    )
    result = subprocess.run([sys.executable, "-S", "-c", script], capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.strip() == b"ok"

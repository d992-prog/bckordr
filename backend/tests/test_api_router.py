from app.api import api_router


def test_control_router_excludes_legacy_checker_routes():
    paths = {route.path for route in api_router.routes}

    assert "/control/overview" in paths
    assert "/worker-runtime/heartbeat" in paths
    assert "/control/discovery/runtime-settings" in paths
    assert not any(path.startswith("/domains") for path in paths)
    assert not any(path.startswith("/proxies") for path in paths)
